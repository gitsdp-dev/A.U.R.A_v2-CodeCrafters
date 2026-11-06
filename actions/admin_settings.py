"""Allowlisted Windows preferences; unsupported system-wide controls are not exposed."""

from core.windows_settings import execute


def admin_settings(parameters: dict, **_context) -> str:
    if isinstance(parameters.get("value"), str):
        value = parameters["value"].strip().casefold()
        if value in {"true", "false"}:
            parameters = {**parameters, "value": value == "true"}
    return execute(parameters.get("operation", "list"), parameters)


TOOL = {
    "name": "admin_settings",
    "description": (
        "Read or change only the explicitly allowlisted Windows settings. "
        "Every setting change requires the confirmation dialog, is verified by reading it back, "
        "and can be undone during this session. This tool cannot run commands or change arbitrary settings."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "description": "list, get, set, or undo",
            },
            "setting_id": {
                "type": "STRING",
                "description": "Allowlisted id: app_theme, show_hidden_files, show_file_extensions, or uac_level (read-only).",
            },
            "value": {
                "type": "STRING",
                "description": "For app_theme use dark or light. For Explorer toggles use true or false.",
            },
        },
        "required": [],
    },
    "handler": admin_settings,
}
