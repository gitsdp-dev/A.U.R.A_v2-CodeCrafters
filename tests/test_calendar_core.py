"""Comprehensive offline tests for core.calendar (no network, fake clock)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.calendar.conflicts import events_overlap, find_conflicts, find_free_slots
from core.calendar.intents import (
    handle_cancel,
    handle_conflicts,
    handle_create,
    handle_free_slots,
    handle_modify,
    handle_view,
    match_and_run_calendar_intent,
)
from core.calendar.models import CalendarError, Event, ReadOnlyCalendarError
from core.calendar.parser import parse_datetime_phrase, parse_range_phrase
from core.calendar.providers.ics_feed import parse_ics_bytes, validate_feed_url
from core.calendar.recurrence import expand_event
from core.calendar.secrets import SecretStore
from core.calendar.service import CalendarService
from core.calendar.store import CalendarStore
from core.calendar.summarizer import summarize_conflicts, summarize_day, summarize_week
from core.undo import clear as clear_undo, undo_last


@pytest.fixture()
def cal_service(tmp_path: Path) -> CalendarService:
    store = CalendarStore(db_path=tmp_path / "calendar.db")
    secrets = SecretStore(fallback_path=tmp_path / "secrets.json", force_file_only=True)
    clear_undo()
    return CalendarService(store=store, secrets=secrets)


# ── 1. Store, CRUD, Read-only, Create -> Verify -> Undo ──────────────────────


def test_create_verify_and_undo_flow(cal_service: CalendarService, tmp_path: Path):
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 11, 25, 9, 0, tzinfo=tz)
    evt = cal_service.create("Dentist", now + timedelta(hours=3), now + timedelta(hours=4))
    assert evt.title == "Dentist"
    assert cal_service.store.get_event(evt.id) is not None

    # Test intent handler + undo stack
    res = handle_create({"title": "Team Sync", "when": "tomorrow at 5 PM"}, service=cal_service)
    assert "Scheduled Team Sync" in res
    matches = cal_service.find_matching_events("Team Sync", now=now)
    assert len(matches) == 1

    undo_msg = undo_last()
    assert "Undone" in undo_msg
    assert cal_service.find_matching_events("Team Sync", now=now) == []


def test_corrupt_db_backup_and_recreate(tmp_path: Path):
    db_path = tmp_path / "calendar.db"
    db_path.write_bytes(b"NOT A SQLITE DATABASE AT ALL")
    store = CalendarStore(db_path=db_path)
    cals = store.list_calendars()
    assert any(c.id == "local" for c in cals)
    backups = list(tmp_path.glob("calendar.corrupt.*.bak"))
    assert len(backups) == 1


def test_read_only_enforcement(cal_service: CalendarService):
    ics_bytes = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:ro-1\r\nSUMMARY:Holiday\r\n"
        b"DTSTART:20261126T100000Z\r\nDTEND:20261126T110000Z\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    resp = SimpleNamespace(
        status_code=200,
        headers={"ETag": '"v1"'},
        iter_content=lambda chunk_size=65536: [ics_bytes],
        close=lambda: None,
    )
    cal_service._http_get = lambda *a, **kw: resp
    cal, health = cal_service.connect_calendar("Google Holidays", "ics_feed", url="https://example.com/cal.ics")
    assert health.ok
    assert cal.read_only is True

    with pytest.raises(ReadOnlyCalendarError):
        cal_service.create("Hack", datetime(2026, 11, 26, 12, 0), calendar_id=cal.id)

    events = cal_service.store.list_raw_events(calendar_id=cal.id, enabled_only=False)
    assert len(events) == 1
    with pytest.raises(ReadOnlyCalendarError):
        cal_service.cancel(events[0].id)


# ── 2. Recurrence (EXDATE & DST) ─────────────────────────────────────────────


def test_recurrence_exdate_and_dst():
    ny = ZoneInfo("America/New_York")
    # 2026-03-08 is US Spring Forward (EST -05:00 -> EDT -04:00)
    start = datetime(2026, 3, 7, 9, 0, tzinfo=ny)
    end = datetime(2026, 3, 7, 10, 0, tzinfo=ny)
    exdate = datetime(2026, 3, 9, 9, 0, tzinfo=ny).isoformat()
    master = Event(
        id="rec-1",
        title="Daily Standup",
        start=start,
        end=end,
        rrule="FREQ=DAILY;COUNT=4",
        exdates=[exdate],
    )
    occ = expand_event(
        master,
        datetime(2026, 3, 7, 0, 0, tzinfo=ny),
        datetime(2026, 3, 12, 0, 0, tzinfo=ny),
        tz=ny,
    )
    # 4 days minus 1 EXDATE (March 9) = 3 occurrences: Mar 7, Mar 8, Mar 10
    assert len(occ) == 3
    assert [o.start.astimezone(ny).day for o in occ] == [7, 8, 10]
    # Wall-clock hour remains 9 AM across DST transition!
    assert all(o.start.astimezone(ny).hour == 9 for o in occ)


# ── 3. Conflicts & Free Slots ────────────────────────────────────────────────


def test_back_to_back_not_flagged_and_all_day_handling():
    tz = ZoneInfo("UTC")
    e1 = Event(title="A", start=datetime(2026, 11, 25, 9, 0, tzinfo=tz), end=datetime(2026, 11, 25, 10, 0, tzinfo=tz))
    e2 = Event(title="B", start=datetime(2026, 11, 25, 10, 0, tzinfo=tz), end=datetime(2026, 11, 25, 11, 0, tzinfo=tz))
    all_day = Event(
        title="Birthday",
        start=datetime(2026, 11, 25, 0, 0, tzinfo=tz),
        end=datetime(2026, 11, 26, 0, 0, tzinfo=tz),
        all_day=True,
    )
    assert not events_overlap(e1, e2)
    assert find_conflicts([e1, e2, all_day]) == []
    assert len(find_conflicts([e1, all_day], include_all_day=True)) == 1

    e3 = Event(title="Overlap", start=datetime(2026, 11, 25, 9, 30, tzinfo=tz), end=datetime(2026, 11, 25, 10, 30, tzinfo=tz))
    clashes = find_conflicts([e1, e2, e3])
    assert len(clashes) == 2


def test_free_slots_within_working_hours():
    tz = ZoneInfo("UTC")
    e1 = Event(title="Morning", start=datetime(2026, 11, 25, 9, 0, tzinfo=tz), end=datetime(2026, 11, 25, 12, 0, tzinfo=tz))
    e2 = Event(title="Late", start=datetime(2026, 11, 25, 15, 0, tzinfo=tz), end=datetime(2026, 11, 25, 18, 0, tzinfo=tz))
    slots = find_free_slots(
        [e1, e2],
        datetime(2026, 11, 25, 0, 0, tzinfo=tz),
        datetime(2026, 11, 26, 0, 0, tzinfo=tz),
        duration_minutes=60,
        work_start_hour=9,
        work_end_hour=18,
    )
    assert len(slots) == 1
    assert slots[0][0].hour == 12 and slots[0][1].hour == 15


# ── 4. Parser Phrases ────────────────────────────────────────────────────────


def test_parser_phrases():
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 11, 25, 8, 0, tzinfo=tz)  # Wednesday

    p1 = parse_datetime_phrase("tomorrow 4pm", now=now, tz=tz)
    assert p1.start is not None
    assert p1.start.day == 26 and p1.start.hour == 16

    p2 = parse_datetime_phrase("next Friday at 10am for 45 minutes", now=now, tz=tz)
    assert p2.start is not None
    assert p2.start.weekday() == 4
    assert p2.duration_minutes == 45

    p3 = parse_datetime_phrase("tomorrow at 5pm remind me an hour before", now=now, tz=tz)
    assert p3.reminder_minutes == 60

    r_start, r_end, label = parse_range_phrase("tomorrow afternoon", now=now, tz=tz)
    assert r_start.day == 26 and r_start.hour == 12 and r_end.hour == 18
    assert label == "tomorrow afternoon"


# ── 5. Feed Refresh ETag 304, Malformed ICS, SSRF, Disconnect ────────────────


def test_feed_etag_304_and_disconnect(cal_service: CalendarService):
    ics_bytes = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:u1\r\nSUMMARY:Sync Test\r\n"
        b"DTSTART:20261126T140000Z\r\nDTEND:20261126T150000Z\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    calls = {"n": 0}

    def fake_get(url, headers=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(
                status_code=200,
                headers={"ETag": '"abc"'},
                iter_content=lambda chunk_size=65536: [ics_bytes],
                close=lambda: None,
            )
        assert headers.get("If-None-Match") == '"abc"'
        return SimpleNamespace(status_code=304, headers={}, close=lambda: None)

    cal_service._http_get = fake_get
    cal, h1 = cal_service.connect_calendar("Work", "ics_feed", url="https://example.com/work.ics")
    assert h1.ok
    h2 = cal_service.refresh_all()[cal.id]
    assert h2.ok and "304" in h2.message

    # Disconnect deletes events and secret
    assert cal_service.disconnect_calendar(cal.id) is True
    assert cal_service.secrets.get_secret(f"ics_url:{cal.id}") is None
    assert cal_service.store.list_raw_events(calendar_id=cal.id, enabled_only=False) == []


def test_malformed_ics_and_ssrf_rejected():
    with pytest.raises(CalendarError):
        parse_ics_bytes(b"NOT VALID ICS", calendar_id="local")
    with pytest.raises(CalendarError):
        validate_feed_url("http://example.com/cal.ics")
    with pytest.raises(CalendarError):
        validate_feed_url("https://127.0.0.1/cal.ics")
    with pytest.raises(CalendarError):
        validate_feed_url("https://localhost/cal.ics")


# ── 6. Reminder Dedup After Restart ──────────────────────────────────────────


def test_reminder_dedup_across_restart(tmp_path: Path):
    db_path = tmp_path / "calendar.db"
    sec_path = tmp_path / "secrets.json"
    tz = ZoneInfo("UTC")
    now = datetime(2026, 11, 25, 14, 45, tzinfo=tz)

    fired_log: list[str] = []
    svc1 = CalendarService(
        store=CalendarStore(db_path=db_path),
        secrets=SecretStore(fallback_path=sec_path, force_file_only=True),
        fire_reminder_fn=lambda ev, mins: fired_log.append(f"{ev.title}:{mins}") or True,
    )
    svc1.create("Doctor", now + timedelta(minutes=15), now + timedelta(minutes=45), reminders=[15])
    res1 = svc1.tick_reminders(now=now)
    assert len(res1) == 1

    # Simulate app restart with new CalendarService instance on same DB
    svc2 = CalendarService(
        store=CalendarStore(db_path=db_path),
        secrets=SecretStore(fallback_path=sec_path, force_file_only=True),
        fire_reminder_fn=lambda ev, mins: fired_log.append(f"{ev.title}:{mins}") or True,
    )
    res2 = svc2.tick_reminders(now=now + timedelta(minutes=1))
    assert res2 == []
    assert len(fired_log) == 1


# ── 7. Briefing & Summarizer Wording (0 / 1 / 2 / many) ──────────────────────


def test_summarizer_and_briefing(cal_service: CalendarService):
    tz = ZoneInfo("UTC")
    now = datetime(2026, 11, 25, 8, 0, tzinfo=tz)

    # No calendars connected / empty local -> briefing returns ""
    assert cal_service.briefing_section(now=now) == ""

    assert "clear" in summarize_day([], now=now).lower()
    e1 = Event(title="Standup", start=datetime(2026, 11, 25, 10, 0, tzinfo=tz), end=datetime(2026, 11, 25, 10, 30, tzinfo=tz))
    e2 = Event(title="Lunch", start=datetime(2026, 11, 25, 12, 0, tzinfo=tz), end=datetime(2026, 11, 25, 13, 0, tzinfo=tz))
    e3 = Event(title="Design review", start=datetime(2026, 11, 25, 16, 0, tzinfo=tz), end=datetime(2026, 11, 25, 17, 0, tzinfo=tz))

    assert "one event" in summarize_day([e1], now=now).lower()
    assert "noon" in summarize_day([e1, e2], now=now).lower()
    s_many = summarize_day([e1, e2, e3], now=now)
    assert "three events" in s_many.lower()

    w_sum = summarize_week([e1, e2, e3], now=now)
    assert "three events" in w_sum.lower()

    # Now add an event and check briefing_section
    cal_service.create("Standup", e1.start, e1.end)
    assert "Standup" in cal_service.briefing_section(now=now)


# ── 8. Local Engine Intent Router ────────────────────────────────────────────


def test_local_intent_router(cal_service: CalendarService):
    r1 = match_and_run_calendar_intent("what's my schedule today?", service=cal_service)
    assert r1 is not None and "clear" in r1.lower()

    r2 = match_and_run_calendar_intent("schedule Dentist tomorrow at 5 PM", service=cal_service)
    assert r2 is not None and "Dentist" in r2
