"""Registry plugin for restricted Python execution."""

from core.agent.general_tools import python_sandbox_tool as _run
from plugins._general_schema import GENERAL_PARAMETERS

PLUGIN = {
    "name": "python_sandbox_tool",
    "description": "Run short generated Python in an isolated subprocess with a timeout, scratch directory, and no network imports.",
    "parameters": GENERAL_PARAMETERS,
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Run one sandbox operation."""
    return _run(parameters, player)
