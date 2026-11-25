"""On-demand screen, window, camera, and file capture for A.U.R.A. Vision.

Privacy guarantees:
- Strictly on-demand. Zero background loops or periodic capture.
- Camera devices are opened only when explicitly requested and ALWAYS released
  in a ``try/finally`` block (including on timeout, cancellation, or error).
- Captures remain in memory; no raw image is written to disk unless the user
  explicitly requests saving.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from core.vision.imaging import (
    extract_pdf_text,
    prepare_image_bytes,
    render_pdf_first_page,
)
from core.vision.models import VisionImage, VisionSource

LOGGER = logging.getLogger(__name__)

try:
    import cv2
    _HAS_CV2 = True
except ImportError:
    cv2 = None  # type: ignore[assignment]
    _HAS_CV2 = False

try:
    import mss
    import mss.tools
    _HAS_MSS = True
except ImportError:
    mss = None  # type: ignore[assignment]
    _HAS_MSS = False

try:
    import pygetwindow as gw
    _HAS_GW = True
except ImportError:
    gw = None  # type: ignore[assignment]
    _HAS_GW = False


class CaptureCancelledError(RuntimeError):
    """Raised when the user cancels an active capture."""


class CaptureUnavailableError(RuntimeError):
    """Raised when a required capture device or dependency is unavailable."""


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CaptureCancelledError("Capture cancelled by user.")


def capture_screen(
    *,
    monitor_index: int = 1,
    window_title: str = "",
    cancel_event: threading.Event | None = None,
) -> VisionImage:
    """Capture the primary screen, a specific monitor, or a named window on demand."""
    _check_cancelled(cancel_event)
    if not _HAS_MSS:
        raise CaptureUnavailableError(
            "Screen capture requires 'mss'. Install it with: pip install mss"
        )

    region = None
    label = f"Screen {monitor_index}"
    source = VisionSource.SCREEN

    if window_title and _HAS_GW:
        try:
            matches = [
                w for w in gw.getWindowsWithTitle(window_title)
                if getattr(w, "width", 0) > 40 and getattr(w, "height", 0) > 40
            ]
            if matches:
                win = matches[0]
                region = {
                    "left": int(win.left),
                    "top": int(win.top),
                    "width": int(win.width),
                    "height": int(win.height),
                }
                label = f"Window: {getattr(win, 'title', window_title)[:48]}"
                source = VisionSource.WINDOW
        except Exception as exc:
            LOGGER.debug("Window lookup failed, falling back to monitor: %s", type(exc).__name__)

    _check_cancelled(cancel_event)
    with mss.mss() as sct:
        monitors = sct.monitors
        if region is None:
            idx = monitor_index if 0 <= monitor_index < len(monitors) else (1 if len(monitors) > 1 else 0)
            target = monitors[idx]
        else:
            target = region
        shot = sct.grab(target)
        png_bytes = mss.tools.to_png(shot.rgb, shot.size)

    _check_cancelled(cancel_event)
    return prepare_image_bytes(
        png_bytes,
        source=source,
        source_label=label,
    )


def capture_camera(
    *,
    ui: Any = None,
    cancel_event: threading.Event | None = None,
    timeout_s: float = 8.0,
    warmup_frames: int = 8,
) -> VisionImage:
    """Capture a single frame from the user's webcam and release immediately."""
    _check_cancelled(cancel_event)
    if not _HAS_CV2:
        raise CaptureUnavailableError(
            "Camera capture requires OpenCV ('opencv-python')."
        )

    from core.camera import open_camera
    from memory.config_manager import load_api_keys

    cfg = load_api_keys()
    try:
        preferred = int(cfg.get("camera_index", 0))
    except (TypeError, ValueError):
        preferred = 0
    indices = tuple(dict.fromkeys((preferred, 0, 1, 2, 3, 4, 5)))

    pause_fn = getattr(ui, "pause_room_watch", None) if ui is not None else None
    resume_fn = getattr(ui, "resume_room_watch", None) if ui is not None else None
    if callable(pause_fn):
        try:
            pause_fn()
        except Exception as exc:
            LOGGER.debug("pause_room_watch failed: %s", type(exc).__name__)

    cap = None
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    try:
        _check_cancelled(cancel_event)
        cap = open_camera(cv2, indices, warmup=0)
        if cap is None:
            raise CaptureUnavailableError(
                "No camera is available. It may be disconnected, busy in another app, "
                "or blocked by OS privacy permissions."
            )

        for _ in range(max(0, warmup_frames)):
            _check_cancelled(cancel_event)
            if time.monotonic() > deadline:
                raise TimeoutError("Camera warmup timed out.")
            cap.read()

        _check_cancelled(cancel_event)
        if time.monotonic() > deadline:
            raise TimeoutError("Camera capture timed out.")

        ok, frame = cap.read()
        if not ok or frame is None:
            raise CaptureUnavailableError(
                "Camera opened but returned no frame. Check camera privacy cover or permissions."
            )

        ok_enc, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok_enc:
            raise RuntimeError("Failed to encode camera frame.")

        return prepare_image_bytes(
            buf.tobytes(),
            source=VisionSource.CAMERA,
            source_label="Webcam",
        )
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception as exc:
                LOGGER.warning("Camera release error: %s", type(exc).__name__)
        if callable(resume_fn):
            try:
                resume_fn()
            except Exception as exc:
                LOGGER.debug("resume_room_watch failed: %s", type(exc).__name__)


def load_file_for_vision(file_path: str | Path) -> VisionImage:
    """Load an image or PDF file into an in-memory VisionImage."""
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path.name}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text, page_count = extract_pdf_text(path)
        rendered = render_pdf_first_page(path)
        if rendered is not None:
            return prepare_image_bytes(
                rendered,
                source=VisionSource.PDF,
                source_label=path.name,
                extracted_text=text,
                page_count=max(1, page_count),
            )
        if not text:
            raise CaptureUnavailableError(
                f"Could not extract text or render '{path.name}'. "
                "Install 'pymupdf' for visual PDF rendering, or ensure the PDF is not encrypted."
            )
        # Text-only PDF fallback: carry extracted text with a tiny 1x1 placeholder PNG
        # so the router/backend can still summarize or answer questions over extracted_text.
        placeholder = _blank_png_bytes()
        return prepare_image_bytes(
            placeholder,
            source=VisionSource.PDF,
            source_label=path.name,
            extracted_text=text,
            page_count=max(1, page_count),
        )

    raw = path.read_bytes()
    return prepare_image_bytes(
        raw,
        source=VisionSource.FILE,
        source_label=path.name,
    )


def _blank_png_bytes() -> bytes:
    """Return a minimal 1x1 white JPEG/PNG in memory for text-only PDF fallback."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?"
        b"\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
    )
