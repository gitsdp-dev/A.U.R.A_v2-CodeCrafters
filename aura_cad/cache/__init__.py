"""Local, validated parametric CAD part cache."""

from __future__ import annotations

import ast
import logging
import re
import threading
from dataclasses import replace

from aura_cad.cache.matcher import PartMatcher
from aura_cad.cache.models import (
    CacheAction,
    CacheStats,
    PartResolution,
)
from aura_cad.cache.parametrizer import PartParametrizer
from aura_cad.cache.policy import PartCachePolicy
from aura_cad.cache.signature import (
    normalized_plan,
    part_function_name,
    plan_signature,
    spec_plan,
)
from aura_cad.cache.storage import PartCacheStorage
from aura_cad.errors import SandboxError
from aura_cad.models import CadSpec, PartPlan
from aura_cad.sandbox.validator import validate_script

LOGGER = logging.getLogger("aura.cad")


class PartCache:
    """Resolve reusable parts and persist only successful validated functions."""

    def __init__(
        self,
        storage: PartCacheStorage | None = None,
        matcher: PartMatcher | None = None,
        policy: PartCachePolicy | None = None,
    ) -> None:
        self.storage = storage or PartCacheStorage()
        self.matcher = matcher or PartMatcher()
        self.policy = policy or PartCachePolicy()
        self.parametrizer = PartParametrizer()
        self._stats_lock = threading.Lock()
        self._stats_snapshot = CacheStats()
        self._enabled_override: bool | None = None

    def resolve_from_spec(self, spec: CadSpec, job_id: str = "") -> PartResolution | None:
        plan = spec_plan(spec)
        resolution = self.resolve_parts([plan], job_id)
        return resolution[0] if resolution and resolution[0].action != CacheAction.GENERATE else None

    def resolve_parts(
        self, plans: list[PartPlan], job_id: str = ""
    ) -> list[PartResolution]:
        if not self._is_enabled(job_id):
            return self._generate(plans, "Part cache is disabled.")
        try:
            parts = self.storage.list_parts()
        except (OSError, ValueError) as exc:
            LOGGER.exception("[job=%s] CAD part cache read failed", job_id or "cache")
            return self._generate(plans, str(exc))
        occurrences: dict[str, int] = {}
        decisions = []
        for plan in plans:
            normalized = normalized_plan(plan)
            base = normalized.name.replace(" ", "_")
            occurrences[base] = occurrences.get(base, 0) + 1
            generated_name = part_function_name(base, occurrences[base])
            decisions.append(
                self._resolve_one(normalized, generated_name, parts, job_id)
            )
        return decisions

    def store_success(
        self,
        resolutions: list[PartResolution],
        script: str,
        job_id: str,
        repairs_used: int = 0,
        warnings: tuple[str, ...] = (),
    ) -> list[str]:
        stored = []
        for resolution in resolutions:
            if resolution.action != CacheAction.GENERATE:
                continue
            if not self.policy.function_is_used(script, resolution.function_name):
                continue
            part = self.policy.create_part(
                resolution.plan,
                resolution.function_name,
                script,
                validated=True,
                warnings=warnings,
                repairs_used=repairs_used,
            )
            if part is None:
                continue
            try:
                self.storage.save(part)
                stored.append(part.id)
            except (OSError, ValueError) as exc:
                LOGGER.exception(
                    "[job=%s] Could not store generated CAD part %s",
                    job_id or "cache",
                    part.name,
                )
        try:
            self.refresh_stats()
        except (OSError, ValueError):
            LOGGER.exception("[job=%s] Could not refresh CAD cache statistics", job_id)
        return stored

    def set_enabled(self, enabled: bool) -> None:
        self.storage.set_enabled(enabled)
        with self._stats_lock:
            self._enabled_override = bool(enabled)
        self.refresh_stats()

    def is_enabled(self, job_id: str = "") -> bool:
        return self._is_enabled(job_id)

    def adapt_refinement(
        self,
        resolutions: list[PartResolution],
        request: str,
        job_id: str,
    ) -> list[PartResolution] | None:
        if not self._is_enabled(job_id):
            return None
        words = {word.rstrip("s") for word in re.findall(r"[a-z0-9]+", request.casefold())}
        ignored = {"make", "bigger", "larger", "smaller", "thicker", "wider", "taller"}
        targets = words - ignored
        eligible = [
            item for item in resolutions
            if item.part is not None
            and item.action in {CacheAction.REUSE, CacheAction.ADAPT}
        ]
        selected = [
            item for item in eligible
            if targets & (
                set(item.part.tags)
                | set(item.part.name.split())
                | set(item.plan.name.casefold().split())
            )
        ]
        if not selected and len(eligible) == 1:
            selected = eligible
        if not selected:
            return None
        updates = {}
        for item in selected:
            result = self.parametrizer.adapt_refinement(
                item.part, item.parameters, request
            )
            if result.status == "needs_regeneration":
                LOGGER.info(
                    "[job=%s] Cached refinement needs regeneration: %s",
                    job_id,
                    result.reason,
                )
                return None
            updates[item.function_name] = result.parameters
        return [
            replace(
                item,
                action=CacheAction.ADAPT,
                parameters=updates[item.function_name],
            )
            if item.function_name in updates
            else item
            for item in resolutions
        ]

    def enabled_snapshot(self) -> bool:
        with self._stats_lock:
            if self._enabled_override is not None:
                return self._enabled_override
            return self._stats_snapshot.enabled

    def set_enabled_snapshot(self, enabled: bool) -> None:
        """Update the UI-visible choice immediately without touching cache files."""
        with self._stats_lock:
            self._enabled_override = bool(enabled)
            self._stats_snapshot = CacheStats(
                self._stats_snapshot.entry_count,
                self._stats_snapshot.size_bytes,
                bool(enabled),
                self._stats_snapshot.loaded,
            )

    def refresh_stats(self) -> CacheStats:
        stats = self.storage.stats()
        with self._stats_lock:
            if self._enabled_override is not None:
                stats = CacheStats(
                    stats.entry_count,
                    stats.size_bytes,
                    self._enabled_override,
                    stats.loaded,
                )
            self._stats_snapshot = stats
        return stats

    def stats_snapshot(self) -> CacheStats:
        with self._stats_lock:
            return self._stats_snapshot

    def clear(self) -> CacheStats:
        self.storage.clear()
        return self.refresh_stats()

    def _resolve_one(
        self, plan: PartPlan, function_name: str, parts, job_id: str
    ) -> PartResolution:
        signature = plan_signature(plan)
        for candidate in self.matcher.rank(signature, parts):
            part = candidate.part
            if not part.validated:
                self._quarantine(part.id, "cache policy rejected unvalidated part", job_id)
                continue
            if not self.policy.compatible(part):
                self._quarantine(part.id, "incompatible backend or prompt version", job_id)
                continue
            if not self._validate_cached_source(part, job_id):
                self._quarantine(part.id, "cached source failed re-validation", job_id)
                continue
            adaptation = self.parametrizer.adapt(part, plan.dimensions)
            if adaptation.status == "needs_regeneration":
                continue
            try:
                part = self.storage.touch(part.id) or part
            except (OSError, ValueError):
                LOGGER.exception("[job=%s] Could not update CAD cache usage", job_id or "cache")
            action = (
                CacheAction.REUSE
                if adaptation.status == "reuse"
                else CacheAction.ADAPT
            )
            return PartResolution(
                plan,
                part.function_name or function_name,
                action,
                part,
                adaptation.parameters,
                candidate.confidence,
            )
        return PartResolution(
            plan, function_name, CacheAction.GENERATE, reason="No compatible cached part."
        )

    def _validate_cached_source(self, part, job_id: str) -> bool:
        try:
            tree = validate_script(
                "import build123d as bd\nimport math\n"
                + part.source_code
                + "\n\ndef build():\n    return None\n"
            )
            if not any(
                isinstance(node, ast.FunctionDef)
                and node.name == part.function_name
                for node in tree.body
            ):
                return False
        except SandboxError:
            LOGGER.warning(
                "[job=%s] Cached CAD part %s failed sandbox re-validation",
                job_id or "cache",
                part.id,
            )
            return False
        return True

    def _quarantine(self, part_id: str, reason: str, job_id: str) -> None:
        try:
            self.storage.quarantine(part_id, reason)
        except (OSError, ValueError):
            LOGGER.exception(
                "[job=%s] Could not quarantine CAD cache entry %s",
                job_id or "cache",
                part_id,
            )

    def _is_enabled(self, job_id: str) -> bool:
        with self._stats_lock:
            if self._enabled_override is not None:
                return self._enabled_override
        try:
            enabled = self.storage.enabled()
            with self._stats_lock:
                self._stats_snapshot = CacheStats(
                    self._stats_snapshot.entry_count,
                    self._stats_snapshot.size_bytes,
                    enabled,
                    self._stats_snapshot.loaded,
                )
            return enabled
        except (OSError, ValueError):
            LOGGER.exception("[job=%s] Could not read CAD cache settings", job_id or "cache")
            return False

    def function_is_used(self, script: str, function_name: str) -> bool:
        return self.policy.function_is_used(script, function_name)

    @staticmethod
    def _generate(plans: list[PartPlan], reason: str) -> list[PartResolution]:
        occurrences: dict[str, int] = {}
        resolutions = []
        for plan in plans:
            normalized = normalized_plan(plan)
            name = normalized.name.replace(" ", "_")
            occurrences[name] = occurrences.get(name, 0) + 1
            resolutions.append(
                PartResolution(
                    normalized,
                    part_function_name(name, occurrences[name]),
                    CacheAction.GENERATE,
                    reason=reason,
                )
            )
        return resolutions


__all__ = ["PartCache"]
