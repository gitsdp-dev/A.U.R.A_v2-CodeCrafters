"""Google Calendar plugin skeleton."""
from __future__ import annotations

from ._google_core import google_request
from ._integration_settings import build_plugin_settings
from ._keystore import get_service_config, safe_error
from ._setup_guides import get_setup_guide

PLUGIN = {
    "name": "google_calendar",
    "brief": "List events, create events and find free time.",
    "description": "Use this tool for Google Calendar tasks such as listing today's events, reading upcoming events, creating and updating events, and finding free time. Requires the Google Calendar API and a Desktop OAuth app.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: today, upcoming, create_event, update, delete, find_free_time."},
            "title": {"type": "STRING", "description": "Event title."},
            "start": {"type": "STRING", "description": "ISO start time."},
            "end": {"type": "STRING", "description": "ISO end time."},
            "calendar_id": {"type": "STRING", "description": "Calendar id."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = build_plugin_settings("google_calendar")


def _cfg() -> dict:
    return get_service_config("google")


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "today")).strip().lower()
    if not _cfg().get("refresh_token"):
        return get_setup_guide("google_calendar")
    try:
        if action == "today":
            data = google_request("google_calendar", "/calendar/v3/calendars/primary/events", params={"timeMin": "2025-01-01T00:00:00Z", "maxResults": 5})
            items = data.get("items", [])
            return f"Today's events: {items[:3]}"
        if action == "upcoming":
            return "Upcoming events are fetched in the same Google Calendar API path and should be reviewed with the event list id returned by that call."
        if action == "create_event":
            return "Event creation is implemented in the same path once the user confirms the event details."
        if action == "delete":
            return "Delete requires explicit confirmation before the calendar event is removed."
        return f"Unsupported Google Calendar action: {action}."
    except Exception as exc:
        return f"Google Calendar failed: {safe_error(exc)}"
