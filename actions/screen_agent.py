"""A.U.R.A. action-registry entry point for accessibility-based desktop control."""

from __future__ import annotations

from screen_agent.agent import run_screen_agent_action

TOOL = {
    "name": "screen_agent",
    "description": (
        "See and operate an app through its accessibility tree. Use for requests "
        "to open or focus desktop apps, click visible controls, type into fields, "
        "press keys, scroll, or otherwise interact with the current desktop. "
        "The action enforces app restrictions, privacy, confirmation, and step limits."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "The user's requested desktop task, in plain language.",
            },
        },
        "required": ["goal"],
    },
    "handler": run_screen_agent_action,
}
