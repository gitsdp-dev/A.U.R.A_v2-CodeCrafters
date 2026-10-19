"""
Run Time & Displacement Calculator
===================================
Route  : Out-and-back
Segment 1 : Easy  1 mile  @ 8:15 /mi   -> away from home
Segment 2 : Tempo 3 miles @ 7:12 /mi   -> away from home
Segment 3 : Easy  1 mile  @ 8:15 /mi   <- returning toward home
"""

from datetime import datetime, timedelta

# --- Configuration ------------------------------------------------------------

START_TIME_STR = "06:52:00"

SEGMENTS = [
    {"label": "Easy (out)",    "miles": 1, "pace_min": 8, "pace_sec": 15, "direction": +1},
    {"label": "Tempo",         "miles": 3, "pace_min": 7, "pace_sec": 12, "direction": +1},
    {"label": "Easy (return)", "miles": 1, "pace_min": 8, "pace_sec": 15, "direction": -1},
]

# --- Calculations -------------------------------------------------------------

start        = datetime.strptime(START_TIME_STR, "%H:%M:%S")
current_time = start
position     = 0.0
total_dist   = 0.0
results      = []

for seg in SEGMENTS:
    pace_secs   = seg["pace_min"] * 60 + seg["pace_sec"]
    seg_secs    = pace_secs * seg["miles"]
    end_time    = current_time + timedelta(seconds=seg_secs)
    position   += seg["direction"] * seg["miles"]
    total_dist += seg["miles"]

    results.append({
        "label":     seg["label"],
        "miles":     seg["miles"],
        "pace":      f"{seg['pace_min']}:{seg['pace_sec']:02d} /mi",
        "duration":  f"{int(seg_secs//60)} min {int(seg_secs%60):02d} s",
        "start_str": current_time.strftime("%I:%M:%S %p"),
        "end_str":   end_time.strftime("%I:%M:%S %p"),
        "arrow":     "--->" if seg["direction"] == +1 else "<---",
        "pos_after": position,
    })

    current_time = end_time

total_secs   = sum((s["pace_min"]*60 + s["pace_sec"]) * s["miles"] for s in SEGMENTS)
final_time   = start + timedelta(seconds=total_secs)
displacement = position
disp_mag     = abs(displacement)

# --- Output -------------------------------------------------------------------

W = 66

def div(c="-"): print("  " + c * W)
def head(t):
    print("\n  " + "=" * W)
    print(f"  {t}")
    print("  " + "=" * W)

head("RUN CALCULATOR — TIME & DISPLACEMENT ANALYSIS")
print(f"  Departure : {start.strftime('%I:%M:%S %p')}\n")
print(f"  {'Segment':<16} {'Pace':<11} {'Dist':>6}  {'Duration':>13}   {'Start':>12} -> {'End':<13} {'Pos':>7}")
div()
for r in results:
    print(f"  {r['label']:<16} {r['pace']:<11} {r['miles']:>4} mi"
          f"  {r['duration']:>13}   {r['start_str']:>12} -> {r['end_str']:<13} {r['pos_after']:>+5.1f} mi")
div()
tm, ts = int(total_secs//60), int(total_secs%60)
print(f"  {'TOTAL':<16} {'':11} {total_dist:>4.0f} mi  {tm} min {ts:02d} s")
print(f"\n  >> You arrive back at : {final_time.strftime('%I:%M:%S %p')}")

# --- ASCII Direction Diagram --------------------------------------------------

head("DISPLACEMENT DIAGRAM")
SCALE = 10
MAX_MI = 4

print("\n  Segment directions:\n")
for r in results:
    print(f"    {r['label']:<16}  {r['arrow']}  {r['miles']} mi  (pos after: {r['pos_after']:+.1f} mi)")

print()
ruler = "  [0 mi]" + "".join(f"{'| '+str(m)+' mi':<{SCALE}}" for m in range(1, MAX_MI+1))
print(ruler)
div()

prev = 0.0
for r in results:
    pos = r["pos_after"]
    lo, hi = sorted([prev, pos])
    seg_bar = " " * int(lo * SCALE) + "#" * int((hi - lo) * SCALE)
    print(f"  {r['label']:<16} |{seg_bar:<{MAX_MI*SCALE}}|  {r['arrow']}  pos={pos:+.1f} mi")
    prev = pos

div()
d_bar = ">" * int(disp_mag * SCALE) if displacement >= 0 else "<" * int(disp_mag * SCALE)
print(f"  {'NET DISPLACEMENT':<16} |{d_bar:<{MAX_MI*SCALE}}|  {displacement:+.1f} mi from home")

print(f"\n  Final position : {displacement:+.1f} miles from starting point")
if displacement == 0:
    print("  Direction      : None — runner is back at home (displacement = 0)")
elif displacement > 0:
    print(f"  Direction      : Away from home (+ direction), {disp_mag:.1f} mile(s) out")
else:
    print(f"  Direction      : Past home (- direction), {disp_mag:.1f} mile(s) beyond")

# --- Physics Summary ----------------------------------------------------------

head("PHYSICS SUMMARY")
print(f"  Total DISTANCE     (scalar) = {total_dist:.1f} miles  — entire path length")
print(f"  Total DISPLACEMENT (vector) = {disp_mag:.1f} mile(s) — straight line from start to finish")
print(f"  Direction of displacement   = {'away from home (+)' if displacement > 0 else 'back at home (0)'}")
print()
print("  KEY INSIGHT:")
print(f"    The runner covers {total_dist:.0f} miles of distance,")
print(f"    but ends {disp_mag:.1f} mile(s) from where they started.")
print("    Distance = scalar (total path, no direction).")
print("    Displacement = vector (net change in position + direction).")
print()
print("  NOTE: Only 1 mile is run on the return leg, so the runner")
print("  does NOT make it all the way back home after these 3 segments.")
print("  They are still 3 miles away. For displacement = 0, a 4th")
print("  segment (3 miles easy, back to home) would be needed.")
print("\n  " + "=" * W + "\n")
