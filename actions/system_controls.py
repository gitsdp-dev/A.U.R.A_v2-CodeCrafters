"""Local system settings action exposed to the Gemini tool registry."""


def manage_system_controls(parameters: dict, player=None) -> str:
    """Read or change a local OS control; all values are verified after writes."""
    if player is None or not hasattr(player, "handle_system_control_action"):
        return "System controls are unavailable."
    return player.handle_system_control_action(parameters)


TOOL = {
    "name": "manage_system_controls",
    "description": (
        "Open or close A.U.R.A.'s local System Controls panel, query the actual "
        "OS state, or change volume, brightness, Wi-Fi, airplane mode, energy "
        "saver, live captions, night light, and accessibility settings. "
        "Only report a change after the tool returns its verified OS read-back. "
        "Do not ask for or pass Wi-Fi passwords; the panel handles secure entry."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "enum": [
                    "open_panel", "close_panel", "status", "set", "adjust",
                    "wifi_scan", "wifi_connect", "wifi_disconnect", "output_switch",
                ],
                "description": "Panel operation, OS-state query, or local setting change.",
            },
            "control_id": {
                "type": "STRING",
                "enum": [
                    "volume", "mute", "brightness", "wifi", "airplane_mode",
                    "energy_saver", "live_captions", "night_light", "narrator",
                    "magnifier", "onscreen_keyboard", "sticky_keys", "filter_keys",
                    "toggle_keys", "high_contrast", "mono_audio", "color_filters",
                    "output_devices",
                    "text_size",
                ],
                "description": "Control to query or change.",
            },
            "value": {
                "type": "STRING",
                "description": "Optional target value as text.",
            },
            "enabled": {
                "type": "BOOLEAN",
                "description": "Target on/off state for a toggle control.",
            },
            "percentage": {
                "type": "NUMBER",
                "description": "Target percentage or text-size value.",
            },
            "delta": {
                "type": "NUMBER",
                "description": "Amount to increase or decrease a numeric setting by.",
            },
            "device": {
                "type": "STRING",
                "description": "Audio output device name.",
            },
            "network": {
                "type": "STRING",
                "description": "Wi-Fi network name; never include a password.",
            },
            "confirmed": {
                "type": "BOOLEAN",
                "description": "Explicit confirmation for Wi-Fi-off or airplane-mode-on.",
            },
            "page": {
                "type": "STRING",
                "enum": ["main", "wifi", "accessibility", "output_devices", "energy_saver", "night_light"],
                "description": "Optional panel submenu to open.",
            },
        },
        "required": ["operation"],
    },
    "handler": manage_system_controls,
}
