"""Cheap, deterministic CAD-intent detection."""

from __future__ import annotations

import re

_OBJECTS = {
    "engine": ("engine", "motor"),
    "gear": ("gear", "cogwheel", "cog"),
    "bracket": ("bracket", "mounting plate"),
    "enclosure": ("enclosure", "case", "housing"),
    "table": ("table", "desk"),
    "house": ("house", "building"),
}
_DESIGN_VERB = re.compile(
    r"\b(?:sketch|design|model|make|build|create|draw|generate)\b", re.I
)
_EXPLANATION = re.compile(r"^\s*(?:what is|how does|explain|describe)\b", re.I)
_CAD_TERMS = re.compile(r"\b(?:3d|3-d|cad|part|solid|mesh)\b", re.I)
_FILLER = {"a", "an", "the", "me", "my", "please", "some", "simple", "custom"}


def detect_intent(text: str) -> str | None:
    """Return a known object name when a request is actually asking for CAD."""
    request = " ".join(str(text or "").split())
    if not request or _EXPLANATION.search(request):
        return None
    lowered = request.casefold()
    object_type = next(
        (
            name for name, aliases in _OBJECTS.items()
            if any(re.search(r"\b" + re.escape(alias) + r"\b", lowered) for alias in aliases)
        ),
        None,
    )
    if object_type:
        return object_type if _DESIGN_VERB.search(request) or _CAD_TERMS.search(request) else None
    if not (_DESIGN_VERB.search(request) and _CAD_TERMS.search(request)):
        return None
    words = re.findall(r"[a-z0-9]+", lowered)
    verb_index = next(
        (index for index, word in enumerate(words) if word in {
            "sketch", "design", "model", "make", "build", "create", "draw", "generate"
        }),
        -1,
    )
    target = [word for word in words[verb_index + 1:] if word not in _FILLER]
    target = [word for word in target if word not in {"3d", "cad", "part", "solid", "mesh"}]
    return "_".join(target[:2]) or "part"
