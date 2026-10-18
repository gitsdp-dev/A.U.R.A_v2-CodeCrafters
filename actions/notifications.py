"""Local notification-center voice command."""


def manage_notifications(parameters: dict, player=None) -> str:
    """Show/read notification history or control an active local readout."""
    if player is None or not hasattr(player, "handle_notification_command"):
        return "The local notification center is unavailable."
    action = str(parameters.get("action") or "show_read").strip().lower()
    return player.handle_notification_command(action)


TOOL = {
    "name": "manage_notifications",
    "description": (
        "Open A.U.R.A.'s local notification center and optionally read new "
        "notifications aloud using local offline speech. Use this for requests "
        "to show/list/read notifications, stop or skip a notification readout, "
        "clear notification history, or turn notifications on or off. "
        "Notification contents remain local and are never returned as tool data."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": [
                    "show_read", "show", "read", "stop", "skip", "clear",
                    "enable", "disable",
                ],
                "description": "Notification-center operation; defaults to show_read.",
            },
        },
        "required": [],
    },
    "handler": manage_notifications,
}
