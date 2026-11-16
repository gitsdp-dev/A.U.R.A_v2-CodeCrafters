"""Convert a confirmed specification into a bounded part plan."""

from __future__ import annotations

import json
import logging
import math

from aura_cad.config import MAX_COMPONENTS
from aura_cad.cache import PartCache
from aura_cad.cache.models import PartResolution
from aura_cad.errors import CodegenError
from aura_cad.llm.adapter import CadLLMAdapter
from aura_cad.llm.prompts import PLAN_PROMPT
from aura_cad.models import CadSpec, PartPlan

LOGGER = logging.getLogger("aura.cad")


class CadPlanner:
    def __init__(self, llm: CadLLMAdapter) -> None:
        self._llm = llm

    def plan(self, spec: CadSpec, job_id: str) -> list[PartPlan]:
        try:
            response = self._llm.generate(
                PLAN_PROMPT.format(spec=json.dumps(spec.as_dict(), ensure_ascii=True)),
                job_id,
            )
        except CodegenError:
            LOGGER.warning(
                "[job=%s] CAD planner response unavailable; using specification-based plan",
                job_id,
            )
            return self._fallback_plan(spec)
        try:
            payload = self._decode_payload(response)
            if not isinstance(payload, dict):
                raise ValueError("planner response must be a JSON object")
            records = payload["parts"]
            if not isinstance(records, list) or not records:
                raise ValueError("parts must be a non-empty list")
            if len(records) > MAX_COMPONENTS:
                raise ValueError("too many parts")
            parts = []
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("each part must be a JSON object")
                name = str(record["name"]).strip()
                description = str(record["description"]).strip()
                dimensions = record.get("dimensions", {})
                if not name or not description or not isinstance(dimensions, dict):
                    raise ValueError("part fields are invalid")
                numeric_dimensions = self._numeric_dimensions(dimensions)
                parts.append(PartPlan(name[:60], description[:300], numeric_dimensions))
            return parts
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning(
                "[job=%s] CAD planner returned invalid structured output; "
                "using specification-based plan (%s)",
                job_id,
                type(exc).__name__,
            )
            return self._fallback_plan(spec)

    def plan_with_cache(
        self, spec: CadSpec, job_id: str, cache: PartCache
    ) -> list[PartResolution]:
        """Resolve an obvious cached single-part request before spending planner quota."""
        cached = cache.resolve_from_spec(spec, job_id)
        if cached is not None:
            return [cached]
        return cache.resolve_parts(self.plan(spec, job_id), job_id)

    @staticmethod
    def _decode_payload(response: str) -> object:
        """Decode the first JSON object even if the model adds fences or prose."""
        text = str(response or "").strip()
        start = text.find("{")
        if start < 0:
            raise ValueError("no JSON object found")
        payload, _end = json.JSONDecoder().raw_decode(text[start:])
        return payload

    @staticmethod
    def _fallback_plan(spec: CadSpec) -> list[PartPlan]:
        dimensions = CadPlanner._numeric_dimensions(spec.slots)
        return [
            PartPlan(
                name=str(spec.object_type or "model")[:60],
                description=str(spec.description or "CAD model")[:300],
                dimensions=dimensions,
            )
        ]

    @staticmethod
    def _numeric_dimensions(values: dict) -> dict[str, float]:
        dimensions = {}
        for key, value in values.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (OverflowError, TypeError, ValueError):
                continue
            if math.isfinite(number):
                dimensions[str(key)[:60]] = number
        return dimensions
