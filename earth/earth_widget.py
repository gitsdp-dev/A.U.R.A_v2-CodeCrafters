from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, QUrl
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkInformation, QNetworkRequest
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

_LOGGER = logging.getLogger(__name__)
_SITE_URL = "https://osirisai.live/"
_SITE_HOST = "osirisai.live"
_RETRY_DELAYS = (2_000, 5_000, 10_000, 20_000, 30_000, 60_000)


class _GodseyePage(QWebEnginePage):
    def __init__(self, profile: QWebEngineProfile, owner: "EarthWidget"):
        super().__init__(profile, owner)
        self._owner = owner

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        try:
            scheme = url.scheme().lower()
            if not is_main_frame:
                return scheme in {"https", "http", "about", "data", "blob"}
            if scheme == "about" and url.toString() == "about:blank":
                return True
            if scheme == "https" and self._owner._is_site_url(url):
                return True
            if scheme in {"https", "http"}:
                self._owner._open_external_in_aura(url)
            else:
                _LOGGER.info("Blocked OSIRIS navigation using unsupported scheme %r", scheme)
        except Exception:
            _LOGGER.exception("Could not handle an OSIRIS navigation request")
        return False

    def createWindow(self, _window_type):
        # Reuse the current page; its navigation policy sends off-site targets
        # to A.U.R.A.'s embedded browser rather than a system browser.
        return self

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        try:
            text = f"{source_id}:{line_number}: {message}"
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                _LOGGER.warning("OSIRIS JavaScript error: %s", text)
            elif os.environ.get("AURA_GODSEYE_DEBUG") == "1":
                _LOGGER.info("OSIRIS JavaScript console: %s", text)
        except Exception:
            _LOGGER.exception("Could not handle an OSIRIS JavaScript console message")


class _EarthHeader(QWidget):
    def __init__(self, owner: "EarthWidget"):
        super().__init__(owner)
        self._owner = owner
        self._drag_offset: QPoint | None = None
        self._press_position: QPoint | None = None

    def mousePressEvent(self, event):
        try:
            if event.button() == Qt.MouseButton.LeftButton and not self._owner._maximized:
                top_left = self._owner.mapToGlobal(QPoint(0, 0))
                self._drag_offset = event.globalPosition().toPoint() - top_left
                self._press_position = event.position().toPoint()
                event.accept()
                return
        except Exception:
            _LOGGER.exception("Could not begin moving the God's Eye panel")
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        try:
            if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
                point = event.globalPosition().toPoint() - self._drag_offset
                self._owner.move_clamped(point)
                self._owner.user_moved = True
                event.accept()
                return
        except Exception:
            _LOGGER.exception("Could not move the God's Eye panel")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                moved = (
                    self._press_position is not None
                    and (event.position().toPoint() - self._press_position).manhattanLength() > 5
                )
                self._drag_offset = None
                self._press_position = None
                if not moved and not self._owner._maximized:
                    self._owner.maximize()
                event.accept()
                return
        except Exception:
            _LOGGER.exception("Could not finish moving the God's Eye panel")
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                if self._owner._maximized:
                    self._owner.minimize()
                else:
                    self._owner.maximize()
                event.accept()
                return
        except Exception:
            _LOGGER.exception("Could not toggle the God's Eye panel size")
        super().mouseDoubleClickEvent(event)


class EarthWidget(QWidget):
    """Persistent in-app QtWebEngine panel for the remote OSIRIS site."""

    _MIN_SIZE = (360, 300)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EarthWidget")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#EarthWidget { background: #000000; "
            "border: 1px solid #8a4f22; border-radius: 6px; }"
        )
        self.on_close = None
        self.user_moved = False
        self._maximized = False
        self._fullscreen_restore: tuple[bool, QRect] | None = None
        self._started = False
        self._loaded_once = False
        self._verifying_response = False
        self._retry_count = 0
        self._last_load_error = ""
        self._retry_when_visible = False
        self._edge_fallback_attempted = False
        self._profile_storage_error = ""

        self._profile = self._create_profile()
        self._page = _GodseyePage(self._profile, self)
        self._view = QWebEngineView(self)
        self._view.setPage(self._page)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._view.setStyleSheet("background: #000000; border: none;")
        self._page.setBackgroundColor(Qt.GlobalColor.black)
        self._configure_settings()

        self._network = QNetworkAccessManager(self)
        self._status_reply = None
        self._network_info = None
        self._connect_network_recovery()

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_timeout)
        self._view.loadStarted.connect(self._load_started)
        self._view.loadProgress.connect(self._load_progress)
        self._view.loadFinished.connect(self._load_finished)
        self._page.loadingChanged.connect(self._loading_changed)
        self._page.renderProcessTerminated.connect(self._renderer_terminated)
        self._page.fullScreenRequested.connect(self._fullscreen_requested)
        self._connect_permission_handlers()

        self._build_ui()
        if os.environ.get("AURA_GODSEYE_DEBUG") == "1":
            self._enable_devtools()

        self.resize(*self._MIN_SIZE)
        self.hide()

    def _create_profile(self) -> QWebEngineProfile:
        profile = QWebEngineProfile("aura_godseye", self)
        base_dir = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[1]
        )
        data_dir = base_dir / "config" / "aura_godseye"
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            cache_dir = data_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            profile.setPersistentStoragePath(str(data_dir))
            profile.setCachePath(str(cache_dir))
            profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
            )
            profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        except Exception as exc:
            self._profile_storage_error = str(exc) or exc.__class__.__name__
            _LOGGER.exception("Could not initialize the persistent God's Eye profile")
            return profile
        try:
            if hasattr(profile, "setPersistentPermissionsPolicy"):
                profile.setPersistentPermissionsPolicy(
                    QWebEngineProfile.PersistentPermissionsPolicy.StoreOnDisk
                )
        except Exception:
            _LOGGER.exception("Could not persist God's Eye permission decisions")
        try:
            user_agent = re.sub(
                r"\s*QtWebEngine/[\d.]+", "", profile.httpUserAgent()
            ).strip()
            profile.setHttpUserAgent(user_agent)
        except Exception:
            _LOGGER.exception("Could not set the OSIRIS desktop user agent")
        return profile

    def _configure_settings(self) -> None:
        settings = self._page.settings()
        attributes = QWebEngineSettings.WebAttribute
        for name, enabled in (
            ("JavascriptEnabled", True),
            ("WebGLEnabled", True),
            ("Accelerated2dCanvasEnabled", True),
            ("LocalStorageEnabled", True),
            ("FullScreenSupportEnabled", True),
            ("PlaybackRequiresUserGesture", False),
            ("JavascriptCanAccessClipboard", True),
            ("JavascriptCanPaste", True),
            ("JavascriptCanOpenWindows", True),
        ):
            try:
                settings.setAttribute(getattr(attributes, name), enabled)
            except Exception:
                _LOGGER.exception("Could not configure QtWebEngine setting %s", name)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        header = _EarthHeader(self)
        header.setFixedHeight(30)
        header.setStyleSheet("background: #090d12; border-bottom: 1px solid #5a341a;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(9, 0, 6, 0)
        header_layout.setSpacing(7)
        marker = QLabel("◈")
        marker.setStyleSheet("color: #ff9224; background: transparent; border: none;")
        header_layout.addWidget(marker)
        title = QLabel("GOD'S EYE · OSIRIS")
        title.setStyleSheet(
            "color: #ffdcb0; background: transparent; border: none; "
            "font: bold 8pt 'Courier New'; letter-spacing: 1px;"
        )
        header_layout.addWidget(title)
        header_layout.addStretch(1)

        self._minimize_button = QPushButton("－")
        self._minimize_button.setToolTip("Restore compact God's Eye panel")
        self._minimize_button.setFixedSize(22, 22)
        self._minimize_button.clicked.connect(self.minimize)
        header_layout.addWidget(self._minimize_button)
        close_button = QPushButton("✕")
        close_button.setToolTip("Close God's Eye")
        close_button.setFixedSize(22, 22)
        close_button.clicked.connect(self._close_clicked)
        header_layout.addWidget(close_button)
        layout.addWidget(header)
        layout.addWidget(self._view, 1)

        self._overlay = QFrame(self)
        self._overlay.setObjectName("GodseyeOverlay")
        self._overlay.setStyleSheet(
            "QFrame#GodseyeOverlay { background: rgba(0, 0, 0, 238); "
            "border: none; }"
            "QLabel { color: #ffdcb0; background: transparent; border: none; "
            "font: 9pt 'Courier New'; }"
            "QLabel#GodseyeDetail { color: #b8936c; font: 8pt 'Courier New'; }"
            "QProgressBar { background: #101010; border: 1px solid #68421f; "
            "height: 5px; }"
            "QProgressBar::chunk { background: #ff9224; }"
            "QPushButton { background: #17100a; color: #ffb456; "
            "border: 1px solid #8a4f22; border-radius: 3px; padding: 5px 12px; "
            "font: bold 8pt 'Courier New'; }"
            "QPushButton:hover { background: #251508; border-color: #ff9224; }"
        )
        overlay_layout = QVBoxLayout(self._overlay)
        overlay_layout.setContentsMargins(18, 14, 18, 14)
        overlay_layout.addStretch(1)
        self._overlay_title = QLabel("ESTABLISHING SECURE CONNECTION…")
        self._overlay_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay_layout.addWidget(self._overlay_title)
        self._overlay_detail = QLabel("Connecting to OSIRIS")
        self._overlay_detail.setObjectName("GodseyeDetail")
        self._overlay_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._overlay_detail.setWordWrap(True)
        overlay_layout.addWidget(self._overlay_detail)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        overlay_layout.addWidget(self._progress)
        self._retry_button = QPushButton("RETRY")
        self._retry_button.clicked.connect(self._retry_now)
        self._retry_button.hide()
        overlay_layout.addWidget(self._retry_button, alignment=Qt.AlignmentFlag.AlignCenter)
        overlay_layout.addStretch(1)
        self._overlay.hide()

        self._resize_grip = QSizeGrip(self)
        self._resize_grip.setStyleSheet("background: transparent;")
        self._resize_grip.setFixedSize(15, 15)

        if os.environ.get("AURA_GODSEYE_DEBUG") == "1":
            shortcut = QShortcut(QKeySequence("Ctrl+Shift+I"), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._toggle_devtools)

    def _enable_devtools(self) -> None:
        try:
            self._devtools_page = QWebEnginePage(self._profile, self)
            self._page.setDevToolsPage(self._devtools_page)
            self._devtools_view = QWebEngineView()
            self._devtools_view.setWindowTitle("God's Eye · Developer Tools")
            self._devtools_view.setPage(self._devtools_page)
            self._devtools_view.resize(900, 600)
        except Exception:
            self._devtools_page = None
            self._devtools_view = None
            _LOGGER.exception("Could not enable God's Eye developer tools")

    def _toggle_devtools(self) -> None:
        try:
            if self._devtools_view is not None:
                self._devtools_view.show()
                self._devtools_view.raise_()
        except Exception:
            _LOGGER.exception("Could not open God's Eye developer tools")

    def _connect_permission_handlers(self) -> None:
        try:
            if hasattr(self._page, "permissionRequested"):
                self._page.permissionRequested.connect(self._permission_requested)
            elif hasattr(self._page, "featurePermissionRequested"):
                self._page.featurePermissionRequested.connect(
                    self._legacy_permission_requested
                )
        except Exception:
            _LOGGER.exception("Could not connect God's Eye permission handlers")

    def _permission_requested(self, permission) -> None:
        try:
            origin = permission.origin()
            kind = permission.permissionType()
            kind_name = getattr(kind, "name", str(kind))
            trusted = origin.scheme().lower() == "https" and self._is_site_url(origin)
            if trusted and kind_name in {"Geolocation", "ClipboardReadWrite"}:
                permission.grant()
                _LOGGER.info("Granted OSIRIS %s permission", kind_name)
            else:
                permission.deny()
                _LOGGER.info("Denied OSIRIS %s permission", kind_name)
        except Exception:
            _LOGGER.exception("Could not resolve a God's Eye permission request")

    def _legacy_permission_requested(self, origin, feature) -> None:
        try:
            policy = QWebEnginePage.PermissionPolicy.PermissionDeniedByUser
            feature_name = getattr(feature, "name", str(feature))
            trusted = origin.scheme().lower() == "https" and self._is_site_url(origin)
            if trusted and feature_name == "Geolocation":
                policy = QWebEnginePage.PermissionPolicy.PermissionGrantedByUser
            elif trusted and feature_name == "ClipboardReadWrite":
                policy = QWebEnginePage.PermissionPolicy.PermissionGrantedByUser
            self._page.setFeaturePermission(origin, feature, policy)
            _LOGGER.info("Resolved OSIRIS %s permission: %s", feature_name, policy.name)
        except Exception:
            _LOGGER.exception("Could not resolve a legacy God's Eye permission request")

    def _connect_network_recovery(self) -> None:
        try:
            QNetworkInformation.loadDefaultBackend()
            self._network_info = QNetworkInformation.instance()
            if self._network_info is not None:
                self._network_info.reachabilityChanged.connect(
                    self._reachability_changed
                )
        except Exception:
            self._network_info = None
            _LOGGER.debug("Qt network reachability notifications are unavailable", exc_info=True)

    @staticmethod
    def _is_site_url(url: QUrl) -> bool:
        host = url.host().lower().rstrip(".")
        return host == _SITE_HOST or host.endswith("." + _SITE_HOST)

    def _open_external_in_aura(self, url: QUrl) -> None:
        try:
            target = url.toString()
            main_window = self.window()
            command_signal = getattr(main_window, "_browser_cmd_sig", None)
            if command_signal is None:
                _LOGGER.warning("No A.U.R.A. in-app browser is available for %s", target)
                return
            command_signal.emit("open", target, "", url.host() or target)
        except Exception:
            _LOGGER.exception("Could not open an external OSIRIS link in A.U.R.A.")

    def _set_lifecycle_state(self, state_name: str) -> None:
        try:
            state = getattr(QWebEnginePage.LifecycleState, state_name, None)
            if state is not None:
                self._page.setLifecycleState(state)
        except Exception:
            _LOGGER.exception("Could not change the OSIRIS page lifecycle state")

    def _load_site(self) -> None:
        try:
            if self._profile_storage_error:
                self._show_error(
                    "Persistent browser storage is unavailable. Check A.U.R.A.'s "
                    "config folder permissions, then retry."
                )
                return
            self._started = True
            self._last_load_error = ""
            self._retry_timer.stop()
            self._show_loading()
            self._set_lifecycle_state("Active")
            self._view.setUrl(QUrl(_SITE_URL))
        except Exception:
            _LOGGER.exception("Could not load OSIRIS")
            self._show_error("A.U.R.A. could not start the embedded OSIRIS page.")

    def _show_loading(self) -> None:
        try:
            self._verifying_response = False
            self._overlay_title.setText("ESTABLISHING SECURE CONNECTION…")
            self._overlay_detail.setText("Connecting to OSIRIS")
            self._progress.setValue(0)
            self._progress.show()
            self._retry_button.hide()
            self._overlay.setGeometry(self._view.geometry())
            self._overlay.show()
            self._overlay.raise_()
        except Exception:
            _LOGGER.exception("Could not display the OSIRIS loading overlay")

    def _show_error(self, reason: str) -> None:
        try:
            self._verifying_response = False
            self._overlay_title.setText("CONNECTION UNAVAILABLE")
            self._overlay_detail.setText(reason)
            self._progress.hide()
            self._retry_button.show()
            self._overlay.setGeometry(self._view.geometry())
            self._overlay.show()
            self._overlay.raise_()
        except Exception:
            _LOGGER.exception("Could not display the OSIRIS error overlay")

    def _load_started(self) -> None:
        try:
            self._last_load_error = ""
            self._show_loading()
        except Exception:
            _LOGGER.exception("Could not handle the OSIRIS load-started event")

    def _load_progress(self, progress: int) -> None:
        try:
            if self._overlay.isVisible() and self._retry_button.isHidden():
                self._progress.setValue(max(0, min(int(progress), 100)))
        except Exception:
            _LOGGER.exception("Could not update OSIRIS load progress")

    def _loading_changed(self, info) -> None:
        try:
            if info.isErrorPage():
                self._last_load_error = info.errorString()
        except Exception:
            _LOGGER.exception("Could not read OSIRIS load error information")

    def _load_finished(self, ok: bool) -> None:
        try:
            if not ok:
                self._show_error(self._human_error(self._last_load_error))
                self._schedule_retry()
                return
            self._loaded_once = True
            self._retry_count = 0
            self._retry_timer.stop()
            self._verifying_response = True
            self._overlay_title.setText("VERIFYING SITE RESPONSE…")
            self._overlay_detail.setText("Checking the OSIRIS server response")
            self._progress.setValue(100)
            self._progress.show()
            self._check_http_status()
            QTimer.singleShot(200, self._probe_webgl)
        except Exception:
            _LOGGER.exception("Could not finish loading OSIRIS")
            self._show_error("OSIRIS loaded with an unexpected error.")

    @staticmethod
    def _human_error(error: str) -> str:
        text = (error or "").strip()
        lowered = text.lower()
        if any(token in lowered for token in ("name_not_resolved", "name not resolved", "dns")):
            return "The OSIRIS server could not be found. Check your internet connection."
        if any(token in lowered for token in ("internet_disconnected", "network is disconnected")):
            return "There is no internet connection. OSIRIS will retry automatically."
        if any(token in lowered for token in ("cert", "ssl", "tls")):
            return "A secure connection to OSIRIS could not be established. Check your network or system clock."
        if text:
            return "Could not load OSIRIS: " + text[:180]
        return "Could not connect to OSIRIS. Check your internet connection and retry."

    def _check_http_status(self) -> None:
        try:
            request = QNetworkRequest(QUrl(_SITE_URL))
            request.setAttribute(
                QNetworkRequest.Attribute.RedirectPolicyAttribute,
                QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
            )
            request.setRawHeader(
                b"User-Agent", self._profile.httpUserAgent().encode("utf-8")
            )
            reply = self._network.head(request)
            self._status_reply = reply
            reply.finished.connect(lambda current=reply: self._http_status_finished(current))
        except Exception:
            _LOGGER.exception("Could not verify the OSIRIS HTTP response")

    def _http_status_finished(self, reply) -> None:
        try:
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if status is not None and int(status) >= 400:
                self._verifying_response = False
                self._show_error(
                    f"The OSIRIS server returned HTTP {int(status)}. Retry in a moment."
                )
                self._schedule_retry()
            elif self._verifying_response:
                self._verifying_response = False
                self._overlay.hide()
        except Exception:
            _LOGGER.exception("Could not process the OSIRIS HTTP status")
            if self._verifying_response:
                self._verifying_response = False
                self._overlay.hide()
        finally:
            try:
                reply.deleteLater()
                if self._status_reply is reply:
                    self._status_reply = None
            except Exception:
                _LOGGER.exception("Could not release the OSIRIS status request")

    def _schedule_retry(self) -> None:
        try:
            if not self.isVisible():
                self._retry_when_visible = True
                return
            delay = _RETRY_DELAYS[min(self._retry_count, len(_RETRY_DELAYS) - 1)]
            self._retry_count += 1
            self._retry_timer.start(delay)
        except Exception:
            _LOGGER.exception("Could not schedule an OSIRIS retry")

    def _retry_timeout(self) -> None:
        try:
            if self.isVisible():
                self._load_site()
            else:
                self._retry_when_visible = True
        except Exception:
            _LOGGER.exception("Could not retry the OSIRIS page")

    def _retry_now(self) -> None:
        try:
            self._retry_count = 0
            self._retry_when_visible = False
            self._load_site()
        except Exception:
            _LOGGER.exception("Could not start an OSIRIS retry")

    def _reachability_changed(self, reachability) -> None:
        try:
            name = getattr(reachability, "name", str(reachability))
            if name in {"Online", "Site"} and self._overlay.isVisible():
                if self.isVisible():
                    QTimer.singleShot(0, self._retry_now)
                else:
                    self._retry_when_visible = True
        except Exception:
            _LOGGER.exception("Could not handle a network-reachability change")

    def _probe_webgl(self) -> None:
        try:
            if not self._loaded_once or self._edge_fallback_attempted:
                return
            script = (
                "(() => { try { const c = document.querySelector('canvas') || "
                "document.createElement('canvas'); return !!c.getContext('webgl2'); "
                "} catch (_) { return false; } })()"
            )
            self._page.runJavaScript(script, self._webgl_probe_finished)
        except Exception:
            _LOGGER.exception("Could not check OSIRIS WebGL support")

    def _webgl_probe_finished(self, supported) -> None:
        try:
            if supported:
                return
            self._edge_fallback_attempted = True
            if self._open_edge_fallback():
                self._show_error(
                    "WebGL is unavailable in QtWebEngine. OSIRIS was opened in "
                    "A.U.R.A.'s Edge-powered in-app browser."
                )
            else:
                self._show_error(
                    "This QtWebEngine renderer cannot provide WebGL, which OSIRIS "
                    "requires. An Edge-powered in-app browser is not available."
                )
        except Exception:
            _LOGGER.exception("Could not handle the OSIRIS WebGL capability check")

    def _open_edge_fallback(self) -> bool:
        try:
            main_window = self.window()
            if getattr(main_window, "_edge_broken", False):
                return False
            from ui import _edge_available

            if not _edge_available():
                return False
            open_in = getattr(main_window, "_open_in", None)
            if callable(open_in):
                open_in("edge", _SITE_URL, "", "OSIRIS")
                return True
            ensure_browser = getattr(main_window, "_ensure_browser", None)
            if not callable(ensure_browser):
                return False
            browser = ensure_browser("edge")
            if browser is None:
                return False
            browser.open_url(_SITE_URL)
            return True
        except Exception:
            _LOGGER.exception("Could not open OSIRIS in A.U.R.A.'s Edge browser")
            return False

    def _renderer_terminated(self, status, exit_code: int) -> None:
        try:
            _LOGGER.error(
                "OSIRIS renderer terminated (status=%s, exit_code=%s)",
                status,
                exit_code,
            )
            self._show_error("The OSIRIS renderer stopped unexpectedly. Retrying…")
            self._schedule_retry()
        except Exception:
            _LOGGER.exception("Could not recover from an OSIRIS renderer termination")

    def _fullscreen_requested(self, request) -> None:
        try:
            if request.toggleOn():
                if self._fullscreen_restore is None:
                    self._fullscreen_restore = (self._maximized, QRect(self.geometry()))
                self.maximize()
            elif self._fullscreen_restore is not None:
                was_maximized, geometry = self._fullscreen_restore
                self._fullscreen_restore = None
                if was_maximized:
                    self.maximize()
                else:
                    self._maximized = False
                    self._minimize_button.hide()
                    self.setGeometry(geometry)
                    self._sync_view_geometry()
            request.accept()
        except Exception:
            _LOGGER.exception("Could not handle an OSIRIS fullscreen request")
            try:
                request.reject()
            except Exception:
                _LOGGER.exception("Could not reject an OSIRIS fullscreen request")

    def _close_clicked(self) -> None:
        try:
            if callable(self.on_close):
                self.on_close()
            else:
                self.hide()
        except Exception:
            _LOGGER.exception("Could not close the God's Eye panel")

    def _sync_view_geometry(self) -> None:
        try:
            if hasattr(self, "_overlay"):
                self._overlay.setGeometry(self._view.geometry())
                if self._overlay.isVisible():
                    self._overlay.raise_()
            if hasattr(self, "_resize_grip"):
                self._resize_grip.move(
                    max(0, self.width() - self._resize_grip.width() - 3),
                    max(0, self.height() - self._resize_grip.height() - 3),
                )
                self._resize_grip.setVisible(not self._maximized)
        except Exception:
            _LOGGER.exception("Could not synchronize God's Eye panel geometry")

    def resizeEvent(self, event) -> None:
        try:
            super().resizeEvent(event)
            self._sync_view_geometry()
        except Exception:
            _LOGGER.exception("Could not resize the God's Eye panel")

    def showEvent(self, event) -> None:
        try:
            super().showEvent(event)
            self._set_lifecycle_state("Active")
            self._sync_view_geometry()
            if not self._started:
                self._load_site()
            elif self._retry_when_visible:
                self._retry_when_visible = False
                self._retry_now()
        except Exception:
            _LOGGER.exception("Could not show the God's Eye panel")

    def hideEvent(self, event) -> None:
        try:
            self._set_lifecycle_state("Frozen")
            if self._retry_timer.isActive():
                self._retry_timer.stop()
                self._retry_when_visible = True
            super().hideEvent(event)
        except Exception:
            _LOGGER.exception("Could not pause the God's Eye panel")

    def closeEvent(self, event) -> None:
        try:
            self._retry_timer.stop()
            self._set_lifecycle_state("Frozen")
            super().closeEvent(event)
        except Exception:
            _LOGGER.exception("Could not close the God's Eye panel cleanly")
            event.accept()

    def maximize(self) -> None:
        try:
            parent = self.parentWidget()
            if parent is None or self._maximized:
                return
            self._maximized = True
            self._minimize_button.show()
            self.setGeometry(parent.rect())
            self.raise_()
            self._sync_view_geometry()
        except Exception:
            _LOGGER.exception("Could not maximize the God's Eye panel")

    def minimize(self) -> None:
        try:
            parent = self.parentWidget()
            if parent is None or not self._maximized:
                return
            self._maximized = False
            self._minimize_button.hide()
            self.resize(*self._MIN_SIZE)
            self._position_compact()
            self.raise_()
            self._sync_view_geometry()
        except Exception:
            _LOGGER.exception("Could not restore the compact God's Eye panel")

    def _position_compact(self) -> None:
        try:
            parent = self.parentWidget()
            if parent is None:
                return
            room = parent.findChild(QWidget, "RoomWatchBox")
            if room is not None and room.isVisible():
                x = room.x() + room.width() - self.width()
                y = room.y() + room.height() + 8
            else:
                x, y = parent.width() - self.width() - 16, 64
            self.move_clamped(QPoint(x, y))
        except Exception:
            _LOGGER.exception("Could not position the compact God's Eye panel")

    def move_clamped(self, point: QPoint) -> None:
        try:
            parent = self.parentWidget()
            if parent is None:
                self.move(point)
                return
            x = max(0, min(point.x(), max(0, parent.width() - self.width())))
            y = max(0, min(point.y(), max(0, parent.height() - self.height())))
            self.move(x, y)
        except Exception:
            _LOGGER.exception("Could not move the God's Eye panel")

    def sync_parent_geometry(self) -> None:
        try:
            parent = self.parentWidget()
            if parent is None:
                return
            if self._maximized:
                self.setGeometry(parent.rect())
            elif self.isVisible():
                self.move_clamped(self.pos())
            self._sync_view_geometry()
        except Exception:
            _LOGGER.exception("Could not synchronize God's Eye parent geometry")

    def toggle(self) -> None:
        try:
            if self.isVisible():
                self.hide()
            else:
                self.show()
        except Exception:
            _LOGGER.exception("Could not toggle the God's Eye panel")

    def request_route(self, origin, destination) -> None:
        try:
            _LOGGER.debug(
                "OSIRIS has no documented route URL parameter; route request ignored: %s -> %s",
                str(origin).strip(),
                str(destination).strip(),
            )
            self.show()
            self.raise_()
            if not self._maximized:
                self.maximize()
        except Exception:
            _LOGGER.exception("Could not handle a God's Eye route request")

    def handle_command(self, command, origin="", destination="") -> None:
        try:
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
        except Exception:
            _LOGGER.exception("Could not handle God's Eye command %r", command)
