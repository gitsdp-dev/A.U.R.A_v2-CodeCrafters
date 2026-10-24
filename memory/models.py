"""Typed models and validation for A.U.R.A. long-term memory."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class MemoryValidationError(ValueError):
    """Raised when a memory entry does not satisfy the storage contract."""


class MemoryCategory(str, Enum):
    """The four supported memory categories."""

    PROTOCOL = "protocol"
    REMINDER = "reminder"
    LIFESTYLE = "lifestyle"
    INSTRUCTION = "instruction"


class Priority(str, Enum):
    """Memory priority, ordered from low to critical."""

    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class MemorySource(str, Enum):
    """How a memory entered the store."""

    MANUAL = "manual"
    VOICE = "voice"
    AUTO = "auto"


class RemindMode(str, Enum):
    """When reminder memories should be delivered."""

    ON_WAKEUP = "on_wakeup"
    ONCE = "once"
    MANUAL_ONLY = "manual_only"


_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: str, field_name: str, maximum: int) -> str:
    """Normalize and validate a user-provided text field."""
    if not isinstance(value, str):
        raise MemoryValidationError(f"{field_name} must be text.")
    if _CONTROL.search(value):
        raise MemoryValidationError(f"{field_name} contains invalid control characters.")
    value = _WHITESPACE.sub(" ", value).strip()
    if not value:
        raise MemoryValidationError(f"{field_name} is required.")
    if len(value) > maximum:
        raise MemoryValidationError(f"{field_name} must be {maximum} characters or fewer.")
    return value


@dataclass
class MemoryEntry:
    """A validated, user-owned long-term memory entry."""

    category: MemoryCategory
    title: str
    description: str
    priority: Priority = Priority.NORMAL
    source: MemorySource = MemorySource.MANUAL
    pinned: bool = False
    enabled: bool = True
    aliases: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_used_at: str | None = None
    use_count: int = 0
    remind_mode: RemindMode = RemindMode.ON_WAKEUP
    acknowledged: bool = False
    schema_version: int = 1
    id: str = field(default_factory=lambda: uuid4().hex)

    def validate(self) -> None:
        """Validate and normalize this entry, raising a readable error."""
        try:
            if not isinstance(self.category, MemoryCategory):
                self.category = MemoryCategory(str(self.category).lower())
            if not isinstance(self.priority, Priority):
                self.priority = Priority(str(self.priority).lower())
            if not isinstance(self.source, MemorySource):
                self.source = MemorySource(str(self.source).lower())
            if not isinstance(self.remind_mode, RemindMode):
                self.remind_mode = RemindMode(str(self.remind_mode).lower())
        except ValueError as error:
            raise MemoryValidationError("Memory category, priority, source, or reminder mode is invalid.") from error
        self.title = normalize_text(self.title, "Title", 80)
        self.description = normalize_text(self.description, "Description", 2000)
        if not isinstance(self.id, str) or not re.fullmatch(r"[0-9a-f]{32}", self.id):
            raise MemoryValidationError("Memory id must be a uuid4 hex value.")
        if not isinstance(self.aliases, list) or len(self.aliases) > 8:
            raise MemoryValidationError("A memory can have at most 8 aliases.")
        self.aliases = [normalize_text(alias, "Alias", 80) for alias in self.aliases]
        if len(set(alias.casefold() for alias in self.aliases)) != len(self.aliases):
            raise MemoryValidationError("Aliases must be unique.")
        if self.category is not MemoryCategory.REMINDER:
            self.remind_mode = RemindMode.MANUAL_ONLY
            self.acknowledged = False
        if not isinstance(self.use_count, int) or self.use_count < 0:
            raise MemoryValidationError("Use count must be a non-negative integer.")
        if self.schema_version != 1:
            raise MemoryValidationError("Unsupported memory schema version.")

    def to_dict(self) -> dict[str, Any]:
        """Serialize this entry to the stable JSON representation."""
        self.validate()
        return {
            "id": self.id,
            "category": self.category.value,
            "title": self.title,
            "description": self.description,
            "priority": self.priority.value,
            "source": self.source.value,
            "pinned": self.pinned,
            "enabled": self.enabled,
            "aliases": list(self.aliases),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
            "remind_mode": self.remind_mode.value,
            "acknowledged": self.acknowledged,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEntry":
        """Build and validate an entry from JSON data."""
        entry = cls(
            id=str(data.get("id") or uuid4().hex),
            category=MemoryCategory(str(data.get("category", "lifestyle")).lower()),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            priority=Priority(str(data.get("priority", "normal")).lower()),
            source=MemorySource(str(data.get("source", "manual")).lower()),
            pinned=bool(data.get("pinned", False)),
            enabled=bool(data.get("enabled", True)),
            aliases=list(data.get("aliases") or []),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
            last_used_at=data.get("last_used_at"),
            use_count=int(data.get("use_count", 0)),
            remind_mode=RemindMode(str(data.get("remind_mode", "on_wakeup")).lower()),
            acknowledged=bool(data.get("acknowledged", False)),
            schema_version=int(data.get("schema_version", 1)),
        )
        entry.validate()
        return entry
