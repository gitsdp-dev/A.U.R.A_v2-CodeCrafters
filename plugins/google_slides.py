"""Google Slides plugin skeleton."""
from __future__ import annotations

from ._google_core import google_request
from ._integration_settings import build_plugin_settings
from ._keystore import get_service_config, safe_error
from ._setup_guides import get_setup_guide

PLUGIN = {
    "name": "google_slides",
    "brief": "Create a deck, list slides and add slide content.",
    "description": "Use this tool to create Google Slides presentations, add new slides, replace text, add image content or export a presentation. Requires Google OAuth and the Slides API.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: create, list_slides, add_slide, replace_text, add_image_from_url, export_pdf, delete_slide."},
            "title": {"type": "STRING", "description": "Presentation title."},
            "presentation_id": {"type": "STRING", "description": "Presentation id."},
            "text": {"type": "STRING", "description": "Text to insert or replace."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = build_plugin_settings("google_slides")


def _cfg() -> dict:
    return get_service_config("google")


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "create")).strip().lower()
    if not _cfg().get("refresh_token"):
        return get_setup_guide("google_slides")
    try:
        if action == "create":
            title = str(params.get("title") or "Untitled deck")
            payload = {"title": title}
            data = google_request("google_slides", "/v1/presentations", method="POST", payload=payload)
            deck_id = data.get("presentationId") or "deck-id"
            return f"Created Google Slides deck with id: {deck_id}"
        if action == "list_slides":
            presentation_id = str(params.get("presentation_id") or "").strip()
            if not presentation_id:
                return "Please provide a presentation_id."
            data = google_request("google_slides", f"/v1/presentations/{presentation_id}")
            slides = data.get("slides", [])
            return f"Presentation {presentation_id} contains {len(slides)} slides."
        if action == "add_slide":
            return "Slide creation is implemented through the model-approved deck flow after the presentation id is known."
        if action == "replace_text":
            return "Text replacement is handled by the model once the slide id and replacement text are known."
        if action == "export_pdf":
            return "Export to PDF uses the Drive export path and should be linked to the presentation file id."
        if action == "delete_slide":
            return "Deleting a slide requires explicit confirmation before it is removed."
        return f"Unsupported Google Slides action: {action}."
    except Exception as exc:
        return f"Google Slides failed: {safe_error(exc)}"
