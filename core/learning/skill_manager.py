"""
core/learning/skill_manager.py — Reusable Procedural Skills & Safety Gate.

Manages procedural routines composed of action sequences with usage metrics.
HARD RULE: Learned skills can never bypass confirmation or grant elevated permissions.
Any routine involving file deletion, system modification, or process termination is
strictly tagged with risk_level='risky'.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .experience_evaluator import ExperienceEvaluator
from .learning_database import LearningDatabase

LOGGER = logging.getLogger("aura.learning.skills")

# Risky action keywords that require explicit user confirmation
_RISKY_ACTIONS_PATTERN = re.compile(
    r"(?i)(?:\b|_)(delete|remove|rm|unlink|drop|format|terminate|kill|shutdown|system_settings|write_config)(?:\b|_)"
)


@dataclass
class ProceduralSkill:
    name: str
    category: str
    procedure: list[str | dict[str, Any]]
    conditions: dict[str, Any]
    success_count: int
    failure_count: int
    risk_level: str
    status: str
    scope: str
    confidence: float
    why_learned: str


class SkillManager:
    """Manages procedural skills stored in the learning database."""

    def __init__(self, db: LearningDatabase) -> None:
        self.db = db

    @classmethod
    def evaluate_risk(cls, procedure: list[Any]) -> str:
        """Classify risk level based on actions contained in the procedure."""
        text_repr = json.dumps(procedure)
        if _RISKY_ACTIONS_PATTERN.search(text_repr):
            return "risky"
        return "safe"

    def register_skill(
        self,
        name: str,
        procedure: list[Any],
        category: str = "general",
        conditions: dict[str, Any] | None = None,
        scope: str = "global",
        status: str = "provisional",
        why_learned: str = "",
    ) -> int | None:
        """Register a procedural skill with automated safety classification."""
        clean_name = name.strip().lower().replace(" ", "_")
        risk_level = self.evaluate_risk(procedure)
        conditions_json = json.dumps(conditions or {})
        procedure_json = json.dumps(procedure)
        now = time.time()

        confidence = 0.85 if status == "verified" else 0.60

        def _insert(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO learned_skills (
                    name, category, procedure, conditions, success_count,
                    failure_count, risk_level, status, scope, confidence,
                    evidence_count, source, created_at, updated_at,
                    last_used_at, reuse_count, why_learned
                ) VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?, 1, 'verified_execution', ?, ?, ?, 0, ?)
                ON CONFLICT(name) DO UPDATE SET
                    procedure = excluded.procedure,
                    conditions = excluded.conditions,
                    risk_level = excluded.risk_level,
                    scope = excluded.scope,
                    updated_at = excluded.updated_at
            """, (
                clean_name, category, procedure_json, conditions_json,
                risk_level, status, scope, confidence, now, now, now,
                why_learned or "Registered procedural routine."
            ))
            skill_id = cursor.lastrowid
            cursor.execute("""
                INSERT INTO learning_audit (table_name, record_id, action, old_status, new_status, reason, timestamp)
                VALUES ('learned_skills', ?, 'insert', NULL, ?, 'Created procedural skill', ?)
            """, (skill_id, status, now))
            return skill_id

        return self.db.execute_write(_insert, wait=True)

    def record_usage(self, name: str, success: bool = True) -> bool:
        """Update skill success/failure tallies and recompute Beta confidence."""
        clean_name = name.strip().lower().replace(" ", "_")
        now = time.time()

        def _update(conn: sqlite3.Connection) -> bool:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, success_count, failure_count FROM learned_skills WHERE name = ?
            """, (clean_name,))
            row = cursor.fetchone()
            if not row:
                return False

            skill_id, s_count, f_count = row["id"], row["success_count"], row["failure_count"]
            if success:
                s_count += 1
            else:
                f_count += 1

            new_conf = ExperienceEvaluator.update_beta_confidence(
                prior_alpha=2.0, prior_beta=2.0, successes=s_count, failures=f_count
            )
            # Demote if confidence dropped below threshold
            new_status = "provisional" if new_conf < 0.40 else "verified"

            cursor.execute("""
                UPDATE learned_skills
                SET success_count = ?, failure_count = ?, confidence = ?, status = ?,
                    reuse_count = reuse_count + 1, last_used_at = ?, updated_at = ?
                WHERE id = ?
            """, (s_count, f_count, new_conf, new_status, now, now, skill_id))
            return True

        return bool(self.db.execute_write(_update, wait=True))

    def get_skill(self, name: str) -> dict[str, Any] | None:
        """Retrieve a skill record by name."""
        clean_name = name.strip().lower().replace(" ", "_")

        def _get(conn: sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM learned_skills WHERE name = ?", (clean_name,))
            row = cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["procedure"] = json.loads(d["procedure"])
            except Exception:
                pass
            try:
                d["conditions"] = json.loads(d["conditions"])
            except Exception:
                pass
            return d

        return self.db.execute_read(_get)
