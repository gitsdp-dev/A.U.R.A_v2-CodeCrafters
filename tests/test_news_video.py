import json
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

import ui


class _Timer:
    def __init__(self, interval=0):
        self.interval = interval
        self.started = None

    def start(self, value=None):
        self.started = self.interval if value is None else value

    def stop(self):
        self.started = None

    def isActive(self):
        return self.started is not None


class _View:
    def __init__(self):
        self.url = None

    def setUrl(self, url):
        self.url = url.toString()


def test_news_player_starts_live_channel_in_app_with_muted_autoplay():
    url = ui.NewsVideoPanel._watch_url(SimpleNamespace(
        _channel_id=None,
        CHANNEL_HANDLE=ui.NewsVideoPanel.CHANNEL_HANDLE,
    ))
    assert url == "https://www.youtube.com/@RepublicWorld/live?autoplay=1&mute=1"

    candidate_timer = _Timer()
    view = _View()
    panel = SimpleNamespace(
        _closed=False,
        _candidate_seq=0,
        _retry_timer=_Timer(30_000),
        _candidate_timer=candidate_timer,
        _poll_timer=_Timer(),
        _view=view,
        _set_title=lambda _title: None,
        _watch_url=lambda: url,
    )

    ui.NewsVideoPanel._play_next(panel)

    assert view.url == url
    assert candidate_timer.started == 60_000


def test_webview2_javascript_probe_returns_player_state():
    app = QApplication.instance() or QApplication([])
    loop = QEventLoop()
    results = []

    class _Task:
        IsCompleted = True
        Result = json.dumps({"playing": True})

    class _Core:
        def ExecuteScriptAsync(self, _script):
            return _Task()

    view = SimpleNamespace(_core=_Core())
    ui._EdgeView.runJavaScript(
        view,
        "(function(){return {playing:true};})()",
        lambda result: (results.append(result), loop.quit()),
    )
    QTimer.singleShot(1000, loop.quit)
    loop.exec()

    assert results == [{"playing": True}]
