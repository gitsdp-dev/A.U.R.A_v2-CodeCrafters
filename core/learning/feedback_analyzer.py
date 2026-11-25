"""
core/learning/feedback_analyzer.py — Natural Language Feedback & Correction Analyzer.

Detects user corrections, ratings, and explicit instructions ("always", "never",
"remember", "from now on") in both English and common Hinglish phrasing.
Determines semantic scope (global, project, task_type) and promotion eligibility.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from .redaction import redact_secrets

LOGGER = logging.getLogger("aura.learning.feedback")


@dataclass
class AnalyzedFeedback:
    raw_text: str
    is_feedback: bool
    feedback_type: str  # "correction" | "instruction" | "praise" | "criticism" | "observation"
    instruction_polarity: str | None  # "always" | "never" | "preference" | None
    key_preference: str | None  # Extracted preference key if explicit
    target_rule: str | None  # Cleaned instruction/rule content
    scope: str  # "global" | "project:<name>" | "task_type:<name>"
    sentiment: float  # -1.0 to 1.0
    auto_verify: bool  # True for explicit user commands ("always", "never", "remember")
    confidence: float
    why_learned: str


# English patterns
_CORRECTION_EN = [
    r"\b(that'?s (wrong|incorrect|not right|false))\b",
    r"\b(no[,.]? (that is not|not like that|don'?t do that|stop))\b",
    r"\b(you made a mistake|you got (it|that) wrong)\b",
    r"\b(do not (do that|use this|call)|don'?t)\b",
    r"\b(instead of (that|this),? (do|use|try))\b",
    r"\b(i meant|i wanted you to|you should have)\b",
]

_INSTRUCTION_EN = [
    r"\b(remember (to|that|always|never)?\s*[a-z]+)\b",
    r"\b(from now on,? (always|never|make sure|do not|don'?t)?\s*[a-z]+)\b",
    r"\b(always\s+[a-z]+)\b",
    r"\b(never\s+[a-z]+)\b",
    r"\b(make sure (to|you always|you never))\b",
    r"\b(my preference is|i prefer)\b",
]

_PRAISE_EN = [
    r"\b(good job|great job|well done|perfect|awesome|excellent|thank you aura|that works|nice)\b",
]

_CRITICISM_EN = [
    r"\b(terrible|horrible|useless|failed completely|bad response|poor answer)\b",
]

# Hinglish patterns
_CORRECTION_HI = [
    r"\b(yeh (galat|wrong) hai)\b",
    r"\b(galat (kiya|hai|answer hai))\b",
    r"\b(aise mat (karo|karna))\b",
    r"\b(sahi nahi hai|ye thik nahi hai)\b",
    r"\b(maine ye nahi (kaha|bola) tha)\b",
    r"\b(nahi[,.]? aise nahi)\b",
]

_INSTRUCTION_HI = [
    r"\b(yaad rakhna|yaad rakho)\b",
    r"\b(aage se|agli baar se)\b",
    r"\b(hamesha (aise hi|yehi|ye)? (karo|karna|use karo))\b",
    r"\b(kabhi (bhi )?mat (karna|karo|use karna))\b",
    r"\b(dhyaan rakhna)\b",
]

_PRAISE_HI = [
    r"\b(bohot (badhiya|achha|sahi)|shabash|ekdum sahi|kamaal kar diya)\b",
]

_CRITICISM_HI = [
    r"\b(bakwas|kisi kaam ka nahi|kuch samajh nahi aaya)\b",
]

# Compile pattern tables
_COMPILED_CORRECTIONS = [re.compile(p, re.IGNORECASE) for p in _CORRECTION_EN + _CORRECTION_HI]
_COMPILED_INSTRUCTIONS = [re.compile(p, re.IGNORECASE) for p in _INSTRUCTION_EN + _INSTRUCTION_HI]
_COMPILED_PRAISE = [re.compile(p, re.IGNORECASE) for p in _PRAISE_EN + _PRAISE_HI]
_COMPILED_CRITICISM = [re.compile(p, re.IGNORECASE) for p in _CRITICISM_EN + _CRITICISM_HI]

# Scope detection patterns
_PROJECT_SCOPE_PATTERN = re.compile(r"(?i)\b(?:in|for|within)\s+(?:project|repo|folder|directory)\s+([a-zA-Z0-9_\-\.]+)")
_TASK_SCOPE_PATTERN = re.compile(r"(?i)\b(?:when|for|while)\s+(?:doing|running|calling|using)\s+([a-zA-Z0-9_\-]+)")


class FeedbackAnalyzer:
    """Analyzes text to detect user corrections and behavioural preferences."""

    @classmethod
    def analyze(cls, text: str, current_project: str | None = None) -> AnalyzedFeedback:
        """Parse user utterance and determine if it contains learning instructions."""
        clean_text = redact_secrets(text.strip())
        if not clean_text or len(clean_text) < 3:
            return cls._empty_feedback(clean_text)

        # 1. Scope detection
        scope = "global"
        proj_match = _PROJECT_SCOPE_PATTERN.search(clean_text)
        if proj_match:
            scope = f"project:{proj_match.group(1).lower()}"
        elif current_project:
            # If explicit project scope indicated in utterance
            if any(w in clean_text.lower() for w in ("in this project", "for this project", "here")):
                scope = f"project:{current_project.lower()}"

        task_match = _TASK_SCOPE_PATTERN.search(clean_text)
        if task_match and scope == "global":
            scope = f"task_type:{task_match.group(1).lower()}"

        # 2. Check for explicit instructions ("always", "never", "remember", "from now on")
        for pattern in _COMPILED_INSTRUCTIONS:
            if pattern.search(clean_text):
                polarity = "preference"
                if re.search(r"(?i)\b(never|kabhi mat|don'?t|mat karo)\b", clean_text):
                    polarity = "never"
                elif re.search(r"(?i)\b(always|hamesha)\b", clean_text):
                    polarity = "always"

                return AnalyzedFeedback(
                    raw_text=clean_text,
                    is_feedback=True,
                    feedback_type="instruction",
                    instruction_polarity=polarity,
                    key_preference=cls._extract_preference_key(clean_text),
                    target_rule=clean_text,
                    scope=scope,
                    sentiment=0.0,
                    auto_verify=True,  # Explicit user command -> verified immediately
                    confidence=0.95,
                    why_learned="Explicit user instruction ('always/never/remember').",
                )

        # 3. Check for corrections
        for pattern in _COMPILED_CORRECTIONS:
            if pattern.search(clean_text):
                return AnalyzedFeedback(
                    raw_text=clean_text,
                    is_feedback=True,
                    feedback_type="correction",
                    instruction_polarity=None,
                    key_preference=None,
                    target_rule=clean_text,
                    scope=scope,
                    sentiment=-0.8,
                    auto_verify=False,  # Needs verification or execution confirmation
                    confidence=0.75,
                    why_learned="Detected direct user correction.",
                )

        # 4. Check for praise
        for pattern in _COMPILED_PRAISE:
            if pattern.search(clean_text):
                return AnalyzedFeedback(
                    raw_text=clean_text,
                    is_feedback=True,
                    feedback_type="praise",
                    instruction_polarity=None,
                    key_preference=None,
                    target_rule=None,
                    scope=scope,
                    sentiment=0.8,
                    auto_verify=False,
                    confidence=0.6,
                    why_learned="User praise for successful action.",
                )

        # 5. Check for criticism
        for pattern in _COMPILED_CRITICISM:
            if pattern.search(clean_text):
                return AnalyzedFeedback(
                    raw_text=clean_text,
                    is_feedback=True,
                    feedback_type="criticism",
                    instruction_polarity=None,
                    key_preference=None,
                    target_rule=None,
                    scope=scope,
                    sentiment=-0.7,
                    auto_verify=False,
                    confidence=0.6,
                    why_learned="User expressed dissatisfaction with result.",
                )

        return cls._empty_feedback(clean_text, scope=scope)

    @staticmethod
    def _empty_feedback(text: str, scope: str = "global") -> AnalyzedFeedback:
        return AnalyzedFeedback(
            raw_text=text,
            is_feedback=False,
            feedback_type="observation",
            instruction_polarity=None,
            key_preference=None,
            target_rule=None,
            scope=scope,
            sentiment=0.0,
            auto_verify=False,
            confidence=0.0,
            why_learned="",
        )

    @staticmethod
    def _extract_preference_key(text: str) -> str:
        """Derive a normalized preference key from user instruction."""
        lowered = text.lower()
        if "browser" in lowered:
            return "default_browser"
        if "confirm" in lowered or "ask before" in lowered:
            return "confirmation_policy"
        if "code" in lowered or "format" in lowered or "style" in lowered:
            return "coding_style"
        if "theme" in lowered or "dark" in lowered or "light" in lowered:
            return "ui_theme"
        if "voice" in lowered or "speak" in lowered or "talk" in lowered:
            return "voice_preference"
        # Fallback to sanitized first 4 significant words
        words = re.findall(r"\b[a-z]{3,}\b", lowered)
        filtered = [w for w in words if w not in {"always", "never", "remember", "from", "that", "this", "with"}][:3]
        return "_".join(filtered) if filtered else "general_preference"
