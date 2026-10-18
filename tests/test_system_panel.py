import os
import subprocess
import sys


def test_panel_actions_are_not_blocked_by_snapshots_and_external_updates_render():
    script = r'''
import time
from threading import Event

from PyQt6.QtWidgets import QApplication
from core.system_controls import ControlResult, ControlState
from dashboard.system_panel import SystemControlsPanel

palette = {
    "BG": "#000000", "PANEL": "#000000", "PANEL2": "#000000",
    "BORDER": "#5a341a", "PRI": "#ff9224", "PRI_GHO": "#2c1400",
    "TEXT": "#ffdcb0", "TEXT_DIM": "#8a5a34", "DARK": "#000000",
}

class Manager:
    def __init__(self):
        self.snapshot_started = Event()
        self.release_snapshot = Event()
        self.change_finished = Event()
        self.snapshot_calls = 0
        self.requested_state = None

    def snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_calls == 1:
            self.snapshot_started.set()
            self.release_snapshot.wait(2)
            return {"energy_saver": ControlState("energy_saver", False)}
        return {"energy_saver": ControlState("energy_saver", True)}

    def set_state(self, control_id, value):
        self.requested_state = (control_id, value)
        self.change_finished.set()
        return ControlResult(True, ControlState(control_id, value))

def wait_until(app, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert predicate(), "Timed out waiting for the system panel."

app = QApplication([])
manager = Manager()
panel = SystemControlsPanel(manager, palette, refresh_seconds=30)
panel.show()
try:
    assert manager.snapshot_started.wait(1)
    panel.update_control_state("energy_saver", ControlState("energy_saver", False))
    panel._tiles["energy_saver"].click()
    assert manager.change_finished.wait(1)
    wait_until(app, lambda: "energy_saver" not in panel._active_operations)
    assert manager.requested_state == ("energy_saver", True)
    assert panel.states["energy_saver"].value is True

    manager.release_snapshot.set()
    wait_until(app, lambda: manager.snapshot_calls == 2 and not panel._snapshot_in_flight)
    assert panel.states["energy_saver"].value is True
    time.sleep(0.05)
    app.processEvents()
    assert manager.snapshot_calls == 2
finally:
    manager.release_snapshot.set()
    panel._timer.stop()
    panel.close()

class MutableManager:
    def __init__(self):
        self.volume = 31
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return {"volume": ControlState("volume", self.volume)}

mutable_manager = MutableManager()
panel = SystemControlsPanel(mutable_manager, palette, refresh_seconds=30)
panel.show()
try:
    wait_until(app, lambda: not panel._snapshot_in_flight)
    assert panel._sliders["volume"].value() == 31
    mutable_manager.volume = 73
    panel.refresh(force=True)
    wait_until(
        app,
        lambda: mutable_manager.snapshot_calls == 2 and not panel._snapshot_in_flight,
    )
    assert panel._sliders["volume"].value() == 73
    assert panel._values["volume"].text() == "73%"
    panel.update_control_state("volume", ControlState("volume", 88))
    assert panel._sliders["volume"].value() == 88
finally:
    panel._timer.stop()
    panel.close()
'''
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
