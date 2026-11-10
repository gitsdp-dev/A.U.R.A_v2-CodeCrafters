import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QApplication, QWidget

from earth.earth_widget import EarthWidget

_APP = QApplication.instance() or QApplication([])


class _CompactPanelProbe(QWidget):
    _position_compact = EarthWidget._position_compact
    move_clamped = EarthWidget.move_clamped


def test_site_domain_check_rejects_lookalike_hosts():
    assert EarthWidget._is_site_url(QUrl("https://osirisai.live"))
    assert EarthWidget._is_site_url(QUrl("https://tiles.osirisai.live"))
    assert not EarthWidget._is_site_url(QUrl("https://osirisai.live.example.org"))
    assert not EarthWidget._is_site_url(QUrl("https://notosirisai.live"))


def test_common_load_errors_get_human_readable_messages():
    assert "internet connection" in EarthWidget._human_error(
        "ERR_NAME_NOT_RESOLVED"
    )
    assert "secure connection" in EarthWidget._human_error("TLS certificate error")


def test_compact_panel_aligns_to_room_watch_without_clipping():
    parent = QWidget()
    parent.resize(1080, 700)
    room = QWidget(parent)
    room.setObjectName("RoomWatchBox")
    room.setGeometry(824, 64, 240, 200)
    parent.show()
    room.show()
    _APP.processEvents()

    panel = _CompactPanelProbe(parent)
    panel.resize(*EarthWidget._MIN_SIZE)
    panel._position_compact()

    assert panel.x() + panel.width() == room.x() + room.width()
    assert panel.x() >= 0
    assert panel.x() + panel.width() <= parent.width()

    panel.close()
    parent.close()
