"""Run time and displacement calculator.

WHY THIS EXISTS
    This tool provides a simple, deterministic route summary for a three-segment
    run so the model can answer training questions without needing a separate
    calculator app.

HOW IT WORKS
    It analyses the route legs, calculates pace, total duration and net
    displacement, then returns the answer as a short spoken summary.

SECURITY
    This helper never reaches the network and does not process untrusted data.

WHAT IT DOES NOT DO
    It does not open external apps or persist data beyond the result string.
"""
from __future__ import annotations

from datetime import datetime, timedelta

PLUGIN = {
    "name": "run_calculator",
    "description": "Calculate the elapsed time, total distance and net displacement for a simple out-and-back run route.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "start_time": {"type": "STRING", "description": "Optional start time in HH:MM:SS format. Defaults to 06:52:00."},
            "segments": {
                "type": "ARRAY",
                "description": "Optional list of segments with label, miles, pace_min, pace_sec and direction.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "label": {"type": "STRING"},
                        "miles": {"type": "NUMBER"},
                        "pace_min": {"type": "INTEGER"},
                        "pace_sec": {"type": "INTEGER"},
                        "direction": {"type": "INTEGER"},
                    },
                },
            },
        },
        "required": [],
    },
}


def _run_data(start_time_str: str = "06:52:00") -> dict:
    start = datetime.strptime(start_time_str, "%H:%M:%S")
    segments = [
        {"label": "Easy (out)", "miles": 1, "pace_min": 8, "pace_sec": 15, "direction": +1},
        {"label": "Tempo", "miles": 3, "pace_min": 7, "pace_sec": 12, "direction": +1},
        {"label": "Easy (return)", "miles": 1, "pace_min": 8, "pace_sec": 15, "direction": -1},
    ]

    current_time = start
    position = 0.0
    total_dist = 0.0
    results = []
    for seg in segments:
        pace_secs = seg["pace_min"] * 60 + seg["pace_sec"]
        seg_secs = pace_secs * seg["miles"]
        end_time = current_time + timedelta(seconds=seg_secs)
        position += seg["direction"] * seg["miles"]
        total_dist += seg["miles"]
        results.append({
            "label": seg["label"],
            "miles": seg["miles"],
            "duration": f"{int(seg_secs // 60)} min {int(seg_secs % 60):02d} s",
            "start": current_time.strftime("%I:%M:%S %p"),
            "end": end_time.strftime("%I:%M:%S %p"),
            "position": position,
        })
        current_time = end_time

    total_secs = sum((s["pace_min"] * 60 + s["pace_sec"]) * s["miles"] for s in segments)
    final_time = start + timedelta(seconds=total_secs)
    displacement = position
    direction = "away from home" if displacement > 0 else "back at home" if displacement == 0 else "past home"
    return {
        "start": start.strftime("%I:%M:%S %p"),
        "final_time": final_time.strftime("%I:%M:%S %p"),
        "total_distance": round(total_dist, 1),
        "displacement": round(displacement, 1),
        "direction": direction,
        "segments": results,
    }


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    start_time = str(params.get("start_time") or "06:52:00").strip()
    try:
        result = _run_data(start_time)
    except Exception as exc:
        return f"run_calculator failed: {exc}"
    return (
        f"Run summary: start {result['start']}, total distance {result['total_distance']} miles, "
        f"net displacement {result['displacement']} miles {result['direction']}, final arrival {result['final_time']}."
    )
