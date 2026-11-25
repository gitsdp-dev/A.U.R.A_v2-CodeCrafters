"""
core/learning/__init__.py — A.U.R.A Adaptive Intelligence Package.

Provides a provider-agnostic self-learning subsystem that improves assistant
behaviour through experience without retraining or modifying model weights.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .learning_engine import BaseLearningEngine, LearningEngine, NoOpLearningEngine
from .learning_settings import LearningSettings, load_settings, save_settings

__all__ = [
    "LearningEngine",
    "NoOpLearningEngine",
    "LearningSettings",
    "get_learning_engine",
    "load_settings",
    "save_settings",
]

LOGGER = logging.getLogger("aura.learning")

_ENGINE_INSTANCE: BaseLearningEngine | None = None
_INIT_LOCK = threading.Lock()


def get_learning_engine() -> BaseLearningEngine:
    """
    Return the global LearningEngine instance.
    If learning is disabled in settings or initialization fails, returns a safe
    NoOpLearningEngine that performs no-ops and never raises.
    """
    global _ENGINE_INSTANCE
    with _INIT_LOCK:
        if _ENGINE_INSTANCE is not None:
            return _ENGINE_INSTANCE

        try:
            settings = load_settings()
            if not settings.enabled:
                LOGGER.info("Learning subsystem is disabled via settings; using NoOpLearningEngine.")
                _ENGINE_INSTANCE = NoOpLearningEngine()
                return _ENGINE_INSTANCE

            _ENGINE_INSTANCE = LearningEngine(settings=settings)
            return _ENGINE_INSTANCE
        except Exception as exc:
            LOGGER.exception("Failed to initialize LearningEngine; falling back to NoOpLearningEngine: %s", exc)
            _ENGINE_INSTANCE = NoOpLearningEngine()
            return _ENGINE_INSTANCE
