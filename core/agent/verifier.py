"""Deterministic and Gemini-backed goal verification for agent steps."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from core import gemini

from .models import PlanStep


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Evidence-backed verification outcome."""

    passed: bool
    evidence: str
    what_is_missing: str = ""


def deterministic_verify(step: PlanStep, output: Any) -> VerificationResult:
    """Apply local checks that do not require a model call."""
    text = str(output or "")
    check = step.success_check.strip()
    if check in {"", "output_non_empty"}:
        passed = bool(text.strip())
        return VerificationResult(
            passed, text[:500], "non-empty output required" if not passed else ""
        )
    if check.startswith("contains:"):
        expected = check.removeprefix("contains:").strip()
        passed = expected in text
        return VerificationResult(
            passed,
            text[:500],
            f"missing required text: {expected}" if not passed else "",
        )
    if check.startswith("file_exists:"):
        from pathlib import Path

        target = Path(check.removeprefix("file_exists:").strip())
        passed = target.is_file()
        return VerificationResult(
            passed, str(target), "file does not exist" if not passed else ""
        )
    if check == "always":
        return VerificationResult(True, "step explicitly marked always", "")
    return VerificationResult(
        bool(text.strip()),
        text[:500],
        "unrecognized check produced empty output" if not text.strip() else "",
    )


class GoalVerifier:
    """Combine deterministic checks with a bounded Gemini evidence review."""

    def __init__(self, timeout_ms: int = 20_000):
        self.timeout_ms = timeout_ms

    async def verify(
        self, goal: str, step: PlanStep, output: Any, *, final: bool = False
    ) -> VerificationResult:
        """Verify one observed result and return machine-readable evidence."""
        local = deterministic_verify(step, output)
        if not local.passed:
            return local
        prompt = (
            "Return only JSON with keys passed, evidence, what_is_missing. "
            "Never infer work that is not visible in the observed output. "
            f"Original goal: {goal}\n"
            f"Step: {step.id} using {step.tool}\n"
            f"Expected result: {step.expected_output}\n"
            f"Observed output: {str(output)[:6000]}\n"
            f"This is the final task check: {final}\n"
        )
        try:
            response = await asyncio.to_thread(
                gemini.text, prompt, gemini.FAST, None, self.timeout_ms
            )
            data = json.loads(
                str(response or "")
                .strip()
                .removeprefix("```json")
                .removesuffix("```")
                .strip()
            )
            return VerificationResult(
                bool(data.get("passed")),
                str(data.get("evidence") or local.evidence),
                str(data.get("what_is_missing") or ""),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return VerificationResult(
                False, local.evidence, "Gemini verifier returned invalid evidence"
            )
        except (RuntimeError, OSError, TimeoutError, ImportError) as exc:
            return VerificationResult(
                False, local.evidence, f"Gemini verifier unavailable: {exc}"
            )
