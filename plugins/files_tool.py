"""Registry plugin for safe common-file operations."""

from core.agent.general_tools import files_tool as _run
from plugins._general_schema import GENERAL_PARAMETERS

PLUGIN = {
    "name": "files_tool",
    "description": "Read and write common text, data, document, spreadsheet, PDF, and image formats inside an approved workspace.",
    "parameters": GENERAL_PARAMETERS,
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Run one filesystem operation."""
    return _run(parameters, player)
