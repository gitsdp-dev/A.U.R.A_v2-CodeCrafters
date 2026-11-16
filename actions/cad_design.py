"""Auto-discovered action shim; integration behavior lives in aura_cad."""

from aura_cad.integration.hooks import run_cad_action

TOOL = {
    "name": "cad_design",
    "description": (
        "Handle CAD model requests and their follow-up answers, confirmations, "
        "refinements, and CAD-version undo. Use for sketch/design/build/create a "
        "3D gear, engine, bracket, enclosure, table, house, or another geometric part. "
        "Pass the user's exact natural-language request in request; do not claim "
        "a build succeeded unless the tool reports a validated exported model."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "request": {
                "type": "STRING",
                "description": "The current user request, answer, confirmation, refinement, or cancellation.",
            }
        },
        "required": ["request"],
    },
    "handler": run_cad_action,
}
