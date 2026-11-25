"""On-demand Advanced Vision action tools discovered by core/action_loader.py."""

from __future__ import annotations

from core.vision.models import VisionSource, VisionTaskType
from core.vision.service import get_vision_service


def _parse_source(raw: str) -> VisionSource:
    val = (raw or "screen").strip().lower()
    for src in VisionSource:
        if src.value == val:
            return src
    return VisionSource.SCREEN


def _parse_task(raw: str) -> VisionTaskType:
    val = (raw or "general").strip().lower()
    for task in VisionTaskType:
        if task.value == val:
            return task
    return VisionTaskType.GENERAL


def analyze_vision(parameters: dict, player=None, speak=None) -> str:
    """Run an on-demand vision task (screen, window, camera, file, or PDF)."""
    params = parameters if isinstance(parameters, dict) else {}
    source = _parse_source(str(params.get("source", "screen")))
    task = _parse_task(str(params.get("task", "general")))
    prompt = str(params.get("prompt", "") or params.get("question", "")).strip()
    file_path = str(params.get("file_path", "")).strip()
    if not file_path and source == VisionSource.FILE and player is not None:
        cur = getattr(player, "current_file", None)
        if callable(cur):
            file_path = str(cur() or "").strip()
    window_title = str(params.get("window_title", "")).strip()
    remember_summary = bool(params.get("remember_summary", False))

    svc = get_vision_service()
    res = svc.run_task(
        source=source,
        task=task,
        prompt=prompt,
        file_path=file_path,
        window_title=window_title,
        remember_summary=remember_summary,
        ui=player,
        speak=None,  # caller / main session speaks or _deliver_to_ui logs it
    )
    if not res.ok:
        return res.error or "Vision analysis failed."
    return res.summary


def vision_followup(parameters: dict, player=None, speak=None) -> str:
    """Answer a follow-up question about the last cached image without re-capturing."""
    params = parameters if isinstance(parameters, dict) else {}
    question = str(params.get("question", "") or params.get("prompt", "")).strip()
    if not question:
        return "Ask a specific question about the image."
    image_hash = str(params.get("image_hash", "")).strip()
    svc = get_vision_service()
    res = svc.run_task(
        source=VisionSource.CACHED,
        task=VisionTaskType.VQA,
        prompt=question,
        image_hash=image_hash,
        ui=player,
        speak=None,
    )
    if not res.ok:
        return res.error or "Could not answer follow-up question."
    return res.summary


def remember_vision_summary(parameters: dict, player=None) -> str:
    """Save the text summary of the most recent vision analysis to memory."""
    params = parameters if isinstance(parameters, dict) else {}
    key_hint = str(params.get("key", "")).strip()
    svc = get_vision_service()
    last = svc._last_result
    if last is None or not last.ok:
        return "No recent vision analysis is available to remember."
    return svc.save_result_to_memory(last, key_hint=key_hint)


TOOLS = [
    {
        "name": "analyze_vision",
        "description": (
            "On-demand, permission-gated visual analysis for screenshots, windows, "
            "camera object recognition, documents/PDFs, diagrams/architecture, charts, "
            "and layout-aware OCR."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "source": {
                    "type": "STRING",
                    "description": "One of: screen, window, camera, file, pdf, cached.",
                },
                "task": {
                    "type": "STRING",
                    "description": "One of: general, vqa, screenshot, document, diagram, chart, ocr, camera_object.",
                },
                "prompt": {
                    "type": "STRING",
                    "description": "User question or analysis focus.",
                },
                "file_path": {
                    "type": "STRING",
                    "description": "Optional path to an image or PDF file.",
                },
                "window_title": {
                    "type": "STRING",
                    "description": "Optional window title when source is 'window'.",
                },
                "remember_summary": {
                    "type": "BOOLEAN",
                    "description": "If true, save a text summary (never raw image) to memory.",
                },
            },
            "required": ["source", "task"],
        },
        "handler": analyze_vision,
    },
    {
        "name": "vision_followup",
        "description": "Ask a follow-up question about the last captured/uploaded image in this session without re-capturing.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "question": {"type": "STRING", "description": "Follow-up question about the cached image."},
                "image_hash": {"type": "STRING", "description": "Optional SHA-256 hash of the cached image."},
            },
            "required": ["question"],
        },
        "handler": vision_followup,
    },
    {
        "name": "remember_vision_summary",
        "description": "Save a text summary of the most recent vision result to long-term memory (never stores raw images).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "key": {"type": "STRING", "description": "Optional short memory key label."},
            },
        },
        "handler": remember_vision_summary,
    },
]
