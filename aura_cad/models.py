"""Typed state exchanged between CAD workflow stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SlotStatus(str, Enum):
    MISSING = "missing"
    FILLED = "filled"
    DEFAULTED = "defaulted"


class JobState(str, Enum):
    CLARIFYING = "clarifying"
    CONFIRMING = "confirming"
    BUILDING = "building"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class Slot:
    key: str
    label: str
    question: str
    options: tuple[str, ...]
    default: Any


@dataclass
class CadSpec:
    object_type: str
    description: str
    slots: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type,
            "description": self.description,
            "slots": dict(self.slots),
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True)
class PartPlan:
    name: str
    description: str
    dimensions: dict[str, float] = field(default_factory=dict)


@dataclass
class CadJob:
    job_id: str
    state: JobState
    spec: CadSpec


@dataclass(frozen=True)
class ScriptVersion:
    project_id: str
    version: int
    script_path: str
    stl_path: str
    obj_path: str
    glb_path: str


@dataclass(frozen=True)
class CadResult:
    state: JobState
    message: str
    project_id: str = ""
    version: int = 0
    viewer_url: str = ""
    details: dict[str, Any] = field(default_factory=dict)
