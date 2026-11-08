"""App access and action confirmation policy for Screen Agent."""

from __future__ import annotations

import re
from typing import Any

_DENIED_APP_PATTERNS = (
    r"\b(?:1password|bitwarden|lastpass|dashlane|keepass|vault)\b",
    r"\b(?:password manager|credential manager)\b",
    r"\b(?:bank|banking|paypal|venmo|cash app|stripe|payment|wallet|crypto|coinbase|purchase|checkout)\b",
)
_DESTRUCTIVE = re.compile(
    r"\b(?:delete|remove|send|submit|pay|purchase|buy|checkout|uninstall|"
    r"admin|administrator|privilege|settings)\b",
    re.IGNORECASE,
)
_PASSWORD = re.compile(
    r"\b(?:password|passcode|passphrase|pin code|security code|one.time code|"
    r"verification code|credential)\b",
    re.IGNORECASE,
)
_UNSAVED = re.compile(
    r"\b(?:unsaved|save changes|don't save|do not save|discard changes|"
    r"changes will be lost)\b",
    re.IGNORECASE,
)


def app_allowed(app: str, allow_apps: list[str], deny_apps: list[str]) -> tuple[bool, str]:
    """Refuse protected, explicitly denied, and non-allow-listed applications."""
    identity = app.strip()
    folded = identity.casefold()
    if not identity:
        return False, "The active application could not be identified safely."
    for pattern in _DENIED_APP_PATTERNS:
        if re.search(pattern, folded, re.IGNORECASE):
            return False, "This app category is blocked by Screen Agent's default deny policy."
    normalized = folded.removesuffix(".exe").strip()
    if any(
        normalized == value.casefold().removesuffix(".exe").strip()
        for value in deny_apps if value.strip()
    ):
        return False, "This application is on the Screen Agent deny list."
    if allow_apps and not any(
        normalized == value.casefold().removesuffix(".exe").strip()
        for value in allow_apps if value.strip()
    ):
        return False, "This application is not on the Screen Agent allow list."
    return True, ""


def app_context_allowed(
    app: str, context: str, allow_apps: list[str], deny_apps: list[str]
) -> tuple[bool, str]:
    """Apply deny patterns to the app and visible window title."""
    allowed, reason = app_allowed(app, [], deny_apps)
    if not allowed:
        return allowed, reason
    title = context.strip()
    if title:
        for pattern in _DENIED_APP_PATTERNS:
            if re.search(pattern, title, re.IGNORECASE):
                return False, "This app category is blocked by Screen Agent's default deny policy."
        if any(value.casefold() in title.casefold() for value in deny_apps if value.strip()):
            return False, "This application is on the Screen Agent deny list."
    return app_allowed(app, allow_apps, deny_apps)


def sensitive_target(element: Any) -> bool:
    """Return whether a target appears to be a password or credential field."""
    role = str(getattr(element, "role", "")).casefold()
    name = str(getattr(element, "name", ""))
    return role in {"password", "passwordbox", "securetext"} or bool(_PASSWORD.search(name))


def confirmation_reason(
    action: dict[str, Any],
    *,
    goal: str,
    element: Any = None,
    snapshot_text: str = "",
) -> str:
    """Return a short reason when an action must be confirmed, else an empty string."""
    action_name = str(action.get("a") or action.get("action") or "").casefold()
    target_text = " ".join((
        str(getattr(element, "name", "")),
        str(getattr(element, "role", "")),
        goal,
    ))
    if action_name == "type" and sensitive_target(element):
        return "Typing into a password-like field"
    if _DESTRUCTIVE.search(target_text):
        return "A potentially destructive or irreversible action"
    if action_name in {"close", "close-app"} and _UNSAVED.search(snapshot_text):
        return "Closing an app that appears to contain unsaved changes"
    return ""
