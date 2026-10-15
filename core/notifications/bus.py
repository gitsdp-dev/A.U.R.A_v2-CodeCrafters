"""Thread-safe in-process pub/sub for A.U.R.A. application notifications."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from core.notifications.models import Notification

LOGGER = logging.getLogger(__name__)
NotificationCallback = Callable[[Notification], None]


class NotificationBus:
    """A minimal publisher that isolates subscriber failures."""

    def __init__(self) -> None:
        self._subscribers: list[NotificationCallback] = []
        self._lock = threading.RLock()

    def subscribe(self, callback: NotificationCallback) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
        return unsubscribe

    def publish(self, notification: Notification) -> None:
        if not isinstance(notification, Notification):
            raise TypeError("publish expects a Notification")
        with self._lock:
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            try:
                callback(notification)
            except Exception:
                LOGGER.exception("Notification subscriber failed")


notification_bus = NotificationBus()


def notify(app_name: str, title: str, body: str, priority: int = 40) -> Notification:
    """Publish an app notification through the shared local notification bus.

    Plugins can use ``from core.notifications.bus import notify`` and call this
    helper; notification content is persisted and displayed locally only.
    """
    notification = Notification(
        source="app",
        app_name=str(app_name or "A.U.R.A."),
        title=str(title or ""),
        body=str(body or ""),
        priority=max(0, min(100, int(priority))),
    )
    notification_bus.publish(notification)
    return notification
