"""Deterministic calendar action handlers and local-engine regex intent router."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import Any

from core.calendar.models import CalendarError, Event, ReadOnlyCalendarError, ensure_aware, get_local_tz
from core.calendar.parser import parse_datetime_phrase, parse_range_phrase
from core.calendar.service import CalendarService, get_calendar_service
from core.calendar.summarizer import (
    format_agenda_lines,
    format_day_label,
    format_time_natural,
    maybe_polish_summary,
    summarize_conflicts,
    summarize_day,
    summarize_week,
)

# Single-turn slot-filling state for missing title/time
_SLOT_LOCK = threading.Lock()
_PENDING_SLOT: dict[str, Any] | None = None


def _get_service(service: CalendarService | None = None) -> CalendarService:
    return service or get_calendar_service()


def _push_undo(label: str, fn) -> None:
    try:
        from core.undo import push_undo

        push_undo(label, fn)
    except Exception:
        pass


def _request_confirm(key: str, title: str, detail: str, fn) -> str:
    try:
        from core.confirm import request as confirm_request

        return confirm_request(key, title, detail, fn)
    except Exception:
        # Headless fallback if confirm module is unavailable
        return fn()


def handle_view(
    parameters: dict,
    *,
    player=None,
    service: CalendarService | None = None,
) -> str:
    svc = _get_service(service)
    tz = get_local_tz()
    now = datetime.now(tz)
    query = str(parameters.get("range") or parameters.get("when") or "today").strip()
    show_panel = bool(parameters.get("show_panel", False))

    if query.lower() in ("next", "next event", "upcoming"):
        nxt = svc.next_event(now=now)
        if nxt is None:
            return "You have no upcoming events scheduled."
        day_lbl = format_day_label(nxt.start, now)
        t_lbl = "all day" if nxt.all_day else format_time_natural(nxt.start, tz)
        loc = f" at {nxt.location}" if nxt.location else ""
        return f"Your next event is {nxt.title} {day_lbl} at {t_lbl}{loc}."

    r_start, r_end, label = parse_range_phrase(query, now=now, tz=tz)
    events = svc.get_range(r_start, r_end)

    if show_panel and player is not None:
        if hasattr(player, "show_agenda_panel"):
            try:
                player.show_agenda_panel(label.title(), format_agenda_lines(events, tz=tz))
            except Exception:
                pass
        elif hasattr(player, "show_content"):
            try:
                player.show_content(f"SCHEDULE — {label.upper()}", format_agenda_lines(events, tz=tz))
            except Exception:
                pass

    is_multi_day = (r_end - r_start) > timedelta(days=1, hours=1)
    if is_multi_day:
        summary = summarize_week(events, now=now, label=label)
    else:
        next_ev = svc.next_event(now=r_end) if not events and label == "today" else None
        summary = summarize_day(events, day_label=label, now=now, next_day_first=next_ev)

    ai_on = bool(svc.store.get_setting("ai_polish_enabled", False))
    return maybe_polish_summary(summary, ai_on)


def handle_free_slots(
    parameters: dict,
    *,
    service: CalendarService | None = None,
) -> str:
    svc = _get_service(service)
    tz = get_local_tz()
    now = datetime.now(tz)
    when = str(parameters.get("when") or "today").strip()
    duration = int(parameters.get("duration_minutes") or 30)
    r_start, r_end, label = parse_range_phrase(when, now=now, tz=tz)
    slots = svc.free_slots(r_start, r_end, duration_minutes=duration)
    if not slots:
        return f"You have no free {duration}-minute slots {label}."
    formatted = [
        f"{format_time_natural(s, tz)} to {format_time_natural(e, tz)}"
        for s, e in slots[:4]
    ]
    return f"You are free {label} from " + ", ".join(formatted) + "."


def handle_conflicts(
    parameters: dict,
    *,
    service: CalendarService | None = None,
) -> str:
    svc = _get_service(service)
    tz = get_local_tz()
    now = datetime.now(tz)
    when = str(parameters.get("when") or "this week").strip()
    r_start, r_end, _ = parse_range_phrase(when, now=now, tz=tz)
    conflicts = svc.find_conflicts(r_start, r_end)
    return summarize_conflicts(conflicts)


def handle_create(
    parameters: dict,
    *,
    player=None,
    service: CalendarService | None = None,
) -> str:
    global _PENDING_SLOT
    svc = _get_service(service)
    tz = get_local_tz()
    now = datetime.now(tz)

    title = str(parameters.get("title") or "").strip()
    when = str(parameters.get("when") or "").strip()
    location = str(parameters.get("location") or "").strip()
    force = bool(parameters.get("force", False))
    duration = int(parameters.get("duration_minutes") or 0)
    reminder_override = parameters.get("reminder_minutes")

    if not title:
        with _SLOT_LOCK:
            _PENDING_SLOT = {"action": "create", "when": when, "location": location}
        return "What would you like to call this event?"

    if not when:
        with _SLOT_LOCK:
            _PENDING_SLOT = {"action": "create", "title": title, "location": location}
        return f"When should I schedule {title}?"

    parsed = parse_datetime_phrase(
        when,
        now=now,
        tz=tz,
        default_duration_minutes=duration or 60,
    )
    if parsed.start is None:
        with _SLOT_LOCK:
            _PENDING_SLOT = {"action": "create", "title": title, "location": location}
        return f"What date and time should I schedule {title}?"

    end_dt = parsed.end or (parsed.start + timedelta(minutes=duration or 60))
    if duration > 0:
        end_dt = parsed.start + timedelta(minutes=duration)

    reminders = None
    if reminder_override is not None:
        reminders = [int(reminder_override)]
    elif parsed.reminder_minutes is not None:
        reminders = [parsed.reminder_minutes]

    if not force:
        clashes = svc.check_candidate_conflicts(parsed.start, end_dt)
        if clashes:
            clash = clashes[0]
            clash_t = format_time_natural(clash.start, tz)
            day_start = datetime.combine(parsed.start.date(), datetime.min.time(), tzinfo=tz)
            slots = svc.free_slots(
                max(now, day_start),
                day_start + timedelta(days=1),
                duration_minutes=int((end_dt - parsed.start).total_seconds() // 60) or 30,
            )
            slot_hint = ""
            if slots:
                slot_hint = f" Your nearest free slot is {format_time_natural(slots[0][0], tz)}."
            return (
                f"Heads up: {title} clashes with {clash.title} at {clash_t}.{slot_hint} "
                f"Should I schedule it anyway or move it to the free slot?"
            )

    try:
        created = svc.create(
            title=title,
            start=parsed.start,
            end=end_dt,
            location=location,
            reminders=reminders,
        )
    except CalendarError as exc:
        return f"Could not schedule {title}: {exc}"

    _push_undo(
        f"schedule '{created.title}'",
        lambda eid=created.id: (svc.cancel(eid), f"Removed '{created.title}'.")[1],
    )

    day_lbl = format_day_label(created.start, now)
    t_lbl = format_time_natural(created.start, tz)
    if player is not None and hasattr(player, "write_log"):
        try:
            player.write_log(f"[Calendar] Scheduled '{created.title}' ({day_lbl} {t_lbl})")
        except Exception:
            pass
    return f"Scheduled {created.title} for {day_lbl} at {t_lbl}."


def handle_modify(
    parameters: dict,
    *,
    service: CalendarService | None = None,
) -> str:
    svc = _get_service(service)
    tz = get_local_tz()
    now = datetime.now(tz)

    query = str(parameters.get("query") or parameters.get("title") or "").strip()
    if not query:
        return "Which event would you like to modify?"

    matches = svc.find_matching_events(query, now=now)
    if not matches:
        return f"I couldn't find an upcoming event matching '{query}'."
    if len(matches) > 1:
        names = ", ".join(
            f"{m.title} ({format_day_label(m.start, now)} at {format_time_natural(m.start, tz)})"
            for m in matches[:4]
        )
        return f"I found multiple matching events: {names}. Which one did you mean?"

    target = matches[0]
    cal = svc.store.get_calendar(target.calendar_id)
    if cal and cal.read_only:
        return (
            f"'{target.title}' is on '{cal.name}', which is read-only. "
            f"Would you like me to copy it into your local calendar and edit it there?"
        )

    new_when = str(parameters.get("new_when") or "").strip()
    new_title = str(parameters.get("new_title") or "").strip() or None
    new_location = str(parameters.get("new_location") or "").strip() or None
    new_duration = int(parameters.get("new_duration_minutes") or 0)

    new_start = None
    new_end = None
    if new_when:
        parsed = parse_datetime_phrase(new_when, now=now, tz=tz, default_duration_minutes=target.duration_minutes or 60)
        if parsed.start is None:
            return f"I couldn't understand the new time '{new_when}'."
        new_start = parsed.start
        new_end = parsed.end
    if new_duration > 0:
        base_s = new_start or target.start
        new_end = base_s + timedelta(minutes=new_duration)

    old_snapshot = Event(
        id=target.id,
        calendar_id=target.calendar_id,
        title=target.title,
        start=target.start,
        end=target.end,
        location=target.location,
        notes=target.notes,
        reminders=list(target.reminders),
    )

    def _do_modify() -> str:
        updated = svc.modify(
            target.id,
            title=new_title,
            start=new_start,
            end=new_end,
            location=new_location,
        )
        _push_undo(
            f"modify '{old_snapshot.title}'",
            lambda snap=old_snapshot: (
                svc.modify(
                    snap.id,
                    title=snap.title,
                    start=snap.start,
                    end=snap.end,
                    location=snap.location,
                ),
                f"Restored '{snap.title}'.",
            )[1],
        )
        return f"Updated {updated.title} to {format_day_label(updated.start, now)} at {format_time_natural(updated.start, tz)}."

    detail = f"Modify '{target.title}'"
    if new_start:
        detail += f" → {format_day_label(new_start, now)} at {format_time_natural(new_start, tz)}"
    return _request_confirm(f"cal_modify_{target.id}", "Modify calendar event?", detail, _do_modify)


def handle_cancel(
    parameters: dict,
    *,
    service: CalendarService | None = None,
) -> str:
    svc = _get_service(service)
    tz = get_local_tz()
    now = datetime.now(tz)

    query = str(parameters.get("query") or parameters.get("title") or "").strip()
    if not query:
        return "Which event would you like to cancel?"

    matches = svc.find_matching_events(query, now=now)
    if not matches:
        return f"I couldn't find an upcoming event matching '{query}'."
    if len(matches) > 1:
        names = ", ".join(
            f"{m.title} ({format_day_label(m.start, now)} at {format_time_natural(m.start, tz)})"
            for m in matches[:4]
        )
        return f"I found multiple matching events: {names}. Which one should I cancel?"

    target = matches[0]
    cal = svc.store.get_calendar(target.calendar_id)
    if cal and cal.read_only:
        return f"'{target.title}' is on '{cal.name}', which is read-only and cannot be cancelled from AURA."

    def _do_cancel() -> str:
        removed = svc.cancel(target.id)
        _push_undo(
            f"cancel '{removed.title}'",
            lambda ev=removed: (
                svc.create(
                    title=ev.title,
                    start=ev.start,
                    end=ev.end,
                    calendar_id=ev.calendar_id,
                    all_day=ev.all_day,
                    location=ev.location,
                    notes=ev.notes,
                    rrule=ev.rrule,
                    reminders=list(ev.reminders),
                ),
                f"Restored '{ev.title}'.",
            )[1],
        )
        return f"Cancelled {removed.title}."

    when_lbl = f"{format_day_label(target.start, now)} at {format_time_natural(target.start, tz)}"
    return _request_confirm(
        f"cal_cancel_{target.id}",
        "Cancel calendar event?",
        f"Cancel '{target.title}' ({when_lbl})",
        _do_cancel,
    )


# ── Local-engine regex/keyword intent router fallback ────────────────────────

_VIEW_RE = re.compile(
    r"\b(?:show|what(?:'s| is)|check|tell me|list)\b.*\b(?:schedule|calendar|agenda|events?|appointments?)\b"
    r"|\bwhat(?:'s| is)\s+next\s+on\s+my\s+(?:schedule|calendar)\b",
    re.IGNORECASE,
)
_FREE_RE = re.compile(
    r"\b(?:when am i free|free time|free slots?|availability|available)\b",
    re.IGNORECASE,
)
_CLASH_RE = re.compile(
    r"\b(?:clash|clashes|conflict|conflicts|overlap|overlaps|double[- ]booked)\b",
    re.IGNORECASE,
)
_CREATE_RE = re.compile(
    r"^(?:please\s+)?(?:schedule|book|add|create)\s+(?:an?\s+event\s+)?(.+?)$",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(
    r"^(?:please\s+)?(?:cancel|delete|remove)\s+(?:the\s+)?(?:event\s+)?(.+?)(?:\s+from\s+(?:my\s+)?calendar)?$",
    re.IGNORECASE,
)
_MOVE_RE = re.compile(
    r"^(?:please\s+)?(?:move|reschedule)\s+(.+?)\s+to\s+(.+?)$",
    re.IGNORECASE,
)
_TIME_SPLIT_RE = re.compile(
    r"\b(today|tomorrow|next\s+\w+|this\s+\w+|on\s+\w+|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
    re.IGNORECASE,
)


def match_and_run_calendar_intent(
    text: str,
    *,
    player=None,
    service: CalendarService | None = None,
) -> str | None:
    """Deterministic intent router for local/non-Gemini engines without function calling.

    Returns a response string if the text matched a calendar intent, else None.
    """
    global _PENDING_SLOT
    raw = (text or "").strip()
    if not raw:
        return None

    # Complete pending slot-filling turn if active
    with _SLOT_LOCK:
        pending = _PENDING_SLOT
        _PENDING_SLOT = None
    if pending and pending.get("action") == "create":
        if not pending.get("title"):
            return handle_create(
                {"title": raw, "when": pending.get("when", ""), "location": pending.get("location", "")},
                player=player,
                service=service,
            )
        if not pending.get("when"):
            return handle_create(
                {"title": pending["title"], "when": raw, "location": pending.get("location", "")},
                player=player,
                service=service,
            )

    if _CLASH_RE.search(raw):
        return handle_conflicts({"when": raw}, service=service)

    if _FREE_RE.search(raw):
        return handle_free_slots({"when": raw}, service=service)

    if _VIEW_RE.search(raw):
        lower = raw.lower()
        show_panel = "show" in lower or "open" in lower
        if "next" in lower:
            rng = "next"
        elif "tomorrow" in lower:
            rng = "tomorrow"
        elif "rest of" in lower and "week" in lower:
            rng = "rest of week"
        elif "week" in lower:
            rng = "upcoming week"
        else:
            rng = "today"
        return handle_view({"range": rng, "show_panel": show_panel}, player=player, service=service)

    m_move = _MOVE_RE.match(raw)
    if m_move:
        return handle_modify({"query": m_move.group(1), "new_when": m_move.group(2)}, service=service)

    m_cancel = _CANCEL_RE.match(raw)
    if m_cancel and any(k in raw.lower() for k in ("event", "meeting", "dentist", "appointment", "calendar")):
        return handle_cancel({"query": m_cancel.group(1)}, service=service)

    m_create = _CREATE_RE.match(raw)
    if m_create:
        body = m_create.group(1).strip()
        # Strip trailing "on my calendar" / "to my calendar"
        body = re.sub(r"\s+(?:on|to)\s+(?:my\s+)?calendar$", "", body, flags=re.IGNORECASE).strip()
        split_m = _TIME_SPLIT_RE.search(body)
        if split_m:
            title = body[: split_m.start()].strip(" ,:-")
            when = body[split_m.start() :].strip()
        else:
            title = body
            when = ""
        return handle_create({"title": title, "when": when}, player=player, service=service)

    return None
