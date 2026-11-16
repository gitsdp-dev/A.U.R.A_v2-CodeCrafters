"""Buildable object templates; add new supported objects here."""

from __future__ import annotations

from aura_cad.models import Slot

_TEMPLATES: dict[str, tuple[Slot, ...]] = {
    "gear": (
        Slot("teeth", "teeth", "How many teeth?", ("12", "20", "32"), 20),
        Slot("module_mm", "module", "What module (tooth size)?", ("1", "2", "3"), 2),
    ),
    "engine": (
        Slot("layout", "layout", "Inline, V-type or flat?", ("Inline", "V-type", "Flat"), "V8"),
        Slot("cylinders", "cylinders", "How many cylinders?", ("4", "6", "8"), 8),
        Slot("detail", "detail", "Simple exterior or visible internals?", ("Exterior", "Internals"), "Internals"),
    ),
    "bracket": (
        Slot("holes", "mounting holes", "How many mounting holes?", ("2", "4", "6"), 4),
        Slot("thickness_mm", "thickness", "Choose a plate thickness in mm.", ("4", "6", "8"), 6),
    ),
    "enclosure": (
        Slot("size", "size", "Small, medium or large enclosure?", ("Small", "Medium", "Large"), "Medium"),
        Slot("ventilation", "ventilation", "Add ventilation slots?", ("Yes", "No"), "No"),
    ),
    "table": (
        Slot("style", "style", "Four legs or a pedestal?", ("Four legs", "Pedestal"), "Four legs"),
        Slot("width_mm", "width", "Choose the tabletop width in mm.", ("600", "900", "1200"), 900),
    ),
    "house": (
        Slot("storeys", "storeys", "One or two storeys?", ("One", "Two"), "One"),
        Slot("roof", "roof", "Flat or pitched roof?", ("Flat", "Pitched"), "Pitched"),
    ),
}

_GENERIC = (
    Slot("purpose", "purpose", "What is the part for?", (), "general use"),
    Slot("size", "size", "Small, medium or large?", ("Small", "Medium", "Large"), "Medium"),
)


def slots_for(object_type: str) -> tuple[Slot, ...]:
    """Return only slots the current generator can represent."""
    return _TEMPLATES.get(object_type, _GENERIC)


def default_description(object_type: str) -> str:
    return f"A simplified, visually clear {object_type} model."
