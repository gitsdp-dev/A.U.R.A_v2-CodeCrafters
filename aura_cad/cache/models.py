"""Typed records shared by the local CAD part cache."""

from __future__ import annotations

import math
from enum import Enum
from dataclasses import dataclass, field
from typing import Any

from aura_cad.models import PartPlan


def _finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class CacheAction(str, Enum):
    REUSE = "reuse"
    ADAPT = "adapt"
    GENERATE = "generate"


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    type: str
    default: int | float
    minimum: int | float | None = None
    maximum: int | float | None = None
    unit: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParameterSpec":
        if not isinstance(data, dict):
            raise ValueError("Invalid cached parameter schema.")
        name = data["name"]
        kind = data["type"]
        default = data["default"]
        if (
            not isinstance(name, str)
            or not name.isidentifier()
            or kind not in {"int", "float"}
        ):
            raise ValueError("Invalid cached parameter schema.")
        if (
            not _finite_number(default)
            or (kind == "int" and not float(default).is_integer())
        ):
            raise ValueError("Invalid cached parameter default.")
        minimum, maximum = data.get("minimum"), data.get("maximum")
        for bound in (minimum, maximum):
            if bound is not None and not _finite_number(bound):
                raise ValueError("Invalid cached parameter bounds.")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("Cached parameter bounds are reversed.")
        if (
            (minimum is not None and default < minimum)
            or (maximum is not None and default > maximum)
        ):
            raise ValueError("Cached parameter default is outside its bounds.")
        unit = data.get("unit", "")
        if not isinstance(unit, str):
            raise ValueError("Invalid cached parameter unit.")
        return cls(
            name=name,
            type=kind,
            default=default,
            minimum=minimum,
            maximum=maximum,
            unit=unit,
        )


@dataclass(frozen=True)
class CachedPart:
    id: str
    name: str
    object_family: str
    tags: tuple[str, ...]
    signature: str
    param_schema: tuple[ParameterSpec, ...]
    source_code: str
    docstring: str
    backend_name: str
    backend_version: str
    prompt_version: str
    code_sha256: str
    created_at: str
    last_used_at: str
    use_count: int = 0
    validated: bool = False
    notes: str = ""
    function_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "object_family": self.object_family,
            "tags": list(self.tags),
            "signature": self.signature,
            "param_schema": [param.as_dict() for param in self.param_schema],
            "source_code": self.source_code,
            "docstring": self.docstring,
            "backend_name": self.backend_name,
            "backend_version": self.backend_version,
            "prompt_version": self.prompt_version,
            "code_sha256": self.code_sha256,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
            "validated": self.validated,
            "notes": self.notes,
            "function_name": self.function_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CachedPart":
        required = (
            "id", "name", "object_family", "signature", "source_code",
            "docstring", "backend_name", "backend_version", "prompt_version",
            "code_sha256", "created_at", "last_used_at", "function_name",
        )
        if not isinstance(data, dict) or any(key not in data for key in required):
            raise ValueError("Cached part record is incomplete.")
        tags = data.get("tags", [])
        schema = data.get("param_schema", [])
        if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
            raise ValueError("Cached part tags are invalid.")
        if not isinstance(schema, list):
            raise ValueError("Cached part parameters are invalid.")
        if not isinstance(data.get("validated", False), bool):
            raise ValueError("Cached part validation marker is invalid.")
        use_count = data.get("use_count", 0)
        if not isinstance(use_count, int) or isinstance(use_count, bool) or use_count < 0:
            raise ValueError("Cached part use count is invalid.")
        notes = data.get("notes", "")
        if not isinstance(notes, str):
            raise ValueError("Cached part notes are invalid.")
        for key in required:
            if not isinstance(data[key], str):
                raise ValueError("Cached part metadata is invalid.")
        if not data["function_name"].isidentifier() or data["function_name"].startswith("_"):
            raise ValueError("Cached part function name is invalid.")
        return cls(
            id=data["id"],
            name=data["name"],
            object_family=data["object_family"],
            tags=tuple(tags),
            signature=data["signature"],
            param_schema=tuple(ParameterSpec.from_dict(item) for item in schema),
            source_code=data["source_code"],
            docstring=data["docstring"],
            backend_name=data["backend_name"],
            backend_version=data["backend_version"],
            prompt_version=data["prompt_version"],
            code_sha256=data["code_sha256"],
            created_at=data["created_at"],
            last_used_at=data["last_used_at"],
            use_count=use_count,
            validated=bool(data.get("validated", False)),
            notes=notes,
            function_name=data["function_name"],
        )


@dataclass(frozen=True)
class PartSignature:
    canonical_name: str
    object_family: str
    tags: tuple[str, ...]
    value: str


@dataclass(frozen=True)
class MatchCandidate:
    part: CachedPart
    confidence: float
    exact: bool = False


@dataclass(frozen=True)
class PartResolution:
    plan: PartPlan
    function_name: str
    action: CacheAction
    part: CachedPart | None = None
    parameters: dict[str, int | float] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class AdaptationResult:
    status: str
    parameters: dict[str, int | float]
    reason: str = ""


@dataclass(frozen=True)
class CacheStats:
    entry_count: int = 0
    size_bytes: int = 0
    enabled: bool = True
    loaded: bool = False
