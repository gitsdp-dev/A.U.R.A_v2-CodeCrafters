"""Data models, exceptions, and timezone/data-dir helpers for core.calendar."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CalendarError(Exception):
    """Base exception for calendar operations."""


class ReadOnlyCalendarError(CalendarError):
    """Raised when attempting to write to a read-only calendar."""


def get_local_tz() -> tzinfo:
    """Return the system's current local timezone (timezone-aware, DST-safe)."""
    try:
        import tzlocal

        tz = tzlocal.get_localzone()
        if tz is not None:
            return tz
    except Exception:
        pass
    local_now = datetime.now().astimezone()
    if local_now.tzinfo is not None:
        return local_now.tzinfo
    try:
        return ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        from datetime import timezone

        return timezone.utc


def ensure_aware(dt: datetime, tz: tzinfo | None = None) -> datetime:
    """Ensure a datetime is timezone-aware, attaching local tz only if naive."""
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=tz or get_local_tz())


def to_tz(dt: datetime, tz: tzinfo | None = None) -> datetime:
    """Convert a datetime (attaching local tz if naive) to target tz."""
    target_tz = tz or get_local_tz()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=target_tz)
    return dt.astimezone(target_tz)


def get_user_data_dir() -> Path:
    """Return a writable per-user data directory (safe for Microsoft Store builds)."""
    override = os.environ.get("AURA_CALENDAR_DATA_DIR", "").strip()
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p

    sys_name = platform.system()
    if sys_name == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        p = base / "AURA" / "calendar"
    elif sys_name == "Darwin":
        p = Path.home() / "Library" / "Application Support" / "AURA" / "calendar"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        p = base / "AURA" / "calendar"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Calendar:
    """Represents a connected or local calendar."""

    id: str = field(default_factory=lambda: f"cal_{uuid4().hex[:12]}")
    name: str = "My Calendar"
    provider: str = "local"  # "local" | "ics_feed" | "caldav" | "google_byo"
    read_only: bool = False
    enabled: bool = True
    color: str = "#ff7b00"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "read_only": self.read_only,
            "enabled": self.enabled,
            "color": self.color,
        }


@dataclass
class Event:
    """Represents a calendar event or expanded occurrence."""

    id: str = field(default_factory=lambda: f"evt_{uuid4().hex[:12]}")
    calendar_id: str = "local"
    title: str = ""
    start: datetime = field(default_factory=lambda: datetime.now(get_local_tz()))
    end: datetime = field(default_factory=lambda: datetime.now(get_local_tz()))
    all_day: bool = False
    location: str = ""
    notes: str = ""
    rrule: str = ""
    reminders: list[int] = field(default_factory=lambda: [15])
    source: str = "local"
    etag: str = ""
    uid: str = field(default_factory=lambda: f"{uuid4().hex}@aura.local")
    last_modified: datetime | None = None
    exdates: list[str] = field(default_factory=list)
    occurrence_start: datetime | None = None

    def __post_init__(self) -> None:
        tz = get_local_tz()
        if isinstance(self.start, str):
            self.start = datetime.fromisoformat(self.start)
        if isinstance(self.end, str):
            self.end = datetime.fromisoformat(self.end)
        self.start = ensure_aware(self.start, tz)
        self.end = ensure_aware(self.end, tz)
        if self.end < self.start:
            self.end = self.start
        if self.last_modified is not None:
            if isinstance(self.last_modified, str):
                self.last_modified = datetime.fromisoformat(self.last_modified)
            self.last_modified = ensure_aware(self.last_modified, tz)
        if isinstance(self.reminders, str):
            import json

            try:
                self.reminders = [int(x) for x in json.loads(self.reminders)]
            except Exception:
                self.reminders = [15]
        self.reminders = sorted({max(0, int(m)) for m in (self.reminders or [])}, reverse=True)
        if not self.uid:
            self.uid = f"{self.id}@aura.local"

    @property
    def duration_minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "calendar_id": self.calendar_id,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "all_day": self.all_day,
            "location": self.location,
            "notes": self.notes,
            "rrule": self.rrule,
            "reminders": list(self.reminders),
            "source": self.source,
            "etag": self.etag,
            "uid": self.uid,
            "last_modified": self.last_modified.isoformat() if self.last_modified else None,
            "exdates": list(self.exdates),
        }
