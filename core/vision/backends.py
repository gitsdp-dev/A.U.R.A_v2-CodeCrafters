"""Provider-agnostic VisionBackend interface, Gemini backend, and Local fallback."""

from __future__ import annotations

import base64
import io
import logging
from abc import ABC, abstractmethod
from typing import Any

import requests

from core.vision.models import VisionImage, VisionResult, VisionSource, VisionTaskType
from memory.config_manager import get_gemini_key, get_local_llm_config

LOGGER = logging.getLogger(__name__)

_VLM_NAME_HINTS = (
    "llava",
    "bakllava",
    "moondream",
    "minicpm-v",
    "qwen2.5vl",
    "qwen2-vl",
    "qwen3-vl",
    "llama3.2-vision",
    "gemma3",
    "pixtral",
    "internvl",
    "vision",
)


def _task_instruction(task: VisionTaskType, prompt: str) -> str:
    base_q = (prompt or "").strip()
    if task == VisionTaskType.DIAGRAM:
        return (
            "Analyze this diagram or architecture image. Return:\n"
            "1. Overview (1-2 sentences)\n"
            "2. Major components and their roles\n"
            "3. Relationships and data/control flow\n"
            "4. Notable observations or bottlenecks\n"
            + (f"\nUser focus: {base_q}" if base_q else "")
        )
    if task == VisionTaskType.CHART:
        return (
            "Analyze this chart or graph. Return:\n"
            "1. Chart type and subject\n"
            "2. Axes / series / units\n"
            "3. Key values, peaks, and trends\n"
            "4. Plain-language takeaway (1-2 sentences)\n"
            + (f"\nUser focus: {base_q}" if base_q else "")
        )
    if task == VisionTaskType.DOCUMENT:
        return (
            "Read and summarize this document/page/form/receipt. Return:\n"
            "1. Concise summary\n"
            "2. Key points, dates, amounts, or fields\n"
            "3. Actionable items if any\n"
            + (f"\nUser focus: {base_q}" if base_q else "")
        )
    if task == VisionTaskType.OCR:
        return (
            "Extract all visible text with layout awareness. Preserve headings, "
            "lists, and format any tables cleanly as Markdown tables. "
            "Do not invent unreadable text; mark unclear text as [unclear]."
            + (f"\nUser focus: {base_q}" if base_q else "")
        )
    if task == VisionTaskType.CAMERA_OBJECT:
        return (
            "Identify the main object(s) shown to the camera and answer the user's question "
            "concisely and accurately based only on what is visible.\n"
            + (f"Question: {base_q}" if base_q else "Question: What is this object?")
        )
    if task == VisionTaskType.SCREENSHOT:
        return (
            "Interpret this screenshot clearly and answer the user's question based only "
            "on what is visible on screen.\n"
            + (f"Question: {base_q}" if base_q else "Question: What is on my screen?")
        )
    return base_q or "Describe what is shown in this image clearly and concisely."


def _concise_spoken(text: str, max_chars: int = 260) -> str:
    cleaned = " ".join((text or "").replace("#", "").replace("*", "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[:max_chars]
    for sep in (". ", "! ", "? "):
        idx = cut.rfind(sep)
        if idx > 60:
            return cut[: idx + 1].strip()
    return cut.rstrip() + "..."


class VisionBackend(ABC):
    """Provider-agnostic multimodal / OCR backend."""

    name: str = "base"
    supports_multimodal: bool = False

    @abstractmethod
    def analyze_image(
        self,
        image: VisionImage,
        prompt: str,
        task: VisionTaskType = VisionTaskType.GENERAL,
        timeout_s: float = 30.0,
    ) -> VisionResult:
        ...

    def ocr(self, image: VisionImage, timeout_s: float = 30.0) -> VisionResult:
        return self.analyze_image(
            image,
            "Extract all text with layout awareness.",
            task=VisionTaskType.OCR,
            timeout_s=timeout_s,
        )

    def describe(self, image: VisionImage, timeout_s: float = 30.0) -> VisionResult:
        return self.analyze_image(
            image,
            "Describe this image clearly.",
            task=VisionTaskType.GENERAL,
            timeout_s=timeout_s,
        )

    def answer_question(
        self,
        image: VisionImage,
        question: str,
        timeout_s: float = 30.0,
    ) -> VisionResult:
        return self.analyze_image(
            image,
            question,
            task=VisionTaskType.VQA,
            timeout_s=timeout_s,
        )


class GeminiVisionBackend(VisionBackend):
    """Gemini backend routed through core.gemini / model_router (silent quota switch)."""

    name = "gemini"
    supports_multimodal = True

    def is_available(self) -> bool:
        key = get_gemini_key()
        return bool(key and len(key.strip()) > 15)

    def analyze_image(
        self,
        image: VisionImage,
        prompt: str,
        task: VisionTaskType = VisionTaskType.GENERAL,
        timeout_s: float = 30.0,
    ) -> VisionResult:
        if not self.is_available():
            return VisionResult(
                ok=False,
                task=task,
                backend=self.name,
                model="",
                summary="",
                error="Gemini API key is not configured.",
                image_hash=image.sha256,
            )

        instruction = _task_instruction(task, prompt)
        if image.extracted_text:
            instruction = (
                f"{instruction}\n\n"
                f"[EXTRACTED DOCUMENT TEXT ({image.page_count} page(s))]:\n"
                f"{image.extracted_text[:12000]}"
            )

        try:
            from google.genai import types as gtypes
            import core.gemini as gemini
            import model_router
        except Exception as exc:
            return VisionResult(
                ok=False,
                task=task,
                backend=self.name,
                model="",
                summary="",
                error=f"Gemini SDK unavailable: {type(exc).__name__}",
                image_hash=image.sha256,
            )

        contents: list[Any] = []
        # For text-only PDF fallback (1x1 placeholder), send text only to save tokens.
        if not (image.source == VisionSource.PDF and image.width <= 2 and image.height <= 2 and image.extracted_text):
            contents.append(gtypes.Part.from_bytes(data=image.data, mime_type=image.mime_type))
        contents.append(instruction)

        task_type = "long_document_generation" if task in (VisionTaskType.DOCUMENT, VisionTaskType.DIAGRAM) else "qa"
        tier = gemini.SMART

        try:
            response, metadata = model_router.router.generate(
                task_type,
                contents,
                timeout_ms=int(timeout_s * 1000),
                legacy_tier=tier,
            )
            text = (getattr(response, "text", None) or "").strip()
            model_used = ""
            if isinstance(metadata, dict):
                model_used = str(metadata.get("model", ""))
            if not text:
                return VisionResult(
                    ok=False,
                    task=task,
                    backend=self.name,
                    model=model_used,
                    summary="",
                    error="Gemini returned an empty vision response.",
                    image_hash=image.sha256,
                )
            return VisionResult(
                ok=True,
                task=task,
                backend=self.name,
                model=model_used or "gemini",
                summary=text,
                details=text,
                spoken_summary=_concise_spoken(text),
                ocr_text=text if task == VisionTaskType.OCR else "",
                image_hash=image.sha256,
                metadata=dict(metadata) if isinstance(metadata, dict) else {},
            )
        except Exception as exc:
            LOGGER.warning("GeminiVisionBackend failed: %s", type(exc).__name__)
            return VisionResult(
                ok=False,
                task=task,
                backend=self.name,
                model="",
                summary="",
                error=gemini.user_facing_error(exc),
                image_hash=image.sha256,
            )


class LocalVisionBackend(VisionBackend):
    """CPU-only, offline fallback using local VLM (Ollama/LM Studio) or OCR + LLM."""

    name = "local"

    def __init__(self) -> None:
        self._ocr_engine_name: str | None = None
        self._rapidocr_instance: Any = None

    @property
    def supports_multimodal(self) -> bool:
        return bool(self._detect_local_vlm())

    def _detect_local_vlm(self) -> tuple[str, str, str] | None:
        """Return (provider, base_url, model) if the active local model supports vision."""
        cfg = get_local_llm_config()
        provider = cfg.get("provider", "ollama")
        base_url = cfg.get("base_url", "http://127.0.0.1:11434").rstrip("/")
        model = cfg.get("model", "").strip()
        if not model:
            return None

        low = model.lower()
        if any(hint in low for hint in _VLM_NAME_HINTS):
            return provider, base_url, model

        if provider == "ollama":
            try:
                resp = requests.post(
                    f"{base_url}/api/show",
                    json={"model": model},
                    timeout=2.0,
                )
                if resp.ok:
                    caps = resp.json().get("capabilities", [])
                    if isinstance(caps, list) and "vision" in caps:
                        return provider, base_url, model
            except Exception:
                pass
        return None

    def _run_local_ocr(self, image: VisionImage) -> tuple[str, str]:
        """Run CPU OCR using RapidOCR, Tesseract, or EasyOCR. Returns (text, engine_name)."""
        if image.extracted_text and image.source == VisionSource.PDF and image.width <= 2:
            return image.extracted_text, "pdf-text"

        # 1. RapidOCR (lightest, pure ONNX CPU)
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]
            if self._rapidocr_instance is None:
                self._rapidocr_instance = RapidOCR()
            result, _ = self._rapidocr_instance(image.data)
            if result:
                lines = [str(item[1]).strip() for item in result if len(item) >= 2 and str(item[1]).strip()]
                if lines:
                    return "\n".join(lines), "rapidocr"
            return "", "rapidocr"
        except ImportError:
            pass
        except Exception as exc:
            LOGGER.debug("RapidOCR failed: %s", type(exc).__name__)

        # 2. pytesseract
        try:
            import pytesseract  # type: ignore[import-not-found]
            import PIL.Image
            with PIL.Image.open(io.BytesIO(image.data)) as im:
                txt = (pytesseract.image_to_string(im) or "").strip()
            return txt, "tesseract"
        except ImportError:
            pass
        except Exception as exc:
            LOGGER.debug("Tesseract OCR failed: %s", type(exc).__name__)

        # 3. EasyOCR
        try:
            import easyocr  # type: ignore[import-not-found]
            reader = easyocr.Reader(["en"], gpu=False, verbose=False)
            lines = reader.readtext(image.data, detail=0)
            return "\n".join(str(x).strip() for x in lines if str(x).strip()), "easyocr"
        except ImportError:
            pass
        except Exception as exc:
            LOGGER.debug("EasyOCR failed: %s", type(exc).__name__)

        return "", ""

    def _call_local_vlm(
        self,
        provider: str,
        base_url: str,
        model: str,
        image: VisionImage,
        prompt: str,
        timeout_s: float,
    ) -> str:
        b64 = base64.b64encode(image.data).decode("ascii")
        if provider == "ollama":
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [b64],
                    }
                ],
                "stream": False,
            }
            resp = requests.post(
                f"{base_url}/api/chat",
                json=payload,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("message", {}).get("content", "")).strip()

        # OpenAI-compatible (LM Studio)
        root = base_url[:-3] if base_url.endswith("/v1") else base_url
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{b64}"}},
                    ],
                }
            ],
            "stream": False,
        }
        resp = requests.post(
            f"{root}/v1/chat/completions",
            json=payload,
            timeout=timeout_s,
        )
        resp.raise_for_status()
        choices = resp.json().get("choices", [])
        if choices:
            return str(choices[0].get("message", {}).get("content", "")).strip()
        return ""

    def _reason_over_text_locally(self, extracted_text: str, instruction: str, timeout_s: float) -> str:
        cfg = get_local_llm_config()
        model = cfg.get("model", "").strip()
        provider = cfg.get("provider", "ollama")
        base_url = cfg.get("base_url", "http://127.0.0.1:11434").rstrip("/")
        if not model:
            return ""
        prompt = (
            f"{instruction}\n\n"
            "Below is text extracted via OCR/PDF parser (no visual layout or graphics were seen):\n"
            f"{extracted_text[:8000]}"
        )
        try:
            if provider == "ollama":
                resp = requests.post(
                    f"{base_url}/api/chat",
                    json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
                    timeout=min(timeout_s, 45.0),
                )
                if resp.ok:
                    return str(resp.json().get("message", {}).get("content", "")).strip()
            else:
                root = base_url[:-3] if base_url.endswith("/v1") else base_url
                resp = requests.post(
                    f"{root}/v1/chat/completions",
                    json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
                    timeout=min(timeout_s, 45.0),
                )
                if resp.ok:
                    choices = resp.json().get("choices", [])
                    if choices:
                        return str(choices[0].get("message", {}).get("content", "")).strip()
        except Exception as exc:
            LOGGER.debug("Local text reasoning failed: %s", type(exc).__name__)
        return ""

    def analyze_image(
        self,
        image: VisionImage,
        prompt: str,
        task: VisionTaskType = VisionTaskType.GENERAL,
        timeout_s: float = 30.0,
    ) -> VisionResult:
        instruction = _task_instruction(task, prompt)
        vlm = self._detect_local_vlm()
        if vlm is not None and not (
            image.source == VisionSource.PDF and image.width <= 2 and image.extracted_text
        ):
            provider, base_url, model = vlm
            try:
                text = self._call_local_vlm(provider, base_url, model, image, instruction, timeout_s)
                if text:
                    return VisionResult(
                        ok=True,
                        task=task,
                        backend=self.name,
                        model=model,
                        summary=text,
                        details=text,
                        spoken_summary=_concise_spoken(text),
                        ocr_text=text if task == VisionTaskType.OCR else "",
                        image_hash=image.sha256,
                    )
            except Exception as exc:
                LOGGER.warning("Local VLM failed, degrading to OCR: %s", type(exc).__name__)

        ocr_text, ocr_engine = self._run_local_ocr(image)
        combined_text = (image.extracted_text or ocr_text or "").strip()

        if not combined_text:
            return VisionResult(
                ok=False,
                task=task,
                backend=self.name,
                model=ocr_engine or "none",
                summary="",
                error=(
                    "No local vision model (such as llava or qwen2.5vl) is configured, "
                    "and no local OCR engine (rapidocr-onnxruntime or pytesseract) is installed "
                    "or no readable text was found in the image."
                ),
                image_hash=image.sha256,
            )

        if task == VisionTaskType.OCR:
            note = f"[OCR via {ocr_engine or 'text'} — offline CPU mode]"
            return VisionResult(
                ok=True,
                task=task,
                backend=self.name,
                model=ocr_engine or "ocr",
                summary=f"{note}\n\n{combined_text}",
                details=combined_text,
                spoken_summary=_concise_spoken(combined_text),
                ocr_text=combined_text,
                image_hash=image.sha256,
            )

        reasoned = self._reason_over_text_locally(combined_text, instruction, timeout_s)
        limitation = (
            "\n\n[Note: No local vision model is configured, so this answer is based "
            f"on text extracted via {ocr_engine or 'document text'} rather than visual graphics.]"
        )
        final_text = (reasoned or combined_text) + limitation
        return VisionResult(
            ok=True,
            task=task,
            backend=self.name,
            model=ocr_engine or "local-ocr",
            summary=final_text,
            details=final_text,
            spoken_summary=_concise_spoken(reasoned or combined_text),
            ocr_text=ocr_text,
            image_hash=image.sha256,
        )
