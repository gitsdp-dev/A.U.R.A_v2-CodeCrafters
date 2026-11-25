"""LocalProvider — AURA's own built-in read/write calendar backed by SQLite."""

from __future__ import annotations

from datetime import datetime

from core.calendar.models import Event
from core.calendar.providers.base import CalendarProvider, ProviderHealth
from core.calendar.recurrence import expand_events
from core.calendar.store import CalendarStore


class LocalProvider(CalendarProvider):
    """Full read/write local calendar stored in SQLite."""

    def __init__(self, store: CalendarStore, calendar_id: str = "local") -> None:
        self._store = store
        self.calendar_id = calendar_id
        self.read_only = False

    def list_events(self, range_start: datetime, range_end: datetime) -> list[Event]:
        non_rec = self._store.query_non_recurring_in_range(
            range_start, range_end, calendar_id=self.calendar_id, enabled_only=False
        )
        masters = self._store.list_recurring_masters(
            calendar_id=self.calendar_id, enabled_only=False
        )
        expanded = expand_events(masters, range_start, range_end)
        combined = non_rec + expanded
        combined.sort(key=lambda e: (e.start, e.title.lower()))
        return combined

    def create_event(self, event: Event) -> Event:
        event.calendar_id = self.calendar_id
        event.source = "local"
        return self._store.upsert_event(event)

    def update_event(self, event: Event) -> Event:
        event.calendar_id = self.calendar_id
        return self._store.upsert_event(event)

    def delete_event(self, event_id: str) -> bool:
        self._store.clear_reminders_for_event(event_id)
        return self._store.delete_event(event_id)

    def health(self) -> ProviderHealth:
        return ProviderHealth(ok=True, message="Local calendar ready")
