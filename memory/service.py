"""Qt-free facade for AI tools, retrieval, and UI consumers."""

from __future__ import annotations

import logging
import threading
from difflib import SequenceMatcher

from .models import MemoryCategory, MemoryEntry, MemorySource, Priority, RemindMode, utc_now
from .retrieval import build_memory_context, match_trigger, score_entry
from .protocols import normalize_protocol_command, split_protocol_tasks
from .store import MemoryStore

LOGGER = logging.getLogger(__name__)


class MemoryService:
    """Coordinate memory storage and deterministic retrieval."""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self.store = store or MemoryStore()
        self._dirty: dict[str, MemoryEntry] = {}
        self._dirty_lock = threading.RLock()

    def context(self, utterance: str = "", char_budget: int = 3500) -> str:
        """Return context and record usage for entries included in it."""
        entries = self.store.list(include_disabled=False)
        context = build_memory_context(entries, utterance, char_budget)
        with self._dirty_lock:
            for entry in entries:
                if entry.title in context:
                    self._dirty[entry.id] = entry
                    entry.use_count += 1
                    entry.last_used_at = utc_now()
        return context

    def flush_usage(self) -> None:
        """Persist usage counters collected during context assembly."""
        with self._dirty_lock:
            dirty = list(self._dirty.values())
            self._dirty.clear()
        for entry in dirty:
            try:
                self.store.update(entry.id, use_count=entry.use_count, last_used_at=entry.last_used_at)
            except KeyError:
                continue

    def save(self, category: str, title: str, description: str, priority: str = "normal",
             source: MemorySource = MemorySource.VOICE) -> MemoryEntry:
        """Create or fuzzy-deduplicate an entry."""
        cat = MemoryCategory(category.casefold())
        matches = [e for e in self.store.list(category=cat) if
                   SequenceMatcher(None, e.title.casefold(), title.casefold().strip()).ratio() >= 0.9]
        if matches:
            return self.store.update(matches[0].id, description=description, priority=priority, source=source.value)
        return self.store.add(MemoryEntry(category=cat, title=title, description=description,
                                          priority=Priority(priority.casefold()), source=source))

    def update_by_title(self, title: str, category: str | None = None, **changes) -> MemoryEntry:
        """Update the unique entry matching a title."""
        entries = self.store.list(category=MemoryCategory(category.casefold()) if category else None)
        matches = [e for e in entries if e.title.casefold() == title.casefold()]
        if len(matches) != 1:
            raise KeyError("A unique memory with that title was not found.")
        return self.store.update(matches[0].id, **changes)

    def delete_by_title(self, title: str, category: str | None = None) -> bool:
        """Delete an entry by its title."""
        entries = self.store.list(category=MemoryCategory(category.casefold()) if category else None)
        matches = [e for e in entries if e.title.casefold() == title.casefold()]
        if len(matches) != 1:
            raise KeyError("A unique memory with that title was not found.")
        return self.store.delete(matches[0].id)

    def recall(self, query: str, limit: int = 8) -> list[MemoryEntry]:
        """Return the highest-scoring relevant entries."""
        return sorted(self.store.list(include_disabled=False),
                      key=lambda e: (-score_entry(e, query), e.id))[:max(1, limit)]

    def trigger(self, utterance: str) -> MemoryEntry | None:
        """Find an unambiguous executable trigger."""
        return match_trigger(self.store.list(include_disabled=False), utterance)

    def resolve_protocol(self, title_or_command: str) -> MemoryEntry | None:
        """Resolve any enabled protocol by title, alias, or spoken command."""
        protocols = [
            entry for entry in self.store.list(
                category=MemoryCategory.PROTOCOL, include_disabled=False
            )
        ]
        query = normalize_protocol_command(title_or_command)
        candidates = {
            query,
            query.removesuffix(" protocol").strip(),
            query.removeprefix("protocol ").strip(),
        }

        def protocol_names(entry: MemoryEntry) -> set[str]:
            names = {entry.title.casefold(), *(alias.casefold() for alias in entry.aliases)}
            names.update(
                name.removesuffix(" protocol").strip()
                for name in tuple(names)
                if name.endswith(" protocol")
            )
            return {normalize_protocol_command(name) for name in names if name}

        for entry in protocols:
            names = protocol_names(entry)
            if any(candidate and candidate in names for candidate in candidates):
                return entry
        contained = [
            entry for entry in protocols
            if any(
                candidate
                and (name.startswith(candidate) or candidate.startswith(name))
                for candidate in candidates
                for name in protocol_names(entry)
            )
        ]
        if len(contained) == 1:
            return contained[0]
        scored: list[tuple[float, MemoryEntry]] = []
        for entry in protocols:
            best = max(
                SequenceMatcher(None, candidate, name).ratio()
                for candidate in candidates if candidate
                for name in protocol_names(entry)
            )
            if best >= 0.72:
                scored.append((best, entry))
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] > 0.08):
            return scored[0][1]
        return match_trigger(protocols, title_or_command)

    def protocol_catalog(self) -> str:
        """Return a concise catalog of enabled custom protocol names."""
        return ", ".join(
            entry.title for entry in self.store.list(
                category=MemoryCategory.PROTOCOL, include_disabled=False
            )
        )

    def protocol_tasks(self, entry: MemoryEntry) -> list[str]:
        """Return the ordered tasks stored in a protocol description."""
        if entry.category is not MemoryCategory.PROTOCOL:
            raise ValueError("Only protocol memories have executable tasks.")
        tasks = split_protocol_tasks(entry.description)
        if not tasks:
            raise ValueError("This protocol has no executable tasks.")
        return tasks
