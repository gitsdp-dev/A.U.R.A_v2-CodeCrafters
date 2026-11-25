"""
Unit tests for Phase 2 of A.U.R.A Adaptive Intelligence subsystem.
Tests:
- Procedural skills registration & automated risk classification
- Skill usage tallies & Beta distribution confidence updates
- Failure analysis (temporary, environment-specific, tool failure)
- Expiry pruning and capacity cap enforcement (MemoryOptimizer)
- Evidence threshold promotion (provisional -> verified)
- Scheduler pause, resume, and stop lifecycle
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from core.learning import LearningEngine, LearningSettings
from core.learning.experience_evaluator import ExperienceEvaluator
from core.learning.learning_scheduler import LearningScheduler
from core.learning.memory_optimizer import MemoryOptimizer
from core.learning.skill_manager import SkillManager


@pytest.fixture
def engine(tmp_path: Path):
    db_file = tmp_path / "test_engine_phase2.db"
    settings = LearningSettings(
        enabled=True,
        max_experiences=100,  # Smaller for cap test
        max_preferences=50,
        max_skills=20,
        provisional_evidence_threshold=3,
    )
    eng = LearningEngine(db_path=db_file, settings=settings, start_scheduler=False)
    yield eng
    eng.shutdown()


# ── 1. Skills & Safety Gate ───────────────────────────────────────────────────

def test_skill_registration_and_risk_classification(engine: LearningEngine):
    sm = engine.skill_manager

    # Safe skill: reading directory
    safe_proc = ["list_files", "read_text"]
    safe_id = sm.register_skill("list_project_files", safe_proc, category="files")
    assert safe_id is not None
    skill_safe = sm.get_skill("list_project_files")
    assert skill_safe["risk_level"] == "safe"
    assert skill_safe["procedure"] == safe_proc

    # Risky skill: deletion / remove
    risky_proc = ["find_temp_files", "delete_file"]
    risky_id = sm.register_skill("clean_temp_files", risky_proc, category="cleanup")
    assert risky_id is not None
    skill_risky = sm.get_skill("clean_temp_files")
    assert skill_risky["risk_level"] == "risky"


def test_skill_usage_and_beta_confidence_update(engine: LearningEngine):
    sm = engine.skill_manager
    sm.register_skill("build_project", ["run_tests", "compile"], status="provisional")

    # Record 4 consecutive successes
    for _ in range(4):
        sm.record_usage("build_project", success=True)

    skill = sm.get_skill("build_project")
    assert skill["success_count"] == 5  # 1 initial + 4
    assert skill["failure_count"] == 0
    assert skill["confidence"] > 0.70

    # Record a failure
    sm.record_usage("build_project", success=False)
    skill_after_fail = sm.get_skill("build_project")
    assert skill_after_fail["failure_count"] == 1
    assert skill_after_fail["confidence"] < skill["confidence"]


# ── 2. Failure Analysis & Beta Scoring ────────────────────────────────────────

def test_failure_analysis_classification():
    evaluator = ExperienceEvaluator()

    # 1. Temporary rate limit
    out_temp = evaluator.classify_outcome(
        "web_search", "HTTP 429: Too Many Requests - rate limit exceeded", error_text="Resource exhausted"
    )
    assert not out_temp.success
    assert out_temp.failure_type == "temporary"
    assert out_temp.expires_at is not None
    assert out_temp.expires_at > time.time()

    # 2. Environment specific
    out_env = evaluator.classify_outcome(
        "run_command", "Command failed", error_text="git: command not found"
    )
    assert not out_env.success
    assert out_env.failure_type == "environment_specific"
    assert out_env.expires_at is not None

    # 3. Tool failure
    out_tool = evaluator.classify_outcome(
        "file_processor", "Exception", error_text="Tool 'file_processor' failed: TypeError: bad arg"
    )
    assert not out_tool.success
    assert out_tool.failure_type == "tool_failure"


def test_beta_confidence_math():
    # Symmetric prior (2, 2) -> 0.5
    conf_init = ExperienceEvaluator.update_beta_confidence(prior_alpha=2.0, prior_beta=2.0, successes=0, failures=0)
    assert conf_init == 0.5

    # 5 successes, 0 failures -> (2+5)/(2+2+5) = 7/9 = 0.778
    conf_high = ExperienceEvaluator.update_beta_confidence(prior_alpha=2.0, prior_beta=2.0, successes=5, failures=0)
    assert conf_high == 0.778

    # With contradiction penalty
    conf_penalized = ExperienceEvaluator.update_beta_confidence(
        prior_alpha=2.0, prior_beta=2.0, successes=5, failures=0, penalty=0.25
    )
    assert round(conf_penalized, 3) == round(0.778 - 0.25, 3)


# ── 3. Memory Consolidation, Expiry & Cap Eviction ─────────────────────────────

def test_memory_optimizer_expiry(engine: LearningEngine):
    now = time.time()
    # Insert an expired record and an active record
    def _seed(conn: sqlite3.Connection):
        # Expired 1 hour ago
        conn.execute("""
            INSERT INTO experiences (
                task_type, task_description, approach, outcome, success,
                lesson, status, scope, confidence, evidence_count, source,
                created_at, updated_at, last_used_at, expires_at
            ) VALUES ('test', 'expired task', '', '', 0, 'expired lesson', 'provisional', 'global', 0.3, 1, 'observation', ?, ?, ?, ?)
        """, (now - 7200, now - 7200, now - 7200, now - 3600))

        # Active record
        conn.execute("""
            INSERT INTO experiences (
                task_type, task_description, approach, outcome, success,
                lesson, status, scope, confidence, evidence_count, source,
                created_at, updated_at, last_used_at, expires_at
            ) VALUES ('test', 'active task', '', '', 1, 'active lesson', 'verified', 'global', 0.8, 1, 'verified_execution', ?, ?, ?, ?)
        """, (now, now, now, now + 3600))
    engine.db.execute_write(_seed)

    optimizer = MemoryOptimizer(engine.db, engine.settings)
    res = optimizer.optimize()
    assert res["expired_experiences"] >= 1

    # Verify only the active record remains
    def _check(conn: sqlite3.Connection):
        c = conn.cursor()
        c.execute("SELECT lesson FROM experiences")
        return [r[0] for r in c.fetchall()]
    lessons = engine.db.execute_read(_check)
    assert "active lesson" in lessons
    assert "expired lesson" not in lessons


def test_memory_optimizer_capacity_caps(engine: LearningEngine):
    # Max experiences is set to 100 in fixture
    optimizer = MemoryOptimizer(engine.db, engine.settings)

    # Insert 120 experiences
    def _seed_120(conn: sqlite3.Connection):
        now = time.time()
        for i in range(120):
            status = "verified" if i >= 60 else "provisional"
            conn.execute("""
                INSERT INTO experiences (
                    task_type, task_description, approach, outcome, success,
                    lesson, status, scope, confidence, evidence_count, source,
                    created_at, updated_at, last_used_at
                ) VALUES ('bulk', ?, '', '', 1, ?, ?, 'global', ?, 1, 'test', ?, ?, ?)
            """, (f"Task {i}", f"Lesson {i}", status, 0.5 + (i * 0.003), now, now, now))
    engine.db.execute_write(_seed_120)

    res = optimizer.optimize()
    assert res["evicted_experiences"] == 20

    # Total remaining must be exactly 100
    def _count(conn: sqlite3.Connection):
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM experiences")
        return c.fetchone()[0]
    count = engine.db.execute_read(_count)
    assert count == 100


# ── 4. Candidate Promotion Rule ───────────────────────────────────────────────

def test_evidence_threshold_promotion(engine: LearningEngine):
    # Insert provisional experience with evidence_count = 3 (threshold is 3)
    exp_id = engine.record_experience(
        task="Format code", outcome="Code formatted with black", success=True
    )
    def _bump_evidence(conn: sqlite3.Connection):
        conn.execute("UPDATE experiences SET evidence_count = 3 WHERE id = ?", (exp_id,))
    engine.db.execute_write(_bump_evidence)

    # Run evaluation
    res = engine.evaluate_learning()
    assert res.get("promoted_count", 0) >= 1

    # Verify status changed to verified
    def _check_status(conn: sqlite3.Connection):
        c = conn.cursor()
        c.execute("SELECT status, confidence FROM experiences WHERE id = ?", (exp_id,))
        return c.fetchone()
    row = engine.db.execute_read(_check_status)
    assert row["status"] == "verified"
    assert row["confidence"] >= 0.85


# ── 5. Scheduler Lifecycle ───────────────────────────────────────────────────

def test_scheduler_lifecycle():
    call_count = 0

    def _tick():
        nonlocal call_count
        call_count += 1

    scheduler = LearningScheduler(maintenance_callback=_tick, interval_seconds=1.0)
    scheduler.start()
    time.sleep(1.5)
    assert call_count >= 1

    # Test pause
    scheduler.pause()
    assert scheduler.is_paused()
    prev = call_count
    time.sleep(1.5)
    assert call_count == prev  # Must not run while paused

    # Test resume
    scheduler.resume()
    assert not scheduler.is_paused()
    time.sleep(1.5)
    assert call_count > prev

    # Test clean stop
    scheduler.stop()
