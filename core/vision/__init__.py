"""A.U.R.A. Advanced Vision System."""

from core.vision.backends import GeminiVisionBackend, LocalVisionBackend, VisionBackend
from core.vision.intents import match_vision_intent
from core.vision.models import (
    VisionCapability,
    VisionImage,
    VisionIntent,
    VisionResult,
    VisionSource,
    VisionTaskType,
)
from core.vision.permissions import VisionPermissionDeniedError, VisionPermissionManager
from core.vision.router import VisionRouter
from core.vision.service import VisionService, get_vision_service

__all__ = [
    "GeminiVisionBackend",
    "LocalVisionBackend",
    "VisionBackend",
    "VisionCapability",
    "VisionImage",
    "VisionIntent",
    "VisionPermissionDeniedError",
    "VisionPermissionManager",
    "VisionResult",
    "VisionRouter",
    "VisionService",
    "VisionSource",
    "VisionTaskType",
    "get_vision_service",
    "match_vision_intent",
]
