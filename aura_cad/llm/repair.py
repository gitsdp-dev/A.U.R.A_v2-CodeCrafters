"""Bounded minimal repair loop prompt helper."""

from __future__ import annotations

import json

from aura_cad.llm.adapter import CadLLMAdapter, strip_code_fence
from aura_cad.llm.codegen import normalize_build123d_math
from aura_cad.llm.prompts import REPAIR_PROMPT
from aura_cad.models import CadSpec


class CadRepair:
    def __init__(self, llm: CadLLMAdapter) -> None:
        self._llm = llm

    def repair(self, spec: CadSpec, script: str, failure: str, job_id: str) -> str:
        repaired = strip_code_fence(self._llm.generate(
            REPAIR_PROMPT.format(
                spec=json.dumps(spec.as_dict(), ensure_ascii=True),
                script=script,
                failure=failure[:1200],
            ),
            job_id,
        ))
        return normalize_build123d_math(repaired)
