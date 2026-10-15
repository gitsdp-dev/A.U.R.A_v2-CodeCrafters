"""Best-effort background capture for desktop notification events."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import shutil
import sqlite3
import threading
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from core.notifications.models import Notification

LOGGER = logging.getLogger(__name__)


class NotificationListener:
    """Platform-selected listener; callbacks are always invoked off the UI thread."""

    def __init__(self, poll_seconds: float = 3.0) -> None:
        self.poll_seconds = max(1.0, poll_seconds)
        self._callbacks: list[Callable[[Notification], None]] = []
        self._error_callbacks: list[Callable[[str], None]] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: set[str] = set()
        self._platform = platform.system()

    def on_notification(self, callback: Callable[[Notification], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def on_unavailable(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._error_callbacks.append(callback)

    def start(self) -> bool:
        """Start the OS listener, returning false when no backend is available."""
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="aura-notifications"
        )
        try:
            self._thread.start()
        except Exception as exc:
            LOGGER.exception("Could not start system notification listener")
            self._unavailable(f"Could not start system notification listener: {exc}")
            return False
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def _emit(self, notification: Notification) -> None:
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(notification)
            except Exception:
                LOGGER.exception("Notification callback failed")

    def _run(self) -> None:
        try:
            if self._platform == "Windows":
                self._run_windows()
            elif self._platform == "Linux":
                self._run_linux()
            elif self._platform == "Darwin":
                self._unavailable(
                    "macOS system notifications are not supported; "
                    "Full Disk Access would be required for database capture"
                )
            else:
                self._unavailable(
                    f"System notification capture is unsupported on {self._platform}"
                )
        except Exception:
            LOGGER.exception("System notification listener stopped unexpectedly")
            self._unavailable("System notification access is unavailable")

    def _unavailable(self, reason: str) -> None:
        LOGGER.warning("%s", reason)
        with self._lock:
            callbacks = tuple(self._error_callbacks)
        for callback in callbacks:
            try:
                callback(reason)
            except Exception:
                LOGGER.exception("Notification listener error callback failed")

    def _run_windows(self) -> None:
        try:
            self._run_windows_winrt()
            return
        except Exception as exc:
            LOGGER.warning("Windows WinRT notification access unavailable: %s", exc)
        self._run_windows_database()

    def _run_windows_winrt(self) -> None:
        from winsdk.windows.ui.notifications import NotificationKinds
        from winsdk.windows.ui.notifications.management import (
            UserNotificationListener,
            UserNotificationListenerAccessStatus,
        )

        async def fetch(listener):
            items = await listener.get_notifications_async(NotificationKinds.TOAST)
            results = []
            for item in items:
                try:
                    toast = item.notification
                    xml = toast.content.get_xml()
                    app = item.app_info.display_info.display_name
                    results.append((
                        str(item.id),
                        _notification_from_xml(xml, app or "Windows", item.id),
                    ))
                except Exception as exc:
                    LOGGER.warning("Could not decode a Windows toast: %s", exc)
            return results

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            listener = UserNotificationListener.get_current()
            access = loop.run_until_complete(listener.request_access_async())
            if access != UserNotificationListenerAccessStatus.ALLOWED:
                raise PermissionError(f"notification access status is {access}")
            LOGGER.info("Windows UserNotificationListener access granted")
            while not self._stop.is_set():
                for event_id, notification in loop.run_until_complete(fetch(listener)):
                    if event_id not in self._seen:
                        self._seen.add(event_id)
                        self._emit(notification)
                self._stop.wait(self.poll_seconds)
        finally:
            loop.close()

    def _run_windows_database(self) -> None:
        local = Path(
            os.environ.get("LOCALAPPDATA")
            or Path.home() / "AppData" / "Local"
        )
        database = local / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"
        if not database.is_file():
            raise FileNotFoundError(f"Windows notification database not found: {database}")
        LOGGER.info("Using read-only Windows notification database fallback")
        first_poll = True
        failure_reported = False
        while not self._stop.is_set():
            try:
                rows = _read_windows_database(database)
                for key, notification in rows:
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                    if not first_poll:
                        self._emit(notification)
                first_poll = False
            except Exception as exc:
                if not failure_reported:
                    LOGGER.exception("Could not read Windows notification database")
                    self._unavailable(
                        f"Windows notification database capture failed: {exc}"
                    )
                    failure_reported = True
                else:
                    LOGGER.debug("Windows notification database remains unavailable: %s",
                                 exc)
            self._stop.wait(self.poll_seconds)

    def _run_linux(self) -> None:
        try:
            asyncio.run(self._linux_monitor())
        except ImportError as exc:
            raise RuntimeError("install dbus-next to capture Linux notifications") from exc

    async def _linux_monitor(self) -> None:
        from dbus_next import Message, MessageType
        from dbus_next.aio import MessageBus

        bus = await MessageBus().connect()
        match = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
        response = await bus.call(Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="BecomeMonitor",
            signature="asu",
            body=[[match], 0],
        ))
        if response.message_type == MessageType.ERROR:
            raise RuntimeError(f"D-Bus monitor access denied: {response.body}")

        def handler(message):
            if (message.message_type != MessageType.METHOD_CALL
                    or message.interface != "org.freedesktop.Notifications"
                    or message.member != "Notify"
                    or len(message.body) < 6):
                return False
            try:
                app, _replaces, _icon, title, body = message.body[:5]
                notification = Notification(
                    source="system",
                    app_name=str(app or "Linux application"),
                    title=str(title or ""),
                    body=str(body or ""),
                    raw=None,
                )
                self._emit(notification)
            except Exception:
                LOGGER.exception("Could not decode D-Bus notification")
            return False

        bus.add_message_handler(handler)
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.25)
        finally:
            bus.disconnect()


def _notification_from_xml(xml: str, app_name: str, event_id: int | str) -> Notification:
    root = ET.fromstring(xml)
    texts = [node.text.strip() for node in root.iter()
             if node.tag.rsplit("}", 1)[-1] == "text" and node.text and node.text.strip()]
    return Notification(
        id=f"winrt-{event_id}",
        source="system",
        app_name=str(app_name or "Windows application"),
        title=texts[0] if texts else "Notification",
        body=" ".join(texts[1:]),
        raw=xml,
    )


def _read_windows_database(database: Path) -> list[tuple[str, Notification]]:
    """Read toast rows from a private copy so SQLite never locks the live DB."""
    with tempfile.TemporaryDirectory(prefix="aura-notifications-") as temp_dir:
        snapshot = Path(temp_dir) / database.name
        shutil.copy2(database, snapshot)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            if sidecar.is_file():
                shutil.copy2(sidecar, Path(f"{snapshot}{suffix}"))
        uri = f"file:{snapshot.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            connection.execute("PRAGMA query_only=ON")
            tables = {
                str(row[0]).casefold(): str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            table = tables.get("notification")
            if not table:
                raise RuntimeError("notification table not found in Windows database")
            columns = [row[1] for row in connection.execute(
                f'PRAGMA table_info("{table}")'
            )]
            folded = {str(column).casefold(): str(column) for column in columns}
            payload_col = next((folded[key] for key in ("payload", "xml", "content")
                                if key in folded), None)
            if payload_col is None:
                raise RuntimeError("notification payload column not found")
            id_col = next((folded[key] for key in ("id", "notificationid", "rowid")
                           if key in folded), None)
            app_col = next((folded[key] for key in ("handlerid", "appid", "app")
                            if key in folded), None)
            selected = [f'"{payload_col}"']
            selected.extend([f'"{id_col}"' if id_col else "rowid"])
            selected.append(f'"{app_col}"' if app_col else "''")
            rows = connection.execute(
                f'SELECT {", ".join(selected)} FROM "{table}" '
                "ORDER BY rowid DESC LIMIT 250"
            ).fetchall()
        finally:
            connection.close()

    result = []
    for payload, row_id, app_name in rows:
        if not payload:
            continue
        payload = payload.decode(errors="replace") if isinstance(payload, bytes) else str(payload)
        key = hashlib.sha256(f"{row_id}:{payload}".encode("utf-8")).hexdigest()
        try:
            notification = _notification_from_xml(
                payload, str(app_name or "Windows application"), row_id
            )
        except (ET.ParseError, ValueError):
            LOGGER.debug("Skipping unparseable Windows toast row %s", row_id)
            continue
        result.append((key, notification))
    return result
