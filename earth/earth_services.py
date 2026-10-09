from __future__ import annotations

import math
import re

import requests

_COORDINATES = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$"
)


def geocode_place(place: str) -> tuple[str, float, float]:
    """Return a display name and WGS84 latitude/longitude for a place."""
    match = _COORDINATES.match(place)
    if match:
        latitude, longitude = map(float, match.groups())
        if -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return place.strip(), latitude, longitude

    response = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": place, "count": 1, "language": "en", "format": "json"},
        timeout=6,
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        raise ValueError(f"Could not find “{place}”.")
    result = results[0]
    name = ", ".join(
        part for part in (result.get("name"), result.get("admin1"), result.get("country"))
        if part
    )
    return name, float(result["latitude"]), float(result["longitude"])


def great_circle_km(origin: tuple[float, float],
                    destination: tuple[float, float]) -> float:
    """Calculate the great-circle distance between (lat, lon) coordinates."""
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, destination)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    haversine = min(1.0, max(0.0, haversine))
    return 6371.0088 * 2 * math.atan2(
        math.sqrt(haversine), math.sqrt(1 - haversine)
    )


def route_summary(origin: str, destination: str) -> str:
    """Produce a spoken-summary distance and broad mode estimates."""
    from_name, from_lat, from_lon = geocode_place(origin)
    to_name, to_lat, to_lon = geocode_place(destination)
    distance = great_circle_km((from_lat, from_lon), (to_lat, to_lon))
    lines = [
        f"{from_name} to {to_name}: about {distance:,.0f} km straight-line "
        f"({distance / 0.621371:,.0f} miles)."
    ]
    if distance >= 150:
        flight_hours = max(1.0, distance / 800 + 0.5)
        lines.append(f"Estimated flight time: about {flight_hours:.1f} hours.")
    else:
        lines.append("A flight is unlikely to be a practical option for this distance.")
    lines.append(
        "The map shows available road routing when the routing service can "
        "find a connected route; travel times are estimates, not schedules."
    )
    return " ".join(lines)
