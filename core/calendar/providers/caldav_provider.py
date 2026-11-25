"""Optional CalDAV provider (lazy-imported; hidden if `caldav` package is not installed)."""

from __future__ import annotations

from datetime import datetime

from core.calendar.models import CalendarError, Event, ensure_aware, get_local_tz
from core.calendar.providers.base import CalendarProvider, ProviderHealth
from core.calendar.recurrence import expand_events
from core.calendar.secrets import SecretStore
from core.calendar.store import CalendarStore


def is_caldav_available() -> bool:
    try:
        import caldav  # noqa: F401

        return True
    except ImportError:
        return False


class CalDavProvider(CalendarProvider):
    """Read/write CalDAV provider using app passwords stored in SecretStore."""

    def __init__(self, store: CalendarStore, secrets: SecretStore, calendar_id: str) -> None:
        if not is_caldav_available():
            raise CalendarError("Optional 'caldav' package is not installed.")
        self._store = store
        self._secrets = secrets
        self.calendar_id = calendar_id
        self.read_only = False

    def list_events(self, range_start: datetime, range_end: datetime) -> list[Event]:
        non_rec = self._store.query_non_recurring_in_range(
            range_start, range_end, calendar_id=self.calendar_id, enabled_only=False
        )
        masters = self._store.list_recurring_masters(
            calendar_id=self.calendar_id, enabled_only=False
        )
        return sorted(non_rec + expand_events(masters, range_start, range_end), key=lambda e: e.start)

    def create_event(self, event: Event) -> Event:
        event.calendar_id = self.calendar_id
        event.source = "caldav"
        return self._store.upsert_event(event)

    def update_event(self, event: Event) -> Event:
        event.calendar_id = self.calendar_id
        return self._store.upsert_event(event)

    def delete_event(self, event_id: str) -> bool:
        return self._store.delete_event(event_id)

    def refresh(self) -> ProviderHealth:
        import json

        raw_cfg = self._secrets.get_secret(f"caldav:{self.calendar_id}")
        if not raw_cfg:
            return ProviderHealth(ok=False, offline=True, message="Missing CalDAV credentials.")
        try:
            import caldav

            cfg = json.loads(raw_cfg)
            client = caldav.DAVClient(
                url=cfg["url"],
                username=cfg["username"],
                password=cfg["password"],
                timeout=8,
            )
            principal = client.principal()
            cals = principal.calendars()
            if not cals:
                return ProviderHealth(ok=False, message="No calendars found on CalDAV server.")
            tz = get_local_tz()
            synced: list[Event] = []
            for comp in cals[0].events():
                for vev in comp.icalendar_instance.walk("VEVENT"):
                    dtstart = vev.get("dtstart")
                    if dtstart is None:
                        continue
                    s = dtstart.dt
                    if not isinstance(s, datetime):
                        s = datetime.combine(s, datetime.min.time(), tzinfo=tz)
                    dtend = vev.get("dtend")
                    e = dtend.dt if dtend is not None else s
                    if not isinstance(e, datetime):
                        e = datetime.combine(e, datetime.min.time(), tzinfo=tz)
                    uid = str(vev.get("uid") or "")
                    synced.append(
                        Event(
                            id=f"{self.calendar_id}:{uid}",
                            calendar_id=self.calendar_id,
                            title=str(vev.get("summary") or "Untitled"),
                            start=ensure_aware(s, tz),
                            end=ensure_aware(e, tz),
                            source="caldav",
                            uid=uid,
                        )
                    )
            self._store.replace_calendar_events(self.calendar_id, synced)
            return ProviderHealth(ok=True, message=f"Synced {len(synced)} events")
        except Exception as exc:
            return ProviderHealth(ok=False, offline=True, message=f"CalDAV sync failed: {type(exc).__name__}")
