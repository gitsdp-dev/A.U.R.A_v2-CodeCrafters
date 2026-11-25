"""Intent matching for A.U.R.A.'s Advanced Vision System."""

from __future__ import annotations

import re

from core.vision.models import VisionIntent, VisionSource, VisionTaskType

_RE_DIAGRAM = re.compile(
    r"\b(?:explain|analyze|describe|break\s+down)\s+(?:this|the)\s+"
    r"(?:architecture|diagram|flowchart|system\s+design|schematic)\b",
    re.IGNORECASE,
)
_RE_CHART = re.compile(
    r"\b(?:explain|analyze|read|summarize|interpret)\s+(?:this|the)\s+"
    r"(?:chart|graph|plot|histogram|bar\s+chart|line\s+chart|pie\s+chart)\b",
    re.IGNORECASE,
)
_RE_DOCUMENT = re.compile(
    r"\b(?:read\s+(?:this|the)\s+(?:page|document|pdf|form|receipt)"
    r"(?:\s+and\s+summarize(?:\s+it)?)?|"
    r"summarize\s+(?:this|the)\s+(?:page|document|pdf|form|receipt)|"
    r"what(?:'s|\s+is)\s+in\s+(?:this|the)\s+(?:document|pdf|page|receipt))\b",
    re.IGNORECASE,
)
_RE_OCR = re.compile(
    r"\b(?:extract\s+(?:the\s+)?text\s+(?:from|in)\s+(?:this|the)\s+"
    r"(?:image|screenshot|screen|page|photo|picture)|"
    r"ocr\s+(?:this|the\s+(?:image|screen|page))|"
    r"copy\s+text\s+from\s+(?:this|the)\s+(?:image|screen|screenshot))\b",
    re.IGNORECASE,
)
_RE_CAMERA = re.compile(
    r"\b(?:what\s+(?:is\s+this\s+object|am\s+i\s+holding|is\s+in\s+my\s+hand)|"
    r"identify\s+this\s+(?:object|item|thing)|"
    r"(?:look\s+at|check)\s+this\s+(?:with|on|via)\s+(?:the\s+)?camera)\b",
    re.IGNORECASE,
)
_RE_SCREEN = re.compile(
    r"\b(?:what(?:'s|\s+is)\s+on\s+(?:my\s+)?screen|"
    r"(?:read|describe|explain|analyze)\s+(?:my\s+|this\s+)?screen(?:shot)?)\b",
    re.IGNORECASE,
)
_RE_REMEMBER_DOC = re.compile(
    r"\bremember\s+what(?:'s|\s+is)\s+in\s+(?:this|the)\s+(?:document|page|image|screenshot|note)\b",
    re.IGNORECASE,
)
_RE_FOLLOWUP = re.compile(
    r"\b(?:about\s+(?:this|that)\s+(?:image|photo|picture|screenshot|diagram|chart)|"
    r"follow[\s-]?up\s+(?:on|about)\s+(?:this|that)\s+(?:image|screenshot))\b",
    re.IGNORECASE,
)


def match_vision_intent(text: str, *, has_selected_file: bool = False) -> VisionIntent | None:
    """Return a VisionIntent if *text* clearly asks for an on-demand vision task."""
    raw = (text or "").strip()
    if not raw or raw.startswith("["):
        return None

    remember = bool(_RE_REMEMBER_DOC.search(raw))

    if _RE_FOLLOWUP.search(raw):
        return VisionIntent(
            task=VisionTaskType.VQA,
            source=VisionSource.CACHED,
            prompt=raw,
            remember_summary=remember,
            is_followup=True,
        )

    if _RE_CAMERA.search(raw):
        return VisionIntent(
            task=VisionTaskType.CAMERA_OBJECT,
            source=VisionSource.CAMERA,
            prompt=raw,
            remember_summary=remember,
        )

    default_source = VisionSource.FILE if has_selected_file else VisionSource.SCREEN

    if _RE_DIAGRAM.search(raw):
        return VisionIntent(
            task=VisionTaskType.DIAGRAM,
            source=default_source,
            prompt=raw,
            remember_summary=remember,
        )
    if _RE_CHART.search(raw):
        return VisionIntent(
            task=VisionTaskType.CHART,
            source=default_source,
            prompt=raw,
            remember_summary=remember,
        )
    if _RE_OCR.search(raw):
        return VisionIntent(
            task=VisionTaskType.OCR,
            source=default_source,
            prompt=raw,
            remember_summary=remember,
        )
    if _RE_DOCUMENT.search(raw) or remember:
        return VisionIntent(
            task=VisionTaskType.DOCUMENT,
            source=default_source,
            prompt=raw,
            remember_summary=remember,
        )
    if _RE_SCREEN.search(raw):
        return VisionIntent(
            task=VisionTaskType.SCREENSHOT,
            source=VisionSource.SCREEN,
            prompt=raw,
            remember_summary=remember,
        )
    return None
