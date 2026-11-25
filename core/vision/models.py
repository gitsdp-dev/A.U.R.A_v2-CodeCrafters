"""Data models for A.U.R.A.'s Advanced Vision System."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class VisionTaskType(str, Enum):
    GENERAL = "general"
    VQA = "vqa"
    SCREENSHOT = "screenshot"
    DOCUMENT = "document"
    DIAGRAM = "diagram"
    CHART = "chart"
    OCR = "ocr"
    CAMERA_OBJECT = "camera_object"


class VisionSource(str, Enum):
    CAMERA = "camera"
    SCREEN = "screen"
    WINDOW = "window"
    FILE = "file"
    PDF = "pdf"
    CACHED = "cached"


class VisionCapability(str, Enum):
    CAMERA = "camera"
    SCREEN = "screen"
    FILE = "file"


@dataclass
class VisionImage:
    """In-memory image payload prepared for vision analysis."""

    data: bytes
    mime_type: str
    sha256: str
    width: int = 0
    height: int = 0
    source: VisionSource = VisionSource.FILE
    source_label: str = ""
    page_count: int = 1
    extracted_text: str = ""


@dataclass
class VisionResult:
    """Structured output returned by a VisionBackend."""

    ok: bool
    task: VisionTaskType
    backend: str
    model: str
    summary: str
    details: str = ""
    spoken_summary: str = ""
    ocr_text: str = ""
    image_hash: str = ""
    error: str = ""
    cached: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VisionIntent:
    """Parsed natural-language vision command."""

    task: VisionTaskType
    source: VisionSource | None
    prompt: str
    remember_summary: bool = False
    is_followup: bool = False
    window_title: str = ""
