"""Notification orchestration shared by system and application sources."""

from __future__ import annotations

import logging
from collections.abc import Callable

from core.notifications.bus import notification_bus
from core.notifications.listener import NotificationListener
from core.notifications.models import Notification
from core.notifications.priority import score_notification
from core.notifications.store import NotificationStore
from memory.config_manager import get_notifications_config

LOGGER = logging.getLogger(__name__)


class NotificationCenter:
    """Apply preferences, score items, persist history, and forward new events."""

    def __init__(self, config: dict | None = None, store: NotificationStore | None = None,
                 listener: NotificationListener | None = None) -> None:
        self.config = dict(config or get_notifications_config())
        self.store = store or NotificationStore(
            max_entries=self.config["max_stored_notifications"]
        )
        self.listener = listener or NotificationListener()
        self._callbacks: list[Callable[[Notification], None]] = []
        self._unsubscribe = notification_bus.subscribe(self._receive)
        self.listener.on_notification(self._receive)

    def subscribe(self, callback: Callable[[Notification], None]) -> None:
        self._callbacks.append(callback)

    def start(self) -> bool:
        if not self.config.get("notifications_enabled", True):
            return False
        return self.listener.start()

    def stop(self) -> None:
        self.listener.stop()
        self._unsubscribe()

    def update_config(self, config: dict) -> None:
        self.config = dict(config)
        self.store.set_max_entries(self.config["max_stored_notifications"])

    def _receive(self, notification: Notification) -> None:
        if not self.config.get("notifications_enabled", True):
            return
        blocked = {app.casefold().strip() for app in self.config.get("blocked_apps", [])}
        if notification.app_name.casefold().strip() in blocked:
            LOGGER.debug("Dropped notification from blocked app %s", notification.app_name)
            return
        score_notification(notification, self.config)
        stored = self.store.add(notification)
        if stored is not notification:
            return
        for callback in tuple(self._callbacks):
            try:
                callback(notification)
            except Exception:
                LOGGER.exception("Notification center subscriber failed")
