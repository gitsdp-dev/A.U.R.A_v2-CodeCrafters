"""Conservative parameter-only adaptation for cached parametric functions."""

from __future__ import annotations

import math
import re

from aura_cad.cache.models import AdaptationResult, CachedPart
from aura_cad.cache.signature import convert_to_mm, normalize_dimension

_STRUCTURAL = re.compile(r"\b(?:add|remove|replace|different|convert|new type)\b", re.I)
_SIZE_WORDS = re.compile(
    r"\b(?:bigger|larger|thicker|wider|taller|enlarge|increase size)\b", re.I
)
_SMALL_WORDS = re.compile(r"\b(?:smaller|thinner|narrower|shorter|shrink)\b", re.I)
_LENGTH_WORDS = {"diameter", "radius", "width", "height", "thickness", "length", "size"}
_COUNT_WORDS = {"count", "teeth", "tooth", "holes", "cylinders", "storeys"}


class PartParametrizer:
    """Change named numeric parameters only; structural edits require generation."""

    def adapt(
        self, part: CachedPart, requested: dict[str, int | float]
    ) -> AdaptationResult:
        schema = {item.name: item for item in part.param_schema}
        normalized = self._normalize_values(requested)
        if set(normalized) - set(schema):
            return AdaptationResult("needs_regeneration", {}, "New dimensions are structural.")
        values = {}
        for name, item in schema.items():
            value = normalized.get(name, item.default)
            checked = self._coerce(item.type, value)
            if checked is None or not self._in_range(checked, item.minimum, item.maximum):
                return AdaptationResult(
                    "needs_regeneration", {}, f"Parameter {name} is outside its safe schema."
                )
            values[name] = checked
        status = "reuse" if all(values[key] == item.default for key, item in schema.items()) else "adapt"
        return AdaptationResult(status, values)

    def adapt_refinement(
        self, part: CachedPart, current: dict[str, int | float], request: str
    ) -> AdaptationResult:
        if _STRUCTURAL.search(request):
            return AdaptationResult("needs_regeneration", {}, "The request changes structure.")
        schemas = {item.name: item for item in part.param_schema}
        next_values = dict(current)
        target = self._explicit_value(request, schemas)
        if target is not None:
            name, value = target
            next_values[name] = value
        else:
            factor = 1.1 if _SIZE_WORDS.search(request) else 0.9 if _SMALL_WORDS.search(request) else None
            if factor is None:
                return AdaptationResult("needs_regeneration", {}, "No safe numeric edit was found.")
            editable = [
                item for item in part.param_schema
                if item.unit == "mm" and not (_COUNT_WORDS & set(item.name.split("_")))
            ]
            if not editable:
                return AdaptationResult("needs_regeneration", {}, "No length parameters are cached.")
            for item in editable:
                value = float(current.get(item.name, item.default)) * factor
                next_values[item.name] = self._coerce(item.type, value)
        result = self.adapt(part, next_values)
        if result.status == "needs_regeneration":
            return result
        return AdaptationResult("adapt", result.parameters)

    @staticmethod
    def _normalize_values(values):
        normalized = {}
        for name, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                key, number = normalize_dimension(name, value)
                normalized[key] = number
        return normalized

    @staticmethod
    def _coerce(kind, value):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            return None
        if kind == "int":
            return int(value) if float(value).is_integer() else None
        return float(value)

    @staticmethod
    def _in_range(value, minimum, maximum):
        return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)

    @staticmethod
    def _explicit_value(request, schemas):
        match = re.search(
            r"\b([a-z][a-z0-9_ ]{0,30}?)\s+(?:to|at|of)\s+"
            r"(\d+(?:\.\d+)?)\s*(mm|cm|m|in|inch|inches)?\b",
            request,
            re.I,
        )
        if not match:
            return None
        phrase = set(match.group(1).casefold().split())
        unit = match.group(3) or ""
        for name, schema in schemas.items():
            if phrase & set(name.split()):
                value = float(match.group(2))
                if unit and schema.unit == "mm":
                    value = convert_to_mm(value, unit)
                return name, PartParametrizer._coerce(schema.type, value)
        return None
