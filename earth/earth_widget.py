from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QPoint, QUrl, Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

try:
    from PyQt6.QtWebChannel import QWebChannel
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except Exception as exc:  # pragma: no cover - exercised on installs without WebEngine
    QWebChannel = None
    QWebEnginePage = None
    QWebEngineSettings = None
    QWebEngineView = None
    _WEBENGINE_IMPORT_ERROR = exc
else:
    _WEBENGINE_IMPORT_ERROR = None

HAVE_WEBENGINE = QWebEngineView is not None

if HAVE_WEBENGINE:
    class _EarthPage(QWebEnginePage):
        """Keep the top-level page local while allowing map data requests."""

        def acceptNavigationRequest(self, url, _navigation_type, is_main_frame):
            if is_main_frame:
                return url.scheme().lower() in {"file", "about", "data"}
            return url.scheme().lower() in {"file", "about", "data"}


    class _EarthWebView(QWebEngineView):
        def __init__(self, parent, wheel_callback):
            super().__init__(parent)
            self._wheel_callback = wheel_callback

        def wheelEvent(self, event):
            if self._wheel_callback(event.angleDelta().y()):
                event.accept()
                return
            super().wheelEvent(event)


class _EarthHeader(QWidget):
    def __init__(self, parent, maximize):
        super().__init__(parent)
        self._maximize = maximize

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._maximize()
            event.accept()
            return
        super().mousePressEvent(event)


class EarthWidget(QWidget):
    """Local QtWebEngine container for A.U.R.A.'s interactive Earth map."""

    # Compact size. The globe is drawn to fit the web view's smaller side, so the
    # view must be roughly square or taller than wide. The header + borders use
    # ~35px, so 244 x 284 leaves a ~242 x 251 map area: the globe fits with a
    # margin and the attribution strip has room at the bottom.
    _MIN_SIZE = (244, 286)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EarthWidget")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#EarthWidget { background: #03080e; "
            "border: 1px solid #8a4f22; border-radius: 6px; }"
        )
        self.on_close = None
        self.user_moved = False
        self._maximized = False
        self._pending_route = None

        layout = QVBoxLayout(self)
        # 1px inset so the header and web view don't paint over the orange border
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        header = _EarthHeader(self, self.maximize)
        header.setStyleSheet("background: #090d12; border-bottom: 1px solid #5a341a;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 6, 4)
        header_layout.setSpacing(6)

        marker = QLabel("◈")
        marker.setStyleSheet("color: #ff9224; background: transparent;")
        header_layout.addWidget(marker)
        title = QLabel("GOD'S EYE")
        title.setStyleSheet(
            "color: #ffdcb0; background: transparent; "
            "font: bold 8pt 'Courier New'; letter-spacing: 1px;"
        )
        header_layout.addWidget(title)
        header_layout.addStretch()

        self._minimize_button = QPushButton("－")
        self._minimize_button.setToolTip("Restore compact Earth panel")
        self._minimize_button.setFixedSize(22, 22)
        self._minimize_button.clicked.connect(self.minimize)
        header_layout.addWidget(self._minimize_button)

        close_button = QPushButton("✕")
        close_button.setToolTip("Close Earth's Eye")
        close_button.setFixedSize(22, 22)
        close_button.clicked.connect(self._close_clicked)
        header_layout.addWidget(close_button)
        layout.addWidget(header)

        self._page = None
        self._view = None
        self._bridge = None
        if HAVE_WEBENGINE:
            self._view = _EarthWebView(self, self._handle_wheel)
            self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            self._view.setStyleSheet("background: #03080e;")
            self._page = _EarthPage(self._view)
            self._page.setBackgroundColor(Qt.GlobalColor.black)
            self._view.setPage(self._page)
            settings = self._page.settings()
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.JavascriptEnabled, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.WebGLEnabled, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
                True,
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
                True,
            )

            from .earth_bridge import EarthBridge

            self._channel = QWebChannel(self._page)
            self._bridge = EarthBridge(self)
            self._bridge.maximizeRequested.connect(self.maximize)
            self._channel.registerObject("earthBridge", self._bridge)
            self._page.setWebChannel(self._channel)
            self._page.loadFinished.connect(self._page_loaded)
            self._page.load(QUrl.fromLocalFile(str(self._index_path())))
            layout.addWidget(self._view, 1)
        else:
            from .earth_fallback import EarthFallbackWidget

            self._view = EarthFallbackWidget(self)
            layout.addWidget(self._view, 1)
            print(
                f"[Earth] QtWebEngine unavailable ({_WEBENGINE_IMPORT_ERROR}); "
                "using the built-in fallback."
            )

        self._minimize_button.hide()
        self.resize(*self._MIN_SIZE)
        self.hide()

    @staticmethod
    def _index_path() -> Path:
        return Path(__file__).resolve().parent / "web" / "index.html"

    def _page_loaded(self, ok):
        if not ok:
            print(f"[Earth] Could not load local map page: {self._index_path()}")
            return
        if self._pending_route:
            self._dispatch_route(*self._pending_route)
        self._sync_web_mode()
        self._fit_map_view()

    def _sync_web_mode(self):
        if self._page is not None:
            mode = "maximized" if self._maximized else "compact"
            self._page.runJavaScript(
                f"window.auraEarth && window.auraEarth.setDisplayMode("
                f"{json.dumps(mode)});"
            )
            self._fit_map_view()

    def _fit_map_view(self):
        if self._page is not None:
            self._page.runJavaScript(
                "window.auraEarth && window.auraEarth.resize();"
            )

    def _close_clicked(self):
        if callable(self.on_close):
            self.on_close()
        else:
            self.hide()

    def _handle_wheel(self, delta):
        if delta > 0 and not self._maximized:
            self.maximize()
            return True
        if delta < 0 and self._maximized:
            self.minimize()
            return True
        return False

    def maximize(self):
        parent = self.parentWidget()
        if parent is None or self._maximized:
            return
        self._maximized = True
        self._minimize_button.show()
        self.setGeometry(parent.rect())
        self.raise_()
        self._sync_web_mode()

    def minimize(self):
        parent = self.parentWidget()
        if parent is None or not self._maximized:
            return
        self._maximized = False
        self._minimize_button.hide()
        self.resize(*self._MIN_SIZE)
        self._position_compact()
        self.raise_()
        self._sync_web_mode()

    def _position_compact(self):
        parent = self.parentWidget()
        if parent is None:
            return
        room = parent.findChild(QWidget, "RoomWatchBox")
        if room is not None and room.isVisible():
            x = room.x()
            y = room.y() + room.height() + 8
        else:
            x = parent.width() - self.width() - 16
            y = 64
        self.move_clamped(QPoint(x, y))

    def move_clamped(self, point):
        parent = self.parentWidget()
        if parent is None:
            self.move(point)
            return
        x = max(0, min(point.x(), max(0, parent.width() - self.width())))
        y = max(0, min(point.y(), max(0, parent.height() - self.height())))
        self.move(x, y)

    def sync_parent_geometry(self):
        parent = self.parentWidget()
        if parent is None:
            return
        if self._maximized:
            self.setGeometry(parent.rect())
        elif self.isVisible():
            self.move_clamped(self.pos())

    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()

    def request_route(self, origin, destination):
        self._pending_route = (str(origin).strip(), str(destination).strip())
        self.show()
        self.raise_()
        if not self._maximized:
            self.maximize()
        if self._page is not None:
            self._dispatch_route(*self._pending_route)

    def _dispatch_route(self, origin, destination):
        if self._page is None:
            return
        script = (
            "window.auraEarth && window.auraEarth.findRoute("
            f"{json.dumps(origin)}, {json.dumps(destination)});"
        )
        self._page.runJavaScript(script)


    def handle_command(self, command, origin="", destination=""):
        if command == "hide":
            self.hide()
        elif command == "minimize":
            if self._maximized:
                self.minimize()
            else:
                self.show()
        elif command == "maximize":
            self.show()
            self.maximize()
        elif command == "route" and origin and destination:
            self.request_route(origin, destination)
        elif command == "toggle":
            self.toggle()
        else:
            self.show()
            self.raise_()