"""CalendarService — thread-safe facade for all AURA calendar operations."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.calendar.conflicts import Conflict, find_conflicts, find_conflicts_for_candidate, find_free_slots
from core.calendar.models import Calendar, CalendarError, Event, ReadOnlyCalendarError, ensure_aware, get_local_tz
from core.calendar.providers.base import CalendarProvider, ProviderHealth
from core.calendar.providers.ics_feed import IcsFeedProvider, validate_feed_url
from core.calendar.providers.ics_file import export_ics_file, import_ics_file
from core.calendar.providers.local import LocalProvider
from core.calendar.reminders import ReminderDispatcher
from core.calendar.secrets import SecretStore
from core.calendar.store import CalendarStore
from core.calendar.summarizer import maybe_polish_summary, summarize_day, summarize_week

LOGGER = logging.getLogger(__name__)


class CalendarService:
    """Coordinates providers, persistence, recurrence, conflicts, reminders, and summaries."""

    def __init__(
        self,
        store: CalendarStore | None = None,
        secrets: SecretStore | None = None,
        http_get=None,
        fire_reminder_fn=None,
    ) -> None:
        self.store = store or CalendarStore()
        self.secrets = secrets or SecretStore()
        self._http_get = http_get
        self._lock = threading.RLock()
        self._providers: dict[str, CalendarProvider] = {}
        self._reminders = ReminderDispatcher(self.store, fire_fn=fire_reminder_fn)
        self._reload_providers()

    def _reload_providers(self) -> None:
        with self._lock:
            self._providers.clear()
            for cal in self.store.list_calendars(enabled_only=False):
                self._providers[cal.id] = self._build_provider(cal)

    def _build_provider(self, cal: Calendar) -> CalendarProvider:
        if cal.provider == "local":
            return LocalProvider(self.store, calendar_id=cal.id)
        if cal.provider == "ics_feed":
            return IcsFeedProvider(
                self.store, self.secrets, calendar_id=cal.id, http_get=self._http_get
            )
        if cal.provider == "caldav":
            try:
                from core.calendar.providers.caldav_provider import CalDavProvider

                return CalDavProvider(self.store, self.secrets, calendar_id=cal.id)
            except Exception:
                return LocalProvider(self.store, calendar_id=cal.id)
        if cal.provider == "google_byo":
            try:
                from core.calendar.providers.google_byo import GoogleByoProvider

                return GoogleByoProvider(self.store, self.secrets, calendar_id=cal.id)
            except Exception:
                return LocalProvider(self.store, calendar_id=cal.id)
        return LocalProvider(self.store, calendar_id=cal.id)

    def _get_provider(self, calendar_id: str) -> CalendarProvider:
        with self._lock:
            if calendar_id not in self._providers:
                cal = self.store.get_calendar(calendar_id)
                if cal is None:
                    raise CalendarError(f"Calendar '{calendar_id}' not found.")
                self._providers[calendar_id] = self._build_provider(cal)
            return self._providers[calendar_id]

    # ── Querying ─────────────────────────────────────────────────────────────

    def get_range(
        self,
        range_start: datetime,
        range_end: datetime,
        *,
        calendar_id: str | None = None,
    ) -> list[Event]:
        tz = get_local_tz()
        r_start = ensure_aware(range_start, tz)
        r_end = ensure_aware(range_end, tz)
        events: list[Event] = []
        with self._lock:
            cals = self.store.list_calendars(enabled_only=True)
            for cal in cals:
                if calendar_id and cal.id != calendar_id:
                    continue
                prov = self._get_provider(cal.id)
                events.extend(prov.list_events(r_start, r_end))
        events.sort(key=lambda e: (e.start, e.title.lower()))
        return events

    def get_today(self, *, now: datetime | None = None) -> list[Event]:
        tz = get_local_tz()
        ref = ensure_aware(now or datetime.now(tz), tz)
        start = datetime.combine(ref.date(), time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        return self.get_range(start, end)

    def next_event(self, *, now: datetime | None = None, horizon_days: int = 14) -> Event | None:
        tz = get_local_tz()
        ref = ensure_aware(now or datetime.now(tz), tz)
        events = self.get_range(ref, ref + timedelta(days=horizon_days))
        for ev in events:
            if ev.end > ref:
                return ev
        return None

    def find_matching_events(
        self,
        query: str,
        *,
        now: datetime | None = None,
        horizon_days: int = 30,
    ) -> list[Event]:
        tz = get_local_tz()
        ref = ensure_aware(now or datetime.now(tz), tz)
        q = (query or "").strip().lower()
        events = self.get_range(ref - timedelta(days=1), ref + timedelta(days=horizon_days))
        if not q:
            return events
        exact = [e for e in events if e.title.lower() == q]
        if exact:
            return exact
        return [e for e in events if q in e.title.lower()]

    # ── Mutations (with post-write verification) ─────────────────────────────

    def create(
        self,
        title: str,
        start: datetime,
        end: datetime | None = None,
        *,
        calendar_id: str = "local",
        all_day: bool = False,
        location: str = "",
        notes: str = "",
        rrule: str = "",
        reminders: list[int] | None = None,
    ) -> Event:
        if not title or not title.strip():
            raise CalendarError("Event title cannot be empty.")
        tz = get_local_tz()
        s_dt = ensure_aware(start, tz)
        e_dt = ensure_aware(end, tz) if end is not None else (s_dt + timedelta(hours=1))
        if e_dt < s_dt:
            raise CalendarError("Event end time cannot be before start time.")

        cal = self.store.get_calendar(calendar_id)
        if cal is None:
            raise CalendarError(f"Calendar '{calendar_id}' does not exist.")
        if cal.read_only:
            raise ReadOnlyCalendarError(f"Calendar '{cal.name}' is read-only.")

        default_rem = int(self.store.get_setting("default_reminder_minutes", 15))
        evt = Event(
            id=f"evt_{uuid4().hex[:12]}",
            calendar_id=calendar_id,
            title=title.strip(),
            start=s_dt,
            end=e_dt,
            all_day=all_day,
            location=location.strip(),
            notes=notes.strip(),
            rrule=rrule.strip(),
            reminders=list(reminders) if reminders is not None else [default_rem],
            source=cal.provider,
        )
        prov = self._get_provider(calendar_id)
        created = prov.create_event(evt)

        # Hard Rule 5: Re-read from store to verify persistence before confirming
        verified = self.store.get_event(created.id)
        if verified is None or verified.title != created.title:
            raise CalendarError("Event creation could not be verified in the store.")
        return verified

    def modify(
        self,
        event_id: str,
        *,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        location: str | None = None,
        notes: str | None = None,
        reminders: list[int] | None = None,
    ) -> Event:
        existing = self.store.get_event(event_id)
        if existing is None:
            raise CalendarError("Event not found.")
        cal = self.store.get_calendar(existing.calendar_id)
        if cal and cal.read_only:
            raise ReadOnlyCalendarError(f"Calendar '{cal.name}' is read-only.")

        tz = get_local_tz()
        new_start = ensure_aware(start, tz) if start is not None else existing.start
        new_end = ensure_aware(end, tz) if end is not None else existing.end
        if end is None and start is not None:
            new_end = new_start + (existing.end - existing.start)
        if new_end < new_start:
            raise CalendarError("Event end time cannot be before start time.")

        updated = Event(
            id=existing.id,
            calendar_id=existing.calendar_id,
            title=title.strip() if title is not None else existing.title,
            start=new_start,
            end=new_end,
            all_day=existing.all_day,
            location=location.strip() if location is not None else existing.location,
            notes=notes.strip() if notes is not None else existing.notes,
            rrule=existing.rrule,
            exdates=list(existing.exdates),
            reminders=list(reminders) if reminders is not None else list(existing.reminders),
            source=existing.source,
            etag=existing.etag,
            uid=existing.uid,
        )
        prov = self._get_provider(existing.calendar_id)
        prov.update_event(updated)
        self.store.clear_reminders_for_event(existing.id)

        # Verify persistence
        verified = self.store.get_event(existing.id)
        if verified is None or verified.title != updated.title or verified.start != updated.start:
            raise CalendarError("Event modification could not be verified in the store.")
        return verified

    def cancel(self, event_id: str) -> Event:
        existing = self.store.get_event(event_id)
        if existing is None:
            raise CalendarError("Event not found.")
        cal = self.store.get_calendar(existing.calendar_id)
        if cal and cal.read_only:
            raise ReadOnlyCalendarError(f"Calendar '{cal.name}' is read-only.")

        prov = self._get_provider(existing.calendar_id)
        deleted = prov.delete_event(event_id)
        if not deleted or self.store.get_event(event_id) is not None:
            raise CalendarError("Event deletion could not be verified in the store.")
        return existing

    # ── Conflicts & Free Slots ───────────────────────────────────────────────

    def find_conflicts(
        self,
        range_start: datetime,
        range_end: datetime,
        *,
        include_all_day: bool = False,
    ) -> list[Conflict]:
        events = self.get_range(range_start, range_end)
        return find_conflicts(events, include_all_day=include_all_day)

    def check_candidate_conflicts(
        self,
        start: datetime,
        end: datetime,
        *,
        ignore_event_id: str | None = None,
    ) -> list[Event]:
        tz = get_local_tz()
        s_dt = ensure_aware(start, tz)
        e_dt = ensure_aware(end, tz)
        day_start = datetime.combine(s_dt.date(), time.min, tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        existing = self.get_range(day_start, day_end)
        candidate = Event(title="__candidate__", start=s_dt, end=e_dt)
        return find_conflicts_for_candidate(candidate, existing, ignore_event_id=ignore_event_id)

    def free_slots(
        self,
        window_start: datetime,
        window_end: datetime,
        duration_minutes: int = 30,
    ) -> list[tuple[datetime, datetime]]:
        settings = self.store.get_settings()
        events = self.get_range(window_start, window_end)
        return find_free_slots(
            events,
            window_start,
            window_end,
            duration_minutes=duration_minutes,
            work_start_hour=int(settings.get("work_start_hour", 9)),
            work_end_hour=int(settings.get("work_end_hour", 18)),
        )

    # ── Connections & Sync ───────────────────────────────────────────────────

    def connect_calendar(
        self,
        name: str,
        provider: str,
        *,
        url: str | None = None,
        color: str = "#ff7b00",
        sync_now: bool = True,
    ) -> tuple[Calendar, ProviderHealth]:
        clean_name = (name or "").strip() or "Subscribed Calendar"
        cal_id = f"{provider}_{uuid4().hex[:10]}"
        if provider == "ics_feed":
            validated_url = validate_feed_url(url or "")
            cal = Calendar(
                id=cal_id,
                name=clean_name,
                provider="ics_feed",
                read_only=True,
                enabled=True,
                color=color,
            )
            self.store.upsert_calendar(cal)
            self.secrets.set_secret(f"ics_url:{cal_id}", validated_url)
            prov = self._build_provider(cal)
            with self._lock:
                self._providers[cal_id] = prov
            health = prov.refresh() if sync_now else ProviderHealth(ok=True, message="Connected")
            return cal, health
        raise CalendarError(f"Unsupported provider type: {provider}")

    def disconnect_calendar(self, calendar_id: str) -> bool:
        if calendar_id == "local":
            raise CalendarError("Cannot disconnect the built-in local calendar.")
        self.secrets.delete_secret(f"ics_url:{calendar_id}")
        self.secrets.delete_secret(f"caldav:{calendar_id}")
        self.secrets.delete_secret(f"google_byo:{calendar_id}")
        with self._lock:
            self._providers.pop(calendar_id, None)
        return self.store.delete_calendar(calendar_id)

    def refresh_all(self) -> dict[str, ProviderHealth]:
        results: dict[str, ProviderHealth] = {}
        for cal in self.store.list_calendars(enabled_only=True):
            if cal.provider == "local":
                continue
            prov = self._get_provider(cal.id)
            results[cal.id] = prov.refresh()
        return results

    def import_ics(self, path: str | Path, calendar_id: str = "local") -> int:
        cal = self.store.get_calendar(calendar_id)
        if cal is None or cal.read_only:
            raise ReadOnlyCalendarError("Target calendar is read-only or missing.")
        return import_ics_file(path, self.store, calendar_id=calendar_id)

    def export_ics(self, path: str | Path, calendar_id: str = "local") -> int:
        return export_ics_file(path, self.store, calendar_id=calendar_id)

    # ── Briefing & Reminders ─────────────────────────────────────────────────

    def has_connected_or_populated_calendar(self) -> bool:
        cals = self.store.list_calendars(enabled_only=True)
        for c in cals:
            if c.provider != "local":
                return True
        return bool(self.store.list_raw_events(calendar_id="local", enabled_only=True))

    def briefing_section(self, *, now: datetime | None = None) -> str:
        """Return a concise 'Schedule' section for Morning Briefing, or '' if nothing connected."""
        if not self.has_connected_or_populated_calendar():
            return ""
        tz = get_local_tz()
        ref = ensure_aware(now or datetime.now(tz), tz)
        today_events = self.get_today(now=ref)
        next_ev = self.next_event(now=ref + timedelta(days=1)) if not today_events else None
        summary = summarize_day(today_events, day_label="today", now=ref, next_day_first=next_ev)
        ai_on = bool(self.store.get_setting("ai_polish_enabled", False))
        return maybe_polish_summary(summary, ai_on)

    def tick_reminders(self, *, now: datetime | None = None) -> list[tuple[Event, int]]:
        tz = get_local_tz()
        ref = ensure_aware(now or datetime.now(tz), tz)
        events = self.get_range(ref - timedelta(minutes=5), ref + timedelta(hours=24))
        return self._reminders.check_and_fire(events, now=ref, tz=tz)


_SERVICE_LOCK = threading.Lock()
_DEFAULT_SERVICE: CalendarService | None = None


def get_calendar_service() -> CalendarService:
    """Return the process-wide CalendarService singleton."""
    global _DEFAULT_SERVICE
    with _SERVICE_LOCK:
        if _DEFAULT_SERVICE is None:
            _DEFAULT_SERVICE = CalendarService()
        return _DEFAULT_SERVICE
