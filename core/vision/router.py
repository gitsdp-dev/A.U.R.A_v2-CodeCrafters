"""Backend routing, session image caching, and notification notices."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from core.vision.backends import GeminiVisionBackend, LocalVisionBackend, VisionBackend
from core.vision.models import VisionImage, VisionResult, VisionTaskType
from memory.config_manager import get_local_llm_config

LOGGER = logging.getLogger(__name__)


def _post_vision_notice(title: str, body: str) -> None:
    """Post a non-warning informational notice to A.U.R.A.'s notification panel."""
    try:
        from core.notifications.bus import notification_bus
        from core.notifications.models import Notification

        notification_bus.publish(
            Notification(
                app_name="AURA Vision",
                title=title,
                body=body,
                category="model_router",
                priority=5,
            )
        )
    except Exception:
        LOGGER.debug("Notification bus unavailable for vision notice.")


class VisionRouter:
    """Selects Gemini vs Local vision backend and caches results by image hash."""

    def __init__(
        self,
        gemini_backend: VisionBackend | None = None,
        local_backend: VisionBackend | None = None,
        config_loader: Callable[[], dict] = get_local_llm_config,
        notifier: Callable[[str, str], None] = _post_vision_notice,
    ) -> None:
        self._gemini = gemini_backend or GeminiVisionBackend()
        self._local = local_backend or LocalVisionBackend()
        self._config_loader = config_loader
        self._notifier = notifier
        self._lock = threading.Lock()
        self._result_cache: dict[tuple[str, str, str], VisionResult] = {}
        self._image_cache: dict[str, VisionImage] = {}
        self._last_image_hash: str = ""

    def get_last_image(self) -> VisionImage | None:
        with self._lock:
            if not self._last_image_hash:
                return None
            return self._image_cache.get(self._last_image_hash)

    def get_cached_image(self, image_hash: str) -> VisionImage | None:
        with self._lock:
            return self._image_cache.get(image_hash)

    def store_image(self, image: VisionImage) -> None:
        with self._lock:
            self._image_cache[image.sha256] = image
            self._last_image_hash = image.sha256

    def clear_cache(self) -> None:
        with self._lock:
            self._result_cache.clear()
            self._image_cache.clear()
            self._last_image_hash = ""

    def select_backends(self) -> list[VisionBackend]:
        cfg = self._config_loader()
        mode = str(cfg.get("mode", "gemini")).strip().lower()
        gemini_ok = getattr(self._gemini, "is_available", lambda: True)()

        if mode == "local":
            return [self._local]
        if mode == "fallback":
            return [self._gemini, self._local] if gemini_ok else [self._local]
        # Default "gemini" mode: prefer Gemini, fall back to local if Gemini key missing/fails
        return [self._gemini, self._local] if gemini_ok else [self._local]

    def analyze(
        self,
        image: VisionImage,
        prompt: str,
        *,
        task: VisionTaskType = VisionTaskType.GENERAL,
        timeout_s: float = 30.0,
        use_cache: bool = True,
    ) -> VisionResult:
        self.store_image(image)
        cache_key = (image.sha256, task.value, " ".join((prompt or "").lower().split()))
        if use_cache:
            with self._lock:
                cached = self._result_cache.get(cache_key)
            if cached is not None:
                return VisionResult(
                    ok=cached.ok,
                    task=cached.task,
                    backend=cached.backend,
                    model=cached.model,
                    summary=cached.summary,
                    details=cached.details,
                    spoken_summary=cached.spoken_summary,
                    ocr_text=cached.ocr_text,
                    image_hash=cached.image_hash,
                    error=cached.error,
                    cached=True,
                    metadata=dict(cached.metadata),
                )

        backends = self.select_backends()
        cfg_mode = str(self._config_loader().get("mode", "gemini")).strip().lower()
        last_result: VisionResult | None = None

        for idx, backend in enumerate(backends):
            res = backend.analyze_image(image, prompt, task=task, timeout_s=timeout_s)
            if res.ok:
                if idx > 0 and cfg_mode != "local":
                    self._notifier(
                        "Vision Fallback",
                        f"Switched to {backend.name} vision backend.",
                    )
                if use_cache:
                    with self._lock:
                        self._result_cache[cache_key] = res
                return res
            last_result = res
            if idx == 0 and len(backends) > 1:
                LOGGER.info("Primary vision backend %s failed; trying fallback.", backend.name)
                self._notifier(
                    "Vision Backend Notice",
                    f"{backend.name.capitalize()} vision unavailable; trying local fallback.",
                )

        return last_result or VisionResult(
            ok=False,
            task=task,
            backend="none",
            model="",
            summary="",
            error="No vision backend is available.",
            image_hash=image.sha256,
        )
