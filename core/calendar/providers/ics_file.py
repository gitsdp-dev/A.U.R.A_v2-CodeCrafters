"""One-time .ics file import into LocalProvider and export from LocalProvider."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import icalendar

from core.calendar.models import CalendarError
from core.calendar.providers.ics_feed import MAX_ICS_BYTES, parse_ics_bytes
from core.calendar.store import CalendarStore


def import_ics_file(path: str | Path, store: CalendarStore, calendar_id: str = "local") -> int:
    """Import events from a local .ics file into the specified writable calendar."""
    p = Path(path)
    if not p.is_file():
        raise CalendarError(f"ICS file not found: {p}")
    size = p.stat().st_size
    if size > MAX_ICS_BYTES:
        raise CalendarError("ICS file exceeds the 5 MB limit.")
    raw = p.read_bytes()
    events = parse_ics_bytes(raw, calendar_id=calendar_id, source="ics_import")
    for ev in events:
        # Ensure unique local IDs so imports don't collide across files
        ev.id = f"imp_{calendar_id}_{abs(hash((ev.uid, ev.start.isoformat())))}"
        store.upsert_event(ev)
    return len(events)


def export_ics_file(path: str | Path, store: CalendarStore, calendar_id: str = "local") -> int:
    """Export all events from a calendar to a standard .ics file."""
    events = store.list_raw_events(calendar_id=calendar_id, enabled_only=False)
    cal = icalendar.Calendar()
    cal.add("prodid", "-//AURA//Calendar//EN")
    cal.add("version", "2.0")

    for ev in events:
        vevent = icalendar.Event()
        vevent.add("uid", ev.uid or ev.id)
        vevent.add("summary", ev.title)
        if ev.all_day:
            vevent.add("dtstart", ev.start.date())
            end_date = ev.end.date()
            if end_date <= ev.start.date():
                end_date = ev.start.date() + timedelta(days=1)
            vevent.add("dtend", end_date)
        else:
            vevent.add("dtstart", ev.start)
            vevent.add("dtend", ev.end)
        if ev.location:
            vevent.add("location", ev.location)
        if ev.notes:
            vevent.add("description", ev.notes)
        if ev.rrule:
            rrule_str = ev.rrule.strip()
            if rrule_str.upper().startswith("RRULE:"):
                rrule_str = rrule_str[6:].strip()
            try:
                vevent.add("rrule", icalendar.vRecur.from_ical(rrule_str))
            except Exception:
                pass
        cal.add_component(vevent)

    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(cal.to_ical())
    return len(events)
