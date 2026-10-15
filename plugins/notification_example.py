"""Example plugin showing how to publish a local A.U.R.A. notification."""

from core.notifications.bus import notify

PLUGIN = {
    "name": "notification_example",
    "description": (
        "Demonstrates A.U.R.A.'s local notification bus. Publish a sample "
        "application notification when explicitly requested."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING", "description": "Short notification title."},
            "body": {"type": "STRING", "description": "Notification body text."},
        },
        "required": ["title", "body"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Publish a sample app notification to the local notification center."""
    notification = notify(
        "A.U.R.A. Example",
        parameters["title"],
        parameters["body"],
        priority=40,
    )
    return f"Local notification published ({notification.id})."
