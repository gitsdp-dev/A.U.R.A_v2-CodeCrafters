"""In-memory image preparation, downscaling, hashing, and PDF extraction."""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

from core.vision.models import VisionImage, VisionSource

LOGGER = logging.getLogger(__name__)

MAX_LONG_SIDE_PX = 1568
JPEG_QUALITY = 85

try:
    import PIL.Image
    _HAS_PIL = True
except ImportError:
    PIL = None  # type: ignore[assignment]
    _HAS_PIL = False

try:
    import fitz  # PyMuPDF (optional)
    _HAS_FITZ = True
except ImportError:
    fitz = None  # type: ignore[assignment]
    _HAS_FITZ = False

try:
    import pdfplumber
    _HAS_PDFPLUMBER = True
except ImportError:
    pdfplumber = None  # type: ignore[assignment]
    _HAS_PDFPLUMBER = False

try:
    import pypdf
    _HAS_PYPDF = True
except ImportError:
    pypdf = None  # type: ignore[assignment]
    _HAS_PYPDF = False


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare_image_bytes(
    raw_bytes: bytes,
    *,
    source: VisionSource = VisionSource.FILE,
    source_label: str = "",
    max_long_side: int = MAX_LONG_SIDE_PX,
    jpeg_quality: int = JPEG_QUALITY,
    extracted_text: str = "",
    page_count: int = 1,
) -> VisionImage:
    """Downscale and JPEG-compress raw image bytes in memory."""
    if not raw_bytes:
        raise ValueError("Image payload is empty.")

    width = 0
    height = 0
    out_bytes = raw_bytes
    mime_type = "image/jpeg"

    if _HAS_PIL:
        try:
            with PIL.Image.open(io.BytesIO(raw_bytes)) as im:
                rgb = im.convert("RGB")
                orig_w, orig_h = rgb.size
                long_side = max(orig_w, orig_h)
                if long_side > max_long_side and max_long_side > 0:
                    scale = max_long_side / float(long_side)
                    new_w = max(1, int(round(orig_w * scale)))
                    new_h = max(1, int(round(orig_h * scale)))
                    rgb = rgb.resize((new_w, new_h), PIL.Image.Resampling.LANCZOS)
                width, height = rgb.size
                buf = io.BytesIO()
                rgb.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
                out_bytes = buf.getvalue()
                mime_type = "image/jpeg"
        except Exception as exc:
            LOGGER.warning("PIL image normalization failed; using raw bytes (%s)", type(exc).__name__)
            mime_type = _sniff_mime(raw_bytes)
    else:
        mime_type = _sniff_mime(raw_bytes)

    return VisionImage(
        data=out_bytes,
        mime_type=mime_type,
        sha256=compute_sha256(out_bytes),
        width=width,
        height=height,
        source=source,
        source_label=source_label,
        page_count=page_count,
        extracted_text=extracted_text,
    )


def _sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def extract_pdf_text(path: Path, max_pages: int = 8) -> tuple[str, int]:
    """Extract text from a PDF using PyMuPDF, pdfplumber, or pypdf."""
    if _HAS_FITZ:
        try:
            doc = fitz.open(str(path))
            try:
                total = len(doc)
                chunks: list[str] = []
                for idx in range(min(total, max_pages)):
                    txt = (doc[idx].get_text("text") or "").strip()
                    if txt:
                        chunks.append(f"--- Page {idx + 1} ---\n{txt}")
                return "\n\n".join(chunks), total
            finally:
                doc.close()
        except Exception as exc:
            LOGGER.warning("PyMuPDF text extraction failed: %s", type(exc).__name__)

    if _HAS_PDFPLUMBER:
        try:
            with pdfplumber.open(str(path)) as pdf:
                total = len(pdf.pages)
                chunks = []
                for idx, page in enumerate(pdf.pages[:max_pages]):
                    txt = (page.extract_text() or "").strip()
                    if txt:
                        chunks.append(f"--- Page {idx + 1} ---\n{txt}")
                return "\n\n".join(chunks), total
        except Exception as exc:
            LOGGER.warning("pdfplumber text extraction failed: %s", type(exc).__name__)

    if _HAS_PYPDF:
        try:
            reader = pypdf.PdfReader(str(path))
            total = len(reader.pages)
            chunks = []
            for idx in range(min(total, max_pages)):
                txt = (reader.pages[idx].extract_text() or "").strip()
                if txt:
                    chunks.append(f"--- Page {idx + 1} ---\n{txt}")
            return "\n\n".join(chunks), total
        except Exception as exc:
            LOGGER.warning("pypdf text extraction failed: %s", type(exc).__name__)

    return "", 0


def render_pdf_first_page(path: Path, dpi: int = 150) -> bytes | None:
    """Render the first page of a PDF to PNG bytes if PyMuPDF is available."""
    if not _HAS_FITZ:
        return None
    doc = None
    try:
        doc = fitz.open(str(path))
        if len(doc) == 0:
            return None
        page = doc[0]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    except Exception as exc:
        LOGGER.warning("PyMuPDF render failed: %s", type(exc).__name__)
        return None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
