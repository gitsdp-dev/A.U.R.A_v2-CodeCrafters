"""Structured sanity checks for generated triangular meshes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from aura_cad.config import MAX_BOUNDING_DIMENSION_MM, MAX_TRIANGLES
from aura_cad.errors import ValidationError


@dataclass(frozen=True)
class MeshCheck:
    watertight: bool
    triangle_count: int
    bounds: tuple[tuple[float, float, float], tuple[float, float, float]]


def simplify_mesh_if_needed(mesh: Any) -> Any:
    """Try trimesh's optional quadric decimator before applying the face cap."""
    try:
        triangle_count = int(len(mesh.faces))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError("The CAD backend returned an invalid mesh.") from exc
    if triangle_count <= MAX_TRIANGLES:
        return mesh
    try:
        simplified = mesh.simplify_quadric_decimation(face_count=MAX_TRIANGLES)
        simplified_count = int(len(simplified.faces))
    except Exception as exc:
        raise ValidationError(
            "The CAD mesh exceeds the triangle limit and could not be simplified."
        ) from exc
    if simplified_count > MAX_TRIANGLES:
        raise ValidationError("The simplified CAD mesh still exceeds the triangle limit.")
    return simplified


def validate_mesh(mesh: Any) -> MeshCheck:
    """Reject empty, non-manifold, oversized, or implausibly scaled output."""
    try:
        triangle_count = int(len(mesh.faces))
        bounds_raw = mesh.bounds
        bounds = tuple(tuple(float(value) for value in row) for row in bounds_raw)
        watertight = bool(mesh.is_watertight)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError("The CAD backend returned an invalid mesh.") from exc
    if triangle_count <= 0:
        raise ValidationError("The CAD model contains no triangles.")
    if triangle_count > MAX_TRIANGLES:
        raise ValidationError(
            f"The CAD model has {triangle_count} triangles; the limit is {MAX_TRIANGLES}."
        )
    if len(bounds) != 2 or any(len(row) != 3 for row in bounds):
        raise ValidationError("The CAD model has invalid dimensions.")
    dimensions = [bounds[1][axis] - bounds[0][axis] for axis in range(3)]
    if any(
        not math.isfinite(value) or value <= 0 or value > MAX_BOUNDING_DIMENSION_MM
        for value in dimensions
    ):
        raise ValidationError("The CAD model dimensions are empty or outside the safe range.")
    if not watertight:
        raise ValidationError("The CAD model is not watertight.")
    return MeshCheck(watertight, triangle_count, (bounds[0], bounds[1]))
