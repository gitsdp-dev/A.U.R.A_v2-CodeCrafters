"""Gemini tool for A.U.R.A.'s in-app OSIRIS panel and route summaries."""

import requests

TOOL = {
    "name": "earth_control",
    "description": (
        "Control A.U.R.A.'s in-app OSIRIS global intelligence panel. Use route "
        "with origin and destination for a place-distance summary; OSIRIS does "
        "not currently expose a documented URL parameter for plotting routes."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "description": "One of: show, hide, toggle, maximize, minimize, route."
            },
            "origin": {
                "type": "STRING",
                "description": "Starting place for a route request."
            },
            "destination": {
                "type": "STRING",
                "description": "Destination place for a route request."
            },
        },
        "required": ["command"],
    },
    "handler": None,
}


def earth_control(parameters: dict, player=None, **_ctx):
    """Send an Earth UI request through the UI's thread-safe signal."""
    params = parameters or {}
    command = str(params.get("command", "show")).strip().lower()
    allowed = {"show", "hide", "toggle", "maximize", "minimize", "route"}
    if command not in allowed:
        return "Earth command not recognized. Use show, hide, toggle, maximize, minimize, or route."
    origin = str(params.get("origin", "")).strip()
    destination = str(params.get("destination", "")).strip()
    if command == "route" and (not origin or not destination):
        return "A route needs both an origin and a destination."
    request = getattr(player, "request_earth_command", None)
    if not callable(request):
        return "The Earth widget is unavailable in the current UI."
    request(command, origin, destination)
    if command == "route":
        from earth.earth_services import route_summary

        try:
            return (
                f"{route_summary(origin, destination)} "
                "The OSIRIS panel is open; it does not support route plotting "
                "through a documented URL parameter."
            )
        except (requests.RequestException, ValueError) as exc:
            return (
                f"The OSIRIS panel opened, but a route summary for {origin} "
                f"to {destination} is currently unavailable: "
                f"{exc}"
            )
    return f"Earth widget command sent: {command}."


TOOL["handler"] = earth_control
