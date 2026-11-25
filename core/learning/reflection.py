"""
core/learning/reflection.py — Idle-Time Reflection Engine & Gemini Budget Gate.

Protects Gemini free-tier quota:
- ZERO LLM calls on hot path.
- Reflection is strictly opt-in behind a setting (default False).
- Token-bucket rate limiter with exponential backoff on 429/quota exhaustion.
- Daily budget persisted in SQLite with midnight reset.
- Provider-agnostic Protocol wrapping existing Gemini and Local engine entry points.
- Never calls Gemini directly — routes through core.gemini and core.llm_client.
"""
from __future__ import annotations

import logging
import random
import sqlite3
import time
from typing import Any, Protocol, runtime_checkable

from .learning_database import LearningDatabase
from .learning_settings import LearningSettings
from .redaction import redact_secrets

LOGGER = logging.getLogger("aura.learning.reflection")


@runtime_checkable
class ReflectionProvider(Protocol):
    """Protocol for LLM reflection backend."""
    def reflect(self, prompt: str) -> str:
        """Hand prompt to provider and return text reply without raising."""
        ...


class LocalReflectionAdapter:
    """Wraps local Ollama / LM Studio client via core.llm_client."""

    def reflect(self, prompt: str) -> str:
        try:
            from core.llm_client import call_llm
            resp = call_llm([{"role": "user", "content": prompt}], timeout=30)
            return str(resp.get("content") or "").strip()
        except Exception as exc:
            LOGGER.debug("Local reflection call skipped or failed: %s", exc)
            return ""


class GeminiReflectionAdapter:
    """Wraps existing core.gemini module using the cheapest suitable tier."""

    def reflect(self, prompt: str) -> str:
        try:
            from core import gemini
            # Use FAST tier (gemini-2.5-flash-lite / live throwaway)
            return gemini.text(prompt, tier=gemini.FAST, timeout_ms=10000, default="")
        except Exception as exc:
            LOGGER.debug("Gemini reflection call skipped or failed: %s", exc)
            return ""


class AutoReflectionAdapter:
    """Prefers Local engine if reachable; otherwise uses Gemini."""

    def __init__(self) -> None:
        self.local = LocalReflectionAdapter()
        self.gemini = GeminiReflectionAdapter()

    def reflect(self, prompt: str) -> str:
        # Check if local engine is reachable
        try:
            from core.llm_client import get_llm_provider, get_llm_settings, is_reachable
            provider = get_llm_provider()
            url, _ = get_llm_settings()
            if is_reachable(provider, url, timeout=1.0):
                res = self.local.reflect(prompt)
                if res:
                    return res
        except Exception:
            pass

        # Fallback to Gemini
        return self.gemini.reflect(prompt)


class LearningBudget:
    """Token-bucket rate limiter and daily budget gate persisted in SQLite."""

    def __init__(self, db: LearningDatabase, settings: LearningSettings) -> None:
        self.db = db
        self.settings = settings
        self._last_call_time = 0.0
        self._backoff_until = 0.0
        self._consecutive_errors = 0

    def can_reflect_now(self) -> bool:
        """Check if reflection is allowed within daily budget and rate limits."""
        if not self.settings.reflection_enabled or self.settings.max_reflections_per_day <= 0:
            return False

        now = time.time()
        # Cooldown / backoff check
        if now < self._backoff_until:
            return False

        # Interval check
        if (now - self._last_call_time) < self.settings.min_reflection_interval_sec:
            return False

        # Daily count check
        date_today = time.strftime("%Y-%m-%d")

        def _check_budget(conn: sqlite3.Connection) -> bool:
            cursor = conn.cursor()
            cursor.execute("SELECT reflections_used FROM budget_counters WHERE date_str = ?", (date_today,))
            row = cursor.fetchone()
            used = row[0] if row else 0
            return used < self.settings.max_reflections_per_day

        return bool(self.db.execute_read(_check_budget))

    def record_reflection_usage(self, success: bool, is_quota_error: bool = False) -> None:
        """Update rate limit timestamps and daily SQLite counters."""
        now = time.time()
        self._last_call_time = now

        if is_quota_error:
            self._consecutive_errors += 1
            # Exponential backoff: base 60s * 2^(errors-1) + jitter, capped at 15 minutes
            backoff_base = min(900.0, 60.0 * (2 ** (self._consecutive_errors - 1)))
            jitter = random.uniform(0.8, 1.2)
            self._backoff_until = now + (backoff_base * jitter)
            LOGGER.warning("LLM quota error recorded. Backing off reflection for %.1fs", self._backoff_until - now)
            return

        if success:
            self._consecutive_errors = 0
            self._backoff_until = 0.0

            date_today = time.strftime("%Y-%m-%d")

            def _increment(conn: sqlite3.Connection):
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO budget_counters (date_str, reflections_used, tokens_used, updated_at)
                    VALUES (?, 1, 0, ?)
                    ON CONFLICT(date_str) DO UPDATE SET
                        reflections_used = budget_counters.reflections_used + 1,
                        updated_at = excluded.updated_at
                """, (date_today, now))

            self.db.execute_write(_increment, wait=False)


class ReflectionEngine:
    """Turns raw execution experiences into generalized lessons during idle time."""

    def __init__(
        self,
        db: LearningDatabase,
        settings: LearningSettings,
        provider: ReflectionProvider | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.budget = LearningBudget(db, settings)
        self.provider = provider or AutoReflectionAdapter()

    def run_reflection_cycle(self) -> int:
        """Process unreflected experiences into clean lessons if budget permits."""
        if not self.budget.can_reflect_now():
            return 0

        # Find a provisional experience without user feedback that succeeded
        def _get_target(conn: sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, task_type, task_description, outcome FROM experiences
                WHERE status = 'provisional' AND success = 1
                ORDER BY created_at DESC LIMIT 1
            """)
            row = cursor.fetchone()
            return dict(row) if row else None

        target = self.db.execute_read(_get_target)
        if not target:
            return 0

        prompt = (
            f"Summarize the following task execution into one concise, actionable rule for future assistance.\n"
            f"Task: {target['task_description']}\n"
            f"Outcome: {target['outcome']}\n"
            f"Output one single sentence stating the lesson:"
        )

        try:
            lesson = self.provider.reflect(prompt)
            if lesson and len(lesson) > 5:
                clean_lesson = redact_secrets(lesson.strip().replace("\n", " "))
                now = time.time()

                def _save_lesson(conn: sqlite3.Connection):
                    conn.execute("""
                        UPDATE experiences SET lesson = ?, status = 'verified', updated_at = ? WHERE id = ?
                    """, (clean_lesson, now, target["id"]))
                    conn.execute("""
                        INSERT INTO learning_audit (table_name, record_id, action, old_status, new_status, reason, timestamp)
                        VALUES ('experiences', ?, 'promote', 'provisional', 'verified', 'Synthesized by reflection', ?)
                    """, (target["id"], now))

                self.db.execute_write(_save_lesson, wait=True)
                self.budget.record_reflection_usage(success=True)
                LOGGER.info("Reflection synthesized lesson for task %d: %s", target["id"], clean_lesson[:60])
                return 1
            else:
                self.budget.record_reflection_usage(success=False)
        except Exception as exc:
            is_quota = "quota" in str(exc).lower() or "429" in str(exc)
            self.budget.record_reflection_usage(success=False, is_quota_error=is_quota)

        return 0
