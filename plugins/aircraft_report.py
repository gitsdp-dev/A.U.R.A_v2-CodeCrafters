"""
Aircraft report — what is flying around you, and a heads-up when one comes close.

    "is there a plane over the house?"      → the aircraft inside your alert radius
    "what's the nearest plane?"             → one or two sentences about it
    "what's flying around?"                 → a list, and a radar picture on the HUD
    "tell me when a plane comes within 1 km, check every 60 seconds"
                                            → a background watch that speaks up
    "stop watching"                         → ends it

NO ACCOUNT, NO KEY
    Positions come from the community ADS-B networks that publish their data
    openly — adsb.lol first, adsb.fi when it does not answer (same format, a
    second source rather than a second format). Routes and aircraft details come
    from adsbdb.com. Measured 2026-09-18 over Istanbul, 25 NM: both feeds
    answered in 0.2 s with 7-9 aircraft; adsbdb answered in 0.3 s.
    airplanes.live refused every request (403), so it is not used.

    Coverage is volunteer receivers: dense over most of Europe, North America
    and big cities elsewhere, thin in some regions. "No aircraft" means none that
    these networks can see — the plugin says which network it asked, and the
    TEST button in the settings shows what is visible from your location.

YOUR LOCATION
    Set once in ⚙ → PLUGIN SETTINGS → AIRCRAFT REPORT: coordinates ("41.0082,
    28.9784") or a place name in any language. A name is looked up once with
    OpenStreetMap's geocoder (verified with Turkish, Japanese, German and Arabic
    names) and cached; coordinates never leave the machine except inside the
    position query itself.

WHY THE WATCH PREDICTS INSTEAD OF ONLY COUNTING WHO IS INSIDE
    An airliner at 800 km/h covers 13 km a minute. Polling every 60 seconds and
    alerting only for aircraft found INSIDE a 1 km circle would miss almost every
    one — it enters and leaves between two polls. So each poll asks for a wide
    enough area, projects every aircraft along its track, and alerts for any
    whose closest approach falls inside your radius before the next poll. The
    alert says how far and in how many seconds, which is also more useful than
    "it is here".

NOTHING HERE KNOWS A LANGUAGE
    The tool returns facts (numbers, codes, names as published) and the
    assistant says them in the user's language. On screen, directions are arrows
    and units are symbols.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Optional

_NS = "aircraft_report"
BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = BASE_DIR / "memory" / "aircraft_report_cache.json"   # git-ignored pattern
OUT_DIR = BASE_DIR / "reports" / "aircraft_report"

_UA = "MarkJARVIS-aircraft_report/1.0 (open-source desktop assistant plugin)"
_FEEDS = (
    ("adsb.lol", "https://api.adsb.lol/v2/point/{lat:.5f}/{lon:.5f}/{nm}"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v2/lat/{lat:.5f}/lon/{lon:.5f}/dist/{nm}"),
)
_MAX_NM = 250                     # the feeds' own limit
_STALE_SECONDS = 60               # a position older than this is not "now"
_FT = 0.3048
_KT = 1.852                       # knots → km/h
_MAX_AIRCRAFT_SPEED = 300.0       # m/s — covers every airliner and most jets
_ALERT_REPEAT_SECONDS = 20 * 60   # the same aircraft is announced again after this
_ALERTS_PER_HOUR = 10             # beyond this, alerts go to the log only
_MIN_INTERVAL = 15.0              # the feeds are free; one request per 15 s at most
# Measured over Istanbul: both public networks returned the same 6 aircraft
# within 15 NM, none low over the user's district. Low-flying aircraft are below
# the nearest receiver's horizon, and helicopters and many light aircraft send
# no position at all — the model is told so, so it can say it when asked.
_COVERAGE_NOTE = ("public volunteer receivers: aircraft flying low, most helicopters and "
                  "aircraft without ADS-B position broadcasts are often not visible")


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

def _test_settings(values: dict) -> tuple[bool, str]:
    """The TEST button: resolve the location and count what is visible there."""
    place = _resolve_location(str((values or {}).get("location") or ""), force=True)
    if not place:
        return False, "Location not found — enter coordinates like 41.0082, 28.9784 or a place name."
    radius_km = _num((values or {}).get("radius_km"), 25)
    flights, feed, problem = _fetch(place["lat"], place["lon"], radius_km * 1000)
    if flights is None:
        return False, f"{place['name']} ✓ — but no aircraft feed answered ({problem})."
    airborne = [f for f in flights if not f["on_ground"]]
    return True, (f"{place['name']} ({place['lat']:.4f}, {place['lon']:.4f}) — "
                  f"{len(airborne)} aircraft in the air within {radius_km:g} km ({feed}).")


PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "✈  AIRCRAFT REPORT",
    "fields": [
        {"key": "location", "label": "Your location — coordinates or a place name",
         "type": "text", "placeholder": "41.0082, 28.9784   or   Kadıköy, İstanbul"},
        {"key": "radius_km", "label": "Area for 'what is flying around' (km)",
         "type": "choice", "options": ["5", "10", "25", "50", "100"], "default": "25"},
        {"key": "alert_m", "label": "Alert radius for 'over me' and the watch (metres)",
         "type": "choice", "options": ["500", "1000", "2000", "3000", "5000", "10000"],
         "default": "3000"},
        {"key": "interval", "label": "Watch: check every (seconds)",
         "type": "choice", "options": ["30", "60", "120", "300"], "default": "60"},
        {"key": "max_alt_m", "label": "Watch: only aircraft below (metres)",
         "type": "choice", "options": ["any", "1000", "3000", "5000", "10000"], "default": "any"},
        {"key": "units", "label": "Units", "type": "choice",
         "options": ["metric", "aviation"], "default": "metric"},
        {"key": "radar", "label": "Show a radar picture on the HUD for lists",
         "type": "toggle", "default": True},
    ],
    "action": {"label": "TEST LOCATION", "run": _test_settings},
}


def _setting(key: str, default: Any = "") -> Any:
    try:
        from memory.config_manager import get_plugin_setting
        value = get_plugin_setting(_NS, key, default)
    except Exception:
        return default
    return default if value in (None, "") else value


def _num(value: Any, default: float) -> float:
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _print(text: str) -> None:
    try:
        print(f"[aircraft_report] {text}")
    except Exception:                     # a console that cannot encode a callsign
        pass


def _log(player, text: str) -> None:
    _print(text)
    if player:
        try:
            player.write_log(f"JARVIS: {text}")
        except Exception:
            pass


def _outcome():
    try:
        from core import outcome
        return outcome
    except Exception:
        return None


def _done(**facts) -> str:
    o = _outcome()
    return o.done("aircraft_report", **facts) if o else json.dumps({"status": "done", **facts})


def _failed(reason: str, **facts) -> str:
    o = _outcome()
    return o.failed("aircraft_report", reason, **facts) if o else f"status=failed reason={reason}"


def _ask(question: str, **facts) -> str:
    o = _outcome()
    return o.ask("aircraft_report", question, **facts) if o else f"status=ask question={question}"


# ─────────────────────────────────────────────────────────────────────────────
# Cache (resolved places, routes, aircraft details)
# ─────────────────────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_mem: dict = {"routes": {}, "aircraft": {}}


def _load_cache() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception as exc:
        _print(f"cache not saved: {exc}")


def _get_json(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(4_000_000).decode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Location
# ─────────────────────────────────────────────────────────────────────────────

def parse_coordinates(text: str) -> Optional[tuple[float, float]]:
    """'41.0082, 28.9784', '41.0082 28.9784', '41,0082; 28,9784', '41,0082, 28,9784'.

    A decimal comma is common in half the world, so it is accepted — the only
    ambiguous shape is four integers, read as two numbers with decimal commas.
    """
    parts = re.findall(r"[+-]?\d+(?:[.,]\d+)?", text or "")
    if len(parts) == 4 and all(re.fullmatch(r"[+-]?\d+", p) for p in parts):
        parts = [f"{parts[0]}.{parts[1]}", f"{parts[2]}.{parts[3]}"]
    if len(parts) != 2:
        return None
    rest = re.sub(r"[+-]?\d+(?:[.,]\d+)?", "", text)
    if re.search(r"[^\s,;°NSEWnsew]", rest):
        return None                      # there are words too — a place name with a number
    try:
        lat, lon = (float(p.replace(",", ".")) for p in parts)
    except ValueError:
        return None
    if re.search(r"[Ss]", rest):
        lat = -abs(lat)
    if re.search(r"[Ww]", rest):
        lon = -abs(lon)
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def _resolve_location(text: str = "", force: bool = False) -> Optional[dict]:
    """{"lat", "lon", "name"} for the configured location, or None."""
    text = (text or str(_setting("location", ""))).strip()
    if not text:
        return None
    coords = parse_coordinates(text)
    if coords:
        return {"lat": coords[0], "lon": coords[1], "name": f"{coords[0]:.4f}, {coords[1]:.4f}"}
    with _cache_lock:
        cache = _load_cache()
        hit = (cache.get("places") or {}).get(text)
    if hit and not force:
        return hit
    try:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": text, "format": "jsonv2", "limit": 1})
        found = _get_json(url, timeout=10)
    except Exception as exc:
        _print(f"place lookup failed: {exc}")
        return hit
    if not found:
        return None
    first = found[0]
    place = {"lat": float(first["lat"]), "lon": float(first["lon"]),
             "name": str(first.get("display_name") or text)[:90]}
    with _cache_lock:
        cache = _load_cache()
        cache.setdefault("places", {})[text] = place
        _save_cache(cache)
    return place


# ─────────────────────────────────────────────────────────────────────────────
# Geometry
# ─────────────────────────────────────────────────────────────────────────────

_EARTH = 6_371_000.0


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH * math.asin(min(1.0, math.sqrt(a)))


def move(lat: float, lon: float, bearing: float, metres: float) -> tuple[float, float]:
    """The point `metres` away along `bearing` (great circle)."""
    d = metres / _EARTH
    b = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
_ARROWS = ("↑", "↗", "→", "↘", "↓", "↙", "←", "↖")


def compass(deg: float) -> str:
    return _COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def arrow(deg: float) -> str:
    return _ARROWS[int((deg % 360) / 45 + 0.5) % 8]


def closest_approach(dist: float, bearing: float, speed_ms: float,
                     track: Optional[float]) -> tuple[float, float]:
    """(closest distance in m, seconds until it) on the current straight track.

    Flat-earth over a few tens of kilometres is well inside a metre's error. A
    closest approach already behind the aircraft returns (current distance, 0).
    """
    if track is None or speed_ms <= 1:
        return dist, 0.0
    px = dist * math.sin(math.radians(bearing))        # aircraft east of home
    py = dist * math.cos(math.radians(bearing))        # aircraft north of home
    vx = speed_ms * math.sin(math.radians(track))
    vy = speed_ms * math.cos(math.radians(track))
    t = -(px * vx + py * vy) / (vx * vx + vy * vy)
    if t <= 0:
        return dist, 0.0
    return math.hypot(px + vx * t, py + vy * t), t


# ─────────────────────────────────────────────────────────────────────────────
# Positions
# ─────────────────────────────────────────────────────────────────────────────

_feed_lock = threading.Lock()
_feed_order = [name for name, _ in _FEEDS]


def normalise(raw: dict, home_lat: float, home_lon: float) -> Optional[dict]:
    """One aircraft from the readsb JSON both feeds publish."""
    try:
        lat, lon = float(raw["lat"]), float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    age = _num(raw.get("seen_pos", raw.get("seen")), 0)
    if age > _STALE_SECONDS:
        return None
    alt = raw.get("alt_baro")
    on_ground = alt == "ground"
    alt_m = 0.0 if on_ground else _num(alt if alt is not None else raw.get("alt_geom"), -1) * _FT
    speed_kmh = _num(raw.get("gs"), 0) * _KT
    track = raw.get("track")
    track = _num(track, 0) % 360 if track is not None else None
    # The feeds hand out the LAST received position, and measured over Istanbul
    # some were 20-23 s old — 3.5 km of flight at 300 kt. Carried forward along
    # its track to now, the distance and "passes in N s" are about the present.
    if age > 1 and track is not None and speed_kmh > 0 and not on_ground:
        lat, lon = move(lat, lon, track, speed_kmh / 3.6 * age)
    dist = distance_m(home_lat, home_lon, lat, lon)
    bear = bearing_deg(home_lat, home_lon, lat, lon)
    cpa, cpa_s = closest_approach(dist, bear, speed_kmh / 3.6, track)
    flags = int(_num(raw.get("dbFlags"), 0))
    return {
        "hex": str(raw.get("hex") or "").lower().strip("~"),
        "callsign": str(raw.get("flight") or "").strip(),
        "reg": str(raw.get("r") or "").strip(),
        "type": str(raw.get("t") or "").strip(),
        "desc": str(raw.get("desc") or "").strip(),
        "operator": str(raw.get("ownOp") or "").strip(),
        "on_ground": on_ground,
        "alt_m": alt_m if alt_m >= 0 else None,
        "vs_ms": _num(raw.get("baro_rate", raw.get("geom_rate")), 0) * _FT / 60,
        "speed_kmh": speed_kmh,
        "track": track,
        "lat": lat, "lon": lon,
        "dist_m": dist, "bearing": bear,
        "cpa_m": cpa, "cpa_s": cpa_s,
        "approaching": cpa_s > 0,
        "military": bool(flags & 1),
        "category": str(raw.get("category") or ""),
    }


def _fetch(lat: float, lon: float, radius_m: float) -> tuple[Optional[list], str, str]:
    """(aircraft sorted by distance, feed name, problem). Tries each feed once,
    starting with the one that answered last."""
    nm = max(1, min(_MAX_NM, math.ceil(radius_m / 1852)))
    problems = []
    with _feed_lock:
        order = list(_feed_order)
    for name in order:
        template = dict(_FEEDS)[name]
        try:
            data = _get_json(template.format(lat=lat, lon=lon, nm=nm))
        except Exception as exc:
            problems.append(f"{name}: {type(exc).__name__}")
            continue
        raw = data.get("ac") if isinstance(data, dict) else None
        if raw is None and isinstance(data, dict):
            raw = data.get("aircraft")
        if not isinstance(raw, list):
            problems.append(f"{name}: unexpected answer")
            continue
        flights = [f for f in (normalise(r, lat, lon) for r in raw if isinstance(r, dict)) if f]
        flights = [f for f in flights if f["dist_m"] <= radius_m * 1.02]
        flights.sort(key=lambda f: f["dist_m"])
        with _feed_lock:
            if _feed_order[0] != name:
                _feed_order.remove(name)
                _feed_order.insert(0, name)
        return flights, name, ""
    return None, "", "; ".join(problems) or "no feed configured"


# ─────────────────────────────────────────────────────────────────────────────
# Route and aircraft details (adsbdb.com), cached
# ─────────────────────────────────────────────────────────────────────────────

_DETAIL_TTL = 6 * 3600
_MISS_TTL = 15 * 60


def _cached(kind: str, key: str, fetch) -> Optional[dict]:
    now = time.time()
    with _cache_lock:
        hit = _mem[kind].get(key)
    if hit and now - hit[0] < (_DETAIL_TTL if hit[1] else _MISS_TTL):
        return hit[1]
    try:
        value = fetch()
    except Exception:
        value = None
    with _cache_lock:
        _mem[kind][key] = (now, value)
    return value


def _airport(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    code = item.get("iata_code") or item.get("icao_code") or ""
    city = item.get("municipality") or item.get("name") or ""
    return f"{city} ({code})" if city and code else (city or code)


def _airport_pos(item: Any) -> Optional[tuple[float, float]]:
    try:
        return float(item["latitude"]), float(item["longitude"])
    except (KeyError, TypeError, ValueError):
        return None


def route_fits(lat: float, lon: float, origin, destination) -> bool:
    """Is an aircraft at (lat, lon) plausibly flying this route?

    adsbdb gives the route REGISTERED for a callsign, and airlines reuse
    callsigns. Seen live: THY8HG climbing over Istanbul was listed as
    Ankara → İzmir, a route 330 km away. So a route is kept only if the aircraft
    is near either airport, or near the great-circle path between them and
    somewhere along it. Unknown airport positions keep the route (nothing to
    check it against).
    """
    if not origin or not destination:
        return True
    near_airport = 150_000.0
    if (distance_m(lat, lon, *origin) <= near_airport
            or distance_m(lat, lon, *destination) <= near_airport):
        return True
    d13 = distance_m(*origin, lat, lon) / _EARTH
    b13 = math.radians(bearing_deg(*origin, lat, lon))
    b12 = math.radians(bearing_deg(*origin, *destination))
    cross = math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(b13 - b12)))) * _EARTH
    along = math.acos(max(-1.0, min(1.0, math.cos(d13) / max(1e-12, math.cos(cross / _EARTH))))) * _EARTH
    return abs(cross) <= 120_000.0 and along <= distance_m(*origin, *destination) + near_airport


def route(callsign: str) -> Optional[dict]:
    callsign = re.sub(r"[^A-Za-z0-9]", "", callsign or "").upper()
    if len(callsign) < 3:
        return None

    def fetch():
        data = _get_json(f"https://api.adsbdb.com/v0/callsign/{urllib.parse.quote(callsign)}", 6)
        fr = ((data or {}).get("response") or {}).get("flightroute") if isinstance(data, dict) else None
        if not isinstance(fr, dict):
            return None
        return {"airline": ((fr.get("airline") or {}).get("name") or ""),
                "from": _airport(fr.get("origin")), "to": _airport(fr.get("destination")),
                "from_pos": _airport_pos(fr.get("origin")),
                "to_pos": _airport_pos(fr.get("destination")),
                "flight": fr.get("callsign_iata") or fr.get("callsign") or callsign}
    return _cached("routes", callsign, fetch)


def aircraft_info(hex_id: str) -> Optional[dict]:
    hex_id = re.sub(r"[^0-9a-fA-F]", "", hex_id or "").upper()
    if len(hex_id) != 6:
        return None

    def fetch():
        data = _get_json(f"https://api.adsbdb.com/v0/aircraft/{hex_id}", 6)
        ac = ((data or {}).get("response") or {}).get("aircraft") if isinstance(data, dict) else None
        if not isinstance(ac, dict):
            return None
        return {"type": " ".join(x for x in (ac.get("manufacturer"), ac.get("type")) if x),
                "owner": ac.get("registered_owner") or "",
                "country": ac.get("registered_owner_country_name") or ""}
    return _cached("aircraft", hex_id, fetch)


def _enrich(flight: dict) -> dict:
    """Add route and model details. Two small lookups, cached; never raises."""
    info = dict(flight)
    r = route(flight["callsign"]) if flight["callsign"] else None
    if r:
        info.update({"airline": r["airline"], "flight_no": r["flight"]})
        origin = r.get("from_pos")
        # Descending low toward the airport the route says it LEFT: it is landing
        # there, so the registered route is not today's flight (seen live: an
        # aircraft on final into IST listed as IST → Samsun).
        landing_at_origin = bool(
            origin and flight["track"] is not None and flight["vs_ms"] < -2
            and flight["alt_m"] is not None and flight["alt_m"] < 4000
            and distance_m(flight["lat"], flight["lon"], *origin) < 80_000
            and abs((bearing_deg(flight["lat"], flight["lon"], *origin)
                     - flight["track"] + 180) % 360 - 180) < 60)
        if not landing_at_origin and route_fits(flight["lat"], flight["lon"], origin,
                                                r.get("to_pos")):
            info.update({"route_from": r["from"], "route_to": r["to"]})
        else:
            _print(f"route for {flight['callsign']} ({r['from']} → {r['to']}) does not match "
                   f"where it is — not shown")
    if not flight["desc"] or not flight["operator"]:
        a = aircraft_info(flight["hex"])
        if a:
            info["desc"] = flight["desc"] or a["type"]
            info["operator"] = flight["operator"] or a["owner"]
    return info


def _enrich_many(flights: list) -> list:
    """_enrich for several at once: eight lookups in parallel cost one round trip."""
    if len(flights) <= 1:
        return [_enrich(f) for f in flights]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(flights))) as pool:
        return list(pool.map(_enrich, flights))


# ─────────────────────────────────────────────────────────────────────────────
# Words and numbers for the model and the panel
# ─────────────────────────────────────────────────────────────────────────────

def _units() -> str:
    return "aviation" if str(_setting("units", "metric")) == "aviation" else "metric"


def fmt_distance(m: float, units: str) -> str:
    if units == "aviation":
        return f"{m / 1852:.1f} NM"
    return f"{m:.0f} m" if m < 1000 else f"{m / 1000:.1f} km"


def fmt_alt(m: Optional[float], units: str) -> str:
    if m is None:
        return "?"
    return f"{m / _FT:,.0f} ft".replace(",", " ") if units == "aviation" else f"{m:,.0f} m".replace(",", " ")


def fmt_speed(kmh: float, units: str) -> str:
    return f"{kmh / _KT:.0f} kt" if units == "aviation" else f"{kmh:.0f} km/h"


def _facts(f: dict, units: str) -> dict:
    """One aircraft as facts for the model — codes and numbers, no prose."""
    facts = {
        "callsign": f["callsign"] or None,
        "flight": f.get("flight_no") or None,
        "airline": f.get("airline") or f.get("operator") or None,
        "aircraft": f.get("desc") or f["type"] or None,
        "registration": f["reg"] or None,
        "distance": fmt_distance(f["dist_m"], units),
        "direction_from_user": f"{compass(f['bearing'])} ({f['bearing']:.0f} deg)",
        "altitude": fmt_alt(f["alt_m"], units),
        "speed": fmt_speed(f["speed_kmh"], units),
        "heading": f"{compass(f['track'])} ({f['track']:.0f} deg)" if f["track"] is not None else None,
        "climbing" if f["vs_ms"] > 1.5 else "descending" if f["vs_ms"] < -1.5 else "level": True,
        "from": f.get("route_from") or None,
        "to": f.get("route_to") or None,
        "military": True if f["military"] else None,
    }
    if f["approaching"] and f["cpa_s"] < 30 * 60:
        facts["closest_pass"] = f"{fmt_distance(f['cpa_m'], units)} in {f['cpa_s']:.0f} s"
    elif f["track"] is not None and abs(math.cos(math.radians(f["track"] - f["bearing"]))) < 0.26:
        facts["passing_by_now"] = True          # flying across the line to the user: closest now
    else:
        facts["moving_away"] = True
    return facts


def _line(f: dict, units: str) -> str:
    """One aircraft on the panel. Arrows and units — nothing to translate."""
    name = f["callsign"] or f["reg"] or f["hex"].upper()
    kind = f.get("desc") or f["type"]
    trail = ""
    if f.get("route_from") or f.get("route_to"):
        trail = f"   {f.get('route_from') or '?'} → {f.get('route_to') or '?'}"
    motion = arrow(f["track"]) if f["track"] is not None else "·"
    vs = "⤴" if f["vs_ms"] > 1.5 else "⤵" if f["vs_ms"] < -1.5 else ""
    return (f"✈ {name}" + (f" · {kind}" if kind else "")
            + f"\n    {fmt_distance(f['dist_m'], units)} {arrow(f['bearing'])} · "
              f"{fmt_alt(f['alt_m'], units)}{vs} · {fmt_speed(f['speed_kmh'], units)} {motion}"
            + trail)


# ─────────────────────────────────────────────────────────────────────────────
# Radar picture
# ─────────────────────────────────────────────────────────────────────────────

def _on_gui_thread(job, timeout: float = 10.0):
    from PyQt6.QtCore import QObject, QThread, pyqtSignal
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        return None
    if app.thread() is QThread.currentThread():
        return job()
    done = threading.Event()
    box: dict = {}

    class _Bridge(QObject):
        fire = pyqtSignal()

    bridge = _Bridge()

    def slot():
        try:
            box["result"] = job()
        except Exception as exc:
            _print(f"radar not drawn: {exc}")
        finally:
            done.set()

    bridge.moveToThread(app.thread())
    bridge.fire.connect(slot)
    bridge.fire.emit()
    done.wait(timeout)
    return box.get("result")


def _accent() -> str:
    try:
        cfg = json.loads((BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))
        color = str(cfg.get("ui_color") or "")
        if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            return color
    except Exception:
        pass
    return "#38d6ff"


def draw_radar(flights: list, radius_m: float, alert_m: float, units: str,
               out_path: Path) -> Optional[str]:
    """A top-down radar: you in the middle, north up, rings, each aircraft an
    arrow pointing where it flies, labelled with its callsign and altitude."""

    def job():
        from PyQt6.QtCore import QPointF, QRectF, Qt
        from PyQt6.QtGui import (QColor, QFont, QGuiApplication, QImage, QPainter,
                                 QPainterPath, QPen, QPolygonF)
        size = 1100
        img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor("#070c11"))
        accent = QColor(_accent())
        p = QPainter(img)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            c = QPointF(size / 2, size / 2)
            r_px = size / 2 - 70
            scale = r_px / radius_m
            font = QFont(QGuiApplication.font())
            font.setPixelSize(19)
            small = QFont(font)
            small.setPixelSize(16)

            # rings at round distances
            ring = accent.darker(170)
            p.setFont(small)
            for i in range(1, 5):
                rr = r_px * i / 4
                p.setPen(QPen(ring, 1.4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, rr, rr)
                p.setPen(QColor("#5f7686"))
                p.drawText(QPointF(c.x() + 6, c.y() - rr + 20), fmt_distance(radius_m * i / 4, units))
            p.setPen(QPen(ring, 1))
            p.drawLine(QPointF(c.x(), c.y() - r_px), QPointF(c.x(), c.y() + r_px))
            p.drawLine(QPointF(c.x() - r_px, c.y()), QPointF(c.x() + r_px, c.y()))
            # north: a filled triangle above the top ring, no letter
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            top = c.y() - r_px - 14
            p.drawPolygon(QPolygonF([QPointF(c.x(), top - 26), QPointF(c.x() - 11, top),
                                     QPointF(c.x() + 11, top)]))
            # alert radius
            if 0 < alert_m < radius_m:
                pen = QPen(QColor("#ffb020"), 2)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(QColor(255, 176, 32, 22))
                p.drawEllipse(c, alert_m * scale, alert_m * scale)
            # you
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(accent)
            p.drawEllipse(c, 8, 8)

            # aircraft, nearest first so the nearest get the best label spots
            p.setFont(font)
            placed: list = []
            for f in flights:
                if f["dist_m"] <= radius_m:
                    ang = math.radians(f["bearing"])
                    q = QPointF(c.x() + math.sin(ang) * f["dist_m"] * scale,
                                c.y() - math.cos(ang) * f["dist_m"] * scale)
                    placed.append(QRectF(q.x() - 12, q.y() - 16, 24, 30))   # the icons themselves
            for f in sorted(flights, key=lambda x: x["dist_m"]):
                if f["dist_m"] > radius_m:
                    continue
                ang = math.radians(f["bearing"])
                pos = QPointF(c.x() + math.sin(ang) * f["dist_m"] * scale,
                              c.y() - math.cos(ang) * f["dist_m"] * scale)
                inside = f["dist_m"] <= alert_m
                col = QColor("#ffb020") if inside else QColor("#e8f3fa")
                if f["on_ground"]:
                    col = QColor("#61717d")
                p.save()
                p.translate(pos)
                p.rotate(f["track"] if f["track"] is not None else 0)
                shape = QPainterPath()
                shape.moveTo(0, -16)
                shape.lineTo(10, 12)
                shape.lineTo(0, 6)
                shape.lineTo(-10, 12)
                shape.closeSubpath()
                p.setPen(QPen(QColor("#070c11"), 2))
                p.setBrush(col)
                p.drawPath(shape)
                p.restore()
                label = f["callsign"] or f["reg"] or f["hex"].upper()
                sub = fmt_alt(f["alt_m"], units)
                width = max(p.fontMetrics().horizontalAdvance(label), 90) + 6
                # Right of the aircraft, else left, below, above — the first
                # spot no earlier label occupies. Near an airport aircraft
                # queue in a line, and stacked labels are unreadable.
                spots = [(16, -24), (-16 - width, -24), (-width / 2, 18), (-width / 2, -66)]
                rect = None
                for dx, dy in spots:
                    candidate = QRectF(pos.x() + dx, pos.y() + dy, width, 46)
                    if not any(candidate.intersects(r) for r in placed):
                        rect = candidate
                        break
                if rect is None:                 # crowded: callsign only, first spot
                    rect = QRectF(pos.x() + 16, pos.y() - 24, width, 24)
                    sub = ""
                placed.append(rect)
                p.setPen(col)
                p.drawText(QRectF(rect.x(), rect.y(), rect.width() + 60, 24),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
                if sub:
                    p.setPen(QColor("#8ba3b5"))
                    p.setFont(small)
                    p.drawText(QRectF(rect.x(), rect.y() + 24, rect.width() + 60, 22),
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub)
                    p.setFont(font)
        finally:
            p.end()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return str(out_path) if img.save(str(out_path), "PNG") else None

    try:
        return _on_gui_thread(job)
    except Exception as exc:
        _print(f"radar not drawn: {exc}")
        return None


def _prune(keep: int = 20) -> None:
    try:
        files = sorted(OUT_DIR.glob("radar_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# The watch
# ─────────────────────────────────────────────────────────────────────────────

class _Watch:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.config: dict = {}
        self.announced: dict[str, float] = {}
        self.alerts = deque()
        self.muted_note = False
        self.polls = 0
        self.failures = 0
        self.last_error = ""
        self.last_poll = 0.0
        self.last_seen = 0

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive() and not self.stop.is_set())


_watch = _Watch()


def _say(player, instruction: str) -> None:
    if player and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


def watch_candidates(flights: list, alert_m: float, horizon_s: float,
                     max_alt_m: Optional[float]) -> list:
    """Aircraft inside the alert radius now, or whose straight track brings them
    inside it before the next check."""
    out = []
    for f in flights:
        if f["on_ground"]:
            continue
        if max_alt_m is not None and f["alt_m"] is not None and f["alt_m"] > max_alt_m:
            continue
        inside = f["dist_m"] <= alert_m
        coming = f["approaching"] and f["cpa_m"] <= alert_m and f["cpa_s"] <= horizon_s
        if inside or coming:
            out.append(f)
    return out


def _watch_loop(player) -> None:
    w = _watch
    cfg = w.config
    units = _units()
    while not w.stop.is_set():
        if cfg["until"] and time.time() >= cfg["until"]:
            _log(player, "Aircraft watch finished.")
            _say(player, "The aircraft watch the user asked for has just ended. Mention it in "
                         "one short sentence.")
            break
        started = time.time()
        # Ask for enough area that anything able to reach the alert radius
        # before the next check is already in the answer.
        reach = cfg["alert_m"] + max(15_000.0, _MAX_AIRCRAFT_SPEED * cfg["interval"])
        flights, feed, problem = _fetch(cfg["lat"], cfg["lon"], reach)
        w.polls += 1
        w.last_poll = time.time()
        if flights is None:
            w.failures += 1
            w.last_error = problem
            if w.failures == 5:
                _log(player, f"Aircraft watch: the position feeds are not answering ({problem}).")
                _say(player, "Tell the user in one short sentence that the aircraft watch cannot "
                             "reach the flight-tracking service right now and keeps trying.")
        else:
            if w.failures >= 5:
                _log(player, "Aircraft watch: the feed is answering again.")
            w.failures, w.last_error = 0, ""
            w.last_seen = len(flights)
            horizon = cfg["interval"] + 15
            now = time.time()
            fresh = [f for f in watch_candidates(flights, cfg["alert_m"], horizon, cfg["max_alt_m"])
                     if now - w.announced.get(f["hex"], 0) > _ALERT_REPEAT_SECONDS]
            if fresh:
                for f in fresh:
                    w.announced[f["hex"]] = now
                while w.alerts and now - w.alerts[0] > 3600:
                    w.alerts.popleft()
                fresh = _enrich_many(sorted(fresh, key=lambda x: x["cpa_m"])[:3])
                lines = []
                for f in fresh:
                    facts = {k: v for k, v in _facts(f, units).items() if v not in (None, False)}
                    lines.append("; ".join(f"{k}: {v}" for k, v in facts.items()))
                    _log(player, _line(f, units).replace("\n    ", " — "))
                if player:
                    try:
                        player.show_content("✈ " + " · ".join(
                            f["callsign"] or f["reg"] or f["hex"].upper() for f in fresh)[:44],
                            "\n\n".join(_line(f, units) for f in fresh))
                    except Exception:
                        pass
                if len(w.alerts) < _ALERTS_PER_HOUR:
                    w.alerts.append(now)
                    _say(player,
                         "[Aircraft watch alert — the user asked to be told when aircraft come "
                         f"within {fmt_distance(cfg['alert_m'], units)} of their location.] "
                         + " | ".join(lines)
                         + " — Tell the user in one or two short, natural sentences: what it "
                           "is, where it is relative to them, and how close it passes. Mention "
                           "a second aircraft only in a few words.")
                elif not w.muted_note:
                    w.muted_note = True
                    _say(player, "Tell the user in one short sentence that many aircraft are "
                                 "passing, so for the next hour you will only list them in the "
                                 "log instead of speaking each one.")
            for key in [k for k, t in w.announced.items() if now - t > 3 * 3600]:
                w.announced.pop(key, None)
        w.stop.wait(max(0.0, cfg["interval"] - (time.time() - started)))
    with w.lock:
        w.stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# Actions
# ─────────────────────────────────────────────────────────────────────────────

def _need_location() -> str:
    return _ask("the user's location is not set — ask them to enter it in Settings → Plugin "
                "settings → Aircraft report (coordinates or a place name), or to say it now "
                "and pass it as 'location'",
                reason="no location configured")


def _place(parameters: dict) -> Optional[dict]:
    spoken = str(parameters.get("location") or "").strip()
    if spoken:
        place = _resolve_location(spoken)
        if place:
            return place
    return _resolve_location()


def _alert_radius(parameters: dict) -> float:
    if parameters.get("radius_m") not in (None, ""):
        return max(100.0, min(100_000.0, _num(parameters.get("radius_m"), 3000)))
    if parameters.get("radius_km") not in (None, ""):
        return max(100.0, min(100_000.0, _num(parameters.get("radius_km"), 3) * 1000))
    return max(100.0, _num(_setting("alert_m", 3000), 3000))


def _area_radius(parameters: dict) -> float:
    if parameters.get("radius_km") not in (None, ""):
        return max(1000.0, min(_MAX_NM * 1852.0, _num(parameters.get("radius_km"), 25) * 1000))
    if parameters.get("radius_m") not in (None, ""):
        return max(1000.0, min(_MAX_NM * 1852.0, _num(parameters.get("radius_m"), 25000)))
    return max(1000.0, _num(_setting("radius_km", 25), 25) * 1000)


def _nearest(parameters: dict, player) -> str:
    place = _place(parameters)
    if not place:
        return _need_location()
    units = _units()
    radius = _area_radius(parameters)
    flights, feed, problem = _fetch(place["lat"], place["lon"], radius)
    if flights is None:
        return _failed(f"no flight-tracking feed answered ({problem})")
    airborne = [f for f in flights if not f["on_ground"]]
    if not airborne:
        return _done(aircraft_in_the_air=0, area=fmt_distance(radius, units), source=feed,
                     note="nothing these volunteer networks can see — coverage varies by region")
    f = _enrich(airborne[0])
    if player:
        try:
            player.show_content("✈ " + (f["callsign"] or f["reg"] or f["hex"].upper()),
                                _line(f, units))
        except Exception:
            pass
    return (_done(**{k: v for k, v in _facts(f, units).items() if v not in (None, False)},
                  others_in_area=len(airborne) - 1, area=fmt_distance(radius, units), source=feed,
                  coverage_note=_COVERAGE_NOTE)
            + "\nAnswer in one or two short sentences in the user's language. Mention the "
              "coverage note only if the user says they see or hear an aircraft that is not "
              "in this answer.")


def _overhead(parameters: dict, player) -> str:
    place = _place(parameters)
    if not place:
        return _need_location()
    units = _units()
    alert = _alert_radius(parameters)
    reach = alert + 60 * _MAX_AIRCRAFT_SPEED
    flights, feed, problem = _fetch(place["lat"], place["lon"], reach)
    if flights is None:
        return _failed(f"no flight-tracking feed answered ({problem})")
    now_in = [f for f in flights if not f["on_ground"] and f["dist_m"] <= alert]
    soon = [f for f in watch_candidates(flights, alert, 60, None) if f not in now_in]
    if not now_in and not soon:
        nearest = next((f for f in flights if not f["on_ground"]), None)
        return _done(overhead=0, radius=fmt_distance(alert, units), source=feed,
                     nearest=(f"{fmt_distance(nearest['dist_m'], units)} "
                              f"{compass(nearest['bearing'])}" if nearest else None),
                     coverage_note=_COVERAGE_NOTE)
    shown = _enrich_many((now_in + soon)[:3])
    if player:
        try:
            player.show_content("✈ " + fmt_distance(alert, units),
                                "\n\n".join(_line(f, units) for f in shown))
        except Exception:
            pass
    facts = {"overhead_now": len(now_in), "arriving_within_60s": len(soon),
             "radius": fmt_distance(alert, units), "source": feed}
    for i, f in enumerate(shown, 1):
        for k, v in _facts(f, units).items():
            if v not in (None, False) and k in ("callsign", "airline", "aircraft", "distance",
                                                "altitude", "from", "to", "closest_pass"):
                facts[f"a{i}_{k}"] = v
    return _done(**facts) + "\nAnswer in one or two short sentences in the user's language."


def _list(parameters: dict, player) -> str:
    place = _place(parameters)
    if not place:
        return _need_location()
    units = _units()
    radius = _area_radius(parameters)
    flights, feed, problem = _fetch(place["lat"], place["lon"], radius)
    if flights is None:
        return _failed(f"no flight-tracking feed answered ({problem})")
    airborne = [f for f in flights if not f["on_ground"]]
    top = _enrich_many(airborne[:8])
    picture = "none"
    if player:
        try:
            body = "\n\n".join(_line(f, units) for f in top) or "—"
            if len(airborne) > len(top):
                body += f"\n\n+{len(airborne) - len(top)} ✈"
            player.show_content(f"✈ {len(airborne)} · {fmt_distance(radius, units)}", body)
        except Exception:
            pass
        if bool(_setting("radar", True)) and airborne:
            name = f"radar_{time.strftime('%Y%m%d_%H%M%S')}.png"
            path = draw_radar(airborne, radius, _alert_radius({}), units, OUT_DIR / name)
            _prune()
            if path:
                try:
                    player.show_image(path, f"✈ {len(airborne)}")
                    picture = "radar"
                except Exception:
                    pass
    summary = [f"{f['callsign'] or f['reg'] or f['hex'].upper()} "
               f"{fmt_distance(f['dist_m'], units)} {compass(f['bearing'])} "
               f"{fmt_alt(f['alt_m'], units)}"
               + (f" {f.get('route_from')}->{f.get('route_to')}" if f.get("route_to") else "")
               for f in top[:5]]
    return (_done(aircraft_in_the_air=len(airborne), on_ground=len(flights) - len(airborne),
                  area=fmt_distance(radius, units), nearest_five=" | ".join(summary) or None,
                  shown=picture, source=feed)
            + "\nSummarise in one or two short sentences; the list is on screen.")


def _start_watch(parameters: dict, player) -> str:
    place = _place(parameters)
    if not place:
        return _need_location()
    units = _units()
    interval = max(_MIN_INTERVAL, min(600.0, _num(parameters.get("interval_seconds"),
                                          _num(_setting("interval", 60), 60))))
    minutes = _num(parameters.get("duration_minutes"), 0)
    max_alt = parameters.get("max_altitude_m")
    if max_alt in (None, ""):
        max_alt = _setting("max_alt_m", "any")
    max_alt_m = None if str(max_alt).strip().lower() == "any" else _num(max_alt, 0) or None
    config = {"lat": place["lat"], "lon": place["lon"], "name": place["name"],
              "alert_m": _alert_radius(parameters), "interval": interval,
              "max_alt_m": max_alt_m,
              "until": time.time() + minutes * 60 if minutes > 0 else 0.0,
              "started": time.time()}
    with _watch.lock:
        if _watch.running():
            _watch.stop.set()
            _watch.thread.join(timeout=2)
        _watch.stop = threading.Event()
        _watch.config = config
        _watch.announced.clear()
        _watch.alerts.clear()
        _watch.muted_note = False
        _watch.polls = _watch.failures = 0
        _watch.thread = threading.Thread(target=_watch_loop, args=(player,), daemon=True,
                                         name="aircraft-watch")
        _watch.thread.start()
    _log(player, f"Aircraft watch on — {fmt_distance(config['alert_m'], units)}, "
                 f"every {interval:.0f} s.")
    return _done(watch="started", radius=fmt_distance(config["alert_m"], units),
                 check_every=f"{interval:.0f} s",
                 below_altitude=fmt_alt(max_alt_m, units) if max_alt_m else None,
                 for_minutes=int(minutes) if minutes > 0 else None,
                 until_stopped=True if minutes <= 0 else None, location=place["name"])


def _stop_watch(player) -> str:
    with _watch.lock:
        was = _watch.running()
        _watch.stop.set()
    if not was:
        return _done(watch="was_not_running")
    _log(player, "Aircraft watch off.")
    return _done(watch="stopped", alerts_given=len(_watch.announced))


def _watch_status() -> str:
    if not _watch.running():
        return _done(watch="off")
    cfg, units = _watch.config, _units()
    left = int((cfg["until"] - time.time()) / 60) if cfg["until"] else None
    return _done(watch="on", radius=fmt_distance(cfg["alert_m"], units),
                 check_every=f"{cfg['interval']:.0f} s", minutes_left=left,
                 checks_done=_watch.polls, aircraft_announced=len(_watch.announced),
                 aircraft_in_view_last_check=_watch.last_seen,
                 feed_problem=_watch.last_error or None)


def _details(parameters: dict, player) -> str:
    """One specific aircraft, by callsign or registration, if it is in the area."""
    wanted = re.sub(r"[^A-Za-z0-9]", "", str(parameters.get("callsign") or "")).upper()
    if not wanted:
        return _nearest(parameters, player)
    place = _place(parameters)
    if not place:
        return _need_location()
    units = _units()
    radius = max(_area_radius(parameters), 100_000.0)
    flights, feed, problem = _fetch(place["lat"], place["lon"], radius)
    if flights is None:
        return _failed(f"no flight-tracking feed answered ({problem})")
    for f in flights:
        keys = {re.sub(r"[^A-Za-z0-9]", "", x).upper() for x in (f["callsign"], f["reg"], f["hex"])}
        if wanted in keys:
            f = _enrich(f)
            if player:
                try:
                    player.show_content("✈ " + (f["callsign"] or f["reg"]), _line(f, units))
                except Exception:
                    pass
            return (_done(**{k: v for k, v in _facts(f, units).items() if v not in (None, False)},
                          source=feed)
                    + "\nAnswer in one or two short sentences in the user's language.")
    r = route(wanted)
    return _done(found_nearby=False, searched=fmt_distance(radius, units),
                 route_from=(r or {}).get("from") or None, route_to=(r or {}).get("to") or None,
                 airline=(r or {}).get("airline") or None)


_ACTIONS = ("nearest", "overhead", "list", "watch", "stop_watch", "watch_status", "details")


def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters if isinstance(parameters, dict) else {}
    action = str(parameters.get("action") or "nearest").strip().lower()
    if action not in _ACTIONS:
        import difflib
        close = difflib.get_close_matches(action, _ACTIONS, n=1, cutoff=0.6)
        action = close[0] if close else "nearest"
    try:
        if action == "overhead":
            return _overhead(parameters, player)
        if action == "list":
            return _list(parameters, player)
        if action == "watch":
            return _start_watch(parameters, player)
        if action == "stop_watch":
            return _stop_watch(player)
        if action == "watch_status":
            return _watch_status()
        if action == "details":
            return _details(parameters, player)
        return _nearest(parameters, player)
    except Exception as exc:
        _log(player, f"Aircraft report failed: {type(exc).__name__}: {exc}")
        return _failed(f"{type(exc).__name__}: {exc}")


PLUGIN = {
    "name": "aircraft_report",
    "description": (
        "Live aircraft around the user's location (public ADS-B flight tracking, no account). "
        "Use in ANY language whenever the user asks about planes, aircraft, flights or "
        "helicopters near them or overhead, or wants to be told when one comes close. "
        "Actions: 'nearest' (default) — the closest aircraft: what it is, where, how high, "
        "where from and to; 'overhead' — is anything inside the alert radius over them right "
        "now; 'list' — everything flying in the area, with a radar on the HUD; 'watch' — start "
        "a background watch that announces aircraft coming within radius_m (e.g. 'tell me "
        "when a plane comes within 1 km' → radius_m 1000; 'check every 60 seconds' → "
        "interval_seconds 60; optional duration_minutes, max_altitude_m); 'stop_watch'; "
        "'watch_status'; 'details' — a specific flight by callsign or registration. Answer "
        "the user briefly (one or two sentences) from the facts returned."
    ),
    "brief": "Live aircraft near the user: nearest, overhead, list+radar, proximity watch.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": list(_ACTIONS),
                       "description": "nearest (default), overhead, list, watch, stop_watch, "
                                      "watch_status or details."},
            "radius_m": {"type": "NUMBER",
                         "description": "Alert radius in metres for watch/overhead (e.g. 1000)."},
            "radius_km": {"type": "NUMBER",
                          "description": "Search area in km for nearest/list."},
            "interval_seconds": {"type": "NUMBER",
                                 "description": "Watch: seconds between checks (15-600)."},
            "duration_minutes": {"type": "NUMBER",
                                 "description": "Watch: stop after this many minutes (omit = "
                                                "until stopped)."},
            "max_altitude_m": {"type": "NUMBER",
                               "description": "Watch: only aircraft below this altitude."},
            "callsign": {"type": "STRING",
                         "description": "details: callsign or registration, e.g. THY1 or TC-JNA."},
            "location": {"type": "STRING",
                         "description": "Only if the user names a different place for this "
                                        "question; otherwise omit to use their saved location."},
        },
        "required": [],
    },
}
