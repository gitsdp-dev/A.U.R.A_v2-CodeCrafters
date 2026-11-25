"""Overlap / conflict detection and free-slot search across all enabled calendars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from core.calendar.models import Event, ensure_aware, get_local_tz


@dataclass(frozen=True)
class Conflict:
    event_a: Event
    event_b: Event
    overlap_start: datetime
    overlap_end: datetime


def events_overlap(a: Event, b: Event, *, include_all_day: bool = False) -> bool:
    """Return True if two events genuinely overlap in time.

    - Back-to-back events (a.end == b.start or b.end == a.start) do NOT overlap.
    - All-day events are excluded unless include_all_day=True.
    """
    if not include_all_day and (a.all_day or b.all_day):
        return False
    return a.start < b.end and b.start < a.end


def find_conflicts(
    events: list[Event],
    *,
    include_all_day: bool = False,
) -> list[Conflict]:
    """Find all overlapping pairs among a list of events."""
    ordered = sorted(events, key=lambda e: (e.start, e.end))
    conflicts: list[Conflict] = []
    for i, a in enumerate(ordered):
        if not include_all_day and a.all_day:
            continue
        for b in ordered[i + 1 :]:
            if not include_all_day and b.all_day:
                continue
            if b.start >= a.end:
                break
            if events_overlap(a, b, include_all_day=include_all_day):
                conflicts.append(
                    Conflict(
                        event_a=a,
                        event_b=b,
                        overlap_start=max(a.start, b.start),
                        overlap_end=min(a.end, b.end),
                    )
                )
    return conflicts


def find_conflicts_for_candidate(
    candidate: Event,
    existing_events: list[Event],
    *,
    ignore_event_id: str | None = None,
    include_all_day: bool = False,
) -> list[Event]:
    """Return existing events that clash with a candidate event."""
    clashes: list[Event] = []
    for ev in existing_events:
        if ignore_event_id and ev.id == ignore_event_id:
            continue
        if events_overlap(candidate, ev, include_all_day=include_all_day):
            clashes.append(ev)
    return clashes


def find_free_slots(
    events: list[Event],
    window_start: datetime,
    window_end: datetime,
    duration_minutes: int = 30,
    *,
    work_start_hour: int = 9,
    work_end_hour: int = 18,
    include_all_day: bool = False,
) -> list[tuple[datetime, datetime]]:
    """Find available [start, end) slots of at least `duration_minutes` within working hours."""
    tz = window_start.tzinfo or get_local_tz()
    w_start = ensure_aware(window_start, tz)
    w_end = ensure_aware(window_end, tz)
    needed = timedelta(minutes=max(1, int(duration_minutes)))
    if w_end <= w_start:
        return []

    busy_intervals = [
        (ev.start, ev.end)
        for ev in events
        if (include_all_day or not ev.all_day) and ev.end > w_start and ev.start < w_end
    ]
    busy_intervals.sort(key=lambda pair: pair[0])

    # Merge overlapping busy intervals
    merged: list[tuple[datetime, datetime]] = []
    for s, e in busy_intervals:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))

    slots: list[tuple[datetime, datetime]] = []
    cur_day = w_start.date()
    end_day = w_end.date()
    while cur_day <= end_day:
        day_work_start = max(
            w_start,
            datetime.combine(cur_day, time(hour=max(0, min(23, work_start_hour))), tzinfo=tz),
        )
        day_work_end = min(
            w_end,
            datetime.combine(cur_day, time(hour=max(1, min(23, work_end_hour))), tzinfo=tz),
        )
        if day_work_end - day_work_start >= needed:
            cursor = day_work_start
            for b_start, b_end in merged:
                if b_end <= cursor:
                    continue
                if b_start >= day_work_end:
                    break
                if b_start - cursor >= needed:
                    slots.append((cursor, min(b_start, day_work_end)))
                cursor = max(cursor, b_end)
            if day_work_end - cursor >= needed:
                slots.append((cursor, day_work_end))
        cur_day += timedelta(days=1)
    return slots
