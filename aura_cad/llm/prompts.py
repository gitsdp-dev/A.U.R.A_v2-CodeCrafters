"""Versioned prompts for local code-CAD generation."""

from aura_cad.sandbox.validator import BUILD123D_API, MATH_API

PROMPT_VERSION = "cad-v3"
_CAD_API_RULES = (
    f"Allowed build123d attributes: {', '.join(sorted(BUILD123D_API))}.\n"
    f"Allowed math functions/constants: {', '.join(sorted(MATH_API))}.\n"
    "Only import build123d as bd and math. Math names belong to math; never "
    "write bd.sin or any other bd.<math-name>.\n"
    "Do not use file I/O, network, OS, reflection, or dynamic execution. "
    "Do not call add or .add; combine shapes with + or +=. Keep loops bounded."
)

PLAN_PROMPT = """You plan simple, watertight mechanical or geometric CAD parts.
Use only buildable features. Return JSON with a `parts` array; each item has
`name`, `description`, and numeric `dimensions`. Keep the model lightweight.
Specification: {spec}
"""

CODEGEN_PROMPT = """Return only Python code, no markdown. Use `import build123d as bd`.
Define small helper functions for parts, then define `build()` returning one
Build123d Part, Compound, or Shape.
""" + _CAD_API_RULES + """
Keep dimensions in millimetres.
Example: `import build123d as bd\ndef build():\n    return bd.Cylinder(10, 4)`
Specification: {spec}
Part plan: {plan}
"""

REPAIR_PROMPT = """Fix this build123d script with the smallest necessary change.
Return the complete corrected Python script only.
""" + _CAD_API_RULES + """
Specification: {spec}
Previous script:
{script}
Exact failure/check:
{failure}
"""

REFINE_PROMPT = """Apply the requested change as a minimal patch to this build123d
script. Preserve working parts and return the complete updated script only.
""" + _CAD_API_RULES + """
Current script:
{script}
Requested change: {request}
"""

PART_CODEGEN_PROMPT = """Return only Python code, no markdown. Use `import build123d as bd`.
Define `build()` and one pure, parametric function for each GENERATED part below.
Use each exact function name and parameter name. Annotate every parameter as
`int` or `float`; do not use defaults. Add a short generic docstring to each
part function, with no user-specific text. Keep part functions side-effect free.

""" + _CAD_API_RULES + """
Do not redefine REUSABLE part functions. Call those exact functions from
`build()` using the supplied parameter values. Only GENERATED parts may receive
new geometry definitions. Keep units in millimetres.

Specification: {spec}
Generated parts: {generate_parts}
Reusable parts and parameter values: {reusable_parts}
"""
