"""Registry plugin for confirmed, sandbox-validated generated plugin proposals."""

from core.agent.proposals import PluginProposalService

PLUGIN = {
    "name": "plugin_proposal",
    "description": "Propose a new plugin with code and description; validate and sandbox-test it, then require confirmation before saving.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING"},
            "name": {"type": "STRING"},
            "code": {"type": "STRING"},
            "confirmed": {"type": "BOOLEAN"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Validate or save one generated plugin proposal."""
    service = PluginProposalService()
    if str(parameters.get("action") or "").casefold() != "propose":
        return "Use action=propose."
    result = service.save_after_confirmation(
        str(parameters.get("name") or ""),
        str(parameters.get("code") or ""),
        bool(parameters.get("confirmed", False)),
    )
    if result.startswith("CONFIRMATION_REQUIRED"):
        from core.confirm import request

        return request(
            "plugin-proposal",
            "Save generated plugin",
            result,
            lambda: service.save_after_confirmation(
                str(parameters.get("name") or ""),
                str(parameters.get("code") or ""),
                True,
            ),
        )
    return result
