"""Atomic JSON persistence for validated local CAD parts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aura_cad.cache.models import CachedPart, CacheStats
from aura_cad.config import (
    PART_CACHE_DIR,
    PART_CACHE_ENABLED_DEFAULT,
    PART_CACHE_MAX_BYTES,
    PART_CACHE_MAX_ENTRIES,
    PART_CACHE_SCHEMA_VERSION,
)

LOGGER = logging.getLogger("aura.cad")


class PartCacheStorage:
    """Keep cache reads resilient and serialize concurrent writes per instance."""

    def __init__(
        self,
        root: Path = PART_CACHE_DIR,
        max_bytes: int = PART_CACHE_MAX_BYTES,
        max_entries: int = PART_CACHE_MAX_ENTRIES,
    ) -> None:
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.quarantine_dir = self.root / "quarantine"
        self.index_path = self.root / "index.json"
        self.max_bytes = max(1, int(max_bytes))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.RLock()
        self._loaded = False
        self._enabled = PART_CACHE_ENABLED_DEFAULT
        self._parts: dict[str, CachedPart] = {}

    def list_parts(self) -> list[CachedPart]:
        with self._lock:
            self._ensure_loaded()
            return list(self._parts.values())

    def get(self, part_id: str) -> CachedPart | None:
        with self._lock:
            self._ensure_loaded()
            return self._parts.get(part_id)

    def save(self, part: CachedPart) -> None:
        if not part.validated:
            raise ValueError("Unvalidated CAD parts cannot be persisted.")
        digest = hashlib.sha256(part.source_code.encode("utf-8")).hexdigest()
        if digest != part.code_sha256:
            raise ValueError("CAD cache source integrity does not match.")
        with self._lock:
            self._ensure_loaded()
            self._write_entry(part)
            self._parts[part.id] = part
            self._evict()
            self._write_index()

    def touch(self, part_id: str) -> CachedPart | None:
        with self._lock:
            self._ensure_loaded()
            part = self._parts.get(part_id)
            if part is None:
                return None
            updated = replace(
                part,
                last_used_at=datetime.now(timezone.utc).isoformat(),
                use_count=part.use_count + 1,
            )
            self._write_entry(updated)
            self._parts[part_id] = updated
            self._write_index()
            return updated

    def quarantine(self, part_id: str, reason: str) -> None:
        with self._lock:
            self._ensure_loaded()
            self._parts.pop(part_id, None)
            self._quarantine_file(self._entry_path(part_id), reason)
            self._write_index()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._ensure_loaded()
            self._enabled = bool(enabled)
            self._write_index()

    def enabled(self) -> bool:
        with self._lock:
            self._ensure_loaded()
            return self._enabled

    def clear(self) -> None:
        with self._lock:
            self._ensure_loaded()
            failures = []
            for directory in (self.entries_dir, self.quarantine_dir):
                for path in directory.glob("*.json"):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError:
                        LOGGER.exception("Could not remove CAD cache file %s", path.name)
                        failures.append(path)
            if failures:
                self._loaded = False
                self._parts.clear()
                raise OSError(
                    f"Could not remove {len(failures)} CAD cache file(s)."
                )
            self._parts.clear()
            self._write_index()

    def stats(self) -> CacheStats:
        with self._lock:
            self._ensure_loaded()
            size = sum(
                path.stat().st_size
                for path in self.entries_dir.glob("*.json")
                if path.is_file()
            )
            return CacheStats(len(self._parts), size, self._enabled, True)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._parts.clear()
        indexed_ids = self._read_index()
        if indexed_ids is None:
            indexed_ids = []
        known = set()
        for part_id in indexed_ids:
            if self._load_entry(part_id):
                known.add(part_id)
        if self.entries_dir.exists():
            for path in self.entries_dir.glob("*.json"):
                part_id = path.stem
                if part_id not in known:
                    if self._load_entry(part_id):
                        known.add(part_id)
        self._loaded = True
        self._write_index()

    def _read_index(self) -> list[str] | None:
        if not self.index_path.exists():
            return None
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if data.get("schema_version") != PART_CACHE_SCHEMA_VERSION:
                raise ValueError("Cache index schema version changed.")
            ids = data.get("entries")
            if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                raise ValueError("Cache index entries are invalid.")
            enabled = data.get("enabled", PART_CACHE_ENABLED_DEFAULT)
            if not isinstance(enabled, bool):
                raise ValueError("Cache enabled setting is invalid.")
            self._enabled = enabled
            return ids
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self._quarantine_file(self.index_path, str(exc))
            return None

    def _load_entry(self, part_id: str) -> bool:
        if not part_id or any(char not in "0123456789abcdef" for char in part_id):
            return False
        path = self._entry_path(part_id)
        try:
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            if wrapper.get("schema_version") != PART_CACHE_SCHEMA_VERSION:
                raise ValueError("Cache entry schema version changed.")
            part = CachedPart.from_dict(wrapper["part"])
            if part.id != part_id or not part.validated:
                raise ValueError("Cached part identity or validation state is invalid.")
            if self._sha256(part.source_code) != part.code_sha256:
                raise ValueError("Cached part integrity check failed.")
            self._parts[part_id] = part
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self._quarantine_file(path, str(exc))
            return False

    def _write_entry(self, part: CachedPart) -> None:
        wrapper = {
            "schema_version": PART_CACHE_SCHEMA_VERSION,
            "part": part.as_dict(),
        }
        self._atomic_json(self._entry_path(part.id), wrapper)

    def _write_index(self) -> None:
        data = {
            "schema_version": PART_CACHE_SCHEMA_VERSION,
            "enabled": self._enabled,
            "entries": sorted(self._parts),
        }
        self._atomic_json(self.index_path, data)

    def _evict(self) -> None:
        def entry_size(part_id: str) -> int:
            path = self._entry_path(part_id)
            return path.stat().st_size if path.is_file() else 0

        total_size = sum(entry_size(part_id) for part_id in self._parts)
        ordered = sorted(
            self._parts.values(),
            key=lambda part: (part.last_used_at, part.created_at),
        )
        while ordered and (
            len(self._parts) > self.max_entries or total_size > self.max_bytes
        ):
            oldest = ordered.pop(0)
            total_size -= entry_size(oldest.id)
            self._parts.pop(oldest.id, None)
            try:
                self._entry_path(oldest.id).unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("Could not evict CAD cache entry %s", oldest.id)

    def _entry_path(self, part_id: str) -> Path:
        if not part_id or any(char not in "0123456789abcdef" for char in part_id):
            raise ValueError("Invalid CAD cache identifier.")
        return self.entries_dir / f"{part_id}.json"

    def _quarantine_file(self, path: Path, reason: str) -> None:
        if not path.exists():
            return
        try:
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            destination = self.quarantine_dir / f"{path.stem}-{stamp}-{uuid4().hex[:8]}.json"
            os.replace(path, destination)
            LOGGER.warning(
                "Quarantined CAD cache file %s: %s", path.name, reason[:240]
            )
        except OSError:
            LOGGER.exception("Could not quarantine CAD cache file %s", path.name)

    @staticmethod
    def _sha256(source: str) -> str:
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    @staticmethod
    def _atomic_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=True, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
