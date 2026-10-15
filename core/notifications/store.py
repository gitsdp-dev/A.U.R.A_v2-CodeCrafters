"""Thread-safe in-memory notification history with atomic JSON persistence."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from core.notifications.models import Notification
from memory.config_manager import get_base_dir

LOGGER = logging.getLogger(__name__)


class NotificationStore:
    """Bounded notification history persisted as a private local JSON file."""

    def __init__(self, path: Path | None = None, max_entries: int = 200) -> None:
        self.path = path or (get_base_dir() / "memory" / "notifications.json")
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.RLock()
        self._items = self._load()

    def _load(self) -> list[Notification]:
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise ValueError("notification history must be a JSON list")
            items = []
            for row in records:
                if not isinstance(row, dict):
                    continue
                try:
                    items.append(Notification.from_dict(row))
                except (TypeError, ValueError, KeyError) as exc:
                    LOGGER.warning("Ignoring invalid notification history entry: %s", exc)
            return items[-self.max_entries:]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            LOGGER.warning("Could not load notification history %s: %s", self.path, exc)
            return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump([item.to_dict() for item in self._items], stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                LOGGER.warning("Could not remove temporary notification history %s",
                               temp_name)
            raise

    @staticmethod
    def _same_recent(left: Notification, right: Notification) -> bool:
        same_text = (
            left.app_name.casefold().strip(),
            left.title.casefold().strip(),
            left.body.casefold().strip(),
        ) == (
            right.app_name.casefold().strip(),
            right.title.casefold().strip(),
            right.body.casefold().strip(),
        )
        return same_text and abs(left.timestamp - right.timestamp) <= timedelta(minutes=2)

    def add(self, notification: Notification) -> Notification:
        """Insert a notification, returning the prior item for recent duplicates."""
        with self._lock:
            for existing in reversed(self._items):
                if self._same_recent(existing, notification):
                    return existing
            self._items.append(notification)
            self._items = self._items[-self.max_entries:]
            self._save()
            return notification

    def list(self, unread_only: bool = False) -> list[Notification]:
        with self._lock:
            items = list(self._items)
        return [item for item in items if not unread_only or not item.read]

    def mark_read(self, notification_id: str, read: bool = True) -> bool:
        with self._lock:
            for item in self._items:
                if item.id == notification_id:
                    item.read = read
                    self._save()
                    return True
        return False

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._save()

    def set_max_entries(self, max_entries: int) -> None:
        with self._lock:
            self.max_entries = max(1, int(max_entries))
            self._items = self._items[-self.max_entries:]
            self._save()
