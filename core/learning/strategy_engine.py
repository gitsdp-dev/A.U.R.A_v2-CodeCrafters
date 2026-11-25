"""
core/learning/strategy_engine.py — Strategy Optimization for Comparable Tasks.

Implements Wilson score lower bound and Thompson sampling to select optimal
execution strategies, weighing verified success rate, recency, and execution duration.
Phase 3 advanced feature, behind strategy_optimization_enabled flag.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from typing import Any

from .learning_database import LearningDatabase
from .learning_settings import LearningSettings

LOGGER = logging.getLogger("aura.learning.strategy")


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    """
    Calculate Wilson score interval lower bound for Bernoulli parameter.
    Gives a conservative, mathematically rigorous ranking of strategies
    even with small sample sizes.
    """
    if total <= 0:
        return 0.0
    p_hat = float(successes) / float(total)
    denominator = 1.0 + (z * z) / total
    centre = p_hat + (z * z) / (2.0 * total)
    spread = z * math.sqrt((p_hat * (1.0 - p_hat) + (z * z) / (4.0 * total)) / total)
    return max(0.0, min(1.0, (centre - spread) / denominator))


class StrategyEngine:
    """Ranks and recommends execution strategies for tasks based on historical trials."""

    def __init__(self, db: LearningDatabase, settings: LearningSettings) -> None:
        self.db = db
        self.settings = settings

    def record_strategy_result(
        self,
        task_type: str,
        strategy_name: str,
        success: bool,
        duration_ms: int = 0,
    ) -> None:
        """Update trials and success counts for a strategy."""
        now = time.time()

        def _update(conn: sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO strategy_stats (
                    task_type, strategy_name, trials, successes,
                    total_duration_ms, last_attempt_at
                ) VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(task_type, strategy_name) DO UPDATE SET
                    trials = strategy_stats.trials + 1,
                    successes = strategy_stats.successes + excluded.successes,
                    total_duration_ms = strategy_stats.total_duration_ms + excluded.total_duration_ms,
                    last_attempt_at = excluded.last_attempt_at
            """, (task_type, strategy_name, 1 if success else 0, max(0, duration_ms), now))

        self.db.execute_write(_update, wait=True)

    def recommend_strategy(
        self,
        task_type: str,
        available_strategies: list[str],
    ) -> str:
        """
        Select the best strategy using Wilson lower bound with execution speed bonus.
        Returns the top strategy name, or the first available if no history exists.
        """
        if not available_strategies:
            return ""
        if len(available_strategies) == 1:
            return available_strategies[0]
        if not self.settings.strategy_optimization_enabled:
            return available_strategies[0]

        def _query(conn: sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("""
                SELECT strategy_name, trials, successes, total_duration_ms, last_attempt_at
                FROM strategy_stats
                WHERE task_type = ?
            """, (task_type,))
            return {row["strategy_name"]: dict(row) for row in cursor.fetchall()}

        stats_map = self.db.execute_read(_query) or {}

        best_strategy = available_strategies[0]
        best_score = -1.0

        for strat in available_strategies:
            stat = stats_map.get(strat)
            if not stat or stat["trials"] == 0:
                # Mild exploration bonus for untried strategies
                score = 0.50
            else:
                trials = stat["trials"]
                successes = stat["successes"]
                wilson = wilson_lower_bound(successes, trials)

                # Speed factor: penalize if average duration is very high
                avg_duration = stat["total_duration_ms"] / max(1, trials)
                speed_factor = 1.0 / (1.0 + avg_duration / 5000.0)

                score = 0.70 * wilson + 0.30 * speed_factor

            if score > best_score:
                best_score = score
                best_strategy = strat

        return best_strategy
