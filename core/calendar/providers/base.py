"""Abstract base class for calendar providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from core.calendar.models import Event


@dataclass
class ProviderHealth:
    ok: bool
    message: str = "OK"
    offline: bool = False
    last_refresh: datetime | None = None


class CalendarProvider(ABC):
    """Common interface for local and external calendar providers."""

    calendar_id: str
    read_only: bool

    @abstractmethod
    def list_events(self, range_start: datetime, range_end: datetime) -> list[Event]:
        """Return events (with recurring events expanded) overlapping [range_start, range_end)."""

    @abstractmethod
    def create_event(self, event: Event) -> Event:
        """Create a new event. Raise ReadOnlyCalendarError if read_only."""

    @abstractmethod
    def update_event(self, event: Event) -> Event:
        """Update an existing event. Raise ReadOnlyCalendarError if read_only."""

    @abstractmethod
    def delete_event(self, event_id: str) -> bool:
        """Delete an event by ID. Raise ReadOnlyCalendarError if read_only."""

    def refresh(self) -> ProviderHealth:
        """Sync from remote source if applicable."""
        return ProviderHealth(ok=True, message="Local")

    def health(self) -> ProviderHealth:
        """Return current provider health/status."""
        return ProviderHealth(ok=True, message="OK")
