"""Deterministic natural-language date/time/duration/reminder parser for core.calendar."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo

import dateparser

from core.calendar.models import ensure_aware, get_local_tz

_DURATION_RE = re.compile(
    r"\b(?:for\s+)?(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b",
    re.IGNORECASE,
)
_REMINDER_RE = re.compile(
    r"\bremind(?:\s+me)?\s+(?:(an|a|\d+)\s+(hours?|hrs?|h|minutes?|mins?|m)\s+before)\b",
    re.IGNORECASE,
)
_EXPLICIT_TIME_RE = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(?:at\s+)(\d{1,2}):(\d{2})\b",
    re.IGNORECASE,
)
_WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_NEXT_WEEKDAY_RE = re.compile(
    r"\b(?:(this|next)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)


@dataclass
class ParsedDateTime:
    start: datetime | None
    end: datetime | None
    duration_minutes: int
    reminder_minutes: int | None
    all_day: bool = False
    has_time: bool = False


def parse_duration_minutes(text: str, default_minutes: int = 60) -> tuple[int, str]:
    """Extract duration like 'for 45 minutes' or '2 hours', returning (minutes, cleaned_text)."""
    m = _DURATION_RE.search(text or "")
    if not m:
        return default_minutes, text
    val = float(m.group(1))
    unit = m.group(2).lower()
    minutes = int(round(val * 60)) if unit.startswith("h") else int(round(val))
    minutes = max(5, min(24 * 60, minutes))
    cleaned = (text[: m.start()] + " " + text[m.end() :]).strip()
    return minutes, re.sub(r"\s+", " ", cleaned)


def parse_reminder_minutes(text: str) -> tuple[int | None, str]:
    """Extract reminder offset like 'remind me an hour before', returning (minutes, cleaned_text)."""
    m = _REMINDER_RE.search(text or "")
    if not m:
        return None, text
    raw_num = m.group(1).lower()
    val = 1 if raw_num in ("a", "an") else int(raw_num)
    unit = m.group(2).lower()
    minutes = val * 60 if unit.startswith("h") else val
    minutes = max(0, min(7 * 24 * 60, minutes))
    cleaned = (text[: m.start()] + " " + text[m.end() :]).strip()
    return minutes, re.sub(r"\s+", " ", cleaned)


def parse_datetime_phrase(
    phrase: str,
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
    default_duration_minutes: int = 60,
    prefer_future: bool = True,
) -> ParsedDateTime:
    """Parse natural-language date/time/duration/reminder phrase deterministically."""
    target_tz = tz or get_local_tz()
    base_now = ensure_aware(now or datetime.now(target_tz), target_tz)
    raw = (phrase or "").strip()
    if not raw:
        return ParsedDateTime(None, None, default_duration_minutes, None)

    reminder_mins, text = parse_reminder_minutes(raw)
    duration_mins, text = parse_duration_minutes(text, default_minutes=default_duration_minutes)

    has_explicit_time = bool(_EXPLICIT_TIME_RE.search(text)) or any(
        kw in text.lower() for kw in ("noon", "midnight", "morning", "afternoon", "evening")
    )

    # Handle "next <weekday>" deterministically before dateparser (dateparser can be inconsistent)
    dt: datetime | None = None
    lower = text.lower()
    wd_match = _NEXT_WEEKDAY_RE.search(lower)
    if wd_match:
        qualifier = (wd_match.group(1) or "").lower()
        wd_idx = _WEEKDAY_MAP[wd_match.group(2).lower()]
        days_ahead = (wd_idx - base_now.weekday()) % 7
        if qualifier == "next":
            if days_ahead == 0:
                days_ahead = 7
            elif days_ahead < 7 and base_now.weekday() >= wd_idx:
                pass
        elif days_ahead == 0 and prefer_future and not has_explicit_time:
            days_ahead = 7
        target_date = (base_now + timedelta(days=days_ahead)).date()
        parsed_time = _extract_time_of_day(text)
        if parsed_time is not None:
            dt = datetime.combine(target_date, parsed_time, tzinfo=target_tz)
            has_explicit_time = True
        else:
            dt = datetime.combine(target_date, time(9, 0), tzinfo=target_tz)

    if dt is None:
        settings = {
            "PREFER_DATES_FROM": "future" if prefer_future else "current_period",
            "RELATIVE_BASE": base_now.replace(tzinfo=None),
            "RETURN_AS_TIMEZONE_AWARE": False,
        }
        parsed = dateparser.parse(text, languages=["en"], settings=settings)
        if parsed is not None:
            dt = parsed.replace(tzinfo=target_tz)
            # Apply noon / midnight / afternoon if dateparser missed them
            tod = _extract_time_of_day(text)
            if tod is not None and not _EXPLICIT_TIME_RE.search(text):
                dt = datetime.combine(dt.date(), tod, tzinfo=target_tz)

    if dt is None:
        return ParsedDateTime(None, None, duration_mins, reminder_mins, has_time=has_explicit_time)

    # If user said "today at 9am" and it's already past, keep today if "today" was explicit;
    # if only a bare time was given ("at 5pm") and it's in the past, dateparser/future handles it.
    if prefer_future and not any(w in lower for w in ("today", "yesterday")) and dt < base_now:
        if _EXPLICIT_TIME_RE.search(text) and not any(
            w in lower for w in ("tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        ):
            dt = dt + timedelta(days=1)

    end_dt = dt + timedelta(minutes=duration_mins)
    return ParsedDateTime(
        start=dt,
        end=end_dt,
        duration_minutes=duration_mins,
        reminder_minutes=reminder_mins,
        all_day=False,
        has_time=has_explicit_time,
    )


def _extract_time_of_day(text: str) -> time | None:
    m = _EXPLICIT_TIME_RE.search(text)
    if m:
        if m.group(1) is not None:
            hr = int(m.group(1))
            mn = int(m.group(2) or 0)
            ampm = m.group(3).lower()
            if ampm == "pm" and hr < 12:
                hr += 12
            elif ampm == "am" and hr == 12:
                hr = 0
            if 0 <= hr <= 23 and 0 <= mn <= 59:
                return time(hr, mn)
        elif m.group(4) is not None:
            hr = int(m.group(4))
            mn = int(m.group(5))
            if 0 <= hr <= 23 and 0 <= mn <= 59:
                return time(hr, mn)
    lower = text.lower()
    if "noon" in lower:
        return time(12, 0)
    if "midnight" in lower:
        return time(0, 0)
    if "morning" in lower:
        return time(9, 0)
    if "afternoon" in lower:
        return time(14, 0)
    if "evening" in lower:
        return time(18, 0)
    return None


def parse_range_phrase(
    phrase: str,
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> tuple[datetime, datetime, str]:
    """Parse a schedule query range like 'today', 'tomorrow afternoon', 'rest of week', 'upcoming week'."""
    target_tz = tz or get_local_tz()
    base_now = ensure_aware(now or datetime.now(target_tz), target_tz)
    lower = (phrase or "today").strip().lower()

    today_start = datetime.combine(base_now.date(), time.min, tzinfo=target_tz)

    if "tomorrow" in lower:
        tom = base_now.date() + timedelta(days=1)
        if "morning" in lower:
            return (
                datetime.combine(tom, time(6, 0), tzinfo=target_tz),
                datetime.combine(tom, time(12, 0), tzinfo=target_tz),
                "tomorrow morning",
            )
        if "afternoon" in lower:
            return (
                datetime.combine(tom, time(12, 0), tzinfo=target_tz),
                datetime.combine(tom, time(18, 0), tzinfo=target_tz),
                "tomorrow afternoon",
            )
        if "evening" in lower:
            return (
                datetime.combine(tom, time(18, 0), tzinfo=target_tz),
                datetime.combine(tom, time(23, 59), tzinfo=target_tz),
                "tomorrow evening",
            )
        return (
            datetime.combine(tom, time.min, tzinfo=target_tz),
            datetime.combine(tom + timedelta(days=1), time.min, tzinfo=target_tz),
            "tomorrow",
        )

    if "rest of" in lower and "week" in lower:
        days_until_sunday = 6 - base_now.weekday()
        end_dt = datetime.combine(base_now.date() + timedelta(days=days_until_sunday + 1), time.min, tzinfo=target_tz)
        return base_now, end_dt, "the rest of this week"

    if any(k in lower for k in ("upcoming week", "next week", "this week", "week")):
        if "next week" in lower:
            days_to_mon = (7 - base_now.weekday()) or 7
            mon = base_now.date() + timedelta(days=days_to_mon)
            return (
                datetime.combine(mon, time.min, tzinfo=target_tz),
                datetime.combine(mon + timedelta(days=7), time.min, tzinfo=target_tz),
                "next week",
            )
        return (
            today_start,
            today_start + timedelta(days=7),
            "the upcoming week",
        )

    if "afternoon" in lower:
        return (
            datetime.combine(base_now.date(), time(12, 0), tzinfo=target_tz),
            datetime.combine(base_now.date(), time(18, 0), tzinfo=target_tz),
            "this afternoon",
        )

    return today_start, today_start + timedelta(days=1), "today"
