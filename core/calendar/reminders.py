"""Calendar reminder bridge — reuses AURA's existing OS-level reminder & notification bus."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, tzinfo

from core.calendar.models import Event, ensure_aware, get_local_tz
from core.calendar.store import CalendarStore

LOGGER = logging.getLogger(__name__)


def _fire_os_reminder(event: Event, offset_minutes: int) -> bool:
    """Trigger the existing OS-level reminder / notification path."""
    msg = (
        f"{event.title} starts in {offset_minutes} min"
        if offset_minutes > 0
        else f"{event.title} is starting now"
    )
    if event.location:
        msg += f" ({event.location})"

    # 1. Publish to AURA's local notification bus (shown in notification panel)
    try:
        from core.notifications.bus import notify

        notify("AURA Calendar", f"Upcoming: {event.title}", msg, priority=65)
    except Exception:
        pass

    # 2. Reuse existing OS-level notification path from actions/reminder.py
    try:
        from actions.reminder import _get_os

        os_name = _get_os()
        if os_name == "windows":
            try:
                from plyer import notification

                notification.notify(title="AURA Calendar", message=msg, timeout=10)
                return True
            except Exception:
                pass
            try:
                from win10toast import ToastNotifier

                ToastNotifier().show_toast("AURA Calendar", msg, duration=10, threaded=True)
                return True
            except Exception:
                pass
        elif os_name == "mac":
            import subprocess

            safe = msg.replace('"', "")
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe}" with title "AURA Calendar"'],
                check=False,
            )
            return True
        else:
            import subprocess

            subprocess.run(
                ["notify-send", "--urgency=normal", "--expire-time=10000", "AURA Calendar", msg],
                check=False,
            )
            return True
    except Exception as exc:
        LOGGER.debug("OS reminder fallback: %s", exc)
    return True


class ReminderDispatcher:
    """Checks upcoming events and dispatches due reminders without double-firing."""

    def __init__(self, store: CalendarStore, fire_fn=None) -> None:
        self._store = store
        self._fire_fn = fire_fn or _fire_os_reminder

    def check_and_fire(
        self,
        events: list[Event],
        *,
        now: datetime | None = None,
        tz: tzinfo | None = None,
    ) -> list[tuple[Event, int]]:
        """Check events for due reminders in [now - 2min, now + 1min] and fire once."""
        target_tz = tz or get_local_tz()
        ref = ensure_aware(now or datetime.now(target_tz), target_tz)
        default_min = int(self._store.get_setting("default_reminder_minutes", 15))
        fired: list[tuple[Event, int]] = []

        for ev in events:
            if ev.start <= ref - timedelta(minutes=2):
                continue
            offsets = ev.reminders if ev.reminders else [default_min]
            occ_iso = (ev.occurrence_start or ev.start).isoformat()
            for offset in offsets:
                trigger_at = ev.start - timedelta(minutes=int(offset))
                # Fire if we are within [trigger_at, trigger_at + 5 min] and event hasn't passed
                if trigger_at <= ref <= trigger_at + timedelta(minutes=5):
                    dedup_key = f"{ev.id}|{occ_iso}|{int(offset)}"
                    if self._store.was_reminder_sent(dedup_key):
                        continue
                    try:
                        ok = self._fire_fn(ev, int(offset))
                    except Exception as exc:
                        LOGGER.warning("Reminder dispatch failed: %s", exc)
                        ok = False
                    if ok:
                        self._store.mark_reminder_sent(dedup_key, ev.id, occ_iso, int(offset))
                        fired.append((ev, int(offset)))
        return fired
