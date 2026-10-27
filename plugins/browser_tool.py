"""Registry plugin for safe Playwright browser operations."""

from core.agent.general_tools import browser_tool as _run
from plugins._general_schema import GENERAL_PARAMETERS

PLUGIN = {
    "name": "browser_tool",
    "description": "Navigate, click, fill, extract text, download, or screenshot web pages with bounded Playwright operations and sensitive-domain confirmation.",
    "parameters": GENERAL_PARAMETERS,
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Run one browser operation."""
    return _run(parameters, player)
