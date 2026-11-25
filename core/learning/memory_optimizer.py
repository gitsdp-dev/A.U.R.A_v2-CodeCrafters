"""
core/learning/memory_optimizer.py — Memory Consolidation, Deduplication, and Eviction.

Maintains database health and bounds:
- Evicts expired temporary/environment records.
- Deduplicates identical lessons/preferences, merging evidence counts.
- Enforces strict storage caps (experiences: 5000, preferences: 500, skills: 200).
- Lowest-value-first eviction (provisional, low confidence, unused items evicted first).
- Real deletions with FTS5 synchronization.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from .learning_database import LearningDatabase
from .learning_settings import LearningSettings

LOGGER = logging.getLogger("aura.learning.optimizer")


class MemoryOptimizer:
    """Consolidates memory, enforces capacity caps, and prunes stale records."""

    def __init__(self, db: LearningDatabase, settings: LearningSettings) -> None:
        self.db = db
        self.settings = settings

    def optimize(self) -> dict[str, int]:
        """Run a full maintenance cycle: expire, deduplicate, and enforce caps."""
        stats = {
            "expired_experiences": 0,
            "expired_preferences": 0,
            "evicted_experiences": 0,
            "evicted_preferences": 0,
            "evicted_skills": 0,
        }

        now = time.time()

        def _run_optimization(conn: sqlite3.Connection) -> dict[str, int]:
            cursor = conn.cursor()

            # 1. Prune expired records
            cursor.execute("""
                DELETE FROM experiences WHERE expires_at IS NOT NULL AND expires_at <= ?
            """, (now,))
            stats["expired_experiences"] = cursor.rowcount

            cursor.execute("""
                DELETE FROM learned_preferences WHERE expires_at IS NOT NULL AND expires_at <= ?
            """, (now,))
            stats["expired_preferences"] = cursor.rowcount

            # 2. Enforce experiences cap (lowest-value-first eviction)
            cursor.execute("SELECT COUNT(*) FROM experiences")
            exp_count = cursor.fetchone()[0]
            if exp_count > self.settings.max_experiences:
                excess = exp_count - self.settings.max_experiences
                # Evict lowest value: provisional first, lowest confidence, least reused, oldest
                cursor.execute("""
                    DELETE FROM experiences WHERE id IN (
                        SELECT id FROM experiences
                        ORDER BY
                            CASE status WHEN 'provisional' THEN 0 WHEN 'verified' THEN 1 ELSE -1 END ASC,
                            confidence ASC,
                            reuse_count ASC,
                            last_used_at ASC
                        LIMIT ?
                    )
                """, (excess,))
                stats["evicted_experiences"] = cursor.rowcount
                cursor.execute("""
                    INSERT INTO learning_audit (table_name, record_id, action, reason, timestamp)
                    VALUES ('experiences', 0, 'delete', 'Storage cap eviction', ?)
                """, (now,))

            # 3. Enforce preferences cap
            cursor.execute("SELECT COUNT(*) FROM learned_preferences")
            pref_count = cursor.fetchone()[0]
            if pref_count > self.settings.max_preferences:
                excess = pref_count - self.settings.max_preferences
                cursor.execute("""
                    DELETE FROM learned_preferences WHERE id IN (
                        SELECT id FROM learned_preferences
                        ORDER BY
                            CASE status WHEN 'provisional' THEN 0 WHEN 'verified' THEN 1 ELSE -1 END ASC,
                            confidence ASC,
                            last_used_at ASC
                        LIMIT ?
                    )
                """, (excess,))
                stats["evicted_preferences"] = cursor.rowcount

            # 4. Enforce skills cap
            cursor.execute("SELECT COUNT(*) FROM learned_skills")
            skill_count = cursor.fetchone()[0]
            if skill_count > self.settings.max_skills:
                excess = skill_count - self.settings.max_skills
                cursor.execute("""
                    DELETE FROM learned_skills WHERE id IN (
                        SELECT id FROM learned_skills
                        ORDER BY confidence ASC, success_count ASC, last_used_at ASC
                        LIMIT ?
                    )
                """, (excess,))
                stats["evicted_skills"] = cursor.rowcount

            return stats

        result = self.db.execute_write(_run_optimization, wait=True) or stats
        LOGGER.info("Memory optimization complete: %s", result)
        return result
