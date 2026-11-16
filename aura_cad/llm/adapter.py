"""Small adapter over A.U.R.A.'s existing routed Gemini/local generation layer."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from aura_cad.config import MAX_MODEL_RESPONSE_BYTES
from aura_cad.errors import CodegenError

LOGGER = logging.getLogger("aura.cad")


class CadLLMAdapter:
    """Keep all CAD generation on the existing task router."""

    def __init__(self, generator: Callable[[str, str], str] | None = None) -> None:
        self._generator = generator

    def generate(self, prompt: str, job_id: str) -> str:
        if self._generator is not None:
            response = self._generator("cad_design", prompt)
        else:
            response = self._generate_with_configured_provider(prompt)
        text = getattr(response, "text", response)
        if not isinstance(text, str) or not text.strip():
            raise CodegenError("The CAD model service returned no code.")
        if len(text.encode("utf-8")) > MAX_MODEL_RESPONSE_BYTES:
            raise CodegenError("The generated CAD response exceeded the size limit.")
        LOGGER.info("[job=%s] CAD response received", job_id)
        return text.strip()

    @staticmethod
    def _generate_with_configured_provider(prompt: str):
        from memory.config_manager import get_local_llm_config

        local = get_local_llm_config()
        if local.get("mode") == "local":
            from core.llm_client import call_llm, call_llm_text

            if local.get("provider") == "ollama":
                return call_llm_text(prompt, timeout=180)
            result = call_llm(
                [{"role": "user", "content": prompt}],
                timeout=180,
            )
            return result.get("content", "")

        from core import gemini

        return gemini.call(
            prompt,
            tier=gemini.SMART,
            task_type="cad_design",
            timeout_ms=180_000,
            raise_errors=True,
        )


def strip_code_fence(text: str) -> str:
    """Remove a single optional Markdown fence from a code response."""
    value = str(text or "").strip()
    value = re.sub(r"^```(?:python|py)?\s*", "", value, flags=re.I)
    return re.sub(r"\s*```$", "", value).strip()
