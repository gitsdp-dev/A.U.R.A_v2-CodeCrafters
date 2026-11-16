"""Deterministic, privacy-conscious signatures and dimension normalization."""

from __future__ import annotations

import hashlib
import json
import re

from aura_cad.models import CadSpec, PartPlan
from aura_cad.cache.models import PartSignature

_TOKEN = re.compile(r"[a-z][a-z0-9]*")
_UNIT = re.compile(r"_(millimeters?|millimetres?|mm|centimeters?|cm|meters?|m|inches?|in)$")
_TO_MM = {
    "mm": 1.0,
    "millimeter": 1.0,
    "millimeters": 1.0,
    "millimetre": 1.0,
    "millimetres": 1.0,
    "cm": 10.0,
    "centimeter": 10.0,
    "centimeters": 10.0,
    "m": 1000.0,
    "meter": 1000.0,
    "meters": 1000.0,
    "in": 25.4,
    "inch": 25.4,
    "inches": 25.4,
}
_UNIT_ALIASES = {
    "mm": "millimeter",
    "millimeter": "millimeter",
    "millimeters": "millimeter",
    "millimetre": "millimeter",
    "millimetres": "millimeter",
    "cm": "centimeter",
    "centimeter": "centimeter",
    "centimeters": "centimeter",
    "m": "meter",
    "meter": "meter",
    "meters": "meter",
    "in": "inch",
    "inch": "inch",
    "inches": "inch",
}
_STOP_WORDS = {
    "a", "an", "and", "another", "build", "component", "design", "for", "from",
    "model", "of", "part", "the", "with",
}
_FEATURE_WORDS = {
    "bevel", "beveled", "chamfer", "circular", "fillet", "flat", "hollow",
    "helical", "hex", "hexagonal", "internals", "keyed", "knurled", "notched",
    "ribbed", "slotted", "smooth", "solid", "spur", "stepped", "threaded",
    "toothed", "vented",
}


def canonical_name(value: str) -> str:
    """Normalize names without retaining punctuation or user prose."""
    return "_".join(_TOKEN.findall(str(value or "").casefold()))[:80]


def normalize_dimension(name: str, value: int | float) -> tuple[str, float]:
    """Convert explicit length units to millimetres and canonicalize the key."""
    key = canonical_name(name)
    match = _UNIT.search(key)
    if not match:
        return key, float(value)
    unit = _UNIT_ALIASES.get(match.group(1), "")
    factor = _TO_MM.get(unit)
    if factor is None:
        return key, float(value)
    return _UNIT.sub("_mm", key), float(value) * factor


def convert_to_mm(value: int | float, unit: str) -> float:
    return float(value) * _TO_MM.get(_UNIT_ALIASES.get(unit.casefold(), ""), 1.0)


def normalized_plan(plan: PartPlan) -> PartPlan:
    dimensions = {}
    for key, value in plan.dimensions.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            name, number = normalize_dimension(key, value)
            dimensions[name] = number
    return PartPlan(
        canonical_name(plan.name).replace("_", " "),
        str(plan.description or "")[:300],
        dimensions,
    )


def spec_plan(spec: CadSpec) -> PartPlan:
    dimensions = {
        key: value
        for key, value in spec.slots.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    return normalized_plan(PartPlan(spec.object_type, spec.description, dimensions))


def plan_signature(plan: PartPlan) -> PartSignature:
    normalized = normalized_plan(plan)
    name = canonical_name(normalized.name)
    family = name.split("_", 1)[0] if name else "part"
    terms = set(_TOKEN.findall(name))
    terms.update(
        term for term in _TOKEN.findall(normalized.description.casefold())
        if term in _FEATURE_WORDS
    )
    for key in normalized.dimensions:
        terms.update(_TOKEN.findall(key))
    tags = tuple(sorted(term for term in terms if term not in _STOP_WORDS))
    record = {
        "name": name,
        "family": family,
        "tags": tags,
        "dimensions": sorted(normalized.dimensions),
    }
    value = json.dumps(record, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return PartSignature(name, family, tags, digest)


def part_function_name(name: str, occurrence: int = 1) -> str:
    slug = canonical_name(name).replace("_", "_") or "part"
    suffix = f"_{occurrence}" if occurrence > 1 else ""
    return f"make_{slug}{suffix}"
