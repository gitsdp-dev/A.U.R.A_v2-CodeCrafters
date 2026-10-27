"""Registry plugin for source-linked web research."""

from core.agent.general_tools import web_research_tool as _run
from plugins._general_schema import GENERAL_PARAMETERS

PLUGIN = {
    "name": "web_research_tool",
    "description": "Search, fetch, summarize, and cite web sources with clickable links and bounded network calls.",
    "parameters": GENERAL_PARAMETERS,
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Run one web research operation."""
    return _run(parameters, player)
