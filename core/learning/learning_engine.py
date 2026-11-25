"""
core/learning/learning_engine.py — Central Coordinator for A.U.R.A Adaptive Intelligence.

The only public entry point for the learning subsystem.
Provides safe, provider-agnostic, zero-hot-path-LLM learning.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .experience_collector import ExperienceCollector
from .experience_evaluator import ExperienceEvaluator
from .feedback_analyzer import FeedbackAnalyzer
from .learning_database import LearningDatabase, get_default_db_path
from .learning_settings import LearningSettings, load_settings
from .learning_scheduler import LearningScheduler
from .memory_optimizer import MemoryOptimizer
from .reflection import ReflectionEngine, ReflectionProvider
from .retrieval import ExperienceRetriever
from .skill_manager import SkillManager
from .strategy_engine import StrategyEngine

LOGGER = logging.getLogger("aura.learning.engine")


@runtime_checkable
class ReflectionProvider(Protocol):
    """Protocol for optional idle-time background LLM reflection."""
    def reflect(self, prompt: str) -> str:
        ...


class BaseLearningEngine:
    """Interface specification for LearningEngine implementations."""
    def retrieve_relevant_experience(self, task: str, scope: str = "global") -> str:
        raise NotImplementedError

    def record_experience(
        self,
        task: str,
        outcome: str,
        task_type: str = "general",
        approach: str = "",
        tools_used: list[str] | None = None,
        duration_ms: int = 0,
        success: bool = True,
        failure_type: str | None = None,
        error_text: str | None = None,
        user_feedback: str | None = None,
        scope: str = "global",
    ) -> int | None:
        raise NotImplementedError

    def process_feedback(self, feedback: str, current_project: str | None = None) -> dict[str, Any] | None:
        raise NotImplementedError

    def learn_preference(
        self,
        key: str,
        value: str,
        category: str = "general",
        scope: str = "global",
        confidence: float = 0.5,
        source: str = "observation",
        status: str = "provisional",
        why_learned: str = "",
    ) -> int | None:
        raise NotImplementedError

    def retrieve_skills(self, task: str, scope: str = "global") -> list[dict[str, Any]]:
        raise NotImplementedError

    def evaluate_learning(self) -> dict[str, Any]:
        raise NotImplementedError

    def recommend_strategy(self, task_type: str, available_strategies: list[str]) -> str:
        return available_strategies[0] if available_strategies else ""

    def record_strategy_outcome(self, task_type: str, strategy_name: str, success: bool, duration_ms: int = 0) -> None:
        pass

    def recommend_strategy(self, task_type: str, available_strategies: list[str]) -> str:
        return self.strategy_engine.recommend_strategy(task_type, available_strategies)

    def record_strategy_outcome(self, task_type: str, strategy_name: str, success: bool, duration_ms: int = 0) -> None:
        self.strategy_engine.record_strategy_result(task_type, strategy_name, success, duration_ms)

    def get_stats(self) -> dict[str, Any]:
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError


class NoOpLearningEngine(BaseLearningEngine):
    """Safe no-op implementation used when learning is disabled or fails to initialize."""

    def retrieve_relevant_experience(self, task: str, scope: str = "global") -> str:
        return ""

    def record_experience(
        self,
        task: str,
        outcome: str,
        task_type: str = "general",
        approach: str = "",
        tools_used: list[str] | None = None,
        duration_ms: int = 0,
        success: bool = True,
        failure_type: str | None = None,
        error_text: str | None = None,
        user_feedback: str | None = None,
        scope: str = "global",
    ) -> int | None:
        return None

    def process_feedback(self, feedback: str, current_project: str | None = None) -> dict[str, Any] | None:
        return None

    def learn_preference(
        self,
        key: str,
        value: str,
        category: str = "general",
        scope: str = "global",
        confidence: float = 0.5,
        source: str = "observation",
        status: str = "provisional",
        why_learned: str = "",
    ) -> int | None:
        return None

    def retrieve_skills(self, task: str, scope: str = "global") -> list[dict[str, Any]]:
        return []

    def evaluate_learning(self) -> dict[str, Any]:
        return {"status": "disabled"}

    def recommend_strategy(self, task_type: str, available_strategies: list[str]) -> str:
        return available_strategies[0] if available_strategies else ""

    def record_strategy_outcome(self, task_type: str, strategy_name: str, success: bool, duration_ms: int = 0) -> None:
        pass

    def recommend_strategy(self, task_type: str, available_strategies: list[str]) -> str:
        return self.strategy_engine.recommend_strategy(task_type, available_strategies)

    def record_strategy_outcome(self, task_type: str, strategy_name: str, success: bool, duration_ms: int = 0) -> None:
        self.strategy_engine.record_strategy_result(task_type, strategy_name, success, duration_ms)

    def get_stats(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "experiences_count": 0,
            "preferences_count": 0,
            "skills_count": 0,
            "reflections_today": 0,
        }

    def shutdown(self) -> None:
        pass


class LearningEngine(BaseLearningEngine):
    """
    Main coordinator for AURA's Adaptive Intelligence subsystem.
    Thread-safe, non-blocking, and database-backed.
    """

    def __init__(self, db_path: Path | None = None, settings: LearningSettings | None = None, start_scheduler: bool = True) -> None:
        self.settings = settings or load_settings()
        self.db = LearningDatabase(db_path)
        self.retriever = ExperienceRetriever(self.settings)
        self.collector = ExperienceCollector()
        self.analyzer = FeedbackAnalyzer()
        self.evaluator = ExperienceEvaluator()
        self.skill_manager = SkillManager(self.db)
        self.optimizer = MemoryOptimizer(self.db, self.settings)
        self.strategy_engine = StrategyEngine(self.db, self.settings)
        self.reflection_engine = ReflectionEngine(self.db, self.settings)

        self._retrieval_count = 0
        self._retrieval_hits = 0

        self.scheduler = LearningScheduler(self.run_maintenance)
        if start_scheduler and self.settings.enabled:
            self.scheduler.start()

        LOGGER.info("LearningEngine active at %s (enabled=%s)", self.db.db_path, self.settings.enabled)

    def pause_learning(self) -> None:
        """Pause scheduler maintenance during active tasks."""
        if hasattr(self, "scheduler"):
            self.scheduler.pause()

    def resume_learning(self) -> None:
        """Resume scheduler maintenance when system returns to idle."""
        if hasattr(self, "scheduler"):
            self.scheduler.resume()

    def run_maintenance(self) -> dict[str, Any]:
        """Consolidate memory, prune expired records, enforce caps, and evaluate provisional items."""
        opt_stats = self.optimizer.optimize()
        eval_stats = self.evaluate_learning()
        return {**opt_stats, **eval_stats}

    def retrieve_relevant_experience(self, task: str, scope: str = "global") -> str:
        """
        Hot path: Retrieve verified experience and format as an injection prompt block.
        Target latency: < 30ms.
        """
        if not self.settings.enabled:
            return ""

        start_time = time.perf_counter()
        self._retrieval_count += 1

        def _do_query(conn: sqlite3.Connection):
            return self.retriever.retrieve(
                conn,
                query=task,
                scope=scope,
                fts_available=self.db.fts_available,
            )

        items = self.db.execute_read(_do_query) or []
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        if items:
            self._retrieval_hits += 1
            # Update reuse_count asynchronously
            item_ids = [item["id"] for item in items if "id" in item and item.get("item_type") == "experience"]
            if item_ids:
                def _touch_reuse(conn: sqlite3.Connection):
                    now = time.time()
                    conn.executemany(
                        "UPDATE experiences SET reuse_count = reuse_count + 1, last_used_at = ? WHERE id = ?",
                        [(now, iid) for iid in item_ids],
                    )
                self.db.execute_write(_touch_reuse, wait=False)

        LOGGER.debug("Retrieved %d items for '%s' in %.2fms", len(items), task[:30], elapsed_ms)
        return self.retriever.format_injection_block(items)

    def record_experience(
        self,
        task: str,
        outcome: str,
        task_type: str = "general",
        approach: str = "",
        tools_used: list[str] | None = None,
        duration_ms: int = 0,
        success: bool = True,
        failure_type: str | None = None,
        error_text: str | None = None,
        user_feedback: str | None = None,
        scope: str = "global",
    ) -> int | None:
        """
        Record a structured task experience record.
        Zero LLM calls. Enforces secret redaction.
        """
        if not self.settings.enabled:
            return None

        # Determine confidence, failure classification, and expiry
        expires_at = None
        if not success or error_text:
            eval_out = self.evaluator.classify_outcome(
                task_type=task_type,
                result=outcome,
                error_text=error_text,
                is_user_correction=bool(user_feedback),
            )
            failure_type = failure_type or eval_out.failure_type
            confidence = eval_out.confidence
            expires_at = eval_out.expires_at
            status = eval_out.status
        else:
            status = "provisional"
            confidence = self.settings.base_confidence_execution

        record = self.collector.create_record(
            task_type=task_type,
            task_description=task,
            approach=approach,
            tools_used=tools_used,
            outcome=outcome,
            duration_ms=duration_ms,
            success=success,
            failure_type=failure_type,
            user_feedback=user_feedback,
            error_text=error_text,
            scope=scope,
            confidence=confidence,
            status=status,
            source="verified_execution" if success else "observation",
            expires_at=expires_at,
        )

        def _insert(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO experiences (
                    task_type, task_description, approach, outcome, success,
                    failure_type, user_feedback, lesson, duration_ms, status,
                    scope, confidence, evidence_count, source, created_at,
                    updated_at, last_used_at, expires_at, reuse_count, why_learned
                ) VALUES (
                    :task_type, :task_description, :approach, :outcome, :success,
                    :failure_type, :user_feedback, :lesson, :duration_ms, :status,
                    :scope, :confidence, :evidence_count, :source, :created_at,
                    :updated_at, :last_used_at, :expires_at, :reuse_count, :why_learned
                )
            """, record)
            inserted_id = cursor.lastrowid
            # Audit log
            cursor.execute("""
                INSERT INTO learning_audit (table_name, record_id, action, old_status, new_status, reason, timestamp)
                VALUES ('experiences', ?, 'insert', NULL, ?, 'Recorded task execution', ?)
            """, (inserted_id, record["status"], time.time()))
            return inserted_id

        return self.db.execute_write(_insert, wait=True)

    def process_feedback(self, feedback: str, current_project: str | None = None) -> dict[str, Any] | None:
        """
        Parse user utterance for corrections, explicit instructions, and praise.
        Explicit instructions ('always', 'never', 'remember') are verified immediately.
        """
        if not self.settings.enabled:
            return None

        analyzed = self.analyzer.analyze(feedback, current_project=current_project)
        if not analyzed.is_feedback:
            return None

        # Log feedback
        def _save_feedback_and_apply(conn: sqlite3.Connection) -> dict[str, Any]:
            cursor = conn.cursor()
            now = time.time()
            cursor.execute("""
                INSERT INTO feedback (raw_text, feedback_type, sentiment, target_task, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (analyzed.raw_text, analyzed.feedback_type, analyzed.sentiment, analyzed.target_rule, now))

            res = {
                "feedback_type": analyzed.feedback_type,
                "scope": analyzed.scope,
                "applied": False,
            }

            # If explicit instruction, promote directly to verified preference
            if analyzed.feedback_type == "instruction" and analyzed.target_rule:
                key = analyzed.key_preference or "user_rule"
                status = "verified" if analyzed.auto_verify else "provisional"
                confidence = analyzed.confidence

                cursor.execute("""
                    INSERT INTO learned_preferences (
                        pref_key, pref_value, category, status, scope,
                        confidence, evidence_count, source, created_at,
                        updated_at, last_used_at, why_learned
                    ) VALUES (?, ?, 'instruction', ?, ?, ?, 1, 'user_correction', ?, ?, ?, ?)
                    ON CONFLICT(pref_key) DO UPDATE SET
                        pref_value = excluded.pref_value,
                        status = excluded.status,
                        confidence = excluded.confidence,
                        evidence_count = learned_preferences.evidence_count + 1,
                        updated_at = excluded.updated_at,
                        why_learned = excluded.why_learned
                """, (
                    key, analyzed.target_rule, status, analyzed.scope,
                    confidence, now, now, now, analyzed.why_learned
                ))
                pref_id = cursor.lastrowid
                cursor.execute("""
                    INSERT INTO learning_audit (table_name, record_id, action, old_status, new_status, reason, timestamp)
                    VALUES ('learned_preferences', ?, 'promote', 'provisional', ?, ?, ?)
                """, (pref_id, status, analyzed.why_learned, now))
                res["applied"] = True
                res["key"] = key
                res["status"] = status

            elif analyzed.feedback_type == "correction":
                # Contradiction: penalize and demote matching verified items
                cursor.execute("""
                    SELECT id, confidence, status, lesson FROM experiences
                    WHERE status = 'verified'
                    ORDER BY last_used_at DESC LIMIT 5
                """)
                for row in cursor.fetchall():
                    exp_id = row["id"]
                    new_conf = max(0.1, float(row["confidence"]) - self.settings.contradiction_penalty)
                    new_status = "provisional" if new_conf < self.settings.demotion_threshold else "verified"
                    cursor.execute("""
                        UPDATE experiences SET confidence = ?, status = ?, updated_at = ? WHERE id = ?
                    """, (new_conf, new_status, now, exp_id))
                    cursor.execute("""
                        INSERT INTO learning_audit (table_name, record_id, action, old_status, new_status, reason, timestamp)
                        VALUES ('experiences', ?, 'demote', 'verified', ?, 'User contradicted experience', ?)
                    """, (exp_id, new_status, now))

                res["demoted_recent"] = True

            return res

        return self.db.execute_write(_save_feedback_and_apply, wait=True)

    def learn_preference(
        self,
        key: str,
        value: str,
        category: str = "general",
        scope: str = "global",
        confidence: float = 0.5,
        source: str = "observation",
        status: str = "provisional",
        why_learned: str = "",
    ) -> int | None:
        """Add or update a preference with confidence and scope."""
        if not self.settings.enabled:
            return None

        now = time.time()

        def _upsert(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO learned_preferences (
                    pref_key, pref_value, category, status, scope,
                    confidence, evidence_count, source, created_at,
                    updated_at, last_used_at, why_learned
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(pref_key) DO UPDATE SET
                    pref_value = excluded.pref_value,
                    status = excluded.status,
                    confidence = excluded.confidence,
                    evidence_count = learned_preferences.evidence_count + 1,
                    updated_at = excluded.updated_at,
                    why_learned = excluded.why_learned
            """, (key, value, category, status, scope, confidence, source, now, now, now, why_learned))
            return cursor.lastrowid

        return self.db.execute_write(_upsert, wait=True)

    def retrieve_skills(self, task: str, scope: str = "global") -> list[dict[str, Any]]:
        """Retrieve verified procedural skills applicable to the task."""
        if not self.settings.enabled:
            return []

        def _get_skills(conn: sqlite3.Connection):
            cursor = conn.execute("""
                SELECT id, name, category, procedure, conditions, success_count, failure_count, confidence, scope
                FROM learned_skills
                WHERE status = 'verified' AND (scope = ? OR scope = 'global')
                ORDER BY confidence DESC
                LIMIT 10
            """, (scope,))
            return [dict(r) for r in cursor.fetchall()]

        return self.db.execute_read(_get_skills) or []

    def evaluate_learning(self) -> dict[str, Any]:
        """Evaluate provisional items; promote candidates that meet the evidence threshold."""
        if not self.settings.enabled:
            return {"status": "disabled"}

        now = time.time()
        promoted = 0

        def _eval(conn: sqlite3.Connection) -> dict[str, Any]:
            nonlocal promoted
            cursor = conn.cursor()
            # Promote provisional experiences with evidence_count >= threshold and success = 1
            cursor.execute("""
                SELECT id, evidence_count FROM experiences
                WHERE status = 'provisional' AND success = 1 AND evidence_count >= ?
            """, (self.settings.provisional_evidence_threshold,))
            for row in cursor.fetchall():
                exp_id = row["id"]
                cursor.execute("""
                    UPDATE experiences SET status = 'verified', confidence = 0.85, updated_at = ?
                    WHERE id = ?
                """, (now, exp_id))
                cursor.execute("""
                    INSERT INTO learning_audit (table_name, record_id, action, old_status, new_status, reason, timestamp)
                    VALUES ('experiences', ?, 'promote', 'provisional', 'verified', 'Met evidence threshold', ?)
                """, (exp_id, now))
                promoted += 1

            return {"promoted_count": promoted}

        return self.db.execute_write(_eval, wait=True) or {}

    def recommend_strategy(self, task_type: str, available_strategies: list[str]) -> str:
        return available_strategies[0] if available_strategies else ""

    def record_strategy_outcome(self, task_type: str, strategy_name: str, success: bool, duration_ms: int = 0) -> None:
        pass

    def recommend_strategy(self, task_type: str, available_strategies: list[str]) -> str:
        return self.strategy_engine.recommend_strategy(task_type, available_strategies)

    def record_strategy_outcome(self, task_type: str, strategy_name: str, success: bool, duration_ms: int = 0) -> None:
        self.strategy_engine.record_strategy_result(task_type, strategy_name, success, duration_ms)

    def get_stats(self) -> dict[str, Any]:
        """Aggregate statistics for UI review dialog and observability."""
        def _calc(conn: sqlite3.Connection) -> dict[str, Any]:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM experiences")
            total_exp = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM experiences WHERE status = 'verified'")
            verified_exp = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM experiences WHERE status = 'provisional'")
            provisional_exp = c.fetchone()[0]

            c.execute("SELECT COUNT(*) FROM learned_preferences")
            total_pref = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM learned_preferences WHERE status = 'verified'")
            verified_pref = c.fetchone()[0]

            c.execute("SELECT COUNT(*) FROM learned_skills")
            total_skills = c.fetchone()[0]

            c.execute("SELECT COUNT(*) FROM feedback")
            total_feedback = c.fetchone()[0]

            # Today's budget
            date_today = time.strftime("%Y-%m-%d")
            c.execute("SELECT reflections_used FROM budget_counters WHERE date_str = ?", (date_today,))
            row = c.fetchone()
            reflections_today = row[0] if row else 0

            hit_rate = (self._retrieval_hits / self._retrieval_count) if self._retrieval_count > 0 else 0.0

            return {
                "enabled": self.settings.enabled,
                "experiences_total": total_exp,
                "experiences_verified": verified_exp,
                "experiences_provisional": provisional_exp,
                "preferences_total": total_pref,
                "preferences_verified": verified_pref,
                "skills_total": total_skills,
                "feedback_total": total_feedback,
                "reflections_today": reflections_today,
                "retrieval_count": self._retrieval_count,
                "retrieval_hit_rate": round(hit_rate, 3),
            }

        return self.db.execute_read(_calc) or {}

    def shutdown(self) -> None:
        """Close DB writer and clean up worker threads."""
        if hasattr(self, "scheduler"):
            try:
                self.scheduler.stop()
            except Exception:
                pass
        self.db.close()
