"""Registry plugin for safe application and file launching."""

from core.agent.general_tools import app_launcher_tool as _run
from plugins._general_schema import GENERAL_PARAMETERS

PLUGIN = {
    "name": "app_launcher_tool",
    "description": "Open or focus a safe application or existing file without shell interpolation.",
    "parameters": GENERAL_PARAMETERS,
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Run one launcher operation."""
    return _run(parameters, player)
