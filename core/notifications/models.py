"""Shared notification data model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass
class Notification:
    """A captured system or A.U.R.A. application notification."""

    id: str = field(default_factory=lambda: uuid4().hex)
    source: str = "app"
    app_name: str = "A.U.R.A."
    title: str = ""
    body: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    priority: int = 0
    category: str = "general"
    read: bool = False
    raw: Any | None = None

    def __post_init__(self) -> None:
        if self.source not in {"system", "app"}:
            raise ValueError("source must be 'system' or 'app'")
        if isinstance(self.timestamp, str):
            self.timestamp = datetime.fromisoformat(self.timestamp)
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Notification":
        values = dict(data)
        if isinstance(values.get("timestamp"), str):
            values["timestamp"] = datetime.fromisoformat(values["timestamp"])
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in values.items() if key in allowed})
