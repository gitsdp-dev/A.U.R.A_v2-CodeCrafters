"""actions/calendar.py — Auto-discovered calendar tools for Gemini & LocalBrain."""

from __future__ import annotations

from core.calendar.intents import (
    handle_cancel,
    handle_conflicts,
    handle_create,
    handle_free_slots,
    handle_modify,
    handle_view,
)


def calendar_view(parameters: dict, player=None, **_) -> str:
    return handle_view(parameters, player=player)


def calendar_create(parameters: dict, player=None, **_) -> str:
    return handle_create(parameters, player=player)


def calendar_modify(parameters: dict, **_) -> str:
    return handle_modify(parameters)


def calendar_cancel(parameters: dict, **_) -> str:
    return handle_cancel(parameters)


def calendar_free_slots(parameters: dict, **_) -> str:
    return handle_free_slots(parameters)


def calendar_conflicts(parameters: dict, **_) -> str:
    return handle_conflicts(parameters)


TOOLS = [
    {
        "name": "calendar_view",
        "description": (
            "View the user's schedule or next event (today, tomorrow, rest of week, "
            "upcoming week, or next event). Set show_panel=true when the user says 'show my schedule'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "range": {
                    "type": "STRING",
                    "description": "Time range: today, tomorrow, rest of week, upcoming week, or next.",
                },
                "show_panel": {
                    "type": "BOOLEAN",
                    "description": "True if the user asked to show/open the schedule panel on screen.",
                },
            },
            "required": [],
        },
        "handler": calendar_view,
    },
    {
        "name": "calendar_create",
        "description": "Schedule a new calendar event after checking for conflicts.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING", "description": "Event title"},
                "when": {
                    "type": "STRING",
                    "description": "Date and time phrase, e.g. 'tomorrow at 5 PM' or 'next Friday at 10am for 45 minutes'",
                },
                "duration_minutes": {
                    "type": "INTEGER",
                    "description": "Duration in minutes (optional, default 60)",
                },
                "location": {"type": "STRING", "description": "Optional location"},
                "reminder_minutes": {
                    "type": "INTEGER",
                    "description": "Optional reminder lead time in minutes",
                },
                "force": {
                    "type": "BOOLEAN",
                    "description": "Set true only if the user explicitly confirmed scheduling despite a clash",
                },
            },
            "required": ["title", "when"],
        },
        "handler": calendar_create,
    },
    {
        "name": "calendar_modify",
        "description": "Modify an existing calendar event (asks user confirmation and supports undo).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Title or keywords of the event to modify"},
                "new_when": {"type": "STRING", "description": "New date/time phrase"},
                "new_title": {"type": "STRING", "description": "New event title"},
                "new_location": {"type": "STRING", "description": "New location"},
                "new_duration_minutes": {"type": "INTEGER", "description": "New duration in minutes"},
            },
            "required": ["query"],
        },
        "handler": calendar_modify,
    },
    {
        "name": "calendar_cancel",
        "description": "Cancel/delete a calendar event (asks user confirmation and supports undo).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Title or keywords of the event to cancel"},
            },
            "required": ["query"],
        },
        "handler": calendar_cancel,
    },
    {
        "name": "calendar_free_slots",
        "description": "Find free time slots in the user's schedule within working hours.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "when": {"type": "STRING", "description": "Day or range, e.g. 'tomorrow afternoon'"},
                "duration_minutes": {"type": "INTEGER", "description": "Required free duration in minutes"},
            },
            "required": [],
        },
        "handler": calendar_free_slots,
    },
    {
        "name": "calendar_conflicts",
        "description": "Check for overlapping/clashing events across all enabled calendars.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "when": {"type": "STRING", "description": "Range to check, e.g. 'this week' or 'today'"},
            },
            "required": [],
        },
        "handler": calendar_conflicts,
    },
]
