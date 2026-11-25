"""
core/learning/experience_evaluator.py — Failure Analysis and Beta-Distribution Confidence Scoring.

Classifies failure types:
- temporary (timeouts, rate limits, busy locks) -> provisional with expires_at
- environment_specific (missing CLI, platform differences) -> low-confidence provisional
- tool_failure (tool raised exception, syntax error)
- incorrect_reasoning (bad approach, misaligned parameters)
- user_correction (explicit negative feedback)
- confirmed_resolution (successful retry after failure)

Implements Bayesian Beta-distribution confidence updates:
confidence = (alpha + successes) / (alpha + beta + total_trials)
with recency decay and contradiction penalties.
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger("aura.learning.evaluator")

# Regex heuristics for failure classification
_TEMPORARY_PATTERNS = re.compile(
    r"(?i)\b(timeout|timed out|busy|rate limit|quota|429|503|connection reset|temporarily unavailable)\b"
)
_ENV_PATTERNS = re.compile(
    r"(?i)\b(command not found|not recognized|no such file or directory|path not found|wmi|ole error|display)\b"
)
_TOOL_PATTERNS = re.compile(
    r"(?i)\b(tool '.*' failed|typeerror|attributeerror|syntaxerror|keyerror|indexerror)\b"
)


@dataclass
class EvaluationOutcome:
    success: bool
    failure_type: str | None  # "temporary" | "environment_specific" | "tool_failure" | "incorrect_reasoning" | "user_correction" | "confirmed_resolution"
    confidence: float
    status: str  # "provisional" | "verified" | "rejected"
    expires_at: float | None
    why_learned: str


class ExperienceEvaluator:
    """Evaluates task execution outcomes and calculates calibrated confidence scores."""

    @staticmethod
    def classify_outcome(
        task_type: str,
        result: str,
        error_text: str | None = None,
        is_user_correction: bool = False,
        retry_of_failed_task: bool = False,
    ) -> EvaluationOutcome:
        """Categorize the outcome and determine failure type, provisional expiry, and confidence."""
        now = time.time()
        combined = f"{result} {error_text or ''}".strip()

        # 1. Success cases
        if not error_text and not is_user_correction:
            if retry_of_failed_task:
                return EvaluationOutcome(
                    success=True,
                    failure_type="confirmed_resolution",
                    confidence=0.85,
                    status="provisional",  # Requires repeated confirmation before promotion
                    expires_at=None,
                    why_learned="Successful retry of previously failed task.",
                )
            return EvaluationOutcome(
                success=True,
                failure_type=None,
                confidence=0.70,
                status="provisional",
                expires_at=None,
                why_learned=f"Verified successful execution of '{task_type}'.",
            )

        # 2. User correction
        if is_user_correction:
            return EvaluationOutcome(
                success=False,
                failure_type="user_correction",
                confidence=0.25,
                status="provisional",
                expires_at=None,
                why_learned="Direct user correction received.",
            )

        # 3. Temporary / transient errors (short-lived provisional lesson)
        if _TEMPORARY_PATTERNS.search(combined):
            # Expires after 2 hours (7200 seconds)
            return EvaluationOutcome(
                success=False,
                failure_type="temporary",
                confidence=0.30,
                status="provisional",
                expires_at=now + 7200.0,
                why_learned="Temporary transient error (rate limit / timeout); expires automatically.",
            )

        # 4. Environment-specific errors (platform/hardware limitations)
        if _ENV_PATTERNS.search(combined):
            # Expires after 24 hours (86400 seconds)
            return EvaluationOutcome(
                success=False,
                failure_type="environment_specific",
                confidence=0.35,
                status="provisional",
                expires_at=now + 86400.0,
                why_learned="Environment or system capability limitation noted.",
            )

        # 5. Tool execution failure
        if _TOOL_PATTERNS.search(combined):
            return EvaluationOutcome(
                success=False,
                failure_type="tool_failure",
                confidence=0.30,
                status="provisional",
                expires_at=None,
                why_learned=f"Tool failure in '{task_type}'.",
            )

        # 6. Default: incorrect reasoning / unfulfilled expectation
        return EvaluationOutcome(
            success=False,
            failure_type="incorrect_reasoning",
            confidence=0.30,
            status="provisional",
            expires_at=None,
            why_learned=f"Execution did not achieve intended outcome for '{task_type}'.",
        )

    @staticmethod
    def update_beta_confidence(
        prior_alpha: float = 2.0,
        prior_beta: float = 2.0,
        successes: int = 1,
        failures: int = 0,
        penalty: float = 0.0,
    ) -> float:
        """
        Calculate posterior Beta expected value:
        E[p] = (alpha + successes) / (alpha + beta + successes + failures) - penalty
        Bounded in [0.05, 0.99].
        """
        alpha_post = max(1.0, prior_alpha + max(0, successes))
        beta_post = max(1.0, prior_beta + max(0, failures))
        expected = alpha_post / (alpha_post + beta_post)
        adjusted = expected - max(0.0, penalty)
        return max(0.05, min(0.99, round(adjusted, 3)))
