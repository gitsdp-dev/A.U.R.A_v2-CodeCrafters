"""Registry plugin for verified bounded screen automation."""

from core.agent.general_tools import screen_tool as _run
from plugins._general_schema import GENERAL_PARAMETERS

PLUGIN = {
    "name": "screen_tool",
    "description": "Capture the screen, ask Gemini vision for a next action, execute it, and recapture with a hard step cap.",
    "parameters": GENERAL_PARAMETERS,
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Run one screen automation goal."""
    return _run(parameters, player)
