"""Template-based natural-language schedule summaries (zero LLM tokens by default).

Optional AI polish is strictly opt-in (default OFF), uses the active engine,
and falls back silently to the deterministic template on any failure.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, tzinfo

from core.calendar.conflicts import Conflict
from core.calendar.models import Event, ensure_aware, get_local_tz


def format_time_natural(dt: datetime, tz: tzinfo | None = None) -> str:
    """Format a datetime naturally ('noon', 'midnight', '4 PM', '3:30 PM')."""
    target_tz = tz or dt.tzinfo or get_local_tz()
    local_dt = ensure_aware(dt, target_tz).astimezone(target_tz)
    if local_dt.hour == 12 and local_dt.minute == 0:
        return "noon"
    if local_dt.hour == 0 and local_dt.minute == 0:
        return "midnight"
    hour_12 = local_dt.hour % 12 or 12
    ampm = "AM" if local_dt.hour < 12 else "PM"
    if local_dt.minute == 0:
        return f"{hour_12} {ampm}"
    return f"{hour_12}:{local_dt.minute:02d} {ampm}"


def format_day_label(dt: datetime, now: datetime | None = None) -> str:
    tz = get_local_tz()
    local_dt = ensure_aware(dt, tz)
    ref = ensure_aware(now or datetime.now(tz), tz)
    if local_dt.date() == ref.date():
        return "today"
    if local_dt.date() == ref.date() + timedelta(days=1):
        return "tomorrow"
    return local_dt.strftime("%A")


def _count_word(n: int) -> str:
    words = {0: "no", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
    return words.get(n, str(n))


def summarize_day(
    events: list[Event],
    *,
    day_label: str = "today",
    now: datetime | None = None,
    next_day_first: Event | None = None,
) -> str:
    """Generate a concise, natural spoken summary for a single day's events."""
    tz = (now.tzinfo if now and now.tzinfo else None) or get_local_tz()
    ref = ensure_aware(now or datetime.now(tz), tz)
    ordered = sorted(events, key=lambda e: (e.start, e.title.lower()))

    if not ordered:
        if day_label == "today" and next_day_first is not None:
            when_day = format_day_label(next_day_first.start, ref)
            when_time = "all day" if next_day_first.all_day else format_time_natural(next_day_first.start, tz)
            return f"Your day is clear. Nothing scheduled until {when_day} at {when_time}."
        if day_label == "today":
            return "Your day is clear. Nothing scheduled today."
        return f"Nothing scheduled {day_label}."

    if len(ordered) == 1:
        ev = ordered[0]
        if ev.all_day:
            return f"You have one all-day event {day_label}: {ev.title}."
        return f"You have one event {day_label}: {ev.title} at {format_time_natural(ev.start, tz)}."

    if len(ordered) == 2:
        a, b = ordered[0], ordered[1]
        a_t = "all day" if a.all_day else format_time_natural(a.start, tz)
        b_t = "all day" if b.all_day else format_time_natural(b.start, tz)
        return f"You have two events {day_label}: {a.title} at {a_t}, and {b.title} at {b_t}."

    # 3 or more events
    upcoming = [e for e in ordered if e.end > ref] if day_label == "today" else ordered
    nxt = upcoming[0] if upcoming else ordered[0]
    nxt_time = "all day" if nxt.all_day else format_time_natural(nxt.start, tz)
    verb = "starts" if day_label == "today" else "is"
    if day_label == "today" and upcoming:
        return f"You have {_count_word(len(ordered))} events today. Your next one, {nxt.title}, {verb} at {nxt_time}."
    return f"You have {_count_word(len(ordered))} events {day_label}, starting with {nxt.title} at {nxt_time}."


def summarize_week(
    events: list[Event],
    *,
    now: datetime | None = None,
    label: str = "the upcoming week",
) -> str:
    """Summarize a multi-day range, grouping by day and noting the busiest day."""
    tz = get_local_tz()
    ref = ensure_aware(now or datetime.now(tz), tz)
    if not events:
        return f"Your schedule is clear for {label}."

    by_day: dict = defaultdict(list)
    for ev in sorted(events, key=lambda e: e.start):
        by_day[ensure_aware(ev.start, tz).date()].append(ev)

    busiest_date, busiest_list = max(by_day.items(), key=lambda item: len(item[1]))
    busiest_label = format_day_label(datetime.combine(busiest_date, datetime.min.time(), tzinfo=tz), ref)
    total = len(events)
    noun = "event" if total == 1 else "events"

    parts = [f"You have {_count_word(total)} {noun} over {label}."]
    if len(by_day) > 1:
        parts.append(f"Your busiest day is {busiest_label} with {len(busiest_list)} events.")

    tomorrow_date = ref.date() + timedelta(days=1)
    if tomorrow_date in by_day:
        first_tom = by_day[tomorrow_date][0]
        t_str = "all day" if first_tom.all_day else format_time_natural(first_tom.start, tz)
        parts.append(f"Tomorrow starts with {first_tom.title} at {t_str}.")
    else:
        first_ev = sorted(events, key=lambda e: e.start)[0]
        d_str = format_day_label(first_ev.start, ref)
        t_str = "all day" if first_ev.all_day else format_time_natural(first_ev.start, tz)
        parts.append(f"Next up is {first_ev.title} {d_str} at {t_str}.")
    return " ".join(parts)


def summarize_conflicts(conflicts: list[Conflict]) -> str:
    """Format conflict list naturally."""
    if not conflicts:
        return "No schedule clashes found."
    c = conflicts[0]
    tz = get_local_tz()
    s_str = format_time_natural(c.overlap_start, tz)
    e_str = format_time_natural(c.overlap_end, tz)
    if len(conflicts) == 1:
        return f"Heads up: {c.event_a.title} and {c.event_b.title} overlap from {s_str} to {e_str}."
    return (
        f"You have {len(conflicts)} schedule clashes, starting with "
        f"{c.event_a.title} and {c.event_b.title} from {s_str} to {e_str}."
    )


def format_agenda_lines(events: list[Event], *, tz: tzinfo | None = None) -> str:
    """Detailed multi-line agenda text for the UI panel."""
    target_tz = tz or get_local_tz()
    if not events:
        return "No events scheduled."
    by_day: dict = defaultdict(list)
    for ev in sorted(events, key=lambda e: (e.start, e.title.lower())):
        by_day[ensure_aware(ev.start, target_tz).date()].append(ev)

    lines: list[str] = []
    for day_date in sorted(by_day.keys()):
        lines.append(day_date.strftime("%A, %b %d"))
        for ev in by_day[day_date]:
            if ev.all_day:
                t_range = "All day"
            else:
                t_range = f"{format_time_natural(ev.start, target_tz)} - {format_time_natural(ev.end, target_tz)}"
            loc = f" @ {ev.location}" if ev.location else ""
            lines.append(f"  • {t_range}: {ev.title}{loc}")
        lines.append("")
    return "\n".join(lines).strip()


def maybe_polish_summary(template_text: str, ai_polish_enabled: bool) -> str:
    """Optionally polish summary phrasing using the active engine; silent fallback on any error."""
    if not ai_polish_enabled or not template_text:
        return template_text
    try:
        from memory.config_manager import get_local_llm_config

        mode = get_local_llm_config().get("mode", "gemini")
        prompt = (
            "Rewrite this brief schedule update in 1-2 natural, conversational sentences "
            "without changing any times, event names, or facts:\n\n" + template_text
        )
        if mode == "local":
            from core.llm_client import call_llm_stream

            out = []
            for ev in call_llm_stream(
                [{"role": "user", "content": prompt}],
                tools=None,
                timeout=8,
                max_tokens=96,
            ):
                if ev.get("type") == "sentence" and ev.get("text"):
                    out.append(str(ev["text"]).strip())
            polished = " ".join(out).strip()
            return polished or template_text

        from core.gemini import text as gemini_text

        polished = (gemini_text(prompt, timeout=6.0) or "").strip()
        return polished or template_text
    except Exception:
        return template_text
