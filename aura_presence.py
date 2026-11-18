"""
aura_presence.py — Presence Awareness for A.U.R.A. v2

Detects when the user is away (screen locked or explicit voice/text command),
suppresses and queues incoming notifications/reminders/tasks, and delivers
a single consolidated catch-up summary upon return.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, pyqtSignal

_LOG = logging.getLogger("aura.presence")

# Windows API Constants
WM_WTSSESSION_CHANGE = 0x02B1
WTS_CONSOLE_CONNECT = 0x1
WTS_CONSOLE_DISCONNECT = 0x2
WTS_REMOTE_CONNECT = 0x3
WTS_REMOTE_DISCONNECT = 0x4
WTS_SESSION_LOGON = 0x5
WTS_SESSION_LOGOFF = 0x6
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
WTS_SESSION_REMOTE_CONTROL = 0x9
NOTIFY_FOR_THIS_SESSION = 0

_MAX_QUEUE_ITEMS = 50


@dataclass
class QueueItem:
    """A notification or event captured while the user was away."""

    id: str
    timestamp: str  # ISO-8601
    source: str     # e.g., "reminder", "message", "task", "app", "system"
    title: str
    body: str
    priority: int = 40
    category: str = "general"
    urgent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueueItem":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})


# ── Pure helper functions (Unit-testable) ────────────────────────────────────

def is_workstation_locked() -> bool:
    """Return True if the Windows desktop is currently locked, False otherwise."""
    if platform.system() != "Windows":
        return False
    try:
        user32 = ctypes.windll.user32
        h_desk = user32.OpenInputDesktop(0, False, 1)
        if h_desk == 0:
            return True
        user32.CloseDesktop(h_desk)
        return False
    except Exception:
        return False


def unlock_workstation(auth_type: str = "hello", credential: str = "") -> bool:
    """
    Attempt to wake the display, dismiss the lock screen curtain, and
    authenticate using the configured security method (Windows Hello, PIN, or Password).
    Returns True if the workstation successfully unlocked, False if it remains locked.
    """
    if platform.system() != "Windows":
        return False

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 1. Wake the display (ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)
        ES_CONTINUOUS = 0x80000000
        ES_DISPLAY_REQUIRED = 0x00000002
        ES_SYSTEM_REQUIRED = 0x00000001
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)

        # 2. Wake monitor and dismiss lock curtain
        # Mouse move + VK_UP (0x26, non-printable so it never types a space into PIN fields)
        user32.mouse_event(0x0001, 1, 1, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(0x26, 0, 0, 0)  # VK_UP down
        time.sleep(0.05)
        user32.keybd_event(0x26, 0, 2, 0)  # VK_UP up (KEYEVENTF_KEYUP = 2)
        time.sleep(0.05)
        user32.keybd_event(0x0D, 0, 0, 0)  # VK_RETURN down
        time.sleep(0.05)
        user32.keybd_event(0x0D, 0, 2, 0)  # VK_RETURN up

        # If Windows Hello (face / fingerprint), raising the lock curtain immediately
        # triggers the Windows Hello camera or sensor.
        if auth_type == "hello" or not credential:
            _LOG.info("Triggered Windows Hello wake on lock screen")
            time.sleep(1.5)
            return not is_workstation_locked()

        # 3. If PIN or Password is configured, wait for credential tile to focus
        time.sleep(0.5)

        # Clear any pre-existing characters with Backspaces (VK_BACK = 0x08)
        for _ in range(16):
            user32.keybd_event(0x08, 0, 0, 0)
            user32.keybd_event(0x08, 0, 2, 0)
        time.sleep(0.05)

        # Enter the credential via UNICODE key events for layout-independent accuracy
        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002
        for ch in credential:
            code = ord(ch)
            user32.keybd_event(0, code, KEYEVENTF_UNICODE, 0)
            time.sleep(0.03)
            user32.keybd_event(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
            time.sleep(0.03)

        # Send Enter (VK_RETURN = 0x0D) to submit credentials
        time.sleep(0.1)
        user32.keybd_event(0x0D, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(0x0D, 0, KEYEVENTF_KEYUP, 0)

        _LOG.info("Submitted %s credentials to lock screen", auth_type.upper())
        time.sleep(1.5)
        return not is_workstation_locked()
    except Exception as exc:
        _LOG.error("Failed to execute workstation unlock sequence: %s", exc)
        return False


def match_presence_intent(text: str) -> str | None:
    """Matches offline voice/text commands for leaving or returning."""
    if not text:
        return None
    normalized = re.sub(r"[^a-z0-9' ]+", " ", str(text).casefold())
    normalized = " ".join(normalized.split())

    # Strip leading or trailing assistant invocation
    clean = re.sub(r"^(?:(?:hey|hi|hello|ok|okay)\s+)?(?:aura|ora|aurora|computer|assistant)[, ]*", "", normalized).strip()
    clean = re.sub(r"[, ]+(?:(?:hey|hi|hello|ok|okay)\s+)?(?:aura|ora|aurora|computer|assistant)$", "", clean).strip()
    clean = re.sub(r"^(?:please\s+|can\s+you\s+|could\s+you\s+|just\s+)", "", clean).strip()
    clean = re.sub(r"[, ]+(?:please|now|thanks|thank you)$", "", clean).strip()

    # Details request (checked first to avoid false match on 'away')
    details_phrases = (
        "read details", "read the details", "what are the details",
        "read away details", "tell me details", "read catch up", "read catchup",
        "show details", "catchup details", "away details", "what did i miss",
        "what happened while i was away", "missed notifications",
    )
    for phrase in details_phrases:
        if phrase in normalized or phrase in clean:
            return "read_details"

    # Return triggers
    return_phrases = (
        "i'm back", "im back", "i am back", "i have returned",
        "back now", "just got back", "returned", "i returned",
        "finally back", "am back", "we're back", "were back",
        "unlock screen", "unlock the screen", "unlock pc", "unlock computer",
        "unlock workstation", "unlock desktop", "unlock windows", "unlock",
        "wake up", "i'm home", "im home", "i am home", "back home",
    )
    for phrase in return_phrases:
        if phrase in normalized or phrase in clean:
            if phrase in ("returned", "i returned") and clean not in ("returned", "i returned", "i have returned", "just returned"):
                continue
            if phrase == "unlock" and clean not in ("unlock", "unlock please", "please unlock"):
                if not any(x in clean for x in ("screen", "pc", "computer", "workstation", "windows", "desktop")):
                    continue
            return "back"

    # Away triggers
    away_phrases = (
        "i'm heading out", "im heading out", "i am heading out", "heading out", "heading off",
        "i'm leaving", "im leaving", "i am leaving", "leaving now", "leaving",
        "going out", "going away", "gonna leave", "about to leave", "taking off",
        "brb", "be right back", "stepping out", "stepping away", "step out", "step away",
        "i have to go", "gotta run", "gotta go", "got to go", "need to go", "have to leave",
        "see you later", "bye for now", "off now", "i'm off", "im off",
        "lock screen", "lock the screen", "lock workstation", "lock computer", "lock pc",
        "lock down", "lock windows", "lock desktop", "lock my screen", "lock my pc", "lock my computer",
        "enter away mode", "away mode", "set away mode", "enable away mode",
    )
    for phrase in away_phrases:
        if phrase in normalized or phrase in clean:
            if phrase == "leaving" and clean not in ("leaving", "leaving now", "i am leaving", "i'm leaving"):
                continue
            return "away"

    return None


def group_queued_items(items: list[QueueItem]) -> dict[str, int]:
    """Group items by friendly source category and return their counts."""
    counts: dict[str, int] = {}
    for item in items:
        combined = f"{item.source} {item.category} {item.title}".lower()

        if "remind" in combined or "alarm" in combined:
            key = "reminder"
        elif any(w in combined for w in ("message", "chat", "mail", "telegram", "slack", "discord", "whatsapp", "teams", "sms")):
            key = "message"
        elif any(w in combined for w in ("task", "background", "agent", "job", "worker", "process", "download", "export")):
            key = "task"
        elif "update" in combined:
            key = "update"
        else:
            key = "notification"

        counts[key] = counts.get(key, 0) + 1
    return counts


def format_catchup_speech(items: list[QueueItem]) -> str:
    """Generate a concise, natural summary sentence for spoken catch-up."""
    if not items:
        return "Welcome back. There were no new notifications while you were away."

    counts = group_queued_items(items)
    # Order by common priority: reminders first, then messages, tasks, updates, notifications
    order = ["reminder", "message", "task", "update", "notification"]
    parts: list[str] = []

    for cat in order:
        cnt = counts.get(cat, 0)
        if cnt <= 0:
            continue
        if cat == "reminder":
            parts.append(f"{cnt} reminder{'s' if cnt > 1 else ''}")
        elif cat == "message":
            parts.append(f"{cnt} message{'s' if cnt > 1 else ''}")
        elif cat == "task":
            parts.append(f"{cnt} completed task{'s' if cnt > 1 else ''}")
        elif cat == "update":
            parts.append(f"{cnt} update notice{'s' if cnt > 1 else ''}")
        else:
            parts.append(f"{cnt} notification{'s' if cnt > 1 else ''}")

    # Any remaining unlisted categories
    for cat, cnt in counts.items():
        if cat not in order and cnt > 0:
            parts.append(f"{cnt} {cat}{'s' if cnt > 1 else ''}")

    if not parts:
        summary_str = f"{len(items)} items"
    elif len(parts) == 1:
        summary_str = parts[0]
    elif len(parts) == 2:
        summary_str = f"{parts[0]} and {parts[1]}"
    else:
        summary_str = ", ".join(parts[:-1]) + f", and {parts[-1]}"

    return f"Welcome back. While you were away: {summary_str}."


def format_catchup_details(items: list[QueueItem]) -> list[str]:
    """Return a list of individual readable strings for detailed catchup."""
    # Sort highest priority first
    sorted_items = sorted(items, key=lambda x: x.priority, reverse=True)
    details: list[str] = []
    for item in sorted_items[:10]:
        title = item.title.strip() if item.title else item.source
        body = item.body.strip()
        if body:
            details.append(f"From {item.source}: {title}. {body}")
        else:
            details.append(f"From {item.source}: {title}")
    return details


# ── Windows Session Event Filter ─────────────────────────────────────────────

if platform.system() == "Windows":
    class _MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint),
            ("wParam", ctypes.c_ulonglong),
            ("lParam", ctypes.c_longlong),
            ("time", ctypes.c_ulong),
            ("pt_x", ctypes.c_long),
            ("pt_y", ctypes.c_long),
        ]


class _SessionEventFilter(QAbstractNativeEventFilter):
    """Intercepts Windows session change notifications (lock / unlock / logon)."""

    def __init__(self, on_session_change: Callable[[int], None]):
        super().__init__()
        self._callback = on_session_change

    def nativeEventFilter(self, event_type, message):
        if platform.system() == "Windows" and event_type == b"windows_generic_MSG":
            try:
                msg = _MSG.from_address(int(message))
                if msg.message == WM_WTSSESSION_CHANGE:
                    self._callback(int(msg.wParam))
            except Exception as exc:
                _LOG.debug("Error processing native Windows session event: %s", exc)
        return False, 0


# ── Presence Manager ─────────────────────────────────────────────────────────

class PresenceManager(QObject):
    """
    Manages user presence state, queueing events while away,
    and delivering a catch-up briefing upon return.
    """

    away_started = pyqtSignal()
    away_ended = pyqtSignal(dict)  # {"summary": str, "count": int, "details": list}
    catchup_card_requested = pyqtSignal(object)  # Notification object for UI toast

    def __init__(
        self,
        persistence_path: Path | None = None,
        interrupt_callback: Callable[[], None] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._lock = threading.RLock()
        self._is_away = False
        self._away_start_time = 0.0
        self._last_unlock_time = 0.0
        self._queue: list[QueueItem] = []
        self._last_catchup_items: list[QueueItem] = []
        self._interrupt_callback = interrupt_callback

        if persistence_path is None:
            base_dir = Path(__file__).resolve().parent
            persistence_path = base_dir / "memory" / "away_queue.json"
        self._persistence_path = Path(persistence_path)

        self._wts_registered = False
        self._registered_hwnd = None
        self._event_filter: _SessionEventFilter | None = None
        self._fallback_timer: QTimer | None = None
        self._last_desktop_state: bool | None = None

        # Load persisted queue if any
        self._load_queue()

    @property
    def is_away(self) -> bool:
        with self._lock:
            return self._is_away

    @property
    def queue_length(self) -> int:
        with self._lock:
            return len(self._queue)

    def set_interrupt_callback(self, cb: Callable[[], None]) -> None:
        self._interrupt_callback = cb

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_queue(self) -> None:
        with self._lock:
            if not self._persistence_path.exists():
                return
            try:
                text = self._persistence_path.read_text(encoding="utf-8")
                data = json.loads(text)
                if isinstance(data, list):
                    self._queue = [QueueItem.from_dict(item) for item in data if isinstance(item, dict)]
                    _LOG.info("Loaded %d away queue items from %s", len(self._queue), self._persistence_path)
            except Exception as exc:
                _LOG.warning("Failed to load away queue: %s", exc)
                self._queue = []

    def _persist_queue(self) -> None:
        with self._lock:
            try:
                self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
                data = [item.to_dict() for item in self._queue]
                temp_path = self._persistence_path.with_suffix(".tmp")
                temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                temp_path.replace(self._persistence_path)
            except Exception as exc:
                _LOG.warning("Failed to persist away queue: %s", exc)

    def _clear_persisted_queue(self) -> None:
        with self._lock:
            try:
                if self._persistence_path.exists():
                    self._persistence_path.unlink()
            except Exception as exc:
                _LOG.warning("Failed to delete away queue file: %s", exc)

    # ── Windows Session Registration ─────────────────────────────────────────

    def start_monitoring(self, app, window=None) -> None:
        """Register for Windows lock/unlock session notifications."""
        if platform.system() != "Windows":
            _LOG.info("Presence monitoring: Windows session notifications not supported on %s", platform.system())
            return

        try:
            self._event_filter = _SessionEventFilter(self._on_windows_session_change)
            app.installNativeEventFilter(self._event_filter)

            if window is not None and hasattr(window, "winId"):
                hwnd = int(window.winId())
                wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
                wtsapi32.WTSRegisterSessionNotification.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                wtsapi32.WTSRegisterSessionNotification.restype = ctypes.c_bool
                if wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION):
                    self._wts_registered = True
                    self._registered_hwnd = hwnd
                    _LOG.info("WTSRegisterSessionNotification succeeded for HWND %s", hwnd)
                else:
                    _LOG.warning("WTSRegisterSessionNotification failed; using fallback polling.")
        except Exception as exc:
            _LOG.warning("Could not register session notifications: %s. Starting fallback polling.", exc)

        # Fallback polling (desktop lock check via OpenInputDesktop)
        if not self._wts_registered:
            self._start_fallback_polling()

    def _start_fallback_polling(self) -> None:
        if self._fallback_timer is not None:
            return
        self._fallback_timer = QTimer(self)
        self._fallback_timer.setInterval(2000)
        self._fallback_timer.timeout.connect(self._check_desktop_lock_fallback)
        self._fallback_timer.start()
        _LOG.info("Started presence fallback polling timer (2s)")

    def _check_desktop_lock_fallback(self) -> None:
        if platform.system() != "Windows":
            return
        try:
            user32 = ctypes.windll.user32
            # OpenInputDesktop returns 0 if desktop is locked or UAC secure desktop active
            h_desk = user32.OpenInputDesktop(0, False, 1)
            is_locked = (h_desk == 0)
            if h_desk:
                user32.CloseDesktop(h_desk)

            if self._last_desktop_state is None:
                self._last_desktop_state = is_locked
                return

            if is_locked and not self._last_desktop_state:
                _LOG.info("Fallback desktop check detected LOCK")
                self.enter_away("Desktop lock (fallback)")
            elif not is_locked and self._last_desktop_state:
                _LOG.info("Fallback desktop check detected UNLOCK")
                self.exit_away("Desktop unlock (fallback)")

            self._last_desktop_state = is_locked
        except Exception as exc:
            _LOG.debug("Error in desktop lock fallback check: %s", exc)

    def stop_monitoring(self, app) -> None:
        """Unregister listeners and clean up."""
        if self._fallback_timer is not None:
            self._fallback_timer.stop()
            self._fallback_timer = None

        if self._event_filter is not None:
            try:
                app.removeNativeEventFilter(self._event_filter)
            except Exception:
                pass
            self._event_filter = None

        if self._wts_registered and self._registered_hwnd:
            try:
                wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
                wtsapi32.WTSUnRegisterSessionNotification(self._registered_hwnd)
            except Exception:
                pass
            self._wts_registered = False
            self._registered_hwnd = None

    def _on_windows_session_change(self, event_code: int) -> None:
        """Handle WTS session change event code."""
        if event_code == WTS_SESSION_LOCK:
            _LOG.info("Windows Session LOCK received")
            self.enter_away("Windows session locked")
        elif event_code in (WTS_SESSION_UNLOCK, WTS_SESSION_LOGON):
            _LOG.info("Windows Session UNLOCK/LOGON received (code=%d)", event_code)
            self.exit_away("Windows session unlocked")

    # ── Away Mode Controls ───────────────────────────────────────────────────

    def enter_away(self, reason: str = "") -> None:
        """Transition into Away Mode and silence ongoing speech."""
        with self._lock:
            if self._is_away:
                return
            self._is_away = True
            self._away_start_time = time.monotonic()
            _LOG.info("Entering Away Mode (reason=%s)", reason)

        # Stop speech gracefully if AURA is talking
        if self._interrupt_callback:
            try:
                self._interrupt_callback()
            except Exception as exc:
                _LOG.warning("Failed to interrupt speech on away start: %s", exc)

        self.away_started.emit()

    def exit_away(self, reason: str = "") -> dict:
        """
        Transition out of Away Mode, debouncing rapid duplicate events,
        and emit catch-up data.
        """
        now = time.monotonic()
        with self._lock:
            if not self._is_away:
                return {}
            # Debounce duplicate unlock signals within 2.0s
            if now - self._last_unlock_time < 2.0:
                _LOG.debug("Ignored duplicate unlock event within debounce window")
                return {}
            self._last_unlock_time = now
            self._is_away = False
            items = list(self._queue)
            self._last_catchup_items = items
            self._queue.clear()
            self._clear_persisted_queue()

        summary_text = format_catchup_speech(items)
        details_list = format_catchup_details(items)
        catchup_data = {
            "summary": summary_text,
            "count": len(items),
            "details": details_list,
            "items": [item.to_dict() for item in items],
            "reason": reason,
        }
        _LOG.info("Exiting Away Mode (reason=%s, queued=%d)", reason, len(items))

        # Emit catchup card for notification UI
        try:
            from core.notifications.models import Notification
            card_notification = Notification(
                source="app",
                app_name="A.U.R.A. Catch-Up",
                title="Welcome Back",
                body=summary_text,
                priority=75,
                category="catchup",
            )
            self.catchup_card_requested.emit(card_notification)
        except Exception as exc:
            _LOG.debug("Could not emit catchup card: %s", exc)

        self.away_ended.emit(catchup_data)
        return catchup_data

    def get_last_catchup_details(self) -> list[str]:
        """Return detail strings for the most recent catch-up."""
        with self._lock:
            return format_catchup_details(self._last_catchup_items)

    def unlock_desktop(self) -> bool:
        """
        Attempt to wake display and authenticate to unlock workstation using
        the configured security method (Windows Hello, PIN, or Password).
        """
        from memory.config_manager import get_presence_config
        cfg = get_presence_config()
        if not cfg.get("auto_unlock_enabled", True):
            return False
        auth_type = cfg.get("unlock_auth_type", "hello")
        credential = cfg.get("unlock_credential", "")
        return unlock_workstation(auth_type=auth_type, credential=credential)

    # ── Notification Interception ────────────────────────────────────────────

    def should_intercept(self, notification) -> bool:
        """
        Determine whether a notification should be intercepted or allowed through.
        Returns True to intercept (queue), False to allow through.
        """
        with self._lock:
            if not self._is_away:
                return False

        # Check config for critical items
        from memory.config_manager import get_presence_config
        cfg = get_presence_config()
        if not cfg.get("enabled", True):
            return False

        allow_critical = cfg.get("allow_critical_while_away", True)
        is_urgent = (
            getattr(notification, "priority", 0) >= 80
            or getattr(notification, "category", "") in ("urgent", "alarm", "critical")
        )
        if is_urgent and allow_critical:
            _LOG.info("Allowing critical notification through during Away Mode: %s", getattr(notification, "title", ""))
            return False

        return True

    def enqueue_notification(self, notification) -> None:
        """Queue a notification while away."""
        now_iso = datetime.now(timezone.utc).isoformat()
        item = QueueItem(
            id=getattr(notification, "id", f"away-{time.monotonic()}"),
            timestamp=getattr(notification, "timestamp", now_iso) if isinstance(getattr(notification, "timestamp", None), str) else now_iso,
            source=getattr(notification, "app_name", "Notification"),
            title=getattr(notification, "title", ""),
            body=getattr(notification, "body", ""),
            priority=getattr(notification, "priority", 40),
            category=getattr(notification, "category", "general"),
            urgent=getattr(notification, "priority", 0) >= 80,
        )
        self.enqueue_item(item)

    def enqueue_item(self, item: QueueItem) -> None:
        """Thread-safe enqueue with size capping and persistence."""
        with self._lock:
            if len(self._queue) >= _MAX_QUEUE_ITEMS:
                # Discard lowest priority old item to protect memory
                self._queue.sort(key=lambda x: x.priority)
                self._queue.pop(0)

            self._queue.append(item)
            _LOG.info("Queued item while away: %s [%s] (total: %d)", item.title, item.source, len(self._queue))
            self._persist_queue()
