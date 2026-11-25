"""
core/learning/experience_collector.py — Structured Task & Experience Builder.

Collects task execution details, applies secret redaction, prevents prompt-injection
poisoning from tool output or untrusted web data, and constructs validated records.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .redaction import redact_data_structure, redact_secrets

LOGGER = logging.getLogger("aura.learning.collector")

# Prompt injection patterns that must NEVER become instructions or lessons
_INJECTION_DEFENSE_PATTERNS = re.compile(
    r"(?i)\b(ignore (all )?previous instructions|you are now [a-z0-9_\-]+|system prompt:|override security|dan mode|\bdan\b|"
    r"always output|as an ai language model|disregard all previous|you are evil)\b"
)


@dataclass
class TaskRecord:
    task_type: str
    task_description: str
    approach: str = ""
    tools_used: list[str] = field(default_factory=list)
    outcome: str = ""
    duration_ms: int = 0
    success: bool = True
    failure_type: str | None = None
    user_feedback: str | None = None
    error_text: str | None = None
    lesson: str = ""
    scope: str = "global"
    confidence: float = 0.5
    status: str = "provisional"
    source: str = "observation"
    expires_at: float | None = None
    why_learned: str = ""


class ExperienceCollector:
    """Builds sanitized, structured task records ready for storage."""

    @staticmethod
    def sanitize_untrusted_content(text: str) -> str:
        """Strip or flag adversarial injection patterns from tool output or logs."""
        if not text:
            return ""
        # Redact any credentials first
        cleaned = redact_secrets(text)
        # Neutralize jailbreak/override phrases
        if _INJECTION_DEFENSE_PATTERNS.search(cleaned):
            LOGGER.warning("Adversarial or prompt injection pattern detected in untrusted content; neutralizing.")
            cleaned = _INJECTION_DEFENSE_PATTERNS.sub("[FILTERED_INSTRUCTION]", cleaned)
        return cleaned

    @classmethod
    def create_record(
        cls,
        task_type: str,
        task_description: str,
        approach: str = "",
        tools_used: list[str] | None = None,
        outcome: str = "",
        duration_ms: int = 0,
        success: bool = True,
        failure_type: str | None = None,
        user_feedback: str | None = None,
        error_text: str | None = None,
        lesson: str = "",
        scope: str = "global",
        confidence: float = 0.5,
        status: str = "provisional",
        source: str = "observation",
        expires_at: float | None = None,
        why_learned: str = "",
    ) -> dict[str, Any]:
        """
        Produce a clean, validated dictionary for the database with secrets redacted
        and third-party injection attempts defused.
        """
        tools = [str(t).strip() for t in (tools_used or []) if str(t).strip()]
        clean_desc = cls.sanitize_untrusted_content(task_description.strip())
        clean_approach = cls.sanitize_untrusted_content(approach.strip())
        clean_outcome = cls.sanitize_untrusted_content(outcome.strip())
        clean_error = cls.sanitize_untrusted_content(error_text.strip()) if error_text else None
        clean_feedback = cls.sanitize_untrusted_content(user_feedback.strip()) if user_feedback else None

        # Build an honest, deterministic lesson if none provided
        if not lesson:
            if success:
                tools_str = f" via {', '.join(tools)}" if tools else ""
                desc_str = f" for '{clean_desc[:50]}'" if clean_desc and clean_desc != task_type else ""
                clean_lesson = f"Successfully executed '{task_type}'{desc_str}{tools_str}: {clean_outcome[:120]}"
            else:
                fail_reason = clean_error or failure_type or "unspecified error"
                desc_str = f" '{clean_desc[:50]}'" if clean_desc and clean_desc != task_type else f" '{task_type}'"
                clean_lesson = f"Task{desc_str} failed ({fail_reason}). Avoid repeated attempt without adjusting approach."
        else:
            clean_lesson = cls.sanitize_untrusted_content(lesson.strip())

        now = time.time()
        record = TaskRecord(
            task_type=task_type.strip() or "general",
            task_description=clean_desc,
            approach=clean_approach,
            tools_used=tools,
            outcome=clean_outcome,
            duration_ms=max(0, int(duration_ms)),
            success=bool(success),
            failure_type=failure_type,
            user_feedback=clean_feedback,
            error_text=clean_error,
            lesson=clean_lesson,
            scope=scope.strip() or "global",
            confidence=max(0.0, min(1.0, float(confidence))),
            status=status if status in {"provisional", "verified", "rejected"} else "provisional",
            source=source,
            expires_at=expires_at,
            why_learned=why_learned or f"Recorded from {source} execution.",
        )

        d = asdict(record)
        d["created_at"] = now
        d["updated_at"] = now
        d["last_used_at"] = now
        d["evidence_count"] = 1
        d["reuse_count"] = 0
        d["approach"] = json.dumps({"approach": clean_approach, "tools": tools})
        return d
