"""Deterministic engine geometry for common builds that need no LLM calls."""

from __future__ import annotations

from aura_cad.models import CadSpec


def engine_script(spec: CadSpec) -> str:
    """Create a bounded, fused engine assembly from clarified engine dimensions."""
    layout = str(spec.slots.get("layout", "Inline")).casefold()
    cylinders = int(spec.slots.get("cylinders", 4))
    if cylinders < 2 or cylinders > 12:
        raise ValueError("Engine cylinder count must be between 2 and 12.")
    if layout == "flat" and cylinders % 2:
        raise ValueError("A flat engine requires an even cylinder count.")

    mode = "flat" if layout == "flat" else "v" if layout.startswith("v") else "inline"
    internals = str(spec.slots.get("detail", "Internals")).casefold() == "internals"
    count = cylinders
    bank_count = count // 2
    length = max(80, (count if mode == "inline" else bank_count) * 22 + 28)
    if mode == "flat":
        engine_block = "bd.Box(length, 44, 28)"
    else:
        engine_block = "bd.Box(length, 48, 28)"
    return (
        "import build123d as bd\n\n"
        "def build():\n"
        f"    count = {count}\n"
        f"    bank_count = {bank_count}\n"
        "    spacing = 22\n"
        f"    length = {length}\n"
        f"    shape = {engine_block}\n"
        f"    if {internals!r}:\n"
        "        shape += bd.Pos(0, 0, -17) * bd.Cylinder(\n"
        "            4, length + 12, rotation=(0, 90, 0)\n"
        "        )\n"
        "    if " + repr(mode) + " == 'inline':\n"
        "        for index in range(count):\n"
        "            x = (index - (count - 1) / 2) * spacing\n"
        "            shape += bd.Pos(x, 0, 29) * bd.Cylinder(9, 42)\n"
        f"            if {internals!r}:\n"
        "                shape += bd.Pos(x, 0, -17) * bd.Cylinder(\n"
        "                    7, 4, rotation=(0, 90, 0)\n"
        "                )\n"
        "    elif " + repr(mode) + " == 'flat':\n"
        "        for index in range(bank_count):\n"
        "            x = (index - (bank_count - 1) / 2) * spacing\n"
        "            shape += bd.Pos(x, 37, 0) * bd.Cylinder(\n"
        "                9, 42, rotation=(-90, 0, 0)\n"
        "            )\n"
        "            shape += bd.Pos(x, -37, 0) * bd.Cylinder(\n"
        "                9, 42, rotation=(90, 0, 0)\n"
        "            )\n"
        "    else:\n"
        "        for index in range(bank_count):\n"
        "            x = (index - (bank_count - 1) / 2) * spacing\n"
        "            shape += bd.Pos(x, 8, 24) * bd.Cylinder(\n"
        "                9, 42, rotation=(35, 0, 0)\n"
        "            )\n"
        "            shape += bd.Pos(x, -8, 24) * bd.Cylinder(\n"
        "                9, 42, rotation=(-35, 0, 0)\n"
        "            )\n"
        "    return shape\n"
    )
