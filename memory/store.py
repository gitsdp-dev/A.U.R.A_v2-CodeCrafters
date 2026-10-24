"""Thread-safe, atomic JSON persistence for long-term memory."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .models import (
    MemoryCategory,
    MemoryEntry,
    MemorySource,
    MemoryValidationError,
    utc_now,
)

LOGGER = logging.getLogger(__name__)


class MemoryStore:
    """Own and persist validated memory entries in one local JSON file."""

    def __init__(self, path: Path | None = None, backup_count: int = 5) -> None:
        if path is None:
            try:
                from .config_manager import get_memory_config
                settings = get_memory_config()
                path = Path(settings["file_path"]) if settings["file_path"] else Path(__file__).resolve().parent / "long_term.json"
                backup_count = settings["backup_count"]
            except (ImportError, OSError, TypeError, ValueError):
                path = Path(__file__).resolve().parent / "long_term.json"
        self.path = Path(path)
        self.backup_count = max(1, int(backup_count))
        self._lock = threading.RLock()
        self._entries: dict[str, MemoryEntry] = {}
        self._callbacks: list[Callable[[], None]] = []
        self._loaded = False

    def register_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked after a successful mutation."""
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def _notify(self) -> None:
        for callback in tuple(self._callbacks):
            try:
                callback()
            except Exception:
                LOGGER.exception("Memory change callback failed.")

    def _backup_dir(self) -> Path:
        return self.path.parent / "backups"

    def _backup(self) -> None:
        if not self.path.exists():
            return
        self._backup_dir().mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        shutil.copy2(self.path, self._backup_dir() / f"{self.path.name}.{stamp}.bak")
        backups = sorted(self._backup_dir().glob(f"{self.path.name}.*.bak"), key=lambda p: p.stat().st_mtime)
        for old in backups[:-self.backup_count]:
            old.unlink(missing_ok=True)

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        payload = {"schema_version": 1, "entries": [e.to_dict() for e in self._entries.values()]}
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _legacy_entries(self, data: dict) -> list[MemoryEntry]:
        entries: list[MemoryEntry] = []
        now = utc_now()
        for category, values in data.items():
            if not isinstance(values, dict):
                continue
            for title, raw in values.items():
                description = raw.get("value", "") if isinstance(raw, dict) else raw
                if not isinstance(description, str) or not description.strip():
                    continue
                mapped = MemoryCategory.LIFESTYLE
                if category in {"notes", "wishes", "preferences", "identity", "relationships", "projects"}:
                    mapped = MemoryCategory.LIFESTYLE
                try:
                    entries.append(MemoryEntry(
                        category=mapped, title=str(title), description=description,
                        source=MemorySource.AUTO, created_at=now, updated_at=now,
                    ))
                except (MemoryValidationError, TypeError, ValueError):
                    LOGGER.warning("Skipping invalid legacy memory entry during migration.")
        return entries

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                self._entries = {e.id: e for e in (MemoryEntry.from_dict(row) for row in data["entries"])}
                if data.get("schema_version") != 1:
                    LOGGER.warning("Loaded unknown memory schema read-safely without rewriting it.")
                return
            if isinstance(data, dict):
                migrated = self._legacy_entries(data)
                self._backup()
                self._entries = {e.id: e for e in migrated}
                self._write()
                LOGGER.info("Migrated legacy memory store: %d entries.", len(migrated))
                return
            raise ValueError("Memory root must be an object.")
        except (OSError, json.JSONDecodeError, ValueError, TypeError, MemoryValidationError):
            corrupt = self.path.with_name(
                f"{self.path.name}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            )
            try:
                self.path.replace(corrupt)
            except OSError:
                LOGGER.warning("Could not quarantine corrupt memory file.")
            for backup in sorted(self._backup_dir().glob(f"{self.path.name}.*.bak"), reverse=True):
                try:
                    data = json.loads(backup.read_text(encoding="utf-8"))
                    rows = data.get("entries", []) if isinstance(data, dict) else []
                    self._entries = {e.id: e for e in (MemoryEntry.from_dict(row) for row in rows)}
                    self._write()
                    LOGGER.warning("Recovered memory store from backup.")
                    return
                except (OSError, json.JSONDecodeError, ValueError, TypeError, MemoryValidationError):
                    continue
            self._entries = {}
            LOGGER.warning("Memory store was corrupt; starting with an empty store.")

    def list(self, category: MemoryCategory | None = None, source: MemorySource | None = None,
             include_disabled: bool = True) -> list[MemoryEntry]:
        """Return entries matching the optional filters."""
        with self._lock:
            self._load()
            return [e for e in self._entries.values()
                    if (category is None or e.category is category)
                    and (source is None or e.source is source)
                    and (include_disabled or e.enabled)]

    def get(self, entry_id: str) -> MemoryEntry | None:
        """Return an entry by id."""
        with self._lock:
            self._load()
            return self._entries.get(entry_id)

    def _mutated(self) -> None:
        self._write()
        self._notify()

    def add(self, entry: MemoryEntry) -> MemoryEntry:
        """Validate, persist, and return a new entry."""
        with self._lock:
            self._load()
            entry.validate()
            if any(e.category is entry.category and e.title.casefold() == entry.title.casefold()
                   for e in self._entries.values()):
                raise MemoryValidationError("An entry with this title already exists in this category.")
            self._entries[entry.id] = entry
            self._mutated()
            return entry

    def update(self, entry_id: str, **changes) -> MemoryEntry:
        """Update fields on an existing entry and persist the result."""
        with self._lock:
            self._load()
            current = self._entries.get(entry_id)
            if current is None:
                raise KeyError("Memory entry was not found.")
            data = current.to_dict()
            data.update(changes)
            data["id"] = entry_id
            data["updated_at"] = utc_now()
            updated = MemoryEntry.from_dict(data)
            if any(e.id != entry_id and e.category is updated.category
                       and e.title.casefold() == updated.title.casefold()
                       for e in self._entries.values()):
                raise MemoryValidationError("An entry with this title already exists in this category.")
            self._entries[entry_id] = updated
            self._mutated()
            return updated

    def delete(self, entry_id: str) -> bool:
        """Delete an entry, returning whether it existed."""
        with self._lock:
            self._load()
            if entry_id not in self._entries:
                return False
            del self._entries[entry_id]
            self._mutated()
            return True

    def bulk_import(self, entries: Iterable[MemoryEntry], mode: str = "merge") -> int:
        """Import entries in merge or replace mode."""
        if mode not in {"merge", "replace"}:
            raise ValueError("Import mode must be 'merge' or 'replace'.")
        with self._lock:
            self._load()
            self._backup()
            incoming = list(entries)
            for entry in incoming:
                entry.validate()
            if mode == "replace":
                self._entries.clear()
            for entry in incoming:
                self._entries[entry.id] = entry
            self._mutated()
            return len(incoming)

    def export_all(self) -> dict:
        """Return a JSON-serializable snapshot of the store."""
        with self._lock:
            self._load()
            return {"schema_version": 1, "entries": [e.to_dict() for e in self._entries.values()]}

    def clear(self, category: MemoryCategory | None = None) -> int:
        """Remove all entries, optionally limited to one category."""
        with self._lock:
            self._load()
            self._backup()
            ids = [i for i, e in self._entries.items() if category is None or e.category is category]
            for entry_id in ids:
                del self._entries[entry_id]
            if ids:
                self._mutated()
            return len(ids)
