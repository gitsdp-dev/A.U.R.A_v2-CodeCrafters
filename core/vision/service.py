"""High-level VisionService orchestrating capture, permissions, UI, and memory."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from core.vision.capture import (
    CaptureCancelledError,
    CaptureUnavailableError,
    capture_camera,
    capture_screen,
    load_file_for_vision,
)
from core.vision.models import (
    VisionCapability,
    VisionImage,
    VisionResult,
    VisionSource,
    VisionTaskType,
)
from core.vision.permissions import (
    VisionPermissionDeniedError,
    VisionPermissionManager,
)
from core.vision.router import VisionRouter
from memory.config_manager import get_vision_config

LOGGER = logging.getLogger(__name__)


def _capability_for_source(source: VisionSource) -> VisionCapability:
    if source == VisionSource.CAMERA:
        return VisionCapability.CAMERA
    if source in (VisionSource.SCREEN, VisionSource.WINDOW):
        return VisionCapability.SCREEN
    return VisionCapability.FILE


class VisionService:
    """Thread-safe coordinator for on-demand multimodal vision tasks."""

    def __init__(
        self,
        router: VisionRouter | None = None,
        permissions: VisionPermissionManager | None = None,
    ) -> None:
        self.router = router or VisionRouter()
        self.permissions = permissions or VisionPermissionManager()
        self._lock = threading.Lock()
        self._active_cancel: threading.Event | None = None
        self._active_mode: str = ""
        self._last_result: VisionResult | None = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active_cancel is not None and not self._active_cancel.is_set()

    def stop_active(self) -> bool:
        """Cancel any active vision capture/analysis immediately."""
        with self._lock:
            ev = self._active_cancel
        if ev is not None:
            ev.set()
            return True
        return False

    def shutdown(self) -> None:
        """Release resources and clear session grants on app exit."""
        self.stop_active()
        self.permissions.clear_session_grants()
        self.router.clear_cache()

    def save_result_to_memory(self, result: VisionResult, key_hint: str = "") -> str:
        """Store a concise text summary (never raw images) in A.U.R.A.'s memory."""
        if not result.ok or not result.summary.strip():
            return "Nothing to save to memory."
        from memory.memory_manager import remember

        key = (key_hint or f"vision_{result.task.value}_{result.image_hash[:8]}").strip()
        summary_text = " ".join(result.summary.strip().split())[:360]
        return remember(key=key, value=summary_text, category="notes")

    def run_task(
        self,
        *,
        source: VisionSource,
        task: VisionTaskType,
        prompt: str,
        file_path: str = "",
        window_title: str = "",
        monitor_index: int = 1,
        image_hash: str = "",
        remember_summary: bool = False,
        ui: Any = None,
        speak: Callable[[str], None] | None = None,
        timeout_s: float = 35.0,
    ) -> VisionResult:
        """Execute one on-demand vision task with permission gating and UI indicators."""
        cancel_event = threading.Event()
        with self._lock:
            if self._active_cancel is not None and not self._active_cancel.is_set():
                return VisionResult(
                    ok=False,
                    task=task,
                    backend="none",
                    model="",
                    summary="",
                    error="Another vision task is already running. Stop it first or wait a moment.",
                )
            self._active_cancel = cancel_event
            self._active_mode = source.value

        mode_label = "CAMERA" if source == VisionSource.CAMERA else (
            "SCREEN" if source in (VisionSource.SCREEN, VisionSource.WINDOW) else "FILE"
        )
        indicator_fn = getattr(ui, "show_vision_indicator", None) if ui is not None else None
        preview_fn = getattr(ui, "confirm_vision_preview", None) if ui is not None else None

        try:
            # 1. Resolve cached image for follow-ups without re-capturing.
            if source == VisionSource.CACHED:
                cached_img = (
                    self.router.get_cached_image(image_hash)
                    if image_hash
                    else self.router.get_last_image()
                )
                if cached_img is None:
                    return VisionResult(
                        ok=False,
                        task=task,
                        backend="none",
                        model="",
                        summary="",
                        error="No recent image is cached in this session. Capture or upload an image first.",
                    )
                image = cached_img
            else:
                # 2. Permission check & confirmation dialog before capture/analysis.
                cap = _capability_for_source(source)
                src_label = (
                    Path(file_path).name if file_path else (window_title or source.value)
                )
                self.permissions.ensure_allowed(
                    cap,
                    source_label=src_label,
                    task_label=task.value,
                )

                # 3. Live HUD indicator for camera/screen capture.
                if callable(indicator_fn) and source in (
                    VisionSource.CAMERA,
                    VisionSource.SCREEN,
                    VisionSource.WINDOW,
                ):
                    try:
                        indicator_fn(mode_label, True)
                    except Exception:
                        pass

                if source == VisionSource.CAMERA:
                    image = capture_camera(
                        ui=ui,
                        cancel_event=cancel_event,
                        timeout_s=min(timeout_s, 10.0),
                    )
                elif source in (VisionSource.SCREEN, VisionSource.WINDOW):
                    image = capture_screen(
                        monitor_index=monitor_index,
                        window_title=window_title,
                        cancel_event=cancel_event,
                    )
                else:
                    if not file_path:
                        return VisionResult(
                            ok=False,
                            task=task,
                            backend="none",
                            model="",
                            summary="",
                            error="No image or PDF file was provided.",
                        )
                    image = load_file_for_vision(file_path)

                # Hide live capture indicator immediately after capture finishes.
                if callable(indicator_fn):
                    try:
                        indicator_fn(mode_label, False)
                    except Exception:
                        pass

                # 4. Short preview thumbnail with Cancel before sending (camera/screen).
                if (
                    callable(preview_fn)
                    and source in (VisionSource.CAMERA, VisionSource.SCREEN, VisionSource.WINDOW)
                    and get_vision_config().get("preview_before_send", True)
                ):
                    approved = preview_fn(image.data, image.source_label or mode_label)
                    if not approved or cancel_event.is_set():
                        raise CaptureCancelledError("Capture cancelled before sending.")

            if cancel_event.is_set():
                raise CaptureCancelledError("Vision task cancelled by user.")

            # 5. Analyze via VisionRouter (Gemini or Local fallback).
            result = self.router.analyze(
                image,
                prompt,
                task=task,
                timeout_s=timeout_s,
            )
            self._last_result = result

            if result.ok:
                if remember_summary:
                    self.save_result_to_memory(result)
                self._deliver_to_ui(result, ui=ui, speak=speak)
            else:
                self._deliver_error(result.error or "Vision analysis failed.", ui=ui, speak=speak)

            return result

        except (VisionPermissionDeniedError, CaptureCancelledError, CaptureUnavailableError, FileNotFoundError, TimeoutError) as exc:
            msg = str(exc)
            self._deliver_error(msg, ui=ui, speak=speak)
            return VisionResult(
                ok=False,
                task=task,
                backend="none",
                model="",
                summary="",
                error=msg,
            )
        except Exception as exc:
            LOGGER.exception("Unexpected vision error: %s", type(exc).__name__)
            msg = f"Vision failed unexpectedly ({type(exc).__name__})."
            self._deliver_error(msg, ui=ui, speak=speak)
            return VisionResult(
                ok=False,
                task=task,
                backend="none",
                model="",
                summary="",
                error=msg,
            )
        finally:
            if callable(indicator_fn):
                try:
                    indicator_fn(mode_label, False)
                except Exception:
                    pass
            with self._lock:
                self._active_cancel = None
                self._active_mode = ""

    def _deliver_to_ui(
        self,
        result: VisionResult,
        *,
        ui: Any = None,
        speak: Callable[[str], None] | None = None,
    ) -> None:
        if ui is not None:
            log_fn = getattr(ui, "write_log", None)
            if callable(log_fn):
                try:
                    preview = result.spoken_summary or result.summary[:240]
                    log_fn(f"AI: {preview}")
                except Exception:
                    pass

            show_vr = getattr(ui, "show_vision_result", None)
            if callable(show_vr):
                try:
                    title = f"VISION · {result.task.value.upper()}"
                    show_vr(title, result.summary, result.image_hash)
                except Exception:
                    pass
            else:
                show_c = getattr(ui, "show_content", None)
                if callable(show_c) and len(result.summary) > 220:
                    try:
                        show_c(f"VISION · {result.task.value.upper()}", result.summary)
                    except Exception:
                        pass

        if callable(speak) and result.spoken_summary:
            try:
                speak(result.spoken_summary)
            except Exception:
                pass

    def _deliver_error(
        self,
        message: str,
        *,
        ui: Any = None,
        speak: Callable[[str], None] | None = None,
    ) -> None:
        if ui is not None:
            log_fn = getattr(ui, "write_log", None)
            if callable(log_fn):
                try:
                    log_fn(f"SYS: {message}")
                except Exception:
                    pass
        if callable(speak):
            try:
                speak(message)
            except Exception:
                pass


_service: VisionService | None = None
_service_lock = threading.Lock()


def get_vision_service() -> VisionService:
    global _service
    with _service_lock:
        if _service is None:
            _service = VisionService()
        return _service
