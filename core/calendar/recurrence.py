"""Recurrence expansion (RRULE, EXDATE, DST-safe) using icalendar + recurring-ical-events."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, tzinfo

import icalendar
import recurring_ical_events

from core.calendar.models import Event, ensure_aware, get_local_tz

LOGGER = logging.getLogger(__name__)


def _to_aware_dt(val: date | datetime, tz: tzinfo, is_all_day: bool = False) -> datetime:
    if isinstance(val, datetime):
        return ensure_aware(val, tz)
    return datetime.combine(val, time.min, tzinfo=tz)


def expand_event(
    event: Event,
    range_start: datetime,
    range_end: datetime,
    tz: tzinfo | None = None,
) -> list[Event]:
    """Expand a single Event (with or without RRULE) into occurrences within [range_start, range_end)."""
    target_tz = tz or get_local_tz()
    r_start = ensure_aware(range_start, target_tz)
    r_end = ensure_aware(range_end, target_tz)

    if not event.rrule:
        if event.end > r_start and event.start < r_end:
            return [event]
        return []

    cal = icalendar.Calendar()
    cal.add("prodid", "-//AURA//Calendar//EN")
    cal.add("version", "2.0")

    vevent = icalendar.Event()
    vevent.add("uid", event.uid or event.id)
    vevent.add("summary", event.title)
    if event.all_day:
        vevent.add("dtstart", event.start.date())
        end_date = event.end.date()
        if end_date <= event.start.date():
            end_date = event.start.date() + timedelta(days=1)
        vevent.add("dtend", end_date)
    else:
        vevent.add("dtstart", event.start)
        vevent.add("dtend", event.end)

    # Parse RRULE string (strip optional "RRULE:" prefix)
    rrule_str = event.rrule.strip()
    if rrule_str.upper().startswith("RRULE:"):
        rrule_str = rrule_str[6:].strip()
    try:
        rrule_dict = icalendar.vRecur.from_ical(rrule_str)
        vevent.add("rrule", rrule_dict)
    except Exception as exc:
        LOGGER.warning("Invalid RRULE on event %s: %s", event.id, exc)
        if event.end > r_start and event.start < r_end:
            return [event]
        return []

    if event.exdates:
        for ex_str in event.exdates:
            try:
                ex_dt = datetime.fromisoformat(ex_str)
                if event.all_day:
                    vevent.add("exdate", ex_dt.date())
                else:
                    vevent.add("exdate", ensure_aware(ex_dt, event.start.tzinfo or target_tz))
            except ValueError:
                pass

    cal.add_component(vevent)

    try:
        occurrences = recurring_ical_events.of(cal).between(r_start, r_end)
    except Exception as exc:
        LOGGER.warning("Recurrence expansion failed for event %s: %s", event.id, exc)
        return []

    out: list[Event] = []
    for occ in occurrences:
        raw_start = occ.decoded("dtstart")
        raw_end = occ.decoded("dtend", None)
        occ_start = _to_aware_dt(raw_start, target_tz, event.all_day)
        if raw_end is not None:
            occ_end = _to_aware_dt(raw_end, target_tz, event.all_day)
        else:
            occ_end = occ_start + (event.end - event.start)
        if occ_end <= r_start or occ_start >= r_end:
            continue
        out.append(
            Event(
                id=event.id,
                calendar_id=event.calendar_id,
                title=event.title,
                start=occ_start,
                end=occ_end,
                all_day=event.all_day,
                location=event.location,
                notes=event.notes,
                rrule=event.rrule,
                reminders=list(event.reminders),
                source=event.source,
                etag=event.etag,
                uid=event.uid,
                last_modified=event.last_modified,
                exdates=list(event.exdates),
                occurrence_start=occ_start,
            )
        )
    return out


def expand_events(
    events: list[Event],
    range_start: datetime,
    range_end: datetime,
    tz: tzinfo | None = None,
) -> list[Event]:
    """Expand a list of events into sorted occurrences within [range_start, range_end)."""
    expanded: list[Event] = []
    for ev in events:
        expanded.extend(expand_event(ev, range_start, range_end, tz=tz))
    expanded.sort(key=lambda e: (e.start, e.title.lower()))
    return expanded
