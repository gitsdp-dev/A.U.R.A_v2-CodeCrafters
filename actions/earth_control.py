"""Gemini tool for the in-app Earth widget and route finder."""

import requests

TOOL = {
    "name": "earth_control",
    "description": (
        "Control A.U.R.A.'s in-app interactive Earth map. Use route with origin "
        "and destination for place-distance or route questions; this opens the "
        "Earth map, plots the route, and displays distance and travel estimates."
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
            return route_summary(origin, destination)
        except (requests.RequestException, ValueError) as exc:
            return (
                f"The Earth route finder opened for {origin} to {destination}, "
                f"but place data is currently unavailable: {exc}"
            )
    return f"Earth widget command sent: {command}."


TOOL["handler"] = earth_control
