"""Deterministic local notification prioritisation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

CRITICAL = "CRITICAL"
HIGH = "HIGH"
NORMAL = "NORMAL"
LOW = "LOW"

LEVELS = {
    CRITICAL: 90,
    HIGH: 70,
    NORMAL: 40,
    LOW: 15,
}

_APP_GROUPS = {
    "calls": (95, ("phone", "call", "dialer", "alarm", "security")),
    "messaging": (78, ("whatsapp", "telegram", "messages", "messenger", "signal")),
    "email": (62, ("mail", "outlook", "gmail", "thunderbird")),
    "calendar": (52, ("calendar",)),
    "social": (35, ("instagram", "facebook", "social", "discord", "reddit")),
    "system": (28, ("windows", "system", "settings", "battery")),
    "promotional": (10, ("promotion", "offer", "shopping", "advert")),
}
_URGENT = (
    ("verification code", 32),
    ("incoming call", 35),
    ("urgent", 28),
    ("security", 25),
    ("otp", 25),
    ("alert", 22),
    ("failed", 20),
    ("payment", 20),
    ("battery low", 18),
)


def _app_base(app_name: str, overrides: dict[str, Any]) -> tuple[int, str, bool]:
    folded = app_name.casefold().strip()
    for name, value in overrides.items():
        if str(name).casefold().strip() == folded:
            if isinstance(value, str):
                return LEVELS.get(value.upper(), 40), value.casefold(), True
            try:
                return max(0, min(100, int(value))), "custom", True
            except (TypeError, ValueError):
                break
    for category, (score, names) in _APP_GROUPS.items():
        if any(name in folded for name in names):
            return score, category, False
    return LEVELS[NORMAL], "general", False


def score_notification(notification, config: dict | None = None,
                       now: datetime | None = None) -> int:
    """Return a 0-100 score; user app overrides take precedence over defaults."""
    settings = config if isinstance(config, dict) else {}
    overrides = settings.get("app_priority_overrides", {})
    overrides = overrides if isinstance(overrides, dict) else {}
    base, category, overridden = _app_base(notification.app_name, overrides)
    if not overridden:
        base = max(base, max(0, min(100, int(notification.priority))))
    text = f"{notification.title} {notification.body}".casefold()
    urgency = max((weight for phrase, weight in _URGENT if phrase in text), default=0)
    current = now or datetime.now(timezone.utc)
    timestamp = notification.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (current - timestamp.astimezone(timezone.utc)).total_seconds() / 3600)
    recency = max(0, 5 - int(age_hours / 4))
    notification.category = category
    notification.priority = max(0, min(100, base + urgency + recency))
    return notification.priority


def priority_level(score: int) -> str:
    """Map a numeric priority score to a named display level."""
    if score >= LEVELS[CRITICAL]:
        return CRITICAL
    if score >= LEVELS[HIGH]:
        return HIGH
    if score >= LEVELS[NORMAL]:
        return NORMAL
    return LOW


def sort_notifications(notifications):
    """Sort by priority descending, then newest first."""
    return sorted(notifications, key=lambda item: (item.priority, item.timestamp), reverse=True)
