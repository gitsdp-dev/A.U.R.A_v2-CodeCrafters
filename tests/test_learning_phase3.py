"""
Unit tests for Phase 3 of A.U.R.A Adaptive Intelligence subsystem.
Tests:
- Strategy optimization (Wilson lower bound & speed bonus)
- LearningBudget gate (token bucket, daily counters, 429 exponential backoff)
- Idle-time ReflectionEngine with a FakeProvider
- UI Review Dialog data loading and operations
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from core.learning import LearningEngine, LearningSettings
from core.learning.reflection import (
    AutoReflectionAdapter,
    GeminiReflectionAdapter,
    LearningBudget,
    LocalReflectionAdapter,
    ReflectionEngine,
    ReflectionProvider,
)
from core.learning.strategy_engine import StrategyEngine, wilson_lower_bound


class FakeReflectionProvider:
    """Mock reflection provider for deterministic testing."""
    def __init__(self, response: str = "Always verify file paths before reading.") -> None:
        self.response = response
        self.calls: list[str] = []

    def reflect(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.response


@pytest.fixture
def engine(tmp_path: Path):
    db_file = tmp_path / "test_engine_phase3.db"
    settings = LearningSettings(
        enabled=True,
        reflection_enabled=True,
        max_reflections_per_day=5,
        min_reflection_interval_sec=0.1,  # Fast for testing
        strategy_optimization_enabled=True,
    )
    eng = LearningEngine(db_path=db_file, settings=settings, start_scheduler=False)
    yield eng
    eng.shutdown()


# ── 1. Strategy Optimization & Wilson Lower Bound ─────────────────────────────

def test_wilson_lower_bound_math():
    # 0 trials -> 0.0
    assert wilson_lower_bound(0, 0) == 0.0

    # 10 successes out of 10 trials
    w_perfect_10 = wilson_lower_bound(10, 10)
    assert 0.70 < w_perfect_10 < 1.0

    # 100 successes out of 100 trials (higher confidence than 10/10)
    w_perfect_100 = wilson_lower_bound(100, 100)
    assert w_perfect_100 > w_perfect_10

    # 5 out of 10 vs 50 out of 100
    w_5_10 = wilson_lower_bound(5, 10)
    w_50_100 = wilson_lower_bound(50, 100)
    assert w_50_100 > w_5_10


def test_strategy_engine_recommendations(engine: LearningEngine):
    strat = engine.strategy_engine

    # Record 10 successes for 'fast_search' with 100ms duration
    for _ in range(10):
        strat.record_strategy_result("search", "fast_search", success=True, duration_ms=100)

    # Record 5 failures for 'slow_search' with 2000ms duration
    for _ in range(5):
        strat.record_strategy_result("search", "slow_search", success=False, duration_ms=2000)

    # Fast search must be recommended over slow search
    best = strat.recommend_strategy("search", ["slow_search", "fast_search"])
    assert best == "fast_search"

    # Single option returns immediately
    assert strat.recommend_strategy("search", ["only_option"]) == "only_option"


# ── 2. Learning Budget & 429 Exponential Backoff ──────────────────────────────

def test_learning_budget_limiter_and_daily_cap(engine: LearningEngine):
    budget = LearningBudget(engine.db, engine.settings)
    assert budget.can_reflect_now()

    # Record usage up to max_reflections_per_day (5)
    for _ in range(5):
        assert budget.can_reflect_now()
        budget.record_reflection_usage(success=True)
        time.sleep(0.12)  # Wait past min_reflection_interval_sec (0.1)

    # Cap reached: must return False
    assert not budget.can_reflect_now()


def test_learning_budget_429_backoff(engine: LearningEngine):
    budget = LearningBudget(engine.db, engine.settings)
    assert budget.can_reflect_now()

    # Simulate 429 quota exhaustion
    budget.record_reflection_usage(success=False, is_quota_error=True)
    # Must immediately block reflection due to backoff cooldown
    assert not budget.can_reflect_now()
    assert budget._backoff_until > time.time() + 30.0


# ── 3. Reflection Engine with Fake Provider ───────────────────────────────────

def test_reflection_engine_synthesis(engine: LearningEngine):
    fake_provider = FakeReflectionProvider("Always handle missing configuration gracefully.")
    refl_engine = ReflectionEngine(engine.db, engine.settings, provider=fake_provider)

    # Insert a provisional experience that needs reflection
    exp_id = engine.record_experience(
        task="Read user config",
        outcome="Config file was missing; loaded defaults instead.",
        task_type="config",
        success=True,
    )

    # Run reflection cycle
    count = refl_engine.run_reflection_cycle()
    assert count == 1
    assert len(fake_provider.calls) == 1

    # Check that lesson was updated in DB and promoted to verified
    def _check(conn: sqlite3.Connection):
        c = conn.cursor()
        c.execute("SELECT lesson, status FROM experiences WHERE id = ?", (exp_id,))
        return c.fetchone()

    row = engine.db.execute_read(_check)
    assert "Always handle missing configuration gracefully." in row["lesson"]
    assert row["status"] == "verified"


# ── 4. Provider Adapters Zero-Crash Safety ────────────────────────────────────

def test_reflection_adapters_safety(monkeypatch):
    """Adapters must never raise exceptions, even when models are offline or unconfigured."""
    # Mock network calls to verify zero-crash behaviour instantly
    monkeypatch.setattr("core.llm_client.call_llm", lambda *a, **k: {"content": "mock local"})
    local_adapter = LocalReflectionAdapter()
    res_local = local_adapter.reflect("test prompt")
    assert res_local == "mock local"

    monkeypatch.setattr("core.gemini.text", lambda *a, **k: "mock gemini")
    gemini_adapter = GeminiReflectionAdapter()
    res_gemini = gemini_adapter.reflect("test prompt")
    assert res_gemini == "mock gemini"

    # Verify exception safety when provider raises
    def _raise(*a, **k):
        raise RuntimeError("Network offline")

    monkeypatch.setattr("core.gemini.text", _raise)
    res_err = gemini_adapter.reflect("test prompt")
    assert res_err == ""


# ── 5. PyQt6 Learning Review Dialog Unit Tests ────────────────────────────────

def test_ui_learning_dialog_initialization(engine: LearningEngine, monkeypatch):
    """Test dialog creation, data population, and button interactions."""
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(["pytest", "-platform", "offscreen"])
    except Exception:
        pytest.skip("PyQt6 GUI environment not available")

    # Patch global engine to our test fixture engine
    monkeypatch.setattr("core.learning.get_learning_engine", lambda: engine)
    monkeypatch.setattr("ui_learning.get_learning_engine", lambda: engine)

    # Insert test data
    engine.record_experience("Test task", "Test outcome", success=True)
    engine.learn_preference("test_pref", "test_value", status="verified")

    from ui_learning import LearningReviewDialog
    dlg = LearningReviewDialog()
    try:
        assert dlg.table.rowCount() >= 2
        assert "VERIFIED:" in dlg.lbl_verified.text()

        # Test toggle
        dlg.toggle_cb.setChecked(False)
        assert not dlg._settings.enabled
        dlg.toggle_cb.setChecked(True)
        assert dlg._settings.enabled
    finally:
        dlg.close()
