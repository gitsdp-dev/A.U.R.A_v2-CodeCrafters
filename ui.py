from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

# Qt's video backend prints the ffmpeg stream banner (codec, bitrate, the whole
# signed stream URL) to the console for every stream it opens. Silenced here,
# before Qt initialises its logging. An explicit user setting is left alone.
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

from PyQt6.QtCore import (
    QEasingCurve, QEvent, QMimeData, QObject, QPoint, QPointF, QRectF, QSize, QSizeF, Qt, QThread,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QCursor, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
# Video playback for the HUD. Part of PyQt6, so it costs no new dependency —
# but the multimedia plugins are a separate piece of the Qt install and can be
# absent on a stripped-down system, so a failure here disables one feature
# rather than stopping the app from starting.
try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
    HAVE_VIDEO = True
except Exception as _e:            # noqa: BLE001 - reported, never fatal
    QAudioOutput = QMediaPlayer = QGraphicsVideoItem = None
    HAVE_VIDEO = False
    print(f"[Video] playback unavailable ({_e}) — the HUD will not show video.")

from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QGraphicsScene, QGraphicsView,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar,
)
try:
    from earth.earth_widget import EarthWidget
    HAVE_EARTH = True
except Exception as _e:  # noqa: BLE001 - never fatal
    EarthWidget = None
    HAVE_EARTH = False
    print(f"[Earth] unavailable ({_e}) — the Earth's-eye widget will stay off.")
from core.vosk_models import DEFAULT_VOSK_MODEL, VOSK_MODEL_OPTIONS

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 206
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

KOKORO_VOICE_GROUPS = {
    "American English — Female": (
        "af_heart", "af_bella", "af_nicole", "af_alloy", "af_aoede",
        "af_jessica", "af_kore", "af_nova", "af_river", "af_sarah", "af_sky",
    ),
    "American English — Male": (
        "am_adam", "am_michael", "am_fenrir", "am_puck", "am_echo",
        "am_eric", "am_liam", "am_onyx", "am_santa",
    ),
    "British English — Female": ("bf_alice", "bf_emma", "bf_lily", "bf_isabella"),
    "British English — Male": ("bm_daniel", "bm_fable", "bm_george", "bm_lewis"),
    "Japanese — Female": (
        "jf_alpha", "jf_gongitsune", "jf_tebukuro", "jf_nezumi",
    ),
    "Japanese — Male": ("jm_kumo",),
    "Mandarin Chinese — Female": (
        "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    ),
    "Mandarin Chinese — Male": (
        "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
    ),
    "Spanish": ("ef_dora", "em_alex", "em_santa"),
    "French": ("ff_siwis",),
    "Hindi": ("hf_alpha", "hf_beta", "hm_omega", "hm_psi"),
    "Italian": ("if_sara", "im_nicola"),
    "Brazilian Portuguese": ("pf_dora", "pm_alex", "pm_santa"),
}

LOCAL_MODEL_SUGGESTIONS = {
    "ollama": (
        "qwen2.5:1.5b", "llama3.2:1b", "qwen2.5:3b", "llama3.2:3b",
        "qwen3:4b", "gemma3:4b",
        "phi4-mini:3.8b", "mistral:7b", "qwen2.5:7b", "llama3.1:8b",
    ),
    "lmstudio": (
        "Qwen2.5-1.5B-Instruct", "Llama-3.2-1B-Instruct",
        "Qwen2.5-3B-Instruct", "Llama-3.2-3B-Instruct",
        "Qwen3-4B", "Gemma-3-4B", "Phi-4-mini",
        "Mistral-7B-Instruct", "Qwen2.5-7B-Instruct",
    ),
}

# ── Face unlock (module-level helpers) ───────────────────────────────────────
# Uses OpenCV's YuNet (detector) + SFace (recognizer). Both are small ONNX
# models that are downloaded once into config/face_unlock/. Only 128-number
# face descriptors are stored (config/face_unlock.json) — never photos.
FACE_DIR      = CONFIG_DIR / "face_unlock"
FACE_CFG_FILE = CONFIG_DIR / "face_unlock.json"
_FACE_MODELS = (
    ("face_detection_yunet_2023mar.onnx", (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    )),
    ("face_recognition_sface_2021dec.onnx", (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    )),
)
_FACE_MODEL_MIN_BYTES = 100_000
_FACE_THRESH    = 0.38     # cosine similarity needed to count as "same person" (SFace default 0.363)
_FACE_NEED      = 3        # consecutive matching checks required before unlocking
_FACE_ENROLL_N  = 8        # face samples captured during enrolment
# Modes in which the lock/enrol overlay covers the window.
_FACE_COVER = ("pending", "prep", "lock", "enroll", "reenroll", "unlocking")


def _face_cfg_load() -> dict:
    cfg = {"enabled": True, "allow_skip": True, "enrolled": []}
    try:
        data = json.loads(FACE_CFG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in ("enabled", "allow_skip", "enrolled"):
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def _face_cfg_save(cfg: dict) -> bool:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = FACE_CFG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg), encoding="utf-8")
        os.replace(tmp, FACE_CFG_FILE)
        return True
    except Exception as e:
        print(f"[FaceUnlock] Could not save settings: {e}")
        return False


def _face_stored_from(cfg: dict):
    """Enrolled descriptors as a unit-normalised (N, 128) array, or None."""
    try:
        import numpy as np
        arr = np.asarray(cfg.get("enrolled") or [], dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] == 0:
            return None
        n = np.linalg.norm(arr, axis=1, keepdims=True)
        n[n == 0] = 1.0
        return arr / n
    except Exception:
        return None


def _face_download(urls, dest: Path, say=None, label: str = "MODEL") -> bool:
    """Download one model file (tries each URL in turn). True on success."""
    import urllib.request
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (AURA)"})
            with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got, last = 0, -1
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total and say:
                        pct = int(got * 100 / total) // 5 * 5
                        if pct != last:
                            last = pct
                            say(f"DOWNLOADING {label}… {pct}%")
            if tmp.stat().st_size < _FACE_MODEL_MIN_BYTES:
                raise ValueError("downloaded file too small")
            os.replace(tmp, dest)
            return True
        except Exception as e:
            print(f"[FaceUnlock] Download failed ({url}): {e}")
            try:
                tmp.unlink()
            except Exception:
                pass
    return False


class _FaceEngine:
    """Face detection + descriptor extraction (OpenCV YuNet + SFace)."""

    def __init__(self):
        self.ready = False
        self._det = None
        self._rec = None
        self._lock = threading.Lock()

    def load(self, say=None) -> bool:
        try:
            import cv2
            if not (hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")):
                print("[FaceUnlock] This OpenCV build has no FaceDetectorYN / FaceRecognizerSF "
                      "(needs opencv-python 4.5.4 or newer).")
                return False
            paths = []
            for name, urls in _FACE_MODELS:
                p = FACE_DIR / name
                ok = p.exists() and p.stat().st_size >= _FACE_MODEL_MIN_BYTES
                if not ok:
                    label = "FACE MODEL" if "sface" in name else "FACE DETECTOR"
                    if say:
                        say(f"DOWNLOADING {label}… (first run only)")
                    if not _face_download(urls, p, say, label):
                        print(f"[FaceUnlock] Could not download {name}. Download it manually "
                              f"from the OpenCV Zoo and place it in: {FACE_DIR}")
                        return False
                paths.append(p)
            try:
                det = cv2.FaceDetectorYN.create(str(paths[0]), "", (320, 320), 0.8, 0.3, 5000)
                rec = cv2.FaceRecognizerSF.create(str(paths[1]), "")
            except Exception as e:
                print(f"[FaceUnlock] Model load failed ({e}) — deleting so it re-downloads next run.")
                for p in paths:
                    try:
                        p.unlink()
                    except Exception:
                        pass
                return False
            self._det, self._rec = det, rec
            self.ready = True
            return True
        except Exception as e:
            print(f"[FaceUnlock] Engine error: {e}")
            return False

    def analyse(self, frame):
        """→ (face_count, unit descriptor of the largest face | None, its width in px)."""
        import cv2
        import numpy as np
        h, w = frame.shape[:2]
        if w > 640:
            frame = cv2.resize(frame, (640, max(1, int(h * 640 / w))))
            h, w = frame.shape[:2]
        with self._lock:
            self._det.setInputSize((w, h))
            _ret, faces = self._det.detect(frame)
            if faces is None or len(faces) == 0:
                return 0, None, 0
            good = [f for f in faces if float(f[14]) >= 0.8 and float(f[2]) >= 40.0]
            if not good:
                return 0, None, 0
            best = max(good, key=lambda f: float(f[2]) * float(f[3]))
            aligned = self._rec.alignCrop(frame, best)
            feat = self._rec.feature(aligned)
        feat = np.asarray(feat, dtype=np.float32).reshape(-1)
        nrm = float(np.linalg.norm(feat))
        if nrm <= 0.0:
            return 0, None, 0
        return len(good), feat / nrm, int(best[2])


class C:
    BG        = "#000000"
    PANEL     = "#000000"
    PANEL2    = "#000000"
    BORDER    = "#5a341a"
    BORDER_B  = "#ff9224"
    BORDER_A  = "#8a4f22"
    PRI       = "#ff9224"
    PRI_DIM   = "#9a5416"
    PRI_GHO   = "#2c1400"
    ACC       = "#ff6b00"
    ACC2      = "#ffcc00"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"
    TEXT      = "#ffdcb0"
    TEXT_DIM  = "#8a5a34"
    TEXT_MED  = "#cf9a5c"
    WHITE     = "#fff6ec"
    DARK      = "#000000"
    BAR_BG    = "#000000"


# Ana renge (accent) bağlı anahtarlar — durum renkleri (ACC, GREEN, RED…) sabit kalır
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]


def apply_ui_accent(accent_hex: str) -> bool:
    """
    Seçilen accent rengine göre tüm turkuaz-ailesi paleti yeniden türetir
    (hue kaydırma — parlaklık/doygunluk oranları korunur, tasarım bozulmaz).
    Boyanan öğeler (HUD, dalga formu, metrikler) bir sonraki karede yeni
    rengi alır; stylesheet tabanlı paneller yeniden kurulduklarında alır.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08   # griye yakın accent → tüm tema desaturize edilir

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def current_palette() -> dict[str, str]:
    """C sınıfındaki accent'e bağlı renklerin anlık kopyası."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    CANLI tam tema değişimi. Uygulamadaki HER widget'ın stylesheet'inde eski
    palet renklerini yenileriyle değiştirir ve yeniden çizdirir. Böylece renk
    değişimi yalnızca boyanan öğelerde değil, panel/buton/kenarlık dahil tüm
    arayüzde ANINDA uygulanır — yeniden başlatma gerekmez.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


# ── Vendor-agnostic GPU usage (covers Intel/AMD iGPUs, not just NVIDIA) ──────
_wmi_conn: object = None   # cached WMI COM connection (root/cimv2)
_gpu_engine_ok: object = None   # None=untested, True=works, False=unavailable


def _gpu_usage_windows_generic() -> float:
    """
    Read the same 'GPU Engine' performance counters Task Manager's
    Performance tab uses. These are populated by the Windows GPU scheduler
    (WDDM) for EVERY GPU — Intel, AMD, NVIDIA — regardless of vendor SDKs,
    so this is what makes an integrated GPU like the UHD 630 show up.
    Zero subprocess: uses the already-imported `wmi` COM wrapper.
    """
    global _wmi_conn, _gpu_engine_ok
    if _gpu_engine_ok is False:
        return -1.0
    try:
        import wmi  # type: ignore
        if _wmi_conn is None:
            _wmi_conn = wmi.WMI(namespace="root/cimv2")
        counters = _wmi_conn.Win32_PerfFormattedData_Counters_GPUEngine()
        if not counters:
            _gpu_engine_ok = False
            return -1.0
        # Task Manager's headline % is the busiest single engine instance
        # across all GPUs/processes at that moment (engines run in
        # parallel, so summing them would over-count).
        best = 0.0
        for c in counters:
            util = float(getattr(c, "UtilizationPercentage", 0) or 0)
            if util > best:
                best = util
        _gpu_engine_ok = True
        return min(100.0, best)
    except Exception:
        _gpu_engine_ok = False
        return -1.0


def _gpu_usage_linux_sysfs() -> float:
    """Best-effort busy% straight from sysfs (AMDGPU / newer i915-Xe) — zero subprocess, zero deps."""
    import glob
    for path in (glob.glob("/sys/class/drm/card?/gpu_busy_percent")
                 + glob.glob("/sys/class/drm/card?/gt_busy_percent")):
        try:
            with open(path) as f:
                return float(f.read().strip())
        except Exception:
            continue
    return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        gpu = self._get_gpu()

        tmp = self._get_temp()

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free, works on all platforms if installed
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:
            pass

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            v = _nvml_gpu_windows()
            if v >= 0:
                return v
        else:
            # Linux / macOS: libnvidia-ml shared lib via ctypes
            try:
                import ctypes
                _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"

                class _Util(ctypes.Structure):
                    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

                nv = ctypes.CDLL(_lib)
                nv.nvmlInit_v2()
                dev = ctypes.c_void_p()
                nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
                u = _Util()
                nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
                return float(u.gpu)
            except Exception:
                pass

        # No NVIDIA card (or NVIDIA path failed) — vendor-agnostic fallback
        # so integrated GPUs (Intel UHD, AMD APU, etc.) still register.
        if _OS == "Windows":
            v = _gpu_usage_windows_generic()
            if v >= 0:
                return v
        elif _OS == "Linux":
            v = _gpu_usage_linux_sysfs()
            if v >= 0:
                return v

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess)
        if _OS == "Windows":
            try:
                import wmi  # type: ignore
                w = wmi.WMI(namespace="root/wmi")
                tz = w.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                pass

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    def __init__(self, face_path: str, assistant_name: str = "J.A.R.V.I.S", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._scan       = 0.0
        self._scan2      = 180.0
        self._rings      = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 50.0, 100.0]
        self._blink      = True
        self._blink_tick = 0
        self._particles: list[list[float]] = []
        self._face_px: QPixmap | None = None
        self._load_face(face_path)

        # ── central ball: procedural golden data-sphere state ────────────────
        self._orb_spin = 0.0
        self._orb_trails = [
            {
                "speed_mult": random.uniform(0.3, 1.6) * random.choice([1, -1]),
                "phase":      random.uniform(0, 360),
                "tilt":       random.uniform(0.12, 0.98),
                "scale":      random.uniform(0.32, 1.08),
                "span":       random.uniform(50, 280),
                "width":      random.uniform(0.6, 1.6),
            }
            for _ in range(22)
        ]
        self._orb_dots = [
            {
                "theta": random.uniform(0, 360),
                "phi":   random.uniform(-84, 84),
                "tw":    random.uniform(0, 1),
                "spd":   random.uniform(0.5, 1.5),
            }
            for _ in range(64)
        ]

        # Live audio reactivity: _live_amp is written from the audio threads
        # (0.0–1.0), _amp_disp is the smoothed value the paint code reads.
        self._live_amp = 0.0
        self._amp_disp = 0.0
        # (frames, start_time, hop) posted by the playback thread — see
        # push_visemes(). None means "no schedule; use the plain level".
        self._visemes = None

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Compatibility hook: the central ball has no eyes, so there is
        nothing to turn. Accepted so callers written for the head never fail."""
        return None

    def push_visemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: hand over a schedule of (level, openness, width) frames.

        The ball has no mouth, so only the loudness of each frame is used —
        _step() plays the schedule out against the wall clock, in step with
        the audio going to the speakers, and the ball pulses with it.
        """
        try:
            if not frames:
                return
            hop = max(1e-3, float(hop))
            at = float(at)
            new = list(frames)
            cur = self._visemes
            if cur is not None:
                old, t0, ohop = cur
                if abs(ohop - hop) < 1e-6:
                    i = int(round((at - t0) / hop))
                    if 0 <= i <= len(old) + 1:
                        merged = old[:i] + new
                        played = int((time.time() - t0) / hop) - 2
                        if played > 60:
                            merged = merged[played:]
                            t0 += played * hop
                        self._visemes = (merged, t0, hop)
                        return
            self._visemes = (new, at, hop)
        except Exception:
            pass

    def set_audio_level(self, level: float) -> None:
        """Thread-safe entry point for the audio threads. Stores the louder of
        the incoming level and the current value so brief gaps between chunks
        don't make the ball stutter; _step() decays it back down."""
        try:
            lv = float(level)
        except (TypeError, ValueError):
            return
        if lv < 0.0:
            lv = 0.0
        elif lv > 1.0:
            lv = 1.0
        if lv > self._live_amp:
            self._live_amp = lv

    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz  = min(img.size)
            img = img.resize((sz, sz), Image.LANCZOS)
            mk  = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
            img.putalpha(mk)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap(); px.loadFromData(buf.getvalue())
            self._face_px = px
        except Exception:
            self._face_px = None

    def _step(self):
        self._tick += 1
        now = time.time()

        # ── Live audio reactivity ────────────────────────────────────────────
        # A viseme schedule, if one is playing, gives the level for this exact
        # instant; otherwise fall back to the peak the audio threads pushed in.
        sched = self._visemes
        if sched is not None:
            frames, t0, hop = sched
            i = int((now - t0) / hop)
            if 0 <= i < len(frames):
                try:
                    lv = float(frames[i][0])
                    if lv > self._live_amp:
                        self._live_amp = min(1.0, lv)
                except Exception:
                    pass
            elif i >= len(frames):
                self._visemes = None        # schedule spent
        self._live_amp *= 0.86
        self._amp_disp += (self._live_amp - self._amp_disp) * 0.45

        if now - self._last_t > (0.12 if self.speaking else 0.5):
            if self.speaking:
                self._tgt_scale = random.uniform(1.06, 1.14)
                self._tgt_halo  = random.uniform(145, 190)
            elif self.muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo  = random.uniform(15, 28)
            else:
                self._tgt_scale = random.uniform(1.001, 1.008)
                self._tgt_halo  = random.uniform(48, 68)
            self._last_t = now

        sp = 0.38 if self.speaking else 0.15
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        # central ball rotation — lively while speaking, comparatively stable
        # (slow, steady) while listening / thinking / idle, near-frozen if muted
        if self.speaking:
            orb_spd = 3.4
        elif self.muted:
            orb_spd = 0.10
        else:
            orb_spd = 0.32
        orb_spd += self._amp_disp * 2.4     # audio makes the ball spin a touch faster
        self._orb_spin = (self._orb_spin + orb_spd) % 360

        speeds = [1.3, -0.9, 2.0] if self.speaking else [0.55, -0.35, 0.9]
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360

        self._scan  = (self._scan  + (3.0 if self.speaking else 1.3)) % 360
        self._scan2 = (self._scan2 + (-2.0 if self.speaking else -0.75)) % 360

        fw  = min(self.width(), self.height())
        lim = fw * 0.74
        spd = 4.2 if self.speaking else 2.0
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.07 if self.speaking else 0.025):
            self._pulses.append(0.0)

        if self.speaking and random.random() < 0.28:
            cx, cy = self.width() / 2, self.height() / 2
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.28
            self._particles.append([
                cx + math.cos(ang) * r_s, cy + math.sin(ang) * r_s,
                math.cos(ang) * random.uniform(0.9, 2.4),
                math.sin(ang) * random.uniform(0.9, 2.4) - 0.4, 1.0,
            ])
        self._particles = [
            [p[0]+p[2], p[1]+p[3], p[2]*0.97, p[3]*0.97, p[4]-0.028]
            for p in self._particles if p[4] > 0
        ]

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
        self.update()

    def _draw_orb(self, p: QPainter, cx: float, cy: float, fw: float):
        """
        Central interactive ball — a golden data-sphere built from tangled,
        independently-rotating orbital trails plus scattered shimmering light
        particles (no text). Fully procedural / vector, so it stays crisp at
        any size and animates live:
          • SPEAKING   → fast spin, bright halo, lively particle shimmer
          • LISTENING / THINKING / PROCESSING → slow, steady spin — stable
          • MUTED      → almost frozen, tinted red as an alert state
        """
        amp   = self._amp_disp
        halo  = min(255.0, self._halo + amp * 90.0)
        orb_r = fw * 0.30 * (self._scale + amp * 0.08)
        muted = self.muted
        ball_col = C.MUTED_C if muted else C.PRI

        # core glow — warm amber/gold radiating outward (red-tinted if muted)
        core_rgb = (255, 60, 70) if muted else (255, 150, 40)
        for i in range(9, 0, -1):
            r2  = orb_r * i / 9
            frc = i / 9
            a   = max(0, min(255, int(halo * 1.05 * frc)))
            rr, gg, bb = core_rgb
            p.setBrush(QBrush(QColor(
                min(255, int(rr * frc + 30)),
                int(gg * frc),
                int(bb * frc),
                a,
            )))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(cx - r2, cy - r2, r2 * 2, r2 * 2))

        # tangled orbital wireframe trails — independently spinning ellipses
        # at varying tilts, giving the illusion of a 3-D globe of light-trails
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i, tr in enumerate(self._orb_trails):
            rot = (self._orb_spin * tr["speed_mult"] + tr["phase"]) % 360
            r_i = orb_r * tr["scale"]
            a   = max(15, min(220, int(halo * (0.95 - (i % 5) * 0.09))))
            p.save()
            p.translate(cx, cy)
            p.rotate(rot)
            rect = QRectF(-r_i, -r_i * tr["tilt"], r_i * 2, r_i * 2 * tr["tilt"])
            p.setPen(QPen(qcol(ball_col, a), tr["width"]))
            p.drawArc(rect, 0, int(tr["span"] * 16))
            p.restore()

        # scattered shimmering light particles across the sphere's surface
        spin_rad = math.radians(self._orb_spin * 0.6)
        for d in self._orb_dots:
            theta = math.radians(d["theta"]) + spin_rad
            phi   = math.radians(d["phi"])
            x = orb_r * math.cos(phi) * math.sin(theta)
            y = orb_r * math.sin(phi)
            z = math.cos(phi) * math.cos(theta)   # -1 (back) .. 1 (front)
            if z < -0.15:
                continue   # backface cull — keep the sphere illusion
            depth  = 0.35 + 0.65 * ((z + 1) / 2)
            tw     = 0.55 + 0.45 * math.sin(self._tick * 0.06 * d["spd"] + d["tw"] * 6.28)
            a      = max(0, min(255, int(halo * 1.4 * depth * tw)))
            size   = 1.1 + 1.3 * depth
            dot_col = C.MUTED_C if muted else (C.WHITE if depth > 0.82 else C.ACC2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(dot_col, a)))
            p.drawEllipse(QPointF(cx + x, cy + y), size, size)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # grid dots
        p.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, W, 48):
            for y in range(0, H, 48):
                p.drawPoint(x, y)

        r_face = fw * 0.31

        # halo glow
        for i in range(10):
            r   = r_face * (1.8 - i * 0.08)
            frc = 1.0 - i / 10
            a   = max(0, min(255, int(self._halo * 0.085 * frc)))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # pulse rings
        for pr in self._pulses:
            a   = max(0, int(230 * (1.0 - pr / (fw * 0.74))))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        # spinning arc rings
        for idx, (r_frac, w_r, arc_l, gap) in enumerate(
            [(0.48, 3, 115, 78), (0.40, 2, 78, 55), (0.32, 1, 56, 40)]
        ):
            ring_r = fw * r_frac
            base   = self._rings[idx]
            a_val  = max(0, min(255, int(self._halo * (1.0 - idx * 0.18))))
            col    = qcol(C.MUTED_C if self.muted else C.PRI, a_val)
            p.setPen(QPen(col, w_r)); p.setBrush(Qt.BrushStyle.NoBrush)
            angle = base
            rect  = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            while angle < base + 360:
                p.drawArc(rect, int(angle * 16), int(arc_l * 16))
                angle += arc_l + gap

        # scanners
        sr = fw * 0.50
        sa = min(255, int(self._halo * 1.5))
        ex = 75 if self.speaking else 44
        p.setPen(QPen(qcol(C.MUTED_C if self.muted else C.PRI, sa), 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        srect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        p.drawArc(srect, int(self._scan * 16), int(ex * 16))
        p.setPen(QPen(qcol(C.ACC, sa // 2), 1.5))
        p.drawArc(srect, int(self._scan2 * 16), int(ex * 16))

        # tick marks
        t_out, t_in = fw * 0.497, fw * 0.474
        p.setPen(QPen(qcol(C.PRI, 140), 1))
        for deg in range(0, 360, 10):
            rad = math.radians(deg)
            inn = t_in if deg % 30 == 0 else t_in + 6
            p.drawLine(
                QPointF(cx + t_out * math.cos(rad), cy - t_out * math.sin(rad)),
                QPointF(cx + inn  * math.cos(rad), cy - inn  * math.sin(rad)),
            )

        # crosshair
        ch_r, gap_h = fw * 0.51, fw * 0.16
        p.setPen(QPen(qcol(C.PRI, int(self._halo * 0.5)), 1))
        p.drawLine(QPointF(cx - ch_r, cy), QPointF(cx - gap_h, cy))
        p.drawLine(QPointF(cx + gap_h, cy), QPointF(cx + ch_r, cy))
        p.drawLine(QPointF(cx, cy - ch_r), QPointF(cx, cy - gap_h))
        p.drawLine(QPointF(cx, cy + gap_h), QPointF(cx, cy + ch_r))

        # central interactive ball — procedural golden data-sphere
        self._draw_orb(p, cx, cy, fw)

        # particles
        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.PRI, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.5, 2.5)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, cy, W, 26), Qt.AlignmentFlag.AlignCenter, self._assistant_name)

class HudButton(QPushButton):
    """Chamfered outline button in the HUD style. Draws its own frame and
    content (text, or a vector microphone icon), so no emoji are needed.
    Colours are read at paint time, so accent re-theming keeps working."""

    def __init__(self, text: str = "", kind: str = "text", parent=None):
        super().__init__(text, parent)
        self._kind  = kind      # "text" | "mic"
        self._alert = False     # True → red (used for the muted mic)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_alert(self, on: bool):
        self._alert = bool(on)
        self.update()

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = float(self.width()), float(self.height())
        col = qcol(C.MUTED_C if self._alert else C.PRI)

        m, c = 1.5, 6.0
        path = QPainterPath()
        path.moveTo(m + c, m)
        path.lineTo(W - m - c, m)
        path.lineTo(W - m, m + c)
        path.lineTo(W - m, H - m - c)
        path.lineTo(W - m - c, H - m)
        path.lineTo(m + c, H - m)
        path.lineTo(m, H - m - c)
        path.lineTo(m, m + c)
        path.closeSubpath()

        if self.isDown():
            p.setBrush(QBrush(qcol(C.PRI_GHO)))
        elif self.underMouse():
            p.setBrush(QBrush(qcol(C.PRI_GHO, 170)))
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(col, 2.0))
        p.drawPath(path)

        if self._kind == "mic":
            pen = QPen(col, 1.7)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            cx, cy = W / 2.0, H / 2.0
            p.drawRoundedRect(QRectF(cx - 3.5, cy - 9, 7, 12), 3.5, 3.5)
            p.drawArc(QRectF(cx - 7, cy - 6, 14, 14), 180 * 16, 180 * 16)
            p.drawLine(QPointF(cx, cy + 8), QPointF(cx, cy + 11))
            p.drawLine(QPointF(cx - 4, cy + 11), QPointF(cx + 4, cy + 11))
            if self._alert:   # muted → slash through the icon
                p.drawLine(QPointF(cx - 9, cy - 9), QPointF(cx + 9, cy + 11))
        else:
            p.setPen(QPen(col, 1))
            p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            p.drawText(QRectF(0, 0, W, H), Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


class TitleBar(QWidget):
    """Header title with angled HUD rules on either side of the name."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.setMinimumWidth(250)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = float(self.width()), float(self.height())
        y1 = H / 2.0 - 2.0
        y2 = y1 + 13.0
        pen = QPen(qcol(C.PRI), 1.4)
        p.setPen(pen)
        p.drawLine(QPointF(2, y1), QPointF(46, y1))
        p.drawLine(QPointF(46, y1), QPointF(60, y2))
        p.drawLine(QPointF(W - 60, y2), QPointF(W - 46, y1))
        p.drawLine(QPointF(W - 46, y1), QPointF(W - 2, y1))
        p.end()


class StatsBox(QWidget):
    """HUD-style system stats box: chamfered orange frame with
    CPU / RAM / GPU / NET rows. Painted with live palette colours (C.PRI)
    so accent re-theming keeps working."""

    _ROWS = ("CPU", "RAM", "GPU", "NET")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vals = {k: "--" for k in self._ROWS}
        self.setFixedSize(190, 150)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_values(self, cpu: str, ram: str, gpu: str, net: str):
        self._vals = {"CPU": cpu, "RAM": ram, "GPU": gpu, "NET": net}
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        col = qcol(C.PRI)

        # chamfered frame
        m, c = 4.0, 12.0
        frame = QPainterPath()
        frame.moveTo(m + c, m)
        frame.lineTo(W - m - c, m)
        frame.lineTo(W - m, m + c)
        frame.lineTo(W - m, H - m - c)
        frame.lineTo(W - m - c, H - m)
        frame.lineTo(m + c, H - m)
        frame.lineTo(m, H - m - c)
        frame.lineTo(m, m + c)
        frame.closeSubpath()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(col, 2.2))
        p.drawPath(frame)

        # small corner notches (inner chamfer accents)
        p.setPen(QPen(col, 1.6))
        n = 5.0
        for (cx, cy, dx, dy) in ((m + c, m, -1, 1), (W - m - c, m, 1, 1),
                                 (W - m - c, H - m, 1, -1), (m + c, H - m, -1, -1)):
            p.drawLine(QPointF(cx, cy + dy * n), QPointF(cx + dx * n, cy + dy * n))

        # rows
        font = QFont("Courier New", 10, QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QPen(col, 1))
        top, bottom = 24.0, H - 24.0
        row_h = (bottom - top) / len(self._ROWS)
        for i, key in enumerate(self._ROWS):
            y = top + i * row_h
            r_lab = QRectF(22, y, 44, row_h)
            r_sep = QRectF(68, y, 12, row_h)
            r_val = QRectF(84, y, W - 84 - 10, row_h)
            al = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            p.drawText(r_lab, al, key)
            p.drawText(r_sep, al, ":")
            p.drawText(r_val, al, self._vals.get(key, "--"))
        p.end()


class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._ai_name_lc = "aura"   # updated when assistant name changes
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        _ai_pfx = f"{self._ai_name_lc}:"
        if   tl.startswith("you:"):                              self._tag = "you"
        elif tl.startswith(_ai_pfx) or tl.startswith("aura:"): self._tag = "ai"
        elif tl.startswith("file:"):                             self._tag = "file"
        elif "err" in tl:                                        self._tag = "err"
        else:                                                    self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                "sys":  qcol(C.ACC2),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for AURA", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol("#241000" if z._drag_over else ("#180d00" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol("#5a3a20"), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Courier New", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol("#4a3018"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont("Courier New", 8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


class _DragHeader(QWidget):
    """Header strip that drags its parent widget around inside the parent's parent."""

    def __init__(self, target: QWidget):
        super().__init__(target)
        self._target = target
        self._off = None
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setStyleSheet("background: transparent;")

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            t = self._target
            p = t.parentWidget()
            gp = ev.globalPosition().toPoint()
            base = p.mapFromGlobal(gp) if p is not None else gp
            self._off = base - t.pos()
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if (ev.buttons() & Qt.MouseButton.LeftButton) and self._off is not None:
            t = self._target
            p = t.parentWidget()
            gp = ev.globalPosition().toPoint()
            base = p.mapFromGlobal(gp) if p is not None else gp
            t.move_clamped(base - self._off)
            t.user_moved = True
            on_move = getattr(t, "on_move", None)
            if callable(on_move):
                on_move()
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._off = None
        super().mouseReleaseEvent(ev)


class _RoomWatchBox(QWidget):
    """Always-on floating room-monitor box: live webcam preview, draggable
    by its header, with a cross in the top-right corner."""

    _W     = 244
    _IMG_W = 228

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RoomWatchBox")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            QWidget#RoomWatchBox {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self.user_moved = False      # True once the user has dragged it somewhere
        self.on_close = None         # callable set by MainWindow

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(5)

        hdr_w = _DragHeader(self)
        hdr = QHBoxLayout(hdr_w)
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
        hdr.addWidget(dot)

        title = QLabel("ROOM WATCH")
        title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        title.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px; border: none;"
        )
        hdr.addWidget(title)
        hdr.addStretch()

        live = QLabel("● LIVE")
        live.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        live.setStyleSheet(f"color: {C.RED}; background: transparent; border: none;")
        hdr.addWidget(live)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("Close room watch  [F8 to reopen]")
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM}; border: none;
            }}
            QPushButton:hover {{ color: {C.MUTED_C}; }}
        """)
        close_btn.clicked.connect(self._close_clicked)
        hdr.addWidget(close_btn)
        lay.addWidget(hdr_w)

        self._img = QLabel("CONNECTING…")
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setFont(QFont("Courier New", 7))
        self._img.setFixedSize(self._IMG_W, 150)
        self._img.setStyleSheet(
            f"background: #000000; color: {C.TEXT_DIM};"
            f" border: 1px solid {C.BORDER}; border-radius: 3px;"
        )
        lay.addWidget(self._img)
        self.adjustSize()
        self.hide()

    def _close_clicked(self) -> None:
        cb = self.on_close
        if callable(cb):
            cb()
        else:
            self.hide()

    def move_clamped(self, pt) -> None:
        p = self.parentWidget()
        if p is None:
            self.move(pt)
            return
        x = max(0, min(pt.x(), max(0, p.width()  - self.width())))
        y = max(0, min(pt.y(), max(0, p.height() - self.height())))
        self.move(x, y)

    def set_frame(self, img_bytes: bytes) -> bool:
        """Show one JPEG frame. Returns True if the box changed size."""
        px = QPixmap()
        if not px.loadFromData(img_bytes) or px.isNull():
            return False
        scaled = px.scaledToWidth(
            self._IMG_W, Qt.TransformationMode.SmoothTransformation
        )
        changed = (self._img.width(), self._img.height()) != (scaled.width(), scaled.height())
        if changed:
            self._img.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self._img.setPixmap(scaled)
        return changed


class FaceLockOverlay(QWidget):
    """Full-window lock / enrolment screen for face unlock."""

    action_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FaceLockOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(f"QWidget#FaceLockOverlay {{ background: {C.BG}; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)
        outer.addStretch(1)
        _c = Qt.AlignmentFlag.AlignHCenter

        title = QLabel("◈  FACE UNLOCK")
        title.setFont(QFont("Courier New", 16, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(title, 0, _c)

        self._status = QLabel("")
        self._status.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setWordWrap(True)
        self._status.setFixedWidth(440)
        outer.addWidget(self._status, 0, _c)

        self._preview = QLabel("CAMERA…")
        self._preview.setFont(QFont("Courier New", 8))
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setFixedSize(320, 240)
        self._preview.setStyleSheet(
            f"background: #000000; color: {C.TEXT_DIM};"
            f" border: 1px solid {C.BORDER_B}; border-radius: 6px;"
        )
        outer.addWidget(self._preview, 0, _c)

        self._bar = QProgressBar()
        self._bar.setFixedSize(320, 8)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet(f"""
            QProgressBar {{ background: {C.PANEL}; border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QProgressBar::chunk {{ background: {C.PRI}; border-radius: 2px; }}
        """)
        self._bar.hide()
        outer.addWidget(self._bar, 0, _c)

        self._btn = QPushButton("")
        self._btn.setFixedHeight(28)
        self._btn.setMinimumWidth(260)
        self._btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 12px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """)
        self._btn.clicked.connect(lambda _checked=False: self.action_clicked.emit())
        self._btn.hide()
        outer.addWidget(self._btn, 0, _c)

        outer.addStretch(1)

        self._btn_timer = QTimer(self)
        self._btn_timer.setSingleShot(True)
        self._btn_timer.timeout.connect(self._btn.show)
        self.hide()

    def set_status(self, text: str, color: str | None = None) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(
            f"color: {color or C.TEXT_MED}; background: transparent;"
        )

    def set_progress(self, n: int, total: int) -> None:
        self._bar.setMaximum(max(1, int(total)))
        self._bar.setValue(max(0, int(n)))

    def set_frame(self, img_bytes: bytes) -> None:
        if not self.isVisible():
            return
        px = QPixmap()
        if not px.loadFromData(img_bytes) or px.isNull():
            return
        px = QPixmap.fromImage(px.toImage().mirrored(True, False))   # selfie view
        self._preview.setPixmap(px.scaled(
            320, 240,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def set_mode(self, mode: str, allow_skip: bool = True) -> None:
        self._btn_timer.stop()
        self._btn.hide()
        self._bar.hide()
        self._bar.setValue(0)
        if mode == "prep":
            self.set_status("PREPARING FACE RECOGNITION…", C.TEXT_MED)
            self._btn.setText("CONTINUE WITHOUT FACE UNLOCK")
            self._btn_timer.start(15000)
        elif mode == "lock":
            self.set_status("LOCKED — LOOK AT THE CAMERA", C.PRI)
            self._btn.setText("ENTER WITHOUT FACE (THIS TIME ONLY)")
            if allow_skip:
                self._btn_timer.start(20000)
        elif mode == "enroll":
            self.set_status("FIRST-TIME SETUP — LOOK STRAIGHT AT THE CAMERA", C.PRI)
            self._btn.setText("SKIP — DON'T USE FACE UNLOCK")
            self._bar.show()
            self._btn.show()
        elif mode == "reenroll":
            self.set_status("RE-ENROLLING — LOOK STRAIGHT AT THE CAMERA", C.PRI)
            self._btn.setText("CANCEL")
            self._bar.show()
            self._btn.show()


class SetupOverlay(QWidget):
    done = pyqtSignal(dict)
    finished = pyqtSignal()
    _probe_done = pyqtSignal(object)
    _voice_test_done = pyqtSignal(str)
    _voice_catalog_done = pyqtSignal(str, object, str)

    def __init__(self, parent=None, config=None):
        super().__init__(parent)
        config = config if isinstance(config, dict) else {}
        from memory.config_manager import is_setup_configured
        self._already_configured = is_setup_configured(config)
        saved_local = config.get("local_llm", {})
        if not isinstance(saved_local, dict):
            saved_local = {}
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(_OS.lower(), "linux")
        saved_os = config.get("os_system")
        if saved_os in {"windows", "mac", "linux"}:
            detected = saved_os
        self._sel_os = detected
        self._model_worker = None
        self._probe_generation = 0
        saved_voices = saved_local.get("voice_by_model", {})
        self._voice_by_model = dict(saved_voices) if isinstance(saved_voices, dict) else {}
        saved_engine_voices = saved_local.get("tts_voice_by_engine", {})
        self._tts_voice_by_engine = (
            dict(saved_engine_voices) if isinstance(saved_engine_voices, dict)
            else {}
        )
        saved_tts_engine = str(saved_local.get("tts_engine", "kokoro"))
        self._last_tts_engine = (
            saved_tts_engine
            if saved_tts_engine in {"kokoro", "edgetts", "elevenlabs"}
            else "kokoro"
        )
        saved_tts_voice = str(saved_local.get("tts_voice", "af_heart"))
        if saved_tts_voice:
            self._tts_voice_by_engine.setdefault(
                self._last_tts_engine, saved_tts_voice
            )
        saved_provider = saved_local.get("provider", "ollama")
        if saved_provider not in {"ollama", "lmstudio"}:
            saved_provider = "ollama"
        self._last_provider = saved_provider
        self._models_by_provider = {saved_provider: str(saved_local.get("model", ""))}
        self._base_urls = {
            "ollama": "http://127.0.0.1:11434",
            "lmstudio": "http://127.0.0.1:1234/v1",
        }
        if saved_provider in self._base_urls and saved_local.get("base_url"):
            self._base_urls[saved_provider] = saved_local["base_url"]
        self._probe_done.connect(self._apply_probe_result)
        self._voice_test_done.connect(self._on_voice_test_done)
        self._voice_catalog_done.connect(self._on_voice_catalog_done)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(0)

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 6)
        top_bar.addStretch(1)
        self._close_btn = QPushButton("×")
        self._close_btn.setAccessibleName("Close setup")
        self._close_btn.setToolTip("Close without saving")
        self._close_btn.setFixedSize(28, 28)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                font: bold 18px "Courier New"; }}
            QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
        """)
        self._close_btn.clicked.connect(self.finished.emit)
        top_bar.addWidget(self._close_btn)
        layout.addLayout(top_bar)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        self._stack = QStackedWidget()
        layout.addWidget(self._stack)
        form = QWidget()
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(12, 8, 12, 8)
        form_layout.setSpacing(6)
        form_layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        form_layout.addWidget(_lbl("Configure A.U.R.A. before first boot.", 9, color=C.PRI_DIM))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        scroll.setWidget(form)
        self._stack.addWidget(scroll)

        def _separator(target):
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet(f"color: {C.BORDER};")
            target.addWidget(line)

        def _combo():
            box = QComboBox()
            box.setFont(QFont("Courier New", 9))
            box.setFixedHeight(29)
            box.setStyleSheet(f"""
                QComboBox {{ background: {C.PANEL2}; color: {C.TEXT};
                    border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 7px; }}
                QComboBox QAbstractItemView {{ background: {C.PANEL};
                    color: {C.TEXT}; selection-background-color: {C.PRI_GHO}; }}
            """)
            return box

        def _pills(box, choices):
            holder = QWidget()
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(5)
            buttons = []

            def refresh():
                for button, value in buttons:
                    selected = box.currentData() == value
                    button.setChecked(selected)
                    button.setStyleSheet(f"""
                        QPushButton {{ background: {C.PRI if selected else C.PANEL2};
                            color: {C.BG if selected else C.TEXT_DIM};
                            border: 1px solid {C.PRI if selected else C.BORDER};
                            border-radius: 3px; padding: 4px 6px; font-weight: bold; }}
                        QPushButton:hover {{ border-color: {C.PRI}; color: {C.TEXT}; }}
                    """)

            for label, value in choices:
                button = QPushButton(label)
                button.setCheckable(True)
                button.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                button.setFixedHeight(29)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(
                    lambda _checked=False, selected=value:
                        box.setCurrentIndex(box.findData(selected))
                )
                buttons.append((button, value))
                row.addWidget(button, 1)
            holder._buttons_by_value = dict(
                (value, button) for button, value in buttons
            )
            box.setParent(holder)
            box.hide()
            box.currentIndexChanged.connect(refresh)
            refresh()
            return holder

        form_layout.addSpacing(4)
        _separator(form_layout)
        form_layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                                   align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setText(str(config.get("gemini_api_key", "")))
        self._key_input.setPlaceholderText("AIza… (required for Gemini modes)")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(30)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{ background: {C.PANEL2}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        form_layout.addWidget(self._key_input)

        form_layout.addWidget(_lbl("INITIALISATION MODE", 8, True, color=C.ACC2,
                                   align=Qt.AlignmentFlag.AlignLeft))
        form_layout.addWidget(_lbl("BRAIN MODE", 8, color=C.TEXT_DIM,
                                   align=Qt.AlignmentFlag.AlignLeft))
        self._mode = _combo()
        self._mode.addItem("GEMINI", "gemini")
        self._mode.addItem("LOCAL ONLY", "local")
        self._mode.addItem("GEMINI + LOCAL FALLBACK", "fallback")
        mode_index = self._mode.findData(saved_local.get("mode", "gemini"))
        self._mode.setCurrentIndex(max(0, mode_index))
        self._mode_pills = _pills(self._mode, [
            ("GEMINI KEY ONLY", "gemini"),
            ("LOCAL AI ONLY", "local"),
            ("GEMINI + LOCAL FALLBACK", "fallback"),
        ])
        form_layout.addWidget(self._mode_pills)
        _separator(form_layout)
        form_layout.addWidget(_lbl(
            "SPEECH-TO-TEXT MODEL", 8, color=C.TEXT_DIM,
            align=Qt.AlignmentFlag.AlignLeft
        ))
        self._vosk_model_label = _lbl(
            "VOSK MODEL  (downloaded once; speech recognition only)",
            7, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft
        )
        self._vosk_model = _combo()
        for label, model in VOSK_MODEL_OPTIONS:
            self._vosk_model.addItem(label, model)
        vosk_model_index = self._vosk_model.findData(
            saved_local.get("vosk_model", DEFAULT_VOSK_MODEL)
        )
        self._vosk_model.setCurrentIndex(max(0, vosk_model_index))
        self._vosk_model.setToolTip(
            "Vosk is local speech recognition, not speech synthesis. It is used "
            "in Local AI mode and when fallback switches from Gemini; Gemini "
            "Live uses its own speech recognition while connected. The model "
            "downloads on the first local listening session."
        )
        form_layout.addWidget(self._vosk_model_label)
        form_layout.addWidget(self._vosk_model)

        form_layout.addWidget(_lbl("LOCAL LLM PROVIDER", 8, color=C.TEXT_DIM,
                                   align=Qt.AlignmentFlag.AlignLeft))
        provider_row = QHBoxLayout()
        provider_row.setSpacing(6)
        self._provider = _combo()
        self._provider.addItem("OLLAMA", "ollama")
        self._provider.addItem("LM STUDIO / OPENAI", "lmstudio")
        self._provider.setCurrentIndex(max(
            0, self._provider.findData(saved_provider)
        ))
        self._provider_pills = _pills(self._provider, [
            ("OLLAMA", "ollama"), ("LM STUDIO / OPENAI", "lmstudio")
        ])
        provider_row.addWidget(self._provider_pills, 1)
        self._refresh = QPushButton("↻ REFRESH")
        self._refresh.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._refresh.setFixedHeight(29)
        self._refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh.setStyleSheet(f"""
            QPushButton {{ background: {C.PANEL2}; color: {C.PRI};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}
            QPushButton:hover {{ border-color: {C.PRI}; }}
        """)
        provider_row.addWidget(self._refresh)
        form_layout.addLayout(provider_row)
        self._base_url = QLineEdit()
        self._base_url.setFont(QFont("Courier New", 9))
        self._base_url.setFixedHeight(28)
        self._base_url.setPlaceholderText("http://127.0.0.1:11434")
        self._base_url.setStyleSheet(self._key_input.styleSheet())
        self._base_url.setText(str(
            saved_local.get("base_url", self._base_urls[saved_provider])
        ))
        form_layout.addWidget(_lbl("SERVER URL", 7, color=C.TEXT_DIM,
                                   align=Qt.AlignmentFlag.AlignLeft))
        form_layout.addWidget(self._base_url)
        self._provider_status = _lbl("● SERVER STATUS: NOT CHECKED", 8, color=C.RED,
                                     align=Qt.AlignmentFlag.AlignLeft)
        form_layout.addWidget(self._provider_status)
        self._model = _combo()
        self._model.setEditable(True)
        self._model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        default_model = LOCAL_MODEL_SUGGESTIONS[saved_provider][0]
        self._model.setEditText(str(saved_local.get("model", default_model)))
        self._model.setToolTip(
            "Choose a model reported as loaded by the server or type its exact model ID."
        )
        form_layout.addWidget(_lbl("MODEL  (select a listed model or type a name)", 8,
                                   color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        form_layout.addWidget(self._model)
        form_layout.addWidget(_lbl(
            "Suggestions are examples; select one or type your loaded model ID.",
            7, color=C.PRI_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._provider_hint = _lbl(
            "Start Ollama, then run: ollama pull qwen2.5:3b",
            7, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft)
        form_layout.addWidget(self._provider_hint)

        _separator(form_layout)
        form_layout.addWidget(_lbl("TEXT-TO-SPEECH ENGINE", 8, color=C.TEXT_DIM,
                                   align=Qt.AlignmentFlag.AlignLeft))
        self._tts = _combo()
        self._tts.addItem("Kokoro (offline neural)", "kokoro")
        self._tts.addItem("Edge TTS (online)", "edgetts")
        self._tts.addItem("ElevenLabs (online)", "elevenlabs")
        tts_index = self._tts.findData(saved_local.get("tts_engine", "kokoro"))
        self._tts.setCurrentIndex(max(0, tts_index))
        self._tts_pills = _pills(self._tts, [
            ("KOKORO", "kokoro"), ("EDGETTS", "edgetts"),
            ("ELEVENLABS", "elevenlabs"),
        ])
        form_layout.addWidget(self._tts_pills)
        self._tts_voice = _combo()
        self._tts_voice.setEditable(True)
        self._tts_voice.addItem("af_heart")
        self._tts_voice.addItem("en-US-GuyNeural")
        saved_tts_voice = str(saved_local.get("tts_voice", "af_heart"))
        if self._tts_voice.findText(saved_tts_voice) < 0:
            self._tts_voice.addItem(saved_tts_voice)
        self._tts_voice.setCurrentText(saved_tts_voice)
        form_layout.addWidget(_lbl("VOICE ID", 8, color=C.TEXT_DIM,
                                   align=Qt.AlignmentFlag.AlignLeft))
        voice_row = QHBoxLayout()
        voice_row.setSpacing(6)
        voice_row.addWidget(self._tts_voice, 1)
        self._refresh_tts_voices_btn = QPushButton("↻ VOICES")
        self._refresh_tts_voices_btn.setFont(
            QFont("Courier New", 8, QFont.Weight.Bold)
        )
        self._refresh_tts_voices_btn.setFixedHeight(29)
        self._refresh_tts_voices_btn.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        self._refresh_tts_voices_btn.setStyleSheet(f"""
            QPushButton {{ background: {C.PANEL2}; color: {C.PRI};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}
            QPushButton:hover {{ border-color: {C.PRI}; }}
        """)
        self._refresh_tts_voices_btn.clicked.connect(self._refresh_tts_voices)
        voice_row.addWidget(self._refresh_tts_voices_btn)
        self._test_voice_btn = QPushButton("▶ TEST")
        self._test_voice_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._test_voice_btn.setFixedHeight(29)
        self._test_voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._test_voice_btn.setStyleSheet(f"""
            QPushButton {{ background: {C.PANEL2}; color: {C.PRI};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}
            QPushButton:hover {{ border-color: {C.PRI}; }}
        """)
        self._test_voice_btn.clicked.connect(self._test_voice)
        voice_row.addWidget(self._test_voice_btn)
        form_layout.addLayout(voice_row)
        self._voice_test_status = _lbl("", 7, color=C.TEXT_DIM,
                                      align=Qt.AlignmentFlag.AlignLeft)
        form_layout.addWidget(self._voice_test_status)
        self._voice_catalog_status = _lbl("", 7, color=C.TEXT_DIM,
                                          align=Qt.AlignmentFlag.AlignLeft)
        form_layout.addWidget(self._voice_catalog_status)
        self._elevenlabs_key_label = _lbl(
            "ELEVENLABS API KEY",
            8, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft
        )
        form_layout.addWidget(self._elevenlabs_key_label)
        self._elevenlabs_key_input = QLineEdit()
        self._elevenlabs_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._elevenlabs_key_input.setText(
            str(config.get("elevenlabs_api_key", ""))
        )
        self._elevenlabs_key_input.setPlaceholderText(
            "Required for ElevenLabs voices"
        )
        self._elevenlabs_key_input.setFont(QFont("Courier New", 9))
        self._elevenlabs_key_input.setFixedHeight(29)
        self._elevenlabs_key_input.setStyleSheet(self._key_input.styleSheet())
        form_layout.addWidget(self._elevenlabs_key_input)
        form_layout.addWidget(_lbl(
                                   "Edge TTS and ElevenLabs need internet. "
                                   "Kokoro works offline; Local AI mode can use any selected voice.",
                                   7, color=C.PRI_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._local_voice_override = QCheckBox(
            "Use selected TTS voice for Gemini responses"
        )
        self._local_voice_override.setChecked(
            bool(saved_local.get("local_voice_override", False))
        )
        self._local_voice_override.setStyleSheet(
            f"QCheckBox {{ color: {C.ACC2}; background: transparent; }}"
            f"QCheckBox::indicator {{ width: 15px; height: 15px; }}"
            f"QCheckBox::indicator:unchecked {{ border: 1px solid {C.BORDER_B}; }}"
            f"QCheckBox::indicator:checked {{ background: {C.PRI}; border: 1px solid {C.PRI}; }}"
        )
        self._local_voice_override.setToolTip(
            "In Gemini + Local Fallback mode, use the selected TTS provider for "
            "Gemini's spoken replies. Kokoro works offline; Edge TTS needs internet; "
            "ElevenLabs needs internet and an API key."
        )
        self._local_voice_override.toggled.connect(self._tts_changed)
        form_layout.addWidget(self._local_voice_override)

        _separator(form_layout)
        form_layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                                   align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        form_layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                                   align=Qt.AlignmentFlag.AlignLeft))
        os_row = QHBoxLayout()
        os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        form_layout.addLayout(os_row)
        self._sel(detected)

        self._init_btn = QPushButton(
            "▸  SAVE CHANGES" if self._already_configured
            else "▸  INITIALISE SYSTEMS"
        )
        self._init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        self._init_btn.setFixedHeight(34)
        self._init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        form_layout.addWidget(self._init_btn)
        self._init_btn.clicked.connect(self._submit)
        self._mode.currentIndexChanged.connect(self._mode_changed)
        self._provider.currentIndexChanged.connect(self._provider_changed)
        self._refresh.clicked.connect(self._refresh_models)
        self._tts.currentIndexChanged.connect(self._tts_changed)
        self._model.currentTextChanged.connect(self._model_changed)
        self._tts_voice.currentTextChanged.connect(self._save_current_model_voice)
        self._mode_changed()
        self._provider_changed()
        self._tts_changed()

        self._status_page = QWidget()
        status_layout = QVBoxLayout(self._status_page)
        status_layout.setContentsMargins(22, 30, 22, 24)
        status_layout.setSpacing(12)
        status_layout.addWidget(_lbl("◈  SYSTEMS INITIALISING", 13, True))
        self._status_rows = {}
        for key, title in (
            ("stt", "SPEECH RECOGNITION  (STT)"),
            ("llm", "LANGUAGE MODEL  (LLM)"),
            ("tts", "VOICE SYNTHESIS  (TTS)"),
        ):
            frame = QFrame()
            frame.setStyleSheet(f"QFrame {{ background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 3px; }}")
            row = QVBoxLayout(frame)
            row.setContentsMargins(10, 8, 10, 8)
            heading = QHBoxLayout()
            heading.addWidget(_lbl(title, 8, True, color=C.ACC2,
                                   align=Qt.AlignmentFlag.AlignLeft))
            status = _lbl("CHECKING...", 8, color=C.PRI,
                          align=Qt.AlignmentFlag.AlignRight)
            heading.addWidget(status)
            row.addLayout(heading)
            progress = QProgressBar()
            progress.setRange(0, 0)
            progress.setFixedHeight(5)
            progress.setTextVisible(False)
            progress.setStyleSheet(f"QProgressBar {{ background: {C.BG}; border: 0; }} QProgressBar::chunk {{ background: {C.PRI}; }}")
            row.addWidget(progress)
            status_layout.addWidget(frame)
            self._status_rows[key] = (status, progress)
        self._status_message = _lbl("Checking local components...", 8, color=C.TEXT_DIM)
        status_layout.addWidget(self._status_message)
        status_layout.addStretch(1)
        self._continue_btn = QPushButton("▸  CONTINUE TO A.U.R.A.")
        self._continue_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        self._continue_btn.setFixedHeight(36)
        self._continue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._continue_btn.setStyleSheet(self._init_btn.styleSheet())
        self._continue_btn.setEnabled(False)
        self._continue_btn.clicked.connect(self.finished.emit)
        status_layout.addWidget(self._continue_btn)
        self._stack.addWidget(self._status_page)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#331700"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {C.PANEL2}; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _mode_changed(self, *_):
        local = self._mode.currentData() != "gemini"
        fallback = self._mode.currentData() == "fallback"
        self._provider_pills.setEnabled(local)
        self._local_voice_override.setVisible(fallback)
        for widget in (self._refresh, self._base_url, self._model):
            widget.setEnabled(local)
        self._key_input.setPlaceholderText(
            "AIza… (optional in Local Only)" if self._mode.currentData() == "local"
            else "AIza… (required for Gemini modes)"
        )
        self._tts_changed()
        if local:
            self._refresh_models()

    def _provider_changed(self, *_):
        provider = self._provider.currentData()
        previous = self._last_provider
        if hasattr(self, "_base_url"):
            self._base_urls[previous] = self._base_url.text().strip() or self._base_urls[previous]
            previous_model = self._model.currentText().strip()
            if previous_model:
                self._models_by_provider[previous] = previous_model
        self._last_provider = provider
        self._base_url.setText(self._base_urls[provider])
        self._provider_hint.setText(
            "Start Ollama, then run: ollama pull qwen2.5:1.5b" if provider == "ollama"
            else "Start the LM Studio local server (Developer → Start Server)."
        )
        self._provider_status.setText("● CHECKING SERVER...")
        self._provider_status.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._model.clear()
        self._model.setEditText(self._models_by_provider.get(
            provider, LOCAL_MODEL_SUGGESTIONS[provider][0]
        ))
        self._populate_model_choices()
        if self._mode.currentData() != "gemini":
            self._refresh_models()

    def _populate_model_choices(self, discovered=()):
        provider = self._provider.currentData()
        current = self._selected_model()
        self._model.blockSignals(True)
        self._model.clear()
        for model in discovered:
            self._model.addItem(model, model)
        for model in LOCAL_MODEL_SUGGESTIONS[provider]:
            if model not in discovered:
                self._model.addItem(f"{model}  ·  suggested; load in server", model)
        self._model.setEditText(current)
        self._model.blockSignals(False)

    def _model_changed(self, model):
        model = self._selected_model()
        provider = self._provider.currentData()
        if model and provider:
            self._models_by_provider[provider] = model
        if not model:
            return
        voice = self._voice_by_model.get(model)
        if voice and self._tts_voice.findData(voice) < 0:
            voice = None
        voice = (
            voice
            or self._tts_voice_by_engine.get(self._tts.currentData())
            or self._default_tts_voice()
        )
        if self._selected_voice() != voice:
            self._tts_voice.setCurrentText(voice)

    def _save_current_model_voice(self, voice):
        model = self._selected_model()
        voice = self._selected_voice()
        if model and voice:
            self._voice_by_model[model] = voice
        engine = self._tts.currentData()
        if engine and voice:
            self._tts_voice_by_engine[engine] = voice

    def _selected_model(self):
        if self._model.isEditable():
            text = self._model.currentText().strip()
            index = self._model.currentIndex()
            if index >= 0 and text == self._model.itemText(index):
                selected = self._model.itemData(index)
                if isinstance(selected, str) and selected:
                    return selected.strip()
            return text
        selected = self._model.currentData()
        if isinstance(selected, str) and selected:
            return selected.strip()
        return self._model.currentText().strip()

    def _selected_voice(self):
        selected = self._tts_voice.currentData()
        if isinstance(selected, str) and selected:
            return selected.strip()
        text = self._tts_voice.currentText().strip()
        return text.split("  ·  ", 1)[0].strip()

    def _select_voice(self, voice_id):
        index = self._tts_voice.findData(voice_id)
        if index >= 0:
            self._tts_voice.setCurrentIndex(index)
        else:
            self._tts_voice.setEditText(voice_id)

    def _default_tts_voice(self):
        return {
            "kokoro": "af_heart",
            "edgetts": "en-US-GuyNeural",
            "elevenlabs": "pNInz6obpgDQGcFmaJgB",
        }.get(self._tts.currentData(), "")

    def _refresh_models(self):
        self._probe_generation += 1
        generation = self._probe_generation
        if self._mode.currentData() == "gemini":
            self._provider_status.setText("● OPTIONAL — SELECT A LOCAL MODE TO CONNECT")
            self._provider_status.setStyleSheet(
                f"color: {C.TEXT_DIM}; background: transparent;"
            )
            return
        if self._model_worker is not None and self._model_worker.isRunning():
            return
        provider, url = self._provider.currentData(), self._base_url.text().strip()
        if not url:
            return
        self._base_urls[provider] = url
        self._refresh.setEnabled(False)

        class Worker(QThread):
            result = pyqtSignal(int, bool, object, str)

            def run(worker_self):
                try:
                    from core.llm_client import is_reachable, list_models
                    reachable = is_reachable(provider, url)
                    models = list_models(provider, url) if reachable else []
                    worker_self.result.emit(generation, reachable, models, "")
                except Exception as exc:
                    worker_self.result.emit(generation, False, [], str(exc))

        self._model_worker = Worker(self)
        self._model_worker.result.connect(self._models_ready)
        self._model_worker.finished.connect(
            lambda request_id=generation: self._model_probe_finished(request_id)
        )
        self._model_worker.start()

    def _model_probe_finished(self, request_id):
        if (request_id != self._probe_generation
                and self._mode.currentData() != "gemini"):
            QTimer.singleShot(0, self._refresh_models)

    def _models_ready(self, request_id, reachable, models, error):
        if request_id != self._probe_generation:
            return
        self._refresh.setEnabled(True)
        self._provider_status.setText(
            "● SERVER CONNECTED" if reachable else "● SERVER NOT RUNNING"
        )
        self._provider_status.setStyleSheet(
            f"color: {C.GREEN if reachable else C.RED}; background: transparent;"
        )
        current = self._selected_model()
        self._populate_model_choices(models)
        if current:
            index = self._model.findData(current)
            if index >= 0:
                self._model.setCurrentIndex(index)
            else:
                self._model.setEditText(current)
        if not reachable and error:
            self._provider_hint.setText(f"Could not query server: {error[:100]}")

    def _tts_changed(self, *_):
        engine = self._tts.currentData()
        previous_engine = self._last_tts_engine
        engine_changed = engine != previous_engine
        if engine_changed:
            previous_voice = self._selected_voice()
            if previous_voice:
                self._tts_voice_by_engine[previous_engine] = previous_voice
            self._last_tts_engine = engine
        model = self._selected_model()
        current_voice = (
            self._tts_voice_by_engine.get(engine)
            if engine_changed else
            self._voice_by_model.get(model)
            or self._tts_voice_by_engine.get(engine)
        ) or self._default_tts_voice()
        self._tts_voice.clear()
        if engine == "kokoro":
            for group, voices in KOKORO_VOICE_GROUPS.items():
                for voice_id in voices:
                    self._tts_voice.addItem(
                        f"{voice_id}  ·  {group}", voice_id
                    )
        elif engine == "edgetts":
            self._tts_voice.addItem("en-US-GuyNeural", "en-US-GuyNeural")
        else:
            self._tts_voice.addItem("pNInz6obpgDQGcFmaJgB", "pNInz6obpgDQGcFmaJgB")
        if current_voice:
            self._select_voice(current_voice)
        if self._selected_voice() != current_voice and current_voice:
            self._voice_test_status.setText(
                "Saved voice unavailable; using the first voice in this engine."
            )
            self._select_voice(
                self._tts_voice.itemData(0) or self._tts_voice.itemText(0)
            )
        self._elevenlabs_key_label.setVisible(engine == "elevenlabs")
        self._elevenlabs_key_input.setVisible(engine == "elevenlabs")
        self._refresh_tts_voices_btn.setVisible(
            engine in {"edgetts", "elevenlabs"}
        )
        local_brain = self._mode.currentData() != "gemini"
        if local_brain and engine == "edgetts":
            self._provider_hint.setText(
                "Edge TTS works with Local AI, but needs an internet connection."
            )
        elif local_brain and engine == "elevenlabs":
            self._provider_hint.setText(
                "ElevenLabs works with Local AI; it needs internet and an API key."
            )

    def _refresh_tts_voices(self):
        engine = self._tts.currentData()
        api_key = self._elevenlabs_key_input.text().strip()
        self._refresh_tts_voices_btn.setEnabled(False)
        self._voice_catalog_status.setText("Loading provider voices...")

        def load_voices():
            try:
                if engine == "edgetts":
                    import asyncio
                    import edge_tts

                    catalog = asyncio.run(edge_tts.list_voices())
                    voices = [
                        (
                            f"{item['ShortName']} · {item.get('Locale', '')} · "
                            f"{item.get('Gender', '')}",
                            item["ShortName"],
                        )
                        for item in catalog
                        if item.get("ShortName")
                    ]
                elif engine == "elevenlabs":
                    if not api_key:
                        raise RuntimeError(
                            "Enter an ElevenLabs API key before loading voices."
                        )
                    import requests

                    response = requests.get(
                        "https://api.elevenlabs.io/v1/voices",
                        headers={"xi-api-key": api_key},
                        timeout=20,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    voices = [
                        (item["name"], item["voice_id"])
                        for item in payload.get("voices", [])
                        if item.get("name") and item.get("voice_id")
                    ]
                    if not voices:
                        raise RuntimeError(
                            "ElevenLabs returned no voices for this account."
                        )
                else:
                    raise RuntimeError("The selected TTS engine has no online voice list.")
                self._voice_catalog_done.emit(engine, voices, "")
            except Exception as exc:
                self._voice_catalog_done.emit(engine, [], str(exc))

        threading.Thread(
            target=load_voices, daemon=True, name="tts-voice-catalog"
        ).start()

    def _on_voice_catalog_done(self, engine, voices, error):
        self._refresh_tts_voices_btn.setEnabled(True)
        if engine != self._tts.currentData():
            return
        if error:
            self._voice_catalog_status.setText(
                f"Voice list unavailable: {error[:140]}"
            )
            self._voice_catalog_status.setStyleSheet(
                f"color: {C.RED}; background: transparent;"
            )
            return

        selected = self._selected_voice()
        self._tts_voice.blockSignals(True)
        self._tts_voice.clear()
        for label, voice_id in voices:
            self._tts_voice.addItem(label, voice_id)
        self._select_voice(selected)
        self._tts_voice.blockSignals(False)
        if self._selected_voice() != selected and self._tts_voice.count():
            self._select_voice(self._tts_voice.itemData(0))
        self._voice_catalog_status.setText(
            f"Loaded {len(voices)} voices from {engine}."
        )
        self._voice_catalog_status.setStyleSheet(
            f"color: {C.GREEN}; background: transparent;"
        )

    def _test_voice(self):
        self._test_voice_btn.setEnabled(False)
        self._voice_test_status.setText("Synthesising sample...")
        engine_name = self._tts.currentData()
        voice = self._selected_voice()
        elevenlabs_api_key = self._elevenlabs_key_input.text().strip()

        def run():
            try:
                from core.tts import (
                    EdgeTTSEngine, ElevenLabsTTSEngine, KokoroTTSEngine,
                )
                if engine_name == "kokoro":
                    engine = KokoroTTSEngine(voice=voice or "af_heart")
                elif engine_name == "edgetts":
                    engine = EdgeTTSEngine(voice=voice or "en-US-GuyNeural")
                else:
                    if not elevenlabs_api_key:
                        raise RuntimeError("Add an ElevenLabs API key in config before testing this voice.")
                    engine = ElevenLabsTTSEngine(
                        elevenlabs_api_key, voice or "pNInz6obpgDQGcFmaJgB"
                    )
                engine.speak("A.U.R.A. voice test. Audio systems are online.")
                try:
                    self._voice_test_done.emit("Voice test complete.")
                except RuntimeError:
                    print("[UI] Voice test finished but the overlay was already closed; skipping notification.")
            except Exception as exc:
                try:
                    self._voice_test_done.emit(f"Voice test failed: {exc}")
                except RuntimeError:
                    print(f"[UI] Voice test failed ({exc}) but the overlay was already closed; skipping notification.")

        threading.Thread(target=run, daemon=True, name="voice-test").start()

    def _on_voice_test_done(self, message):
        self._test_voice_btn.setEnabled(True)
        self._voice_test_status.setText(str(message))
        self._voice_test_status.setStyleSheet(
            f"color: {C.GREEN if message == 'Voice test complete.' else C.RED};"
            " background: transparent;"
        )

    def begin_initialising(self, settings: dict):
        self._stack.setCurrentIndex(1)
        self._continue_btn.setEnabled(False)
        self._status_message.setText("Checking selected STT, LLM and TTS components...")
        self._status_worker = threading.Thread(
            target=self._probe_components, args=(settings,), daemon=True
        )
        self._status_worker.start()

    def _probe_components(self, settings):
        import importlib.util
        mode = settings["mode"]
        stt_ok = mode == "gemini" or importlib.util.find_spec("vosk") is not None
        local_tts_required = mode != "gemini"
        tts_modules = {
            "kokoro": ("kokoro",),
            "edgetts": ("edge_tts", "miniaudio"),
            "elevenlabs": ("requests", "miniaudio"),
        }[settings["tts_engine"]]
        missing_tts_modules = [
            package for package in tts_modules
            if importlib.util.find_spec(package) is None
        ]
        missing_tts_key = (
            settings["tts_engine"] == "elevenlabs"
            and not bool(settings.get("elevenlabs_api_key"))
        )
        tts_ok = (
            not local_tts_required
            or (not missing_tts_modules and not missing_tts_key)
        )
        missing_tts_package = ", ".join(missing_tts_modules)
        if mode == "gemini":
            llm_ok = bool(settings.get("gemini_api_key"))
            llm_message = "API key configured" if llm_ok else "API key missing"
        else:
            try:
                from core.llm_client import is_reachable, list_models
                provider, url = settings["provider"], settings["base_url"]
                llm_ok = is_reachable(provider, url)
                names = list_models(provider, url) if llm_ok else []
                model = settings["model"]
                if llm_ok and model and names:
                    llm_ok = model in names
                llm_message = (
                    "Connected" if llm_ok else
                    "Model not listed" if is_reachable(provider, url) else
                    "Server not running"
                )
            except Exception as exc:
                llm_ok, llm_message = False, str(exc)[:80]
        results = {
            "stt": (stt_ok, "READY" if stt_ok else f"Missing {stt_package}"),
            "llm": (llm_ok, llm_message),
            "tts": (
                tts_ok,
                (
                    "OPTIONAL — GEMINI LIVE VOICE"
                    if not local_tts_required else "READY"
                ) if tts_ok else
                "ElevenLabs API key missing" if missing_tts_key else
                f"Missing {missing_tts_package}",
            ),
        }
        self._probe_done.emit(results)

    def _apply_probe_result(self, results):
        ready_count = 0
        for key, (ok, detail) in results.items():
            label, progress = self._status_rows[key]
            label.setText(("● " if ok else "● ") + detail.upper())
            label.setStyleSheet(
                f"color: {C.GREEN if ok else C.RED}; background: transparent;"
            )
            progress.setRange(0, 1)
            progress.setValue(1 if ok else 0)
            progress.setStyleSheet(
                f"QProgressBar {{ background: {C.BG}; border: 0; }}"
                f"QProgressBar::chunk {{ background: {C.GREEN if ok else C.RED}; }}"
            )
            ready_count += int(ok)
        self._status_message.setText(
            "All selected components are ready." if ready_count == 3
            else "Review the missing component status; setup is saved and can be changed later."
        )
        self._continue_btn.setEnabled(True)

    def _submit(self):
        key = self._key_input.text().strip()
        mode = self._mode.currentData()
        tts_engine = self._tts.currentData()
        if mode != "gemini" and not self._selected_model():
            self._provider_hint.setText("Choose a local model or enter its model ID.")
            self._provider_hint.setStyleSheet(f"color: {C.RED}; background: transparent;")
            return
        if mode in {"gemini", "fallback"} and not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            self._provider_hint.setText(
                "Gemini API key is required for Gemini-only and fallback modes."
            )
            self._provider_hint.setStyleSheet(f"color: {C.RED}; background: transparent;")
            return
        elevenlabs_api_key = self._elevenlabs_key_input.text().strip()
        if tts_engine == "elevenlabs" and not elevenlabs_api_key:
            self._provider_hint.setText(
                "Enter an ElevenLabs API key to use or test ElevenLabs voices."
            )
            self._provider_hint.setStyleSheet(
                f"color: {C.RED}; background: transparent;"
            )
            return
        from urllib.parse import urlparse
        parsed = urlparse(self._base_url.text().strip())
        if mode != "gemini" and (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
        ):
            self._provider_hint.setText("Enter a valid http:// or https:// provider URL.")
            self._provider_hint.setStyleSheet(f"color: {C.RED}; background: transparent;")
            return
        if mode != "gemini" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            answer = QMessageBox.warning(
                self,
                "Non-local AI endpoint",
                "This host is not localhost. Prompts and conversation context may be sent to it. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.done.emit({
            "gemini_api_key": key,
            "elevenlabs_api_key": elevenlabs_api_key,
            "os_system": self._sel_os,
            "mode": mode,
            "provider": self._provider.currentData(),
            "base_url": self._base_url.text().strip(),
            "model": self._selected_model(),
            "stt_engine": "vosk",
            "vosk_model": self._vosk_model.currentData(),
            "tts_engine": tts_engine,
            "tts_voice": self._selected_voice(),
            "local_voice_override": self._local_voice_override.isChecked(),
            "voice_id": self._selected_voice(),
            "voice_by_model": dict(self._voice_by_model),
            "tts_voice_by_engine": dict(self._tts_voice_by_engine),
        })


class HueWheel(QWidget):
    """
    Dairesel renk seçici. Kullanıcı tutamacı (küçük beyaz daire) çarkın
    çevresinde sürükleyerek TÜM renk tonları arasından seçim yapar.
    Merkezdeki dolu daire seçilen rengin canlı önizlemesidir.
    """

    hue_picked    = pyqtSignal(str)   # sürükleme sırasında (canlı)
    hue_committed = pyqtSignal(str)   # tutamaç bırakıldığında

    _RING = 16   # halka kalınlığı (px)

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.set_color(initial_hex)

    # ── API ──────────────────────────────────────────────────────────────────
    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def set_color(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    # ── geometri yardımcıları ────────────────────────────────────────────────
    def _ring_rect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _hue_from_pos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()          # ekran y'si aşağı — matematiksel eksene çevir
        ang = math.atan2(dy, dx)      # [-π, π], saat yönünün tersi
        return (ang / (2 * math.pi)) % 1.0

    # ── çizim ────────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self._ring_rect()
        center = rect.center()

        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        # merkez önizleme dairesi
        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)

        # sürüklenen tutamaç
        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#040200"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)

    # ── fare ─────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self._hue_from_pos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self._hue_from_pos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


class CustomizeOverlay(QWidget):
    """Floating overlay — change assistant name, user name, UI colour and voice."""

    saved = pyqtSignal(str, str, str, str, bool)   # assistant_name, user_name, ui_color, voice, local_voice
    configure_local_ai = pyqtSignal()
    _OW, _OH = 400, 660

    def __init__(self, assistant_name="AURA", user_name="",
                 ui_color=DEFAULT_UI_COLOR, voice="", local_voice=False, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            CustomizeOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        _fs = (f"QLineEdit {{ background: {C.PANEL2}; color: {C.TEXT}; "
               f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
               f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        lay.addWidget(_lbl("⚙  CUSTOMISE ASSISTANT", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_lbl("ASSISTANT NAME", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(QFont("Courier New", 10))
        self._name_input.setFixedHeight(32)
        self._name_input.setStyleSheet(_fs)
        lay.addWidget(self._name_input)

        lay.addSpacing(4)
        lay.addWidget(_lbl("YOUR NAME  (leave blank for default sir / efendim)", 8,
                            color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("e.g.  Tony   (leave blank for auto)")
        self._user_input.setFont(QFont("Courier New", 10))
        self._user_input.setFixedHeight(32)
        self._user_input.setStyleSheet(_fs)
        lay.addWidget(self._user_input)

        local_ai_btn = QPushButton("LOCAL AI / STT / TTS SETTINGS")
        local_ai_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        local_ai_btn.setFixedHeight(28)
        local_ai_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        local_ai_btn.setStyleSheet(f"""
            QPushButton {{ background: {C.PANEL2}; color: {C.PRI};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ border-color: {C.PRI}; }}
        """)
        local_ai_btn.clicked.connect(self.configure_local_ai.emit)
        lay.addWidget(local_ai_btn)

        # ── Assistant voice — Gemini prebuilt voices ─────────────────────────
        # Names are language-neutral proper nouns, so the row reads the same in
        # every locale. Selecting one and applying rebuilds the Live session.
        from memory.config_manager import AVAILABLE_VOICES, DEFAULT_VOICE
        lay.addSpacing(4)
        lay.addWidget(_lbl("ASSISTANT VOICE", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._sel_voice   = (voice or DEFAULT_VOICE)
        if self._sel_voice not in AVAILABLE_VOICES:
            self._sel_voice = DEFAULT_VOICE
        self._voice_btns: dict[str, QPushButton] = {}
        voice_row = QHBoxLayout(); voice_row.setSpacing(4)
        for _v in AVAILABLE_VOICES:
            b = QPushButton(_v)
            b.setCheckable(True)
            b.setFixedHeight(28)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, name=_v: self._on_voice_pick(name))
            self._voice_btns[_v] = b
            voice_row.addWidget(b)
        lay.addLayout(voice_row)
        self._refresh_voice_btns()

        # ── JARVIS — fully offline local British voice ───────────────────────
        # Independent of the Gemini voice picker above: when ON, main.py speaks
        # every reply through an on-device British voice instead of Gemini's
        # own audio, no internet required. The Gemini voice pick above is kept
        # (just greyed out) so it's ready the moment JARVIS is switched off.
        #
        # The JARVIS voice-selection UI (LOCAL VOICE label, toggle button,
        # and manual voice picker) has been removed from this menu. All the
        # widgets below are still built — just never added to `lay` — because
        # _refresh_jarvis_btn(), _toggle_jarvis(), _pick_jarvis_voice() and
        # _save() (which reads self._jarvis_on) all still reference them.
        try:
            from core import local_tts
            self._jarvis_voice_id  = local_tts.available_voice_id()
            self._jarvis_all_voices = local_tts.list_voices()
        except Exception:
            self._jarvis_voice_id  = None
            self._jarvis_all_voices = []
        self._jarvis_on = bool(local_voice) and self._jarvis_voice_id is not None

        self._jarvis_btn = QPushButton("🇬🇧  JARVIS — OFFLINE BRITISH VOICE")
        self._jarvis_btn.setCheckable(True)
        self._jarvis_btn.setFixedHeight(30)
        self._jarvis_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._jarvis_btn.clicked.connect(self._toggle_jarvis)

        # Manual fallback: shown only when no installed voice got auto-flagged
        # British — e.g. a differently-named voice pack, or an engine whose
        # driver doesn't expose a language tag pyttsx3 can read. Rather than a
        # dead end, the user can point JARVIS at any voice the engine reports.
        self._jarvis_pick_hint = _lbl("", 7, color=C.TEXT_DIM,
                                        align=Qt.AlignmentFlag.AlignLeft)
        self._jarvis_pick_hint.setWordWrap(True)

        self._jarvis_pick_widget = QWidget()
        pick_row = QHBoxLayout(self._jarvis_pick_widget)
        pick_row.setContentsMargins(0, 0, 0, 0)
        pick_row.setSpacing(4)
        self._jarvis_pick_combo = QComboBox()
        self._jarvis_pick_combo.setFixedHeight(26)
        self._jarvis_pick_combo.setFont(QFont("Courier New", 8))
        self._jarvis_pick_combo.setStyleSheet(
            f"QComboBox {{ background: {C.PANEL2}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox QAbstractItemView {{ background: {C.PANEL2}; color: {C.TEXT}; }}"
        )
        for v in self._jarvis_all_voices:
            self._jarvis_pick_combo.addItem(v["name"] or v["id"], v["id"])
        pick_btn = QPushButton("USE")
        pick_btn.setFixedSize(48, 26)
        pick_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        pick_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pick_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        pick_btn.clicked.connect(self._pick_jarvis_voice)
        pick_row.addWidget(self._jarvis_pick_combo, 1)
        pick_row.addWidget(pick_btn)

        self._refresh_jarvis_btn()

        # ── UI colour — renk çarkı ───────────────────────────────────────────
        lay.addSpacing(4)
        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(_lbl("UI COLOUR  —  drag the handle", 8,
                               color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAULT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        df_btn.clicked.connect(lambda: self._set_color(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        lay.addLayout(clr_hdr)

        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color     = self._initial_color
        self.on_preview     = None   # callable(hex) — canlı önizleme; MainWindow bağlar

        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        lay.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self._on_wheel_pick)
        self._wheel.hue_committed.connect(self._on_wheel_commit)

        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText("#ff9224   (custom hex colour)")
        self._hex_input.setFont(QFont("Courier New", 10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.setStyleSheet(_fs)
        self._hex_input.textEdited.connect(self._on_hex_edited)
        lay.addWidget(self._hex_input)

        lay.addSpacing(6)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        save_btn = QPushButton("▸  APPLY CHANGES")
        save_btn.setFixedHeight(34)
        save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(QFont("Courier New", 9))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    # ── voice selection ──────────────────────────────────────────────────────
    def _on_voice_pick(self, name: str):
        self._sel_voice = name
        self._refresh_voice_btns()

    def _refresh_voice_btns(self):
        """Highlight the selected voice pill; dim the rest."""
        for name, b in self._voice_btns.items():
            on = (name == self._sel_voice)
            b.setChecked(on)
            if on:
                b.setStyleSheet(f"""
                    QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                        border: 1px solid {C.PRI}; border-radius: 3px; }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_MED};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
                """)

    # ── JARVIS (local voice) toggle ──────────────────────────────────────────
    def _toggle_jarvis(self):
        self._jarvis_on = not self._jarvis_on
        self._refresh_jarvis_btn()

    def _refresh_jarvis_btn(self):
        """Style the JARVIS pill for its on/off state, grey the Gemini voice
        pills out while it's on (they aren't what's speaking), and show the
        manual voice picker only when auto-detection found nothing British."""
        b = self._jarvis_btn
        b.setChecked(self._jarvis_on)
        b.setEnabled(self._jarvis_voice_id is not None)
        if self._jarvis_on:
            b.setStyleSheet(f"""
                QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                    border: 1px solid {C.PRI}; border-radius: 3px; }}
            """)
        elif self._jarvis_voice_id:
            b.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.TEXT_MED};
                    border: 1px solid {C.BORDER}; border-radius: 3px; }}
                QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
            """)
        else:
            b.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px; }}
            """)
        for vb in self._voice_btns.values():
            vb.setEnabled(not self._jarvis_on)

        if self._jarvis_voice_id:
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(
                "Speaks with a local, offline British voice instead of "
                "Gemini's — no internet needed for audio once this is on."
            )
            self._jarvis_pick_hint.setVisible(False)
            self._jarvis_pick_widget.setVisible(False)
        elif self._jarvis_all_voices:
            b.setToolTip(
                "No installed voice was auto-detected as British — pick "
                "one below, then switch JARVIS on."
            )
            self._jarvis_pick_hint.setText(
                f"{len(self._jarvis_all_voices)} local voice(s) found, none "
                "flagged British — pick one:"
            )
            self._jarvis_pick_hint.setVisible(True)
            self._jarvis_pick_widget.setVisible(True)
        else:
            b.setToolTip(
                "No text-to-speech voices were found on this machine — "
                "install pyttsx3 and a system voice to use JARVIS mode."
            )
            self._jarvis_pick_hint.setVisible(False)
            self._jarvis_pick_widget.setVisible(False)

    def _pick_jarvis_voice(self):
        """Pin whatever voice is selected in the fallback combo as JARVIS's,
        bypassing the British-name heuristics entirely."""
        idx = self._jarvis_pick_combo.currentIndex()
        if idx < 0:
            return
        voice_id = self._jarvis_pick_combo.itemData(idx)
        try:
            from core import local_tts
            ok = local_tts.set_voice_id(voice_id)
            if ok:
                self._jarvis_voice_id = local_tts.available_voice_id(force_refresh=True)
        except Exception:
            ok = False
        if ok:
            self._jarvis_on = True
        self._refresh_jarvis_btn()

    # ── renk akışı ───────────────────────────────────────────────────────────
    def _set_color(self, hx: str, update_wheel: bool = True, preview: bool = True):
        """Seçili rengi günceller; hex kutusu + çark senkron kalır, tema canlı önizlenir."""
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.set_color(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def _on_wheel_pick(self, hx: str):
        # Sürükleme sırasında: hex kutusunu güncelle, temayı henüz uygulama
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def _on_wheel_commit(self, hx: str):
        # Tutamaç bırakıldı → tüm arayüzü canlı önizle
        self._set_color(hx, update_wheel=False)

    def _on_hex_edited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._set_color(t, update_wheel=True, preview=True)

    def _cancel(self):
        # Önizleme uygulandıysa açılıştaki renge geri dön
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        self.hide()

    def _save(self):
        name = self._name_input.text().strip() or "AURA"
        user = self._user_input.text().strip()
        self.saved.emit(name, user, self._sel_color or DEFAULT_UI_COLOR,
                         self._sel_voice, self._jarvis_on)
        self.hide()


class PluginManagerOverlay(QWidget):
    """Floating overlay — lists discovered plugins with per-plugin ON/OFF toggles."""

    _OW = 420

    def __init__(self, plugins: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginManagerOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🧩  PLUGIN MANAGER")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        if not plugins:
            empty = QLabel("No plugins found in /plugins.")
            empty.setFont(QFont("Courier New", 8))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(empty)

        for p in plugins:
            lay.addLayout(self._build_row(p))

        lay.addSpacing(4)
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)
        self.adjustSize()

    def _build_row(self, p: dict) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(6)

        label_text = p["name"] if p["valid"] else f"{p['name']}  (⚠ {p['file']})"
        lbl = QLabel(label_text)
        lbl.setFont(QFont("Courier New", 8))
        lbl.setStyleSheet(f"color: {C.TEXT if p['valid'] else C.TEXT_DIM}; background: transparent;")
        lbl.setToolTip(p["description"] if p["valid"] else p["error"])
        lbl.setWordWrap(False)
        row.addWidget(lbl, stretch=1)

        btn = QPushButton()
        btn.setFixedSize(72, 24)
        btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        if not p["valid"]:
            btn.setText("BROKEN")
            btn.setEnabled(False)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
            """)
        else:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._style_toggle(btn, p["enabled"])
            btn.clicked.connect(lambda _, name=p["name"], b=btn: self._toggle(name, b))
        row.addWidget(btn)
        return row

    def _style_toggle(self, btn: QPushButton, enabled: bool):
        if enabled:
            btn.setText("ON")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            btn.setText("OFF")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
            """)

    def _toggle(self, name: str, btn: QPushButton):
        from memory.config_manager import get_plugin_enabled, save_plugin_enabled
        new_val = not get_plugin_enabled(name)
        save_plugin_enabled(name, new_val)
        self._style_toggle(btn, new_val)


class _HudOverlay(QWidget):
    """Base for the floating panels placed by hand over the HUD.

    They are children of the central widget but sit in no layout, so Qt never
    invalidates the region they occupy when they hide or shrink: the HUD keeps
    painting around them and their last frame stays on screen as a ghost. Any
    overlay positioned with _centre_overlay needs this."""

    def hideEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            # Repaint exactly what we were covering, before we stop covering it.
            p.update(self.geometry())
        super().hideEvent(e)

    def closeEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().closeEvent(e)


class ConfirmBanner(_HudOverlay):
    """The gate in front of an action that cannot be taken back.

    Nothing blocks while it is up: the assistant keeps talking, so this costs
    no latency."""

    answered = pyqtSignal(bool)
    _OW = 430

    def __init__(self, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ConfirmBanner {{
                background: rgba(14, 3, 0, 250);
                border: 1px solid {C.ACC};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)

        hdr = QLabel("⚠  CONFIRM")
        hdr.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        lay.addWidget(hdr)

        ttl = QLabel(title)
        ttl.setWordWrap(True)
        ttl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        ttl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(ttl)

        if detail:
            dtl = QLabel(detail)
            dtl.setWordWrap(True)
            dtl.setFont(QFont("Courier New", 8))
            dtl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            lay.addWidget(dtl)

        row = QHBoxLayout(); row.setSpacing(8)

        yes = QPushButton("▸  CONFIRM")
        yes.setFixedHeight(32)
        yes.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        yes.setCursor(Qt.CursorShape.PointingHandCursor)
        yes.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC};
                border: 1px solid {C.ACC}; border-radius: 3px; }}
            QPushButton:hover {{ background: rgba(255,107,0,40); }}
        """)
        yes.clicked.connect(lambda: self.answered.emit(True))
        row.addWidget(yes)

        no = QPushButton("CANCEL")
        no.setFixedHeight(32)
        no.setFont(QFont("Courier New", 9))
        no.setCursor(Qt.CursorShape.PointingHandCursor)
        no.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        no.clicked.connect(lambda: self.answered.emit(False))
        row.addWidget(no)
        lay.addLayout(row)

        # Default focus on CANCEL: if someone hits Enter without reading, the
        # safe answer wins.
        no.setDefault(True)
        no.setFocus()


class AudioDeviceOverlay(_HudOverlay):
    """Choose which microphone AURA listens to and which speakers it uses."""

    picked = pyqtSignal()      # emitted after Apply, when something changed
    _OW = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        from core.audio_devices import list_devices, DEFAULT_LABEL
        from memory.config_manager import get_input_device, get_output_device

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            AudioDeviceOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🎧  AUDIO DEVICES")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        _combo_css = (
            f"QComboBox {{ background: {C.PANEL2}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox QAbstractItemView {{ background: {C.PANEL2}; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; border: 1px solid {C.BORDER}; }}"
        )

        def _row(label: str, kind: str, current: str) -> QComboBox:
            cap = QLabel(label)
            cap.setFont(QFont("Courier New", 8))
            cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(cap)

            box = QComboBox()
            box.setFont(QFont("Courier New", 9))
            box.setFixedHeight(30)
            box.setStyleSheet(_combo_css)
            # The list is served from a cache warmed on a background thread at
            # startup, so opening this panel never blocks the Qt thread on the
            # host audio API.
            box.addItem(DEFAULT_LABEL, "")
            for name in list_devices(kind):
                box.addItem(name, name)
            idx = box.findData(current) if current else 0
            box.setCurrentIndex(idx if idx >= 0 else 0)
            if current and idx < 0:
                # Saved device is not plugged in right now. Show it rather than
                # silently resetting the user's choice to default.
                box.addItem(f"{current}  (not connected)", current)
                box.setCurrentIndex(box.count() - 1)
            lay.addWidget(box)
            return box

        self._in_box  = _row("MICROPHONE — what AURA hears you with",
                             "input", get_input_device())
        lay.addSpacing(4)
        self._out_box = _row("SPEAKERS — what AURA talks through",
                             "output", get_output_device())

        note = QLabel("Applying reconnects the session. Your conversation is kept.")
        note.setWordWrap(True)
        note.setFont(QFont("Courier New", 7))
        note.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addSpacing(6)
        lay.addWidget(note)

        row = QHBoxLayout(); row.setSpacing(8)
        ok = QPushButton("▸  APPLY")
        ok.setFixedHeight(32)
        ok.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        ok.clicked.connect(self._apply)
        row.addWidget(ok)

        cancel = QPushButton("CLOSE")
        cancel.setFixedHeight(32)
        cancel.setFont(QFont("Courier New", 9))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _apply(self):
        from memory.config_manager import (
            get_input_device, get_output_device,
            save_input_device, save_output_device,
        )
        new_in  = self._in_box.currentData()  or ""
        new_out = self._out_box.currentData() or ""
        changed = (new_in != get_input_device()) or (new_out != get_output_device())
        save_input_device(new_in)
        save_output_device(new_out)
        self.hide()
        # Only rebuild the session if something actually moved — a no-op Apply
        # should not cost a reconnect.
        if changed:
            self.picked.emit()


class MemoryOverlay(_HudOverlay):
    """Everything AURA has stored about you, and when it learned it."""

    _OW = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            MemoryOverlay {{
                background: rgba(0, 6, 10, 246);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(20, 16, 20, 16)
        self._lay.setSpacing(5)
        self._rebuild()

    def _clear_layout(self):
        """Take every item out of the layout and detach it from the widget tree
        in this call.

        deleteLater() on its own is not enough: it queues destruction for the
        next event-loop pass, and until then the old rows are still children of
        this widget and still paint. setParent(None) removes them from the tree
        now; deleteLater() then frees them safely."""
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    si = sub.takeAt(0)
                    sw = si.widget()
                    if sw is not None:
                        sw.hide()
                        sw.deleteLater()
                sub.deleteLater()

    def _settle(self, before):
        """Size the panel to its content, re-centre it, and repaint what the old
        size covered."""
        self._lay.invalidate()
        self._lay.activate()
        self.updateGeometry()
        self.adjustSize()

        p = self.parentWidget()
        if p is None:
            self.update()
            return
        self.move(max(0, (p.width()  - self.width())  // 2),
                  max(0, (p.height() - self.height()) // 2))
        p.update(before.united(self.geometry()))
        self.update()

    def _rebuild(self):
        before = self.geometry()
        self._clear_layout()

        from memory.memory_manager import all_entries_for_ui

        hdr = QLabel("🧠  WHAT AURA REMEMBERS")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        self._lay.addWidget(sep)

        rows = all_entries_for_ui()

        cap = QLabel(f"{len(rows)} stored facts — newest first. "
                     f"Nothing here is sent anywhere; it lives locally on this machine.")
        cap.setWordWrap(True)
        cap.setFont(QFont("Courier New", 7))
        cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._lay.addWidget(cap)

        if not rows:
            empty = QLabel("Nothing stored yet.")
            empty.setFont(QFont("Courier New", 9))
            empty.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            self._lay.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(min(420, 34 * len(rows) + 10))
            scroll.setStyleSheet(
                f"QScrollArea {{ border: 1px solid {C.BORDER}; border-radius: 3px; "
                f"background: transparent; }}"
            )
            inner = QWidget()
            ilay  = QVBoxLayout(inner)
            ilay.setContentsMargins(6, 6, 6, 6)
            ilay.setSpacing(3)

            for r in rows:
                line = QHBoxLayout(); line.setSpacing(6)
                txt = QLabel(f"<b>{r['key'].replace('_', ' ')}</b> "
                             f"<span style='color:{C.TEXT_MED}'>— {r['value']}</span>")
                txt.setWordWrap(True)
                txt.setFont(QFont("Courier New", 8))
                txt.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
                line.addWidget(txt, 1)

                meta = QLabel(f"{r['category'][:4]} · {r['updated'] or '—'}")
                meta.setFont(QFont("Courier New", 7))
                meta.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
                line.addWidget(meta)

                rm = QPushButton("✕")
                rm.setFixedSize(20, 20)
                rm.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                rm.setCursor(Qt.CursorShape.PointingHandCursor)
                rm.setToolTip("Forget this")
                rm.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
                """)
                rm.clicked.connect(
                    lambda _=False, c=r["category"], k=r["key"]: self._forget(c, k))
                line.addWidget(rm)

                holder = QWidget()
                holder.setLayout(line)
                ilay.addWidget(holder)

            ilay.addStretch()
            scroll.setWidget(inner)
            self._lay.addWidget(scroll)

        close = QPushButton("CLOSE")
        close.setFixedHeight(30)
        close.setFont(QFont("Courier New", 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        self._lay.addWidget(close)

        self._settle(before)
        QTimer.singleShot(0, lambda g=before: self._settle(g))

    def _forget(self, category: str, key: str):
        from memory.memory_manager import forget
        forget(key, category)
        # Rebuild on the NEXT event-loop turn, not inside this click handler.
        QTimer.singleShot(0, self._rebuild)


class PluginSettingsOverlay(QWidget):
    """Floating overlay — renders per-plugin settings forms.

    Fully generic: it iterates the settings schemas a plugin declared and
    builds a form for each. Install a plugin that declares fields and its
    section appears here; install none and this panel simply says there's
    nothing to configure.
    """

    _test_done = pyqtSignal(str, bool, str)   # namespace, ok, message
    _OW = 460

    def __init__(self, sections: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginSettingsOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._sections = sections or []
        self._widgets: dict[tuple, object] = {}    # (namespace, key) -> input widget
        self._types:   dict[tuple, str]    = {}     # (namespace, key) -> field type
        self._status_labels: dict[str, QLabel] = {} # namespace -> status QLabel
        self._test_done.connect(self._on_test_done)

        self._fs = (f"QLineEdit {{ background: {C.PANEL2}; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
                    f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(8)

        root.addWidget(self._lbl("⚙  PLUGIN SETTINGS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        root.addWidget(sep)

        if not self._sections:
            root.addWidget(self._lbl(
                "No configurable plugins are installed.\nDrop a plugin that needs "
                "settings into the plugins folder and it will show up here.",
                9, color=C.TEXT_DIM))
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            inner = QWidget()
            inner.setStyleSheet("background: transparent;")
            form = QVBoxLayout(inner)
            form.setContentsMargins(0, 0, 6, 0)
            form.setSpacing(6)
            for sec in self._sections:
                self._build_section(form, sec)
            form.addStretch(1)
            scroll.setWidget(inner)
            root.addWidget(scroll, 1)

        # ── bottom buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        if self._sections:
            save_btn = QPushButton("▸  SAVE")
            save_btn.setFixedHeight(34)
            save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            save_btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
            """)
            save_btn.clicked.connect(self._save_all)
            btn_row.addWidget(save_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(34)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _lbl(self, txt, fs=9, bold=False, color=C.PRI,
             align=Qt.AlignmentFlag.AlignLeft):
        w = QLabel(txt); w.setAlignment(align); w.setWordWrap(True)
        w.setFont(QFont("Courier New", fs,
                        QFont.Weight.Bold if bold else QFont.Weight.Normal))
        w.setStyleSheet(f"color: {color}; background: transparent;")
        return w

    def _build_section(self, form: QVBoxLayout, sec: dict):
        ns     = sec.get("namespace") or sec.get("plugin") or "plugin"
        title  = sec.get("title") or ns
        fields = sec.get("fields") or []
        values = sec.get("values") or {}

        form.addSpacing(4)
        form.addWidget(self._lbl(title, 10, True, C.PRI))

        for field in fields:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            key   = field["key"]
            ftype = (field.get("type") or "text").lower()
            label = field.get("label") or key
            default = field.get("default")
            stored  = values.get(key, default)

            form.addWidget(self._lbl(label.upper(), 8, color=C.TEXT_DIM))

            if ftype == "choice":
                w = QComboBox()
                w.addItems([str(o) for o in field.get("options", [])])
                w.setFont(QFont("Courier New", 9))
                w.setFixedHeight(30)
                w.setStyleSheet(
                    f"QComboBox {{ background: {C.PANEL2}; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}"
                    f"QComboBox QAbstractItemView {{ background: {C.PANEL2}; color: {C.TEXT}; "
                    f"selection-background-color: {C.PRI_GHO}; }}")
                if stored is not None:
                    w.setCurrentText(str(stored))
            elif ftype == "toggle":
                w = QPushButton()
                w.setCheckable(True)
                w.setChecked(bool(stored))
                w.setFixedHeight(28)
                w.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                w.setCursor(Qt.CursorShape.PointingHandCursor)
                self._style_toggle(w)
                w.toggled.connect(lambda _=False, b=w: self._style_toggle(b))
            else:  # text / password
                w = QLineEdit("" if stored is None else str(stored))
                w.setFont(QFont("Courier New", 10))
                w.setFixedHeight(30)
                w.setStyleSheet(self._fs)
                if field.get("placeholder"):
                    w.setPlaceholderText(str(field["placeholder"]))
                if ftype == "password":
                    w.setEchoMode(QLineEdit.EchoMode.Password)

            self._widgets[(ns, key)] = w
            self._types[(ns, key)]   = ftype
            form.addWidget(w)

        # optional test/connect action button + status line
        action = sec.get("action")
        if isinstance(action, dict) and callable(action.get("run")):
            form.addSpacing(2)
            ab = QPushButton(str(action.get("label") or "TEST"))
            ab.setFixedHeight(30)
            ab.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            ab.setCursor(Qt.CursorShape.PointingHandCursor)
            ab.setStyleSheet(f"""
                QPushButton {{ background: #1f0e00; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            """)
            ab.clicked.connect(lambda _=False, n=ns: self._run_action(n))
            form.addWidget(ab)

        status = self._lbl("", 8, color=C.TEXT_DIM)
        self._status_labels[ns] = status
        form.addWidget(status)

        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {C.BORDER}; margin: 4px 0;")
        form.addWidget(line)

    def _style_toggle(self, btn: QPushButton):
        on = btn.isChecked()
        btn.setText("ON" if on else "OFF")
        if on:
            btn.setStyleSheet(f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                              f"border: 1px solid {C.PRI}; border-radius: 3px; }}")
        else:
            btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                              f"border: 1px solid {C.BORDER}; border-radius: 3px; }}")

    # ── data ──────────────────────────────────────────────────────────────────
    def _gather(self, ns: str) -> dict:
        out = {}
        for (n, key), w in self._widgets.items():
            if n != ns:
                continue
            t = self._types.get((n, key), "text")
            if t == "choice":
                out[key] = w.currentText()
            elif t == "toggle":
                out[key] = w.isChecked()
            else:
                out[key] = w.text().strip()
        return out

    def _save_ns(self, ns: str):
        from memory.config_manager import save_plugin_config
        save_plugin_config(ns, self._gather(ns))

    def _save_all(self):
        for sec in self._sections:
            ns = sec.get("namespace") or sec.get("plugin")
            if ns:
                self._save_ns(ns)
                lbl = self._status_labels.get(ns)
                if lbl:
                    lbl.setText("Saved ✓")
                    lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")

    def _run_action(self, ns: str):
        sec = next((s for s in self._sections
                    if (s.get("namespace") or s.get("plugin")) == ns), None)
        if not sec:
            return
        run_fn = (sec.get("action") or {}).get("run")
        if not callable(run_fn):
            return
        self._save_ns(ns)                 # persist what the user typed before testing
        values = self._gather(ns)
        lbl = self._status_labels.get(ns)
        if lbl:
            lbl.setText("Testing…")
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        def worker():
            try:
                res = run_fn(values)
                if isinstance(res, tuple) and len(res) == 2:
                    ok, msg = bool(res[0]), str(res[1])
                else:
                    ok, msg = bool(res), str(res)
            except Exception as e:
                ok, msg = False, str(e)
            self._test_done.emit(ns, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, ns: str, ok: bool, msg: str):
        lbl = self._status_labels.get(ns)
        if not lbl:
            return
        lbl.setText(msg)
        color = C.PRI if ok else "#ff6b6b"
        lbl.setStyleSheet(f"color: {color}; background: transparent;")


class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — offers quick AURA actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(QFont("Courier New", 8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(QFont("Courier New", 8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


# ── Weather widget (Open-Meteo) ──────────────────────────────────────────────
# Location comes from the machine's public IP (no API key, no GPS, no extra
# dependency — stdlib urllib only). Several providers are tried in turn so one
# being down or rate-limited never leaves the widget blank. The forecast itself
# comes from the Open-Meteo API, requested explicitly in °C and km/h.
_WEATHER_GEO_URLS = (
    "https://ipwho.is/",
    "https://ipapi.co/json/",
    "http://ip-api.com/json/",
)
_WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat:.4f}&longitude={lon:.4f}"
    "&current=temperature_2m,wind_speed_10m"
    "&temperature_unit=celsius&wind_speed_unit=kmh&timezone=auto"
)


def _weather_http_json(url: str, timeout: float = 8.0) -> dict:
    import urllib.request
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (AURA weather widget)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    if not isinstance(data, dict):
        raise ValueError("unexpected response")
    return data


def _weather_locate() -> dict | None:
    """Best-effort location of this machine → {lat, lon, city, country} or None."""
    for url in _WEATHER_GEO_URLS:
        try:
            d = _weather_http_json(url, timeout=6.0)
            if d.get("success") is False or d.get("error") or d.get("status") == "fail":
                continue
            lat = d.get("latitude", d.get("lat"))
            lon = d.get("longitude", d.get("lon"))
            lat, lon = float(lat), float(lon)
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                continue
            city = str(d.get("city") or d.get("region") or "").strip()
            country = str(d.get("country_name") or d.get("country") or "").strip()
            return {"lat": lat, "lon": lon, "city": city, "country": country}
        except Exception as e:
            print(f"[Weather] Location lookup failed ({url}): {e}")
    return None


def _weather_fetch(lat: float, lon: float) -> tuple[float, float]:
    """Open-Meteo current conditions → (temperature °C, wind speed km/h)."""
    d = _weather_http_json(_WEATHER_URL.format(lat=lat, lon=lon), timeout=10.0)
    cur = d.get("current") or {}
    return float(cur["temperature_2m"]), float(cur["wind_speed_10m"])


class WeatherWidget(_HudOverlay):
    """Small floating weather pop-up: temperature (°C) and wind speed (km/h)
    for wherever the user is. Draggable by its header, closable with the ✕
    (F9 brings it back). Spawns in the top-left corner of the AURA window.

    All network work runs on a worker thread; results come back to the Qt
    thread through a signal, so the UI never blocks."""

    _W          = 244
    _MARGIN     = 12                # gap from the left edge
    _TOP        = 54 + 10           # just below the 54 px header bar
    _REFRESH_MS = 10 * 60 * 1000    # auto-refresh while visible

    _result_sig = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("WeatherWidget")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            QWidget#WeatherWidget {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self.user_moved = False         # True once the user has dragged it
        self.on_close   = None          # optional callable
        self._loc       = None          # cached {lat, lon, city, country}
        self._busy      = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 10)
        lay.setSpacing(5)

        # ── header: drag handle + refresh + close ───────────────────────────
        hdr_w = _DragHeader(self)
        hdr = QHBoxLayout(hdr_w)
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
        hdr.addWidget(dot)

        title = QLabel("WEATHER")
        title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
        hdr.addWidget(title)
        hdr.addStretch()

        def _hdr_btn(text: str, tip: str, hover: str) -> QPushButton:
            b = QPushButton(text)
            b.setFixedSize(16, 16)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.TEXT_DIM}; border: none; }}
                QPushButton:hover {{ color: {hover}; }}
            """)
            return b

        refresh_btn = _hdr_btn("↻", "Refresh (re-detect location)", C.PRI)
        refresh_btn.clicked.connect(lambda _=False: self._refresh(redetect=True))
        hdr.addWidget(refresh_btn)

        close_btn = _hdr_btn("✕", "Close weather  [F9 to reopen]", C.MUTED_C)
        close_btn.clicked.connect(self._close_clicked)
        hdr.addWidget(close_btn)
        lay.addWidget(hdr_w)

        # ── body ────────────────────────────────────────────────────────────
        self._loc_lbl = QLabel("LOCATING…")
        self._loc_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._loc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(self._loc_lbl)

        self._temp_lbl = QLabel("-- °C")
        self._temp_lbl.setFont(QFont("Courier New", 24, QFont.Weight.Bold))
        self._temp_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(self._temp_lbl)

        self._wind_lbl = QLabel("WIND  -- km/h")
        self._wind_lbl.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        self._wind_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(self._wind_lbl)

        self._status_lbl = QLabel("")
        self._status_lbl.setFont(QFont("Courier New", 7))
        self._status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addWidget(self._status_lbl)

        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self._refresh(redetect=False))
        self._result_sig.connect(self._on_result)

        self.adjustSize()
        self.place_default()
        self.hide()

    # ── placement ────────────────────────────────────────────────────────────
    def place_default(self) -> None:
        self.user_moved = False
        self.move(self._MARGIN, self._TOP)

    def move_clamped(self, pt) -> None:
        """Called by _DragHeader while dragging: keep the box inside the window."""
        p = self.parentWidget()
        if p is None:
            self.move(pt)
            return
        x = max(0, min(pt.x(), max(0, p.width()  - self.width())))
        y = max(0, min(pt.y(), max(0, p.height() - self.height())))
        self.move(x, y)

    def reposition(self) -> None:
        """After a window resize: stay put if dragged (but clamped), else top-left."""
        if self.user_moved:
            self.move_clamped(self.pos())
        else:
            self.move(self._MARGIN, self._TOP)

    def open_widget(self) -> None:
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()

    # ── visibility ───────────────────────────────────────────────────────────
    def _close_clicked(self) -> None:
        cb = self.on_close
        if callable(cb):
            cb()
        self.hide()

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh(redetect=False)
        self._timer.start(self._REFRESH_MS)

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)

    # ── data ─────────────────────────────────────────────────────────────────
    def _refresh(self, redetect: bool = False) -> None:
        if self._busy:
            return
        self._busy = True
        self._status_lbl.setText("UPDATING…")
        self._status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        cached = None if redetect else self._loc
        threading.Thread(target=self._worker, args=(cached,),
                         daemon=True, name="weather").start()

    def _worker(self, cached) -> None:
        try:
            loc = cached or _weather_locate()
            if not loc:
                raise RuntimeError("could not detect your location")
            temp, wind = _weather_fetch(loc["lat"], loc["lon"])
            res = {"ok": True, "loc": loc, "temp": temp, "wind": wind}
        except Exception as e:
            print(f"[Weather] Update failed: {e}")
            res = {"ok": False, "err": str(e)}
        try:
            self._result_sig.emit(res)
        except RuntimeError:
            pass                        # widget already destroyed (app closing)

    def _on_result(self, res) -> None:
        self._busy = False
        try:
            if res.get("ok"):
                loc = res["loc"]
                self._loc = loc
                place = ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
                place = place or f"{loc['lat']:.2f}, {loc['lon']:.2f}"
                if len(place) > 30:
                    place = place[:29] + "…"
                self._loc_lbl.setText(f"⌖ {place.upper()}")
                self._temp_lbl.setText(f"{res['temp']:.1f} °C")
                self._wind_lbl.setText(f"WIND  {res['wind']:.1f} km/h")
                self._status_lbl.setText(f"UPDATED {time.strftime('%H:%M')}  ·  OPEN-METEO")
                self._status_lbl.setStyleSheet(
                    f"color: {C.TEXT_DIM}; background: transparent;")
            else:
                self._status_lbl.setText("OFFLINE — COULD NOT FETCH WEATHER")
                self._status_lbl.setStyleSheet(f"color: {C.RED}; background: transparent;")
        except Exception as e:
            print(f"[Weather] Display error: {e}")


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont("Courier New", 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Courier New", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — AURA ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class _MiniBar(QWidget):
    """Mic + interrupt buttons that drop down under the floating orb.

    They are the very same HudButton widgets the header uses (same size, same
    chamfered outline, same vector mic icon, same red 'alert' state), wired to
    the very same MainWindow handlers. The window itself is a separate frameless
    always-on-top tool window so the orb can stay exactly 128 x 128.
    """

    _W, _H = 158, 42

    def __init__(self, win):
        super().__init__(None, (Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowStaysOnTopHint
                                | Qt.WindowType.Tool
                                | Qt.WindowType.WindowDoesNotAcceptFocus))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFixedSize(self._W, self._H)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(8)

        # Identical to the header: mic 58x30 (icon) + "stop" 76x30 (text).
        self.mic_btn = HudButton("", "mic")
        self.mic_btn.setFixedSize(58, 30)
        self.mic_btn.clicked.connect(win._toggle_mute)
        lay.addWidget(self.mic_btn)

        self.stop_btn = HudButton("stop", "text")
        self.stop_btn.setFixedSize(76, 30)
        self.stop_btn.setToolTip("Interrupt  [ESC]")
        self.stop_btn.clicked.connect(win._do_interrupt)
        lay.addWidget(self.stop_btn)

        self._synced_muted = None
        self.sync_muted(bool(getattr(win, "_muted", False)))

    def sync_muted(self, muted: bool) -> None:
        """Mirror the main window's mic state (F4, remote, video auto-mute…)."""
        if muted == self._synced_muted:
            return
        self._synced_muted = muted
        self.mic_btn.set_alert(muted)
        self.mic_btn.setToolTip(
            "Microphone muted — click to unmute" if muted
            else "Microphone active — click to mute")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = float(self.width()), float(self.height())
        m, c = 1.5, 8.0
        path = QPainterPath()
        path.moveTo(m + c, m)
        path.lineTo(W - m - c, m)
        path.lineTo(W - m, m + c)
        path.lineTo(W - m, H - m - c)
        path.lineTo(W - m - c, H - m)
        path.lineTo(m + c, H - m)
        path.lineTo(m, H - m - c)
        path.lineTo(m, m + c)
        path.closeSubpath()
        p.setBrush(QBrush(qcol(C.BG, 238)))
        p.setPen(QPen(qcol(C.BORDER_A), 1.2))
        p.drawPath(path)
        p.end()


class MiniOrbWidget(QWidget):
    """Always-on-top, draggable 128 x 128 AURA orb.

    Shown only while the main window is minimised (MainWindow.changeEvent) and
    hidden again as soon as it is restored.

    The picture is the HUD's central ball: HudCanvas._draw_orb is called
    directly, and the scale / halo / spin logic below is a line-for-line mirror
    of HudCanvas._step, driven by the live HUD state (speaking, muted and the
    smoothed audio level), so the orb behaves exactly like the one in the HUD:
        SPEAKING  -> fast spin, bright halo, pulses with the voice
        LISTENING -> slow, steady spin
        MUTED     -> almost frozen and tinted red

    Hover the orb  -> mic + stop buttons appear just below it.
    Leave the orb  -> the buttons stay for HIDE_DELAY_MS (4 s), then vanish.
    Drag anywhere on the orb to move it. Double-click it to restore AURA.
    """

    SIZE          = 128
    HIDE_DELAY_MS = 4000      # buttons linger this long after the mouse leaves
    _EDGE         = 24        # default gap from the screen corner
    _BAR_GAP      = 2         # gap between the orb and the button bar

    def __init__(self, win):
        super().__init__(None, (Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowStaysOnTopHint
                                | Qt.WindowType.Tool
                                | Qt.WindowType.WindowDoesNotAcceptFocus))
        self._win = win
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to move  ·  double-click to restore")

        # ── animation state, mirrors HudCanvas ──────────────────────────────
        hud = win.hud
        self.muted       = False
        self.speaking    = False
        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._amp_disp   = 0.0
        self._orb_spin   = 0.0
        self._rings      = [0.0, 120.0, 240.0]
        # Same trails / dots as the HUD ball, so it is recognisably the same orb.
        self._orb_trails = [dict(t) for t in hud._orb_trails]
        self._orb_dots   = [dict(d) for d in hud._orb_dots]

        # ── hover / drag state ──────────────────────────────────────────────
        self._bar          = _MiniBar(win)
        self._hover        = False
        self._hide_at      = None        # monotonic deadline for hiding the bar
        self._drag_off     = None
        self._saved_pos    = None        # remembered between minimise / restore

        self._tmr = QTimer(self)
        self._tmr.setInterval(16)
        self._tmr.timeout.connect(self._step)

    # ── show / hide (driven by MainWindow.changeEvent) ──────────────────────
    def show_mini(self) -> None:
        if self.isVisible():
            return
        self.move(self._start_pos())
        self._hide_at = None
        self._bar.hide()
        self.show()
        self.raise_()

    def hide_mini(self) -> None:
        self._drag_off = None
        self._hide_at = None
        self._bar.hide()
        self.hide()

    def showEvent(self, e):
        self._tmr.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self._tmr.stop()
        super().hideEvent(e)

    # ── placement ───────────────────────────────────────────────────────────
    def _screen_rect(self, pt):
        scr = QApplication.screenAt(pt) or QApplication.primaryScreen()
        return scr.availableGeometry()

    def _clamp(self, pt, ref=None):
        ag = self._screen_rect(ref if ref is not None else pt)
        x = max(ag.left(), min(pt.x(), ag.right()  + 1 - self.SIZE))
        y = max(ag.top(),  min(pt.y(), ag.bottom() + 1 - self.SIZE))
        return type(pt)(x, y)

    def _start_pos(self):
        """Where the orb appears: last spot, else the bottom-right corner."""
        if self._saved_pos is not None:
            return self._clamp(self._saved_pos)
        ag = QApplication.primaryScreen().availableGeometry()
        return QPoint(
            ag.right()  + 1 - self.SIZE - self._EDGE,
            ag.bottom() + 1 - self.SIZE - self._EDGE - _MiniBar._H - self._BAR_GAP,
        )

    def _place_bar(self) -> None:
        """Centre the buttons just below the orb (flip above only if the
        screen has no room underneath)."""
        g, b = self.geometry(), self._bar
        ag = self._screen_rect(g.center())
        x = g.x() + (g.width() - b.width()) // 2
        y = g.y() + g.height() + self._BAR_GAP
        if y + b.height() > ag.bottom() + 1:
            y = g.y() - b.height() - self._BAR_GAP
        x = max(ag.left(), min(x, ag.right() + 1 - b.width()))
        b.move(x, y)

    def moveEvent(self, e):
        super().moveEvent(e)
        if self._bar.isVisible():
            self._place_bar()

    # ── per-frame update (mirrors HudCanvas._step) ──────────────────────────
    def _step(self):
        hud = self._win.hud
        self._tick += 1
        now = time.time()
        self.speaking  = bool(getattr(hud, "speaking", False))
        self.muted     = bool(getattr(hud, "muted", False))
        self._amp_disp = float(getattr(hud, "_amp_disp", 0.0))

        if now - self._last_t > (0.12 if self.speaking else 0.5):
            if self.speaking:
                self._tgt_scale = random.uniform(1.06, 1.14)
                self._tgt_halo  = random.uniform(145, 190)
            elif self.muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo  = random.uniform(15, 28)
            else:
                self._tgt_scale = random.uniform(1.001, 1.008)
                self._tgt_halo  = random.uniform(48, 68)
            self._last_t = now

        sp = 0.38 if self.speaking else 0.15
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        if self.speaking:
            orb_spd = 3.4
        elif self.muted:
            orb_spd = 0.10
        else:
            orb_spd = 0.32
        orb_spd += self._amp_disp * 2.4
        self._orb_spin = (self._orb_spin + orb_spd) % 360

        speeds = [1.3, -0.9, 2.0] if self.speaking else [0.55, -0.35, 0.9]
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360

        self._bar.sync_muted(bool(getattr(self._win, "_muted", False)))
        self._poll_hover()
        self.update()

    # ── hover logic ─────────────────────────────────────────────────────────
    def _over_orb(self, pos) -> bool:
        g = self.geometry()
        dx = pos.x() - (g.x() + self.SIZE / 2.0)
        dy = pos.y() - (g.y() + self.SIZE / 2.0)
        return dx * dx + dy * dy <= (self.SIZE / 2.0) ** 2

    def _poll_hover(self) -> None:
        pos = QCursor.pos()
        over_orb = self._drag_off is not None or self._over_orb(pos)
        over_bar = self._bar.isVisible() and self._bar.geometry().contains(pos)
        self._hover = over_orb

        if over_orb or over_bar:
            self._hide_at = None
            if not self._bar.isVisible():
                self._place_bar()
                self._bar.show()
                self._bar.raise_()
        elif self._bar.isVisible():
            t = time.monotonic()
            if self._hide_at is None:
                self._hide_at = t + self.HIDE_DELAY_MS / 1000.0
            elif t >= self._hide_at:
                self._bar.hide()
                self._hide_at = None

    # ── drag / double-click ─────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            gp = e.globalPosition().toPoint()
            self._drag_off = gp - self.frameGeometry().topLeft()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_off is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            gp = e.globalPosition().toPoint()
            self.move(self._clamp(gp - self._drag_off, ref=gp))
            e.accept()
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag_off is not None:
            self._drag_off = None
            self._saved_pos = self.pos()
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            w = self._win
            w.setWindowState((w.windowState() & ~Qt.WindowState.WindowMinimized)
                             | Qt.WindowState.WindowActive)
            w.show()
            w.raise_()
            w.activateWindow()
            e.accept()
        else:
            super().mouseDoubleClickEvent(e)

    # ── painting ────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        S = float(self.SIZE)
        cx = cy = S / 2.0
        col = C.MUTED_C if self.muted else C.PRI

        # round dark disc (the HUD's black background, cut to a circle)
        disc = QRectF(2, 2, S - 4, S - 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(qcol(C.BG)))
        p.drawEllipse(disc)

        # the HUD's central ball, clipped to the disc
        clip = QPainterPath()
        clip.addEllipse(disc)
        p.setClipPath(clip)
        HudCanvas._draw_orb(self, p, cx, cy, S * 1.05)
        p.setClipping(False)

        # outer spinning arc ring — same colour / alpha logic as the HUD's
        ring_r = S * 0.455
        a_val  = max(0, min(255, int(self._halo)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcol(col, a_val), 2.2))
        rect = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
        angle = self._rings[0]
        while angle < self._rings[0] + 360:
            p.drawArc(rect, int(angle * 16), int(115 * 16))
            angle += 115 + 78

        # thin rim; a touch brighter while hovered
        p.setPen(QPen(qcol(col, 170 if self._hover else 90), 1.4))
        p.drawEllipse(disc)
        p.end()


class MainWindow(QMainWindow):
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _reconfig_sig   = pyqtSignal()           # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)      # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)       # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)      # live camera frame → HUD area
    _video_open_sig  = pyqtSignal(str, str, bool, str)  # video, title, muted, audio
    _video_close_sig = pyqtSignal()
    _video_mute_sig  = pyqtSignal(bool)
    _clipboard_sig  = pyqtSignal(str)        # clipboard text changed (thread-safe)
    _confirm_sig    = pyqtSignal(str, str)   # (title, detail) — irreversible-action gate
    _confirm_hide_sig = pyqtSignal()
    _quiz_sig       = pyqtSignal(str, object, object)  # (topic, questions, grader)
    _quiz_hide_sig  = pyqtSignal()
    _review_sig     = pyqtSignal(str, str, object, object)  # document review payload
    _room_frame_sig = pyqtSignal(bytes)      # always-on room-watch preview frame
    _room_lost_sig  = pyqtSignal()           # room-watch camera missing / unplugged
    _earth_command_sig = pyqtSignal(str, str, str)
    _face_cam_sig   = pyqtSignal(bool)       # first camera check: suitable camera present?
    _face_ready_sig = pyqtSignal(bool)       # face engine loaded (True) / unavailable (False)
    _face_evt_sig   = pyqtSignal(str, int, str)  # (kind, n, message) from the face worker

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        # Load customization from config
        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "AURA").strip()
        _display = self._assistant_name.upper()

        # Kayıtlı UI rengini panel/stylesheet'ler kurulmadan ÖNCE uygula
        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display}")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_interrupt      = None   # callable: () -> None — stop AURA mid-speech
        self.on_voice_change   = None   # callable: () -> None — rebuild session with new voice
        self.on_audio_device_change = None  # callable: () -> None — reopen audio streams
        self.on_brain_change   = None   # callable: () -> None — apply brain/provider settings
        self._confirm_overlay  = None   # live ConfirmBanner, if one is on screen
        self.get_plugins       = None   # callable: () -> list[dict], set by AuraLive
        self.get_plugin_settings = None # callable: () -> list[dict] settings schemas, set by AuraLive
        self.on_push_to_talk   = None   # callable: (enable: bool) -> str scope
        self.ptt_hold          = None   # callable: (held: bool) -> None — windowed chord
        self._muted            = False
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(face_path, _display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()
        self._quiz_panel = self._build_quiz_panel()

        # Live camera container — replaces HUD when camera stream is active
        _cam_cont = QWidget()
        _cam_cont.setStyleSheet("background: #050200;")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        _cam_title = QLabel("◈  CAMERA FEED")
        _cam_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(_cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self.stop_camera_stream)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        # Video surface — stacked beside the camera page: something takes the
        # centre of the HUD for a while and then gives it back.
        self._video_split = False        # is the sound a separate stream?
        self._video_auto_muted = False   # did the app close the mic, or the user?
        self._video_on = False           # plain flag: safe to read from plugin threads
        self._video_cont = self._build_video_surface()

        # Stack: 0 = animated HUD, 1 = live camera, 2 = video
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)
        self._hud_cam_stack.addWidget(self._video_cont)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._quiz_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)
        body.addWidget(self._center_split, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)

        # Quick-access drawer (floating overlay, built after central widget layout is done)
        self._quick_drawer = self._build_quick_drawer()
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metrik güncelleme timer'ı
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._confirm_sig.connect(self._show_confirm_banner)
        self._confirm_hide_sig.connect(self._hide_confirm_banner)
        self._video_open_sig.connect(self._on_video_open)
        self._video_close_sig.connect(self._on_video_close)
        self._video_mute_sig.connect(self._on_video_mute)
        self._quiz_sig.connect(self._show_quiz)
        self._quiz_hide_sig.connect(self._hide_quiz)
        self._review_sig.connect(self._show_review)
        self._cam_stop = threading.Event()

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())

        # Always-on room watch: floating live preview under the header buttons.
        # Stays hidden unless a camera actually delivers frames.
        self._room_enabled = True            # False after the user closes the box
        self._room_live    = False           # True while the camera is delivering frames
        self._cam_relay    = False           # True while the HUD live view reuses room-watch frames
        self._room_lock    = threading.Lock()
        self._room_quit    = threading.Event()
        self._room_thread  = None
        self._earth_command_sig.connect(self._handle_earth_command)
        self._room_box = _RoomWatchBox(self.centralWidget())
        self._room_box.on_close = self._room_close
        self._room_box.on_move = self._position_earth_box
        if EarthWidget is not None:
            self._earth_box = EarthWidget(self.centralWidget())
            self._earth_box.on_close = self._earth_close
            self._earth_box.show()
            self._position_earth_box()
        else:
            self._earth_box = None
        self._room_frame_sig.connect(self._on_room_frame)
        self._room_lost_sig.connect(self._on_room_lost)
        sc_room = QShortcut(QKeySequence("F8"), self)
        sc_room.activated.connect(self._toggle_room_watch)
        if EarthWidget is not None:
            sc_earth = QShortcut(QKeySequence("F10"), self)
            sc_earth.activated.connect(self._toggle_earth_widget)

        # Face unlock state. The gate holds the assistant back until the lock
        # (if one is active on this setup) has let the user in.
        self._face_gate         = threading.Event()
        self._face_mode         = "pending"   # pending|prep|lock|enroll|reenroll|unlocking|unlocked|off
        self._face_target       = "auto"
        self._face_cam_reported = False
        self._face_engine       = _FaceEngine()
        self._face_cfg          = _face_cfg_load()
        self._face_stored       = None
        self._face_samples      = []
        self._face_new          = []
        self._face_streak       = 0
        self._face_last         = 0.0
        self._face_last_sample  = 0.0
        self._face_prev_enabled = {}
        try:
            self._face_init()
        except Exception as e:
            print(f"[FaceUnlock] Init error: {e}")
            self._face_mode = "off"
            self._face_gate.set()
        self._room_start()

        # Clipboard panel (child of central widget, bottom-center)
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

        # Weather pop-up: draggable + closable, spawns top-left. F9 toggles it.
        self._weather_widget = WeatherWidget(self.centralWidget())
        self._weather_widget.open_widget()
        sc_weather = QShortcut(QKeySequence("F9"), self)
        sc_weather.activated.connect(self._toggle_weather_widget)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)

        # Floating always-on-top orb: appears only while this window is minimised.
        self._mini_orb = MiniOrbWidget(self)

    def _show_camera_frame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._hud_cam_stack.setCurrentIndex(1)
        else:
            self._cam_live_lbl.clear()
            if not self._video_on:      # a playing video keeps the centre
                self._hud_cam_stack.setCurrentIndex(0)

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        if self._room_enabled and self._room_live:
            # The room watch already owns the camera — reuse its frames
            # instead of opening the device a second time.
            self._cam_relay = True
            self._room_start()
            return
        self._cam_relay = False
        t = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        t.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in api_keys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            # warm-up frames
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()
        if self._cam_relay:
            self._cam_relay = False
            self._cam_stream_sig.emit(False)

    # --- Always-on room watch ----------------------------------------------
    def _room_start(self) -> None:
        """Start the room-watch thread (no-op if running or closed by the user)."""
        with self._room_lock:
            if not self._room_enabled:
                return
            t = self._room_thread
            if t is not None and t.is_alive() and not self._room_quit.is_set():
                return
            old = t
        if old is not None and old.is_alive():
            old.join(1.5)            # let it release the camera first
        with self._room_lock:
            cur = self._room_thread
            if cur is not None and cur is not old and cur.is_alive():
                return
            if not self._room_enabled:
                return
            ev = threading.Event()
            self._room_quit = ev
            th = threading.Thread(target=self._room_loop, args=(ev,),
                                  daemon=True, name="room-watch")
            self._room_thread = th
            th.start()

    def _room_halt(self, join: float = 0.0) -> None:
        with self._room_lock:
            ev, t = self._room_quit, self._room_thread
            ev.set()
        if join and t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(join)

    def pause_room_watch(self) -> None:
        """Thread-safe: briefly release the camera (another capture needs it)."""
        self._room_halt(3.0)

    def resume_room_watch(self) -> None:
        """Thread-safe: take the camera back after pause_room_watch()."""
        if self._room_enabled:
            time.sleep(0.3)
            self._room_start()

    def _room_open(self, cv2):
        """Open the setup camera (configured index first, then 0). None if absent."""
        idxs = []
        try:
            idxs.append(int(_read_full_config().get("camera_index", 0)))
        except Exception:
            pass
        if 0 not in idxs:
            idxs.append(0)
        try:
            backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
        except AttributeError:
            backend = 0
        for i in idxs:
            cap = None
            try:
                cap = cv2.VideoCapture(i, backend)
                if cap is not None and cap.isOpened():
                    ok, fr = cap.read()
                    if ok and fr is not None:
                        return cap
                if cap is not None:
                    cap.release()
            except Exception:
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass
        return None

    def _room_loop(self, quit_ev: threading.Event) -> None:
        try:
            import cv2
        except Exception as e:
            print(f"[RoomWatch] OpenCV unavailable: {e}")
            if not self._face_cam_reported:
                self._face_cam_reported = True
                self._face_cam_sig.emit(False)
            return
        cap = None
        fails = 0
        try:
            while not quit_ev.is_set():
                if cap is None:
                    cap = self._room_open(cv2)
                    if cap is None:
                        if not self._face_cam_reported:
                            self._face_cam_reported = True
                            self._face_cam_sig.emit(False)      # no camera → no face unlock
                        if self._room_live:
                            self._room_live = False
                            self._room_lost_sig.emit()
                        quit_ev.wait(10.0)       # no camera — retry later (hot-plug)
                        continue
                    self._room_live = True
                    fails = 0
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails >= 30:              # camera vanished
                        try:
                            cap.release()
                        except Exception:
                            pass
                        cap = None
                        self._room_live = False
                        self._room_lost_sig.emit()
                    quit_ev.wait(0.05)
                    continue
                fails = 0
                if not self._face_cam_reported:
                    self._face_cam_reported = True
                    try:
                        _fh, _fw = frame.shape[:2]
                        self._face_cam_sig.emit(bool(_fw >= 320 and _fh >= 240))
                    except Exception:
                        self._face_cam_sig.emit(False)
                try:
                    h, w = frame.shape[:2]
                    small = frame
                    if w > 320 and h > 0:
                        small = cv2.resize(frame, (320, max(1, int(h * 320 / w))))
                    okj, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
                    if okj:
                        self._room_frame_sig.emit(buf.tobytes())
                    if self._cam_relay and not self._cam_stop.is_set():
                        okf, fbuf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                        if okf:
                            self._cam_frame_sig.emit(fbuf.tobytes())
                    self._face_on_frame(frame)
                except Exception as e:
                    print(f"[RoomWatch] Frame error: {e}")
                quit_ev.wait(0.066)              # ~15 fps
        except Exception as e:
            print(f"[RoomWatch] Loop error: {e}")
        finally:
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass

    def _on_room_frame(self, data: bytes) -> None:
        if self._face_mode in _FACE_COVER:
            # The lock screen is up: feed its preview, keep the floating box away.
            self._face_overlay.set_frame(data)
            return
        if not self._room_enabled:
            return
        changed = self._room_box.set_frame(data)
        if not self._room_box.isVisible():
            self._position_room_box()
            self._room_box.show()
            self._room_box.raise_()
            if self._earth_box is not None and self._earth_box.isVisible():
                self._position_earth_box()
        elif changed:
            self._position_room_box()
            if self._earth_box is not None and self._earth_box.isVisible():
                self._position_earth_box()

    def _on_room_lost(self) -> None:
        self._room_box.hide()
        if self._earth_box is not None and self._earth_box.isVisible():
            self._position_earth_box()
        if self._face_mode in ("prep", "lock", "enroll", "reenroll"):
            self._face_fail_open("camera lost")

    def _room_close(self) -> None:
        """✕ pressed: hide the box and let go of the camera."""
        self._room_enabled = False
        self._room_live = False
        self._room_halt(0.0)
        self._room_box.hide()
        if self._earth_box is not None and self._earth_box.isVisible():
            self._position_earth_box()
        if self._cam_relay:
            self.stop_camera_stream()

    def _toggle_room_watch(self) -> None:
        if self._face_mode in _FACE_COVER:
            return                      # the lock screen needs the camera
        if self._room_enabled:
            self._room_close()
        else:
            self._room_enabled = True
            self._room_start()

    # --- Face unlock ---------------------------------------------------------
    def _face_init(self) -> None:
        self._face_overlay = FaceLockOverlay(self.centralWidget())
        self._face_overlay.action_clicked.connect(self._face_action)
        self._face_cam_sig.connect(self._on_face_cam)
        self._face_ready_sig.connect(self._on_face_ready)
        self._face_evt_sig.connect(self._on_face_evt)
        self._face_stored = _face_stored_from(self._face_cfg)
        self._update_face_btn()
        if not self._face_cfg.get("enabled", True):
            self._face_mode = "off"
            self._face_gate.set()
            return
        # Safety net: if the camera never answers, don't hold the assistant back.
        QTimer.singleShot(12000, self._face_timeout)

    def _face_timeout(self) -> None:
        if self._face_mode == "pending":
            self._face_mode = "off"
            self._face_gate.set()

    def _face_lock_widgets(self) -> list:
        return [w for w in (getattr(self, "_right_panel", None),
                            getattr(self, "_mute_btn", None),
                            getattr(self, "_interrupt_btn", None),
                            getattr(self, "_drawer_btn", None)) if w is not None]

    def _face_cover(self, on: bool) -> None:
        ov = self._face_overlay
        if on:
            try:
                self._close_drawer()
            except Exception:
                pass
            if not self._face_prev_enabled:
                for w in self._face_lock_widgets():
                    self._face_prev_enabled[w] = w.isEnabled()
                    w.setEnabled(False)
            self._room_box.hide()
            ov.setGeometry(self.centralWidget().rect())
            ov.show()
            ov.raise_()
            ov.setFocus()
        else:
            ov.hide()
            for w, was in self._face_prev_enabled.items():
                w.setEnabled(was)
            self._face_prev_enabled = {}

    def _on_face_cam(self, ok: bool) -> None:
        """First camera check result (main thread)."""
        if ok:
            self._face_btn.show()
            self._face_re_btn.show()
        if self._face_mode != "pending":
            return
        if not ok or not self._face_cfg.get("enabled", True):
            self._face_mode = "off"
            self._face_gate.set()
            return
        self._face_begin("auto")

    def _face_begin(self, target: str) -> None:
        self._face_target = target
        self._face_mode = "prep"
        self._face_overlay.set_mode("prep", True)
        self._face_cover(True)
        if self._face_engine.ready:
            self._on_face_ready(True)
        else:
            threading.Thread(target=self._face_prep_worker, daemon=True,
                             name="face-init").start()

    def _face_prep_worker(self) -> None:
        ok = False
        try:
            ok = self._face_engine.load(lambda m: self._face_evt_sig.emit("info", 0, m))
        except Exception as e:
            print(f"[FaceUnlock] Prep error: {e}")
        self._face_ready_sig.emit(bool(ok))

    def _on_face_ready(self, ok: bool) -> None:
        if self._face_mode != "prep":
            return                      # user skipped while it was loading
        if not ok:
            self._face_fail_open("face recognition is unavailable on this setup")
            return
        if self._face_target == "reenroll":
            self._face_enter("reenroll")
        elif self._face_stored is not None and len(self._face_stored) > 0:
            self._face_enter("lock")
        else:
            self._face_enter("enroll")

    def _face_enter(self, mode: str) -> None:
        self._face_samples = []
        self._face_new = []
        self._face_streak = 0
        self._face_last = 0.0
        self._face_last_sample = 0.0
        self._face_overlay.set_mode(mode, bool(self._face_cfg.get("allow_skip", True)))
        self._face_mode = mode          # set last: the worker starts on this

    def _face_fail_open(self, reason: str) -> None:
        self._face_mode = "off"
        self._face_cover(False)
        self._face_gate.set()
        self._log.append_log(f"SYS: Face unlock skipped — {reason}.")

    def _face_unlock_ui(self, text: str) -> None:
        self._face_mode = "unlocking"
        self._face_overlay.set_status(text, C.GREEN)
        QTimer.singleShot(700, self._face_finish_unlock)

    def _face_finish_unlock(self) -> None:
        self._face_mode = "unlocked"
        self._face_cover(False)
        self._face_gate.set()

    def _face_action(self) -> None:
        """The overlay's button (meaning depends on the current mode)."""
        mode = self._face_mode
        if mode == "prep":
            self._face_fail_open("skipped while preparing")
        elif mode == "lock":
            self._log.append_log("SYS: Face unlock bypassed for this launch.")
            self._face_finish_unlock()
        elif mode == "enroll":
            self._face_cfg["enabled"] = False
            _face_cfg_save(self._face_cfg)
            self._update_face_btn()
            self._log.append_log("SYS: Face unlock turned off (re-enable it from the settings menu).")
            self._face_finish_unlock()
        elif mode == "reenroll":
            self._face_finish_unlock()

    def _on_face_evt(self, kind: str, n: int, msg: str) -> None:
        try:
            if kind in ("info", "warn"):
                if self._face_mode in _FACE_COVER and self._face_mode != "unlocking":
                    self._face_overlay.set_status(msg, C.RED if kind == "warn" else C.TEXT_MED)
            elif kind == "progress":
                if self._face_mode in ("enroll", "reenroll"):
                    self._face_overlay.set_progress(n, _FACE_ENROLL_N)
                    if msg:
                        self._face_overlay.set_status(msg, C.PRI)
            elif kind == "unlock":
                self._log.append_log("SYS: Face recognised — unlocked.")
                self._face_unlock_ui("ACCESS GRANTED")
            elif kind == "enrolled":
                samples = list(self._face_new or [])
                if not samples:
                    self._face_fail_open("no face data was captured")
                    return
                self._face_cfg["enrolled"] = samples
                self._face_cfg["enabled"] = True
                if not _face_cfg_save(self._face_cfg):
                    self._log.append_log("ERR: Could not save face data — you will be asked to enrol again next launch.")
                self._face_stored = _face_stored_from(self._face_cfg)
                self._update_face_btn()
                self._log.append_log("SYS: Face registered. Face unlock is on.")
                self._face_unlock_ui("FACE REGISTERED")
        except Exception as e:
            print(f"[FaceUnlock] Event error: {e}")

    def _face_on_frame(self, frame) -> None:
        """Runs on the room-watch thread with each full-size camera frame."""
        try:
            mode = self._face_mode
            if mode not in ("lock", "enroll", "reenroll") or not self._face_engine.ready:
                return
            now = time.monotonic()
            if now - self._face_last < 0.25:
                return
            self._face_last = now
            import numpy as np
            n, feat, fw = self._face_engine.analyse(frame)
            emit = self._face_evt_sig.emit
            if n == 0 or feat is None:
                self._face_streak = 0
                emit("info", 0, "NO FACE DETECTED — LOOK AT THE CAMERA")
                return
            if mode == "lock":
                stored = self._face_stored
                if stored is None or len(stored) == 0:
                    return
                score = float(np.max(stored @ feat))
                if score >= _FACE_THRESH:
                    self._face_streak += 1
                    if self._face_streak >= _FACE_NEED:
                        self._face_mode = "unlocking"
                        emit("unlock", 0, "")
                    else:
                        emit("info", 0, "VERIFYING…")
                else:
                    self._face_streak = 0
                    emit("warn", 0, "FACE NOT RECOGNISED")
                return
            # ── enrolment ──
            if n > 1:
                emit("warn", 0, "ONLY ONE PERSON IN FRAME, PLEASE")
                return
            if fw < 90:
                emit("warn", 0, "MOVE A LITTLE CLOSER TO THE CAMERA")
                return
            if now - self._face_last_sample < 0.6:
                return
            self._face_last_sample = now
            self._face_samples.append(feat)
            k = len(self._face_samples)
            emit("progress", k, f"CAPTURING… {k}/{_FACE_ENROLL_N}")
            if k >= _FACE_ENROLL_N:
                arr = np.stack(self._face_samples)
                sims = arr @ arr.T
                mean_sim = float((sims.sum() - k) / (k * (k - 1)))
                if mean_sim < 0.45:
                    self._face_samples = []
                    emit("progress", 0, "")
                    emit("warn", 0, "COULD NOT GET A STABLE READING — HOLD STILL AND TRY AGAIN")
                else:
                    self._face_new = [[round(float(x), 6) for x in s] for s in self._face_samples]
                    self._face_mode = "unlocking"
                    emit("enrolled", k, "")
        except Exception as e:
            print(f"[FaceUnlock] Frame error: {e}")

    def _update_face_btn(self) -> None:
        if not hasattr(self, "_face_btn"):
            return
        if self._face_cfg.get("enabled", True):
            self._face_btn.setText("◉  FACE UNLOCK: ON")
            self._face_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._face_btn.setText("◉  FACE UNLOCK: OFF")
            self._face_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_face_unlock(self) -> None:
        if self._face_mode not in ("unlocked", "off"):
            return
        if self._face_cfg.get("enabled", True):
            self._face_cfg["enabled"] = False
            _face_cfg_save(self._face_cfg)
            self._log.append_log("SYS: Face unlock OFF.")
        else:
            self._face_cfg["enabled"] = True
            _face_cfg_save(self._face_cfg)
            if self._face_stored is None or len(self._face_stored) == 0:
                self._update_face_btn()
                self._reenroll_face()
                return
            self._log.append_log("SYS: Face unlock ON — it will lock on the next launch.")
        self._update_face_btn()

    def _reenroll_face(self) -> None:
        if self._face_mode not in ("unlocked", "off"):
            return
        if not (self._room_enabled and self._room_live):
            self._log.append_log("SYS: Face enrolment needs the camera — no live camera "
                                 "(press F8 to turn the room watch back on).")
            return
        self._face_begin("reenroll")

    def _toggle_weather_widget(self) -> None:
        """F9 — show / hide the weather pop-up."""
        w = getattr(self, "_weather_widget", None)
        if w is None or self._face_mode in _FACE_COVER:
            return                      # nothing to toggle / lock screen is up
        if w.isVisible():
            w.hide()
        else:
            w.open_widget()

    def _position_room_box(self) -> None:
        b = getattr(self, "_room_box", None)
        cw = self.centralWidget()
        if b is None or cw is None:
            return
        if b.user_moved:
            b.move_clamped(b.pos())
        else:
            # Top-right corner, just below the mic / stop / settings buttons
            # (the header is 54 px tall).
            b.move(max(0, cw.width() - b.width() - 16), 54 + 10)

    def _earth_close(self) -> None:
        b = getattr(self, "_earth_box", None)
        if b is not None:
            b.hide()

    def _toggle_earth_widget(self) -> None:
        b = getattr(self, "_earth_box", None)
        if b is None:
            return
        if b.isVisible():
            b.hide()
        else:
            b.show()
            if b._maximized:
                b.sync_parent_geometry()
            else:
                self._position_earth_box()
            b.raise_()

    def request_earth_command(self, command: str, origin: str = "",
                              destination: str = "") -> None:
        self._earth_command_sig.emit(command, origin, destination)

    def _handle_earth_command(self, command: str, origin: str,
                              destination: str) -> None:
        b = getattr(self, "_earth_box", None)
        if b is None:
            self.write_log("SYS: Earth widget is unavailable.")
            return
        b.handle_command(command, origin, destination)
        if command != "hide" and not b._maximized:
            self._position_earth_box()
        b.raise_()

    def _position_earth_box(self) -> None:
        b = getattr(self, "_earth_box", None)
        cw = self.centralWidget()
        if b is None or cw is None:
            return
        if b._maximized:
            b.sync_parent_geometry()
            return
        if b.user_moved:
            b.move_clamped(b.pos())
        else:
            room = getattr(self, "_room_box", None)
            room_x = cw.width() - b.width() - 16 if room is None or not room.isVisible() else room.x()
            room_y = 54 + 10 if room is None or not room.isVisible() else room.y() + room.height() + 8
            b.move(max(0, room_x), max(0, room_y))

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.Type.WindowStateChange:
            mini = getattr(self, "_mini_orb", None)
            if mini is not None:
                # Windows' own minimise button -> orb appears; restore -> it goes.
                if self.isMinimized():
                    mini.show_mini()
                else:
                    mini.hide_mini()

    def closeEvent(self, e):
        try:
            self._mini_orb.hide_mini()
        except Exception:
            pass
        try:
            self._room_enabled = False
            self._room_halt(1.0)
        except Exception:
            pass
        super().closeEvent(e)

    # --- Video in the HUD area ---------------------------------------------
    #
    # Everything below runs on the Qt thread. Plugins and the assistant reach it
    # through the signals above, the same way every other panel is driven, so a
    # background thread never touches a widget.
    def _build_video_surface(self) -> QWidget:
        cont = QWidget()
        cont.setStyleSheet(f"background: {C.BG};")
        v = QVBoxLayout(cont)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(4)

        hdr = QHBoxLayout()
        self._video_title = QLabel("▶  VIDEO")
        self._video_title.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        self._video_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(self._video_title)
        hdr.addStretch()

        def _vid_btn(text: str) -> QPushButton:
            b = QPushButton(text)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    color: {C.TEXT_DIM}; background: transparent;
                    border: none; padding: 2px 6px;
                }}
                QPushButton:hover {{ color: {C.PRI}; }}
            """)
            return b

        # Muted is the default and the button says so: a soundtrack talking
        # over the assistant is the one way this feature could make it worse.
        self._video_mute_btn = _vid_btn("🔇  SOUND OFF")
        self._video_mute_btn.clicked.connect(self._toggle_video_mute)
        hdr.addWidget(self._video_mute_btn)
        close = _vid_btn("✕  CLOSE")
        close.clicked.connect(self.stop_video)
        hdr.addWidget(close)
        v.addLayout(hdr)

        self._video_scene = self._video_item = self._video_widget = None
        self._video_player = self._video_audio = None
        self._video_sound = self._video_sound_out = self._video_sync = None
        ok = False
        if HAVE_VIDEO:
            try:
                # A GRAPHICS ITEM, NOT A QVideoWidget: QVideoWidget gets a
                # native window that the compositor paints above every ordinary
                # widget (so drawers would open underneath the video). A video
                # item drawn into a QGraphicsView goes through Qt's own painter,
                # and anything laid over it stays over it.
                self._video_scene = QGraphicsScene(self)
                self._video_item = QGraphicsVideoItem()
                self._video_scene.addItem(self._video_item)
                self._video_widget = QGraphicsView(self._video_scene)
                self._video_widget.setStyleSheet("background: #000; border: none;")
                self._video_widget.setFrameShape(QGraphicsView.Shape.NoFrame)
                self._video_widget.setHorizontalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                self._video_widget.setVerticalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                self._video_widget.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
                v.addWidget(self._video_widget, stretch=1)

                self._video_audio = QAudioOutput()
                self._video_audio.setMuted(True)
                self._video_player = QMediaPlayer()
                self._video_player.setVideoOutput(self._video_item)
                self._video_item.nativeSizeChanged.connect(self._fit_video)
                self._video_player.setAudioOutput(self._video_audio)
                self._video_player.errorOccurred.connect(self._on_video_error)

                # A SECOND player for sound that arrives as a separate stream
                # (picture and sound are two URLs on some sites). Started
                # together and nudged back into line by the timer below.
                self._video_sound = QMediaPlayer()
                self._video_sound_out = QAudioOutput()
                self._video_sound_out.setMuted(True)
                self._video_sound.setAudioOutput(self._video_sound_out)

                self._video_sync = QTimer(self)
                self._video_sync.setInterval(1000)
                self._video_sync.timeout.connect(self._sync_video_sound)
                ok = True
            except Exception as e:
                print(f"[Video] setup failed ({e}) — the HUD will not show video.")
                self._video_scene = self._video_item = self._video_widget = None
                self._video_player = self._video_audio = None
                self._video_sound = self._video_sound_out = self._video_sync = None
        if not ok:
            miss = QLabel("Video playback is not available in this Qt install.")
            miss.setAlignment(Qt.AlignmentFlag.AlignCenter)
            miss.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            v.addWidget(miss, stretch=1)
        return cont

    def _on_video_open(self, source: str, title: str, muted: bool,
                       audio_source: str = "") -> None:
        if not HAVE_VIDEO or not self._video_player:
            self._log.append_log("SYS: Video playback is not available in this Qt "
                                 "install.")
            return
        # A video and the live camera cannot share the centre of the HUD.
        self._cam_stop.set()
        self._cam_relay = False

        self._video_title.setText(f"▶  {(title or 'VIDEO')[:44].upper()}")
        self._video_split = bool(audio_source)
        self._set_video_muted(bool(muted))

        try:
            is_file = Path(source).exists()
        except Exception:
            is_file = False
        url = QUrl.fromLocalFile(source) if is_file else QUrl(source)
        self._video_player.setSource(url)
        if self._video_split:
            self._video_sound.setSource(QUrl(audio_source))
        self._hud_cam_stack.setCurrentIndex(2)
        self._video_on = True
        # Re-run now that the video counts as playing: _set_video_muted ran
        # before this line and saw no video, so its mic check was a no-op.
        self._sync_mic_for_video()
        self._video_player.play()
        if self._video_split:
            self._video_sound.play()
            self._video_sync.start()

    def _fit_video(self, *_a) -> None:
        """Size the picture to the panel, keeping its shape."""
        if not (self._video_item and self._video_widget):
            return
        try:
            native = self._video_item.nativeSize()
            if native.isEmpty():
                return
            view = self._video_widget.viewport().size()
            scale = min(view.width() / native.width(),
                        view.height() / native.height())
            w, h = native.width() * scale, native.height() * scale
            self._video_item.setSize(QSizeF(w, h))
            self._video_scene.setSceneRect(0, 0, w, h)
            self._video_widget.centerOn(self._video_item)
        except Exception:
            pass

    def _on_video_close(self) -> None:
        if self._video_sync:
            self._video_sync.stop()
        for p in (self._video_player, self._video_sound):
            if p:
                p.stop()
                p.setSource(QUrl())
        self._video_split = False
        was_on = self._video_on
        self._video_on = False
        if was_on or self._hud_cam_stack.currentIndex() == 2:
            self._hud_cam_stack.setCurrentIndex(0)
        self._sync_mic_for_video()      # gives the microphone back

    def _set_video_muted(self, muted: bool) -> None:
        """Silence whichever output is carrying the sound for this video."""
        muted = bool(muted)
        if self._video_audio:
            self._video_audio.setMuted(muted)
        if self._video_sound_out:
            self._video_sound_out.setMuted(muted)
        self._sync_video_mute_btn()
        self._sync_mic_for_video()

    def _on_video_mute(self, muted: bool) -> None:
        self._set_video_muted(muted)

    def _sync_mic_for_video(self) -> None:
        """Close the microphone while the video is making sound.

        The assistant subtracts its OWN output from the microphone, but a video
        plays through a different output entirely, so it would answer the film.
        The microphone therefore closes for exactly as long as the sound is on
        and opens again by itself when it goes off or the video is closed.
        Only if the app closed it: a microphone the user muted stays muted, and
        pressing the mute key during a video hands the decision back to them.
        """
        sound_on = bool(self._video_on and self._video_sound_out
                        and not self._video_sound_out.isMuted())
        if sound_on and not self._muted:
            self._video_auto_muted = True
            self._set_muted(True, "The video's sound is on — silence it, close "
                                  "it, or press F4 to talk. Typing still works.")
        elif not sound_on and self._video_auto_muted:
            self._video_auto_muted = False
            self._set_muted(False, "The video is quiet again.")

    def _sync_video_sound(self) -> None:
        """Keep the separate soundtrack in step with the picture (only nudged
        when the drift is audible, since correcting less is itself audible)."""
        if not (self._video_split and self._video_sound and self._video_player):
            return
        try:
            if self._video_player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                return
            drift = self._video_sound.position() - self._video_player.position()
            if abs(drift) > 300:
                self._video_sound.setPosition(self._video_player.position())
        except Exception:
            pass

    def _sync_video_mute_btn(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        muted = bool(out and out.isMuted())
        self._video_mute_btn.setText("🔇  SOUND OFF" if muted else "🔊  SOUND ON")

    def _toggle_video_mute(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        if out:
            self._set_video_muted(not out.isMuted())

    def _on_video_error(self, *_a) -> None:
        err = ""
        try:
            err = self._video_player.errorString()
        except Exception:
            pass
        self._log.append_log(f"SYS: The video could not be played{(' — ' + err) if err else ''}.")
        self._on_video_close()

    def stop_video(self) -> None:
        self._video_close_sig.emit()

    def video_is_playing(self) -> bool:
        return bool(self._video_on)

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def _build_aura_icon(out_path: Path) -> bool:
        """
        Render an AURA arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def _get_desktop_dir() -> Path:
        """
        Resolve the user's REAL desktop directory instead of assuming
        ~/Desktop, which breaks when:
          • OneDrive "Known Folder Move" relocates the desktop
            (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
          • the XDG desktop is localized on Linux (~/Masaüstü,
            ~/Schreibtisch, ~/Bureau, …).
        Falls back to ~/Desktop only as a last resort.
        """
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            # ── 1) SHGetKnownFolderPath(FOLDERID_Desktop) — the canonical
            #       answer; follows OneDrive redirection. No dependencies. ──
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            # ── 2) Registry: User Shell Folders (may contain %VARS%) ──────
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            # ── xdg-user-dir honours localized names (~/Masaüstü, …) ──────
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        # macOS: ~/Desktop is always the real path (localization is
        # display-only). Everything else lands here as a last resort.
        return home / "Desktop"

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = self._get_desktop_dir()

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = Path(__file__).resolve().parent / "config" / "aura.ico"
        if not ico_path.exists():
            self._build_aura_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "AURA.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "AURA.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "AURA"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>AURA</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.aura.assistant</string>\n'
                    '  <key>CFBundleName</key><string>AURA</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "AURA.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=AURA\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._customize_overlay and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )
        self._fit_video()
        # Face-unlock screen covers the whole window
        if hasattr(self, '_face_overlay') and self._face_overlay.isVisible():
            self._face_overlay.setGeometry(cw.rect())
        # Room-watch box — top-right, below the header buttons
        if hasattr(self, '_room_box') and self._room_box.isVisible():
            self._position_room_box()
        if hasattr(self, '_earth_box') and self._earth_box is not None and self._earth_box.isVisible():
            self._position_earth_box()
        # Clipboard panel — bottom-center
        if hasattr(self, '_clipboard_panel') and self._clipboard_panel.isVisible():
            self._position_clipboard_panel()
        # Weather widget — top-left by default, clamped inside if dragged
        if hasattr(self, '_weather_widget') and self._weather_widget.isVisible():
            self._weather_widget.reposition()
        # Quick drawer — reposition if open
        if hasattr(self, '_quick_drawer') and self._quick_drawer.isVisible():
            self._position_quick_drawer()
        # Content panel — reposition if open
        if hasattr(self, '_content_panel') and self._content_panel.isVisible():
            self._position_content_panel()

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu_str = f"{snap['cpu']:.0f}%"

        # RAM
        ram_str = f"{snap['mem']:.0f}%"

        # GPU
        gpu = snap["gpu"]
        gpu_str = f"{gpu:.0f}%" if gpu >= 0 else "N/A"

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"

        self._stats_box.set_values(cpu_str, ram_str, gpu_str, net_str)


    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(54)
        w.setStyleSheet(f"background: {C.DARK};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(8)

        # ── title with angled HUD rules (left) ──────────────────────────────
        _disp = self._assistant_name.upper()
        title_bar = TitleBar()
        tl = QHBoxLayout(title_bar)
        tl.setContentsMargins(70, 0, 70, 0)
        self._title_lbl = QLabel(_disp)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _tf = QFont("Courier New", 15, QFont.Weight.Bold)
        _tf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        self._title_lbl.setFont(_tf)
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        tl.addWidget(self._title_lbl)
        lay.addWidget(title_bar)

        # Subtitle / clock / date are no longer shown in the header, but the
        # labels are kept (hidden) because other code updates them.
        _sub_text = ("Autonomous User Responsive Agent"
                     if _disp in ("AURA", "A.U.R.A")
                     else "Personal AI Assistant")
        self._sub_lbl = QLabel(_sub_text, w)
        self._sub_lbl.setFont(QFont("Courier New", 7))
        self._sub_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._sub_lbl.hide()
        self._clock_lbl = QLabel("00:00:00", w)
        self._clock_lbl.setFont(QFont("Courier New", 14, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.hide()
        self._date_lbl = QLabel("", w)
        self._date_lbl.setFont(QFont("Courier New", 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.hide()

        lay.addStretch()

        # ── microphone + stop (interrupt) buttons ───────────────────────────
        self._mute_btn = HudButton("", "mic")
        self._mute_btn.setFixedSize(58, 30)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        self._interrupt_btn = HudButton("stop", "text")
        self._interrupt_btn.setFixedSize(76, 30)
        self._interrupt_btn.setToolTip("Interrupt  [ESC]")
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        lay.addWidget(self._interrupt_btn)

        # ── settings button (unchanged, just moved to the far right) ────────
        self._drawer_btn = QPushButton("⚙")
        self._drawer_btn.setFixedSize(26, 26)
        self._drawer_btn.setFont(QFont("Courier New", 11))
        self._drawer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drawer_btn.setToolTip("Settings & Controls")
        self._drawer_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: none; border-radius: 4px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
            QPushButton:checked {{ color: {C.PRI}; background: {C.PRI_GHO}; }}
        """)
        self._drawer_btn.setCheckable(True)
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        lay.addWidget(self._drawer_btn)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.DARK};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(6)

        # Push the stats box to the bottom-left corner.
        lay.addStretch(1)

        self._stats_box = StatsBox()
        lay.addWidget(self._stats_box, 0,
                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)

        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: {C.DARK};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 6)
        lay.setSpacing(2)

        def _sec(txt):
            l = QLabel(f"▸ {txt}")
            l.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            l.setStyleSheet(f"color: {C.TEXT_MED};")
            l.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            return l

        # ACTIVITY LOG box removed from the UI. self._log is still created
        # (but never added to a layout, so it stays invisible) because many
        # call sites elsewhere use self._log.append_log(...) / self._log.* —
        # keeping the instance alive means none of that code needs to change.
        self._log = LogWidget()

        # Push the whole FILE UPLOAD / COMMAND INPUT cluster down to the
        # bottom of the panel, close to the bottom-right corner, instead of
        # letting it spread out to fill the available vertical space.
        lay.addStretch(1)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("")
        self._file_hint.setFont(QFont("Courier New", 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        self._file_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        lay.addWidget(self._file_hint)

        lay.addWidget(_sec(""))
        lay.addLayout(self._build_input_row())

        return w

    def _build_quick_drawer(self) -> QWidget:
        """Floating overlay panel shown when the ⚙ header button is toggled."""
        _BTN_STYLE_PRI = f"""
            QPushButton {{
                background: #1f0e00; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """
        _BTN_STYLE_DIM = f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """

        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(f"""
            QWidget#QuickDrawer {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        # ── Title bar (draggable, with its own close button) ────────────────
        hdr_w = QWidget()
        hdr_w.setStyleSheet(f"border-bottom: 1px solid {C.BORDER};")
        hdr_row = QHBoxLayout(hdr_w)
        hdr_row.setContentsMargins(0, 0, 0, 5)
        hdr_row.setSpacing(4)

        hdr = QLabel("◈ CONTROLS")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; border: none;")
        hdr_row.addWidget(hdr)
        hdr_row.addStretch()

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: none;
            }}
            QPushButton:hover {{ color: {C.MUTED_C}; }}
        """)
        close_btn.clicked.connect(self._close_drawer)
        hdr_row.addWidget(close_btn)
        lay.addWidget(hdr_w)

        # Let the title bar be dragged to reposition the dialog freely.
        def _hdr_mouse_press(ev):
            if ev.button() == Qt.MouseButton.LeftButton:
                w._drag_pos = ev.globalPosition().toPoint() - w.pos()
                ev.accept()

        def _hdr_mouse_move(ev):
            if ev.buttons() & Qt.MouseButton.LeftButton and hasattr(w, "_drag_pos"):
                w.move(ev.globalPosition().toPoint() - w._drag_pos)
                ev.accept()

        hdr_w.mousePressEvent = _hdr_mouse_press
        hdr_w.mouseMoveEvent = _hdr_mouse_move

        remote_btn = QPushButton("◉  REMOTE CONTROL")
        remote_btn.setFixedHeight(30)
        remote_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setStyleSheet(_BTN_STYLE_PRI)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        fs_btn = QPushButton("⛶  FULLSCREEN  [F11]")
        fs_btn.setFixedHeight(26)
        fs_btn.setFont(QFont("Courier New", 7))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet(_BTN_STYLE_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        if EarthWidget is not None:
            earth_btn = QPushButton("◉  GOD'S EYE  [F10]")
            earth_btn.setFixedHeight(26)
            earth_btn.setFont(QFont("Courier New", 7))
            earth_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            earth_btn.setStyleSheet(_BTN_STYLE_DIM)
            earth_btn.clicked.connect(self._toggle_earth_widget)
            lay.addWidget(earth_btn)

        sc_btn = QPushButton("⊞  CREATE DESKTOP SHORTCUT")
        sc_btn.setFixedHeight(26)
        sc_btn.setFont(QFont("Courier New", 7))
        sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sc_btn.setStyleSheet(_BTN_STYLE_DIM)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)

        self._autostart_btn = QPushButton("◉  AUTO-START: OFF")
        self._autostart_btn.setFixedHeight(26)
        self._autostart_btn.setFont(QFont("Courier New", 7))
        self._autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autostart_btn.clicked.connect(self._toggle_autostart)
        lay.addWidget(self._autostart_btn)

        cust_btn = QPushButton("⚙  CUSTOMISE ASSISTANT")
        cust_btn.setFixedHeight(26)
        cust_btn.setFont(QFont("Courier New", 7))
        cust_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cust_btn.setStyleSheet(_BTN_STYLE_DIM)
        cust_btn.clicked.connect(self._open_customize)
        lay.addWidget(cust_btn)

        self._brief_btn = QPushButton()
        self._brief_btn.setFixedHeight(26)
        self._brief_btn.setFont(QFont("Courier New", 7))
        self._brief_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._brief_btn.clicked.connect(self._toggle_brief)
        lay.addWidget(self._brief_btn)

        # Face unlock buttons — hidden until a suitable camera is detected.
        self._face_btn = QPushButton()
        self._face_btn.setFixedHeight(26)
        self._face_btn.setFont(QFont("Courier New", 7))
        self._face_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._face_btn.clicked.connect(self._toggle_face_unlock)
        self._face_btn.hide()
        lay.addWidget(self._face_btn)

        self._face_re_btn = QPushButton("⟳  RE-ENROLL MY FACE")
        self._face_re_btn.setFixedHeight(26)
        self._face_re_btn.setFont(QFont("Courier New", 7))
        self._face_re_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._face_re_btn.setStyleSheet(_BTN_STYLE_DIM)
        self._face_re_btn.clicked.connect(self._reenroll_face)
        self._face_re_btn.hide()
        lay.addWidget(self._face_re_btn)

        self._ptt_btn = QPushButton()
        self._ptt_btn.setFixedHeight(26)
        self._ptt_btn.setFont(QFont("Courier New", 7))
        self._ptt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ptt_btn.clicked.connect(self._toggle_ptt)
        lay.addWidget(self._ptt_btn)

        self._refresh_talk_btns()

        audio_btn = QPushButton("🎧  AUDIO DEVICES")
        audio_btn.setFixedHeight(26)
        audio_btn.setFont(QFont("Courier New", 7))
        audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        audio_btn.setStyleSheet(_BTN_STYLE_DIM)
        audio_btn.clicked.connect(self._open_audio_devices)
        lay.addWidget(audio_btn)

        mem_btn = QPushButton("🧠  MEMORY")
        mem_btn.setFixedHeight(26)
        mem_btn.setFont(QFont("Courier New", 7))
        mem_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        mem_btn.setStyleSheet(_BTN_STYLE_DIM)
        mem_btn.clicked.connect(self._open_memory_panel)
        lay.addWidget(mem_btn)

        plugin_btn = QPushButton("🧩  PLUGINS")
        plugin_btn.setFixedHeight(26)
        plugin_btn.setFont(QFont("Courier New", 7))
        plugin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        plugin_btn.setStyleSheet(_BTN_STYLE_DIM)
        plugin_btn.clicked.connect(self._open_plugin_manager)
        lay.addWidget(plugin_btn)

        settings_btn = QPushButton("⚙  PLUGIN SETTINGS")
        settings_btn.setFixedHeight(26)
        settings_btn.setFont(QFont("Courier New", 7))
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setStyleSheet(_BTN_STYLE_DIM)
        settings_btn.clicked.connect(self._open_plugin_settings)
        lay.addWidget(settings_btn)

        w.adjustSize()
        return w

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._position_quick_drawer()
            self._quick_drawer.show()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _close_drawer(self):
        """Close the settings dialog via its own ✕ button."""
        self._quick_drawer.hide()
        self._drawer_btn.setChecked(False)

    def _position_quick_drawer(self):
        if not hasattr(self, '_quick_drawer'):
            return
        cw = self.centralWidget()
        _W = 220
        self._quick_drawer.setFixedWidth(_W)
        self._quick_drawer.adjustSize()
        h = self._quick_drawer.sizeHint().height()
        x = (cw.width() - _W) // 2
        y = (cw.height() - h) // 2
        self._quick_drawer.setGeometry(x, y, _W, h)

    def _position_content_panel(self):
        """Floating overlay, like the SETTINGS drawer: anchored near the
        bottom of the HUD/center area rather than filling the window."""
        if not hasattr(self, '_content_panel'):
            return
        cs = self._center_split
        origin = cs.mapTo(self.centralWidget(), cs.rect().topLeft())
        pw = min(max(cs.width() - 48, 280), 520)
        ph = min(max(cs.height() - 60, 160), 280)
        x = origin.x() + (cs.width() - pw) // 2
        y = origin.y() + cs.height() - ph - 18
        self._content_panel.setGeometry(x, y, pw, ph)

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(5)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont("Courier New", 9))
        self._input.setFixedHeight(30)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {C.PANEL2}; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 7px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(30, 30)
        send.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        """
        Floating overlay panel — shows search results, news, briefings and
        document reviews. Styled and behaves like the SETTINGS drawer: a
        rounded floating box with a draggable header and a small ✕ button
        in the top-right corner, positioned over the bottom of the HUD area.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget(self.centralWidget())
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 8, 10, 10)
        lay.setSpacing(5)

        # ── header row (draggable, with its own close button) ───────────────
        hdr_w = QWidget()
        hdr_w.setStyleSheet(f"border-bottom: 1px solid {C.BORDER};")
        hdr = QHBoxLayout(hdr_w)
        hdr.setContentsMargins(0, 0, 0, 5)
        hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px; border: none;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Courier New", 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("✕")
        dismiss.setFixedSize(16, 16)
        dismiss.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: none;
            }}
            QPushButton:hover {{ color: {C.MUTED_C}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addWidget(hdr_w)

        # Let the header be dragged to reposition the panel freely, just
        # like the SETTINGS drawer's title bar.
        def _hdr_mouse_press(ev):
            if ev.button() == Qt.MouseButton.LeftButton:
                w._drag_pos = ev.globalPosition().toPoint() - w.pos()
                ev.accept()

        def _hdr_mouse_move(ev):
            if ev.buttons() & Qt.MouseButton.LeftButton and hasattr(w, "_drag_pos"):
                w.move(ev.globalPosition().toPoint() - w._drag_pos)
                ev.accept()

        hdr_w.mousePressEvent = _hdr_mouse_press
        hdr_w.mouseMoveEvent = _hdr_mouse_move

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont("Courier New", 8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        self._position_content_panel()
        self._content_panel.show()
        self._content_panel.raise_()

    # ── document review ──────────────────────────────────────────────────────
    # Rendered as rich text into the content panel that already exists, rather
    # than into a panel of its own. Severity decides colour and order here
    # because that is presentation; the caller supplies no styling of its own.
    _REVIEW_MARKS = {"serious": ("RED", "▲"), "caution": ("ACC2", "●"), "note": ("PRI_DIM", "·")}

    @staticmethod
    def _esc(s) -> str:
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\n", "<br>"))

    def _show_review(self, title: str, summary: str, findings, unclear):
        """Slot — Qt main thread. Lays a document review into the content panel."""
        e = self._esc
        parts = [f'<div style="color:{C.TEXT}; font-family:Courier New;">']

        if summary:
            parts.append(
                f'<div style="color:{C.WHITE}; border-left:2px solid {C.PRI};'
                f' padding-left:8px; margin-bottom:10px;">{e(summary)}</div>')

        for f in (findings or []):
            key, mark = self._REVIEW_MARKS.get(f.get("severity"), ("PRI_DIM", "·"))
            colour = getattr(C, key)
            parts.append(f'<div style="margin-bottom:11px;">')
            parts.append(
                f'<span style="color:{colour}; font-weight:bold;">{mark}</span> '
                f'<span style="color:{C.WHITE}; font-weight:bold;">'
                f'{e(f.get("heading"))}</span>')
            if f.get("detail"):
                parts.append(f'<div style="margin-left:12px;">{e(f["detail"])}</div>')
            if f.get("quote"):
                # The document's own wording, visually separated from the
                # explanation so the two are never mistaken for each other.
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_DIM};'
                    f' border-left:1px solid {C.BORDER}; padding-left:7px;">'
                    f'&ldquo;{e(f["quote"])}&rdquo;</div>')
            if f.get("suggestion"):
                parts.append(
                    f'<div style="margin-left:12px; color:{C.PRI};">'
                    f'&rarr; {e(f["suggestion"])}</div>')
            parts.append('</div>')

        if unclear:
            parts.append(
                f'<div style="margin-top:6px; border-top:1px solid {C.BORDER};'
                f' padding-top:7px; color:{C.TEXT_MED};">'
                'The document does not settle:</div>')
            for u in unclear:
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_MED};">'
                    f'&middot; {e(u)}</div>')
        parts.append('</div>')

        import time as _time
        # Left as written, not upper-cased: the other content-panel titles are
        # the app's own English labels, but this one is the document's name in
        # the user's language, and str.upper() applies English casing rules
        # that can corrupt some non-English scripts.
        self._content_title_lbl.setText((title or "Document")[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setHtml("".join(parts))
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start)
        self._position_content_panel()
        self._content_panel.show()
        self._content_panel.raise_()

    # ── quiz panel ───────────────────────────────────────────────────────────
    # An interactive twin of the content panel. The caller only ever hands over
    # questions; everything about asking, marking and reporting happens here,
    # and the finished result is pushed back into the conversation the same way
    # a dropped file is — as a message AURA reads and responds to.

    def _quiz_btn(self, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setFont(QFont("Courier New", 8))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(24)
        edge = C.BORDER_B if primary else C.BORDER
        col = C.PRI if primary else C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {col};
                border: 1px solid {edge}; border-radius: 2px;
                padding: 3px 9px; text-align: left;
            }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI_DIM}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; }}
        """)
        return b

    def _build_quiz_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("QuizPanel")
        w.setStyleSheet(f"""
            QWidget#QuizPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(6)

        hdr = QHBoxLayout(); hdr.setSpacing(6)
        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._quiz_title_lbl = QLabel("QUIZ")
        self._quiz_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._quiz_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(self._quiz_title_lbl)
        hdr.addStretch()

        self._quiz_count_lbl = QLabel("")
        self._quiz_count_lbl.setFont(QFont("Courier New", 7))
        self._quiz_count_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._quiz_count_lbl)

        quit_btn = QPushButton("DISMISS  ✕")
        quit_btn.setFont(QFont("Courier New", 7))
        quit_btn.setFixedHeight(18)
        quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        quit_btn.clicked.connect(self._hide_quiz)
        hdr.addWidget(quit_btn)
        lay.addLayout(hdr)

        rule = QFrame(); rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {C.BORDER};")
        lay.addWidget(rule)

        self._quiz_q_lbl = QLabel("")
        self._quiz_q_lbl.setWordWrap(True)
        self._quiz_q_lbl.setFont(QFont("Courier New", 9))
        self._quiz_q_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        lay.addWidget(self._quiz_q_lbl)

        self._quiz_answers = QWidget()
        self._quiz_answers.setStyleSheet("background: transparent;")
        self._quiz_answers_lay = QVBoxLayout(self._quiz_answers)
        self._quiz_answers_lay.setContentsMargins(0, 2, 0, 0)
        self._quiz_answers_lay.setSpacing(4)
        lay.addWidget(self._quiz_answers)

        self._quiz_note_lbl = QLabel("")
        self._quiz_note_lbl.setWordWrap(True)
        self._quiz_note_lbl.setFont(QFont("Courier New", 8))
        self._quiz_note_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._quiz_note_lbl.hide()
        lay.addWidget(self._quiz_note_lbl)

        foot = QHBoxLayout()
        foot.addStretch()
        self._quiz_next_btn = self._quiz_btn("NEXT  →", primary=True)
        self._quiz_next_btn.setFixedWidth(110)
        self._quiz_next_btn.clicked.connect(self._quiz_next)
        self._quiz_next_btn.hide()
        foot.addWidget(self._quiz_next_btn)
        lay.addLayout(foot)

        self._quiz = None
        return w

    def _show_quiz(self, topic: str, questions, grader=None):
        """Slot — Qt main thread. Puts a fresh quiz on the board."""
        if not questions:
            return
        self._quiz = {
            "topic": topic or "",
            "questions": list(questions),
            "grader": grader,
            "i": 0,
            "results": [],
            "answered": False,
        }
        self._quiz_title_lbl.setText((topic or "quiz").upper()[:48])
        first_show = not self._quiz_panel.isVisible()
        self._quiz_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 250, 120), 0, 250])
        self._quiz_render()

    def _hide_quiz(self):
        self._quiz = None
        self._quiz_panel.hide()

    def _quiz_clear_answers(self):
        while self._quiz_answers_lay.count():
            item = self._quiz_answers_lay.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()

    def _quiz_render(self):
        q = self._quiz["questions"][self._quiz["i"]]
        n, total = self._quiz["i"] + 1, len(self._quiz["questions"])
        self._quiz_count_lbl.setText(f"{n} / {total}")
        self._quiz_q_lbl.setText(q.get("question", ""))
        self._quiz_note_lbl.hide()
        self._quiz_next_btn.hide()
        self._quiz["answered"] = False
        self._quiz_clear_answers()

        opts = q.get("options") or []
        if opts:
            for text in opts:
                b = self._quiz_btn("   " + text)
                b.clicked.connect(lambda _=False, t=text: self._quiz_submit(t))
                self._quiz_answers_lay.addWidget(b)
        else:
            row = QWidget(); row.setStyleSheet("background: transparent;")
            h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            field = QLineEdit()
            field.setFont(QFont("Courier New", 9))
            field.setPlaceholderText("your answer")
            field.setStyleSheet(f"""
                QLineEdit {{
                    background: {C.PANEL2}; color: {C.WHITE};
                    border: 1px solid {C.BORDER}; border-radius: 2px; padding: 4px 7px;
                }}
                QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}
            """)
            send = self._quiz_btn("ANSWER", primary=True)
            send.setFixedWidth(90)
            field.returnPressed.connect(lambda: self._quiz_submit(field.text()))
            send.clicked.connect(lambda: self._quiz_submit(field.text()))
            h.addWidget(field, stretch=1)
            h.addWidget(send)
            self._quiz_answers_lay.addWidget(row)
            field.setFocus()

    def _quiz_submit(self, given: str):
        if self._quiz is None or self._quiz["answered"]:
            return
        self._quiz["answered"] = True
        q = self._quiz["questions"][self._quiz["i"]]
        grader = self._quiz.get("grader")
        verdict = None
        if callable(grader):
            try:
                verdict = grader(q, given)
            except Exception:
                verdict = None
        self._quiz["results"].append({
            "question": q.get("question", ""),
            "type": q.get("type", ""),
            "given": str(given or "").strip(),
            "answer": q.get("answer", ""),
            "correct": verdict,
        })

        for i in range(self._quiz_answers_lay.count()):
            wdg = self._quiz_answers_lay.itemAt(i).widget()
            if wdg is not None:
                wdg.setEnabled(False)

        if verdict is True:
            mark, colour = "✓  correct", C.GREEN
        elif verdict is False:
            mark, colour = "✕  " + str(q.get("answer", "")), C.RED
        else:
            # Open answers and near-miss gap-fills are AURA's to judge. Saying
            # so is honest; marking it wrong here would be a guess.
            mark, colour = "…  noted — I'll go over this one with you", C.ACC2
        note = q.get("note") or ""
        self._quiz_note_lbl.setText(mark + (("\n" + note) if note else ""))
        self._quiz_note_lbl.setStyleSheet(f"color: {colour}; background: transparent;")
        self._quiz_note_lbl.show()

        last = self._quiz["i"] >= len(self._quiz["questions"]) - 1
        self._quiz_next_btn.setText("FINISH  →" if last else "NEXT  →")
        self._quiz_next_btn.show()
        self._quiz_next_btn.setFocus()

    def _quiz_next(self):
        if self._quiz is None:
            return
        if self._quiz["i"] >= len(self._quiz["questions"]) - 1:
            self._quiz_finish()
        else:
            self._quiz["i"] += 1
            self._quiz_render()

    def _quiz_finish(self):
        if self._quiz is None:
            return
        topic = self._quiz["topic"]
        results = self._quiz["results"]
        right = sum(1 for r in results if r["correct"] is True)
        unsure = sum(1 for r in results if r["correct"] is None)
        total = len(results)
        self._quiz_panel.hide()
        self._quiz = None

        self._log.append_log(f"QUIZ: {topic or 'quiz'} — {right}/{total} correct")

        # Hand it back to AURA as a message, not as a tool return: the tool
        # call ended minutes ago. This is the same channel a dropped file uses.
        lines = [f"[QUIZ_DONE] topic={topic or 'general'} | "
                 f"auto-marked {right}/{total} correct"
                 + (f", {unsure} still need your marking" if unsure else "")]
        for i, r in enumerate(results, 1):
            state = ("correct" if r["correct"] is True
                     else "wrong" if r["correct"] is False else "NEEDS MARKING")
            lines.append(
                f"{i}. [{r['type']}] {r['question']} | they answered: "
                f"{r['given'] or '(blank)'} | expected: {r['answer']} | {state}")
        lines.append(
            "Mark every question flagged NEEDS MARKING yourself — accept an answer "
            "that means the same thing. Then tell them how they did in their own "
            "language: the score, what they got wrong and why, in a couple of "
            "sentences. Offer another round only if it fits. "
            "Remember something only if it would still matter next week — that they "
            "are working through a subject, or keep missing the same thing. A score "
            "from one session is not worth a memory, and a memory per quiz would "
            "bury the things that are.")
        msg = "\n".join(lines)
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        w.setStyleSheet(f"background: {C.DARK};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(QFont("Courier New", 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        lay.addWidget(_fl("By CodeCrafters", C.PRI_DIM))
        return w

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell {self._assistant_name} what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                try:
                    winreg.QueryValueEx(key, "AURA_AI")
                    return True
                except FileNotFoundError:
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents"
                        / "com.aura.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "aura.desktop").exists()
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(Path(__file__).resolve().parent / "main.py")
            if _OS == "Windows":
                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if currently_on:
                    winreg.DeleteValue(reg, "AURA_AI")
                else:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, "AURA_AI", 0, winreg.REG_SZ,
                                      f'"{exe}" "{script}"')
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.aura.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        '  <key>Label</key><string>com.aura.assistant</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n'
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "aura.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._log.append_log(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, '_autostart_btn'):
            return
        if enabled:
            self._autostart_btn.setText("◉  AUTO-START: ON")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._autostart_btn.setText("◉  AUTO-START: OFF")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, '_brief_btn'):
            return
        if enabled:
            self._brief_btn.setText("☀  MORNING BRIEF: ON")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._brief_btn.setText("☀  MORNING BRIEF: OFF")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _refresh_talk_btns(self):
        """Repaint the push-to-talk row from the saved setting."""
        if not hasattr(self, "_ptt_btn"):
            return
        try:
            from core.hotkey import chord_label
            from memory.config_manager import get_push_to_talk_enabled
            ptt = get_push_to_talk_enabled()
            label = chord_label()
        except Exception:
            ptt = False
            label = "?"
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""

        self._ptt_btn.setText(f"🎚  PUSH-TO-TALK: {label}" if ptt
                              else "🎚  PUSH-TO-TALK: OFF")
        self._ptt_btn.setStyleSheet(_on if ptt else _off)
        self._ptt_btn.setToolTip(
            "Microphone stays closed until you hold the key — nothing is sent "
            "while you are not holding it." if ptt
            else "Hold a key to talk instead of streaming the mic continuously.")

    def _toggle_ptt(self):
        try:
            from memory.config_manager import (get_push_to_talk_enabled,
                                               save_push_to_talk_enabled)
            want = not get_push_to_talk_enabled()
            save_push_to_talk_enabled(want)
        except Exception as e:
            self._log.append_log(f"ERR: Push-to-talk unavailable — {e}")
            return
        scope = None
        if self.on_push_to_talk:
            try:
                scope = self.on_push_to_talk(want)
            except Exception as e:
                self._log.append_log(f"ERR: Push-to-talk failed — {e}")
                save_push_to_talk_enabled(False)
                want = False
        self._apply_ptt_shortcut(want and scope != "global")
        self._refresh_talk_btns()

    def _apply_ptt_shortcut(self, needed: bool):
        """Bind the chord inside the window when no global hook is available.

        On macOS and Linux there is no dependency-free way to read global key
        state, so the chord is at least live whenever this window has focus.
        Qt gives no key-release for a QShortcut, so a press latches the mic open
        and a short timer closes it; held down, auto-repeat keeps pushing that
        timer out, which behaves like holding a key.
        """
        if not needed:
            sc = getattr(self, "_ptt_sc", None)
            if sc is not None:
                sc.setEnabled(False)
                self._ptt_sc = None
            self._ptt_hold(False)
            return
        if getattr(self, "_ptt_sc", None) is not None:
            return
        try:
            from core.hotkey import qt_sequence
        except Exception:
            return

        self._ptt_release = QTimer(self)
        self._ptt_release.setSingleShot(True)
        self._ptt_release.setInterval(420)
        self._ptt_release.timeout.connect(lambda: self._ptt_hold(False))

        def _press():
            self._ptt_hold(True)
            self._ptt_release.start()

        self._ptt_sc = QShortcut(QKeySequence(qt_sequence()), self)
        self._ptt_sc.setAutoRepeat(True)
        self._ptt_sc.activated.connect(_press)

    def _ptt_hold(self, held: bool):
        """Report a windowed press/release to whoever owns the microphone."""
        cb = getattr(self, "ptt_hold", None)
        if cb:
            try:
                cb(bool(held))
            except Exception:
                pass

    # ── Customization ────────────────────────────────────────────────────────────

    def _open_customize(self):
        cfg = _read_full_config()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        
        _local_voice_on = False
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "AURA") or "AURA",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            cfg.get("voice_name", ""),
            _local_voice_on,
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self._preview_ui_color
        ov.saved.connect(self._apply_name_update)
        ov.configure_local_ai.connect(self._open_local_ai_settings)
        ov.show()
        self._customize_overlay = ov

    def _open_local_ai_settings(self):
        if self._customize_overlay:
            self._customize_overlay.hide()
        self._show_setup()

    def _preview_ui_color(self, hex_color: str):
        """Canlı önizleme — tüm arayüzü yeni renge boyar (config'e YAZMAZ)."""
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = "",
                           voice: str = "", local_voice: bool = False):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or "AURA"
        display = self._assistant_name.upper()
        self._title_lbl.setText(display)
        if display in ("AURA", "A.U.R.A"):
            self._sub_lbl.setText("Autonomous User Responsive Assistant")
        else:
            self._sub_lbl.setText("Personal AI Assistant")
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                # Tüm arayüzü (paneller, butonlar, kenarlıklar, HUD) canlı boya
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI

        # Voice change → persist and, if it actually changed, rebuild the Live
        # session so the new voice takes effect (it's fixed at connect time).
        voice_changed = False
        if voice:
            try:
                from memory.config_manager import get_voice, save_voice
                if voice != get_voice():
                    save_voice(voice)
                    voice_changed = True
            except Exception:
                pass

        # JARVIS (local voice) toggle → persist only. Unlike the Gemini voice
        # above, this needs no Live-session rebuild: main.py's _receive_audio
        # checks core.local_tts.active() live, per turn, so flipping it takes
        # effect on the very next sentence spoken.
        local_voice_changed = False
        try:
            from core import local_tts
            want = bool(local_voice)
            if want != local_tts.is_enabled():
                (local_tts.enable if want else local_tts.disable)()
                local_voice_changed = True
        except Exception:
            pass

        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
            if voice_changed:
                self._log.append_log(f"SYS: Voice set — {voice}")
            if local_voice_changed:
                self._log.append_log(
                    f"SYS: JARVIS local voice {'ON' if local_voice else 'OFF'}"
                )
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

        if voice_changed and self.on_voice_change:
            self.on_voice_change()

    def _centre_overlay(self, ov) -> None:
        """Place a floating overlay in the middle of the HUD and show it."""
        cw = self.centralWidget()
        ov.adjustSize()
        ov.setGeometry(
            max(0, (cw.width()  - ov.width())  // 2),
            max(0, (cw.height() - ov.height()) // 2),
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()

    # ── Audio devices ────────────────────────────────────────────────────────

    def _open_audio_devices(self):
        try:
            ov = AudioDeviceOverlay(parent=self.centralWidget())
        except Exception as e:
            self._log.append_log(f"ERR: Audio devices unavailable — {e}")
            return
        ov.picked.connect(self._on_audio_devices_applied)
        self._centre_overlay(ov)
        self._audio_overlay = ov            # keep a reference so it isn't GC'd

    def _on_audio_devices_applied(self):
        self._log.append_log("SYS: Audio devices updated.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Memory panel ─────────────────────────────────────────────────────────

    def _open_memory_panel(self):
        try:
            ov = MemoryOverlay(parent=self.centralWidget())
        except Exception as e:
            self._log.append_log(f"ERR: Memory panel unavailable — {e}")
            return
        self._centre_overlay(ov)
        self._memory_overlay = ov

    # ── Irreversible-action confirmation ─────────────────────────────────────

    def _show_confirm_banner(self, title: str, detail: str):
        self._hide_confirm_banner()
        ov = ConfirmBanner(title, detail, parent=self.centralWidget())
        ov.answered.connect(self._on_confirm_answered)
        self._centre_overlay(ov)
        self._confirm_overlay = ov

    def _hide_confirm_banner(self):
        ov = getattr(self, "_confirm_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._confirm_overlay = None

    def _on_confirm_answered(self, accepted: bool):
        # Tear the banner down first: resolving the confirmation may be about
        # to shut the machine down, and a live widget mid-callback is not where
        # you want to be when that happens.
        self._hide_confirm_banner()
        try:
            from core.confirm import resolve
            resolve(bool(accepted))
        except Exception as e:
            self._log.append_log(f"ERR: Confirmation failed — {e}")

    def _open_plugin_manager(self):
        plugins = self.get_plugins() if self.get_plugins else []
        cw = self.centralWidget()
        ov = PluginManagerOverlay(plugins, parent=cw)
        ov.adjustSize()
        ov.setGeometry(
            (cw.width()  - ov.width())  // 2,
            (cw.height() - ov.height()) // 2,
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()
        self._plugin_manager_overlay = ov   # keep a reference so it isn't GC'd

    def _open_plugin_settings(self):
        sections = self.get_plugin_settings() if self.get_plugin_settings else []
        cw = self.centralWidget()
        ov = PluginSettingsOverlay(sections, parent=cw)
        ow = PluginSettingsOverlay._OW
        oh = min(560, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.show()
        ov.raise_()
        self._plugin_settings_overlay = ov   # keep a reference so it isn't GC'd

    # ── Clipboard intelligence ───────────────────────────────────────────────────

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────────

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        # A deliberate press settles the question: whatever the video did, or is
        # about to do, the user has now said what they want.
        self._video_auto_muted = False
        self._set_muted(not self._muted)

    def _set_muted(self, muted: bool, note: str = ""):
        muted = bool(muted)
        if muted == self._muted:
            return
        self._muted = muted
        self.hud.muted = muted
        self._style_mute_btn()
        if muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted." + (f" {note}" if note else ""))
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active." + (f" {note}" if note else ""))

    def _style_mute_btn(self):
        self._mute_btn.set_alert(self._muted)
        self._mute_btn.setToolTip(
            "Microphone muted — click to unmute" if self._muted
            else "Microphone active — click to mute")

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            from memory.config_manager import is_setup_configured
            return is_setup_configured(d)
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget(), _read_full_config())
        cw = self.centralWidget()
        ow, oh = min(620, cw.width() - 24), min(540, cw.height() - 24)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.finished.connect(self._finish_setup)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, settings: dict):
        was_ready = self._ready
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            data = _read_full_config()
            if settings.get("gemini_api_key"):
                data["gemini_api_key"] = settings["gemini_api_key"]
            else:
                data.pop("gemini_api_key", None)
            if settings.get("elevenlabs_api_key"):
                data["elevenlabs_api_key"] = settings["elevenlabs_api_key"]
            else:
                data.pop("elevenlabs_api_key", None)
            data["os_system"] = settings["os_system"]
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            from memory.config_manager import save_local_llm_config
            save_local_llm_config({
                "mode": settings["mode"],
                "provider": settings["provider"],
                "base_url": settings["base_url"],
                "model": settings["model"],
                "stt_engine": settings["stt_engine"],
                "vosk_model": settings["vosk_model"],
                "tts_engine": settings["tts_engine"],
                "tts_voice": settings["tts_voice"],
                "local_voice_override": settings["local_voice_override"],
                "voice_id": settings["voice_id"],
                "voice_by_model": settings["voice_by_model"],
                "tts_voice_by_engine": settings["tts_voice_by_engine"],
            })
        except Exception as exc:
            self._log.append_log(f"ERR: Could not save setup: {exc}")
            return
        self._ready = True
        if was_ready and self.on_brain_change:
            self.on_brain_change()
        if self._overlay:
            self._overlay.begin_initialising(settings)
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "A.U.R.A") or "AURA"
        self._log.append_log(
            f"SYS: Setup saved. OS={settings['os_system'].upper()}; "
            f"mode={settings['mode'].upper()}."
        )

    def _finish_setup(self):
        if self._overlay:
            self._overlay.hide()
            self._overlay.deleteLater()
            self._overlay = None

class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class AuraUI:
    def __init__(self, face_path: str, size=None):
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
        for flag in (
            "--enable-webgl", "--enable-unsafe-swiftshader",
            "--use-gl=angle", "--use-angle=swiftshader",
        ):
            if flag not in flags:
                flags.append(flag)
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path)
        self._win.show()
        self.root = _RootShim(self._app)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_voice_change(self):
        return self._win.on_voice_change

    @on_voice_change.setter
    def on_voice_change(self, cb):
        self._win.on_voice_change = cb

    @property
    def on_audio_device_change(self):
        return self._win.on_audio_device_change

    @on_audio_device_change.setter
    def on_audio_device_change(self, cb):
        self._win.on_audio_device_change = cb

    @property
    def on_brain_change(self):
        return self._win.on_brain_change

    @on_brain_change.setter
    def on_brain_change(self, cb):
        self._win.on_brain_change = cb

    def show_confirm(self, title: str, detail: str) -> None:
        """Thread-safe: raise the irreversible-action gate. Called from action
        handlers running in executor threads, so it goes through a signal."""
        self._win._confirm_sig.emit(str(title)[:120], str(detail)[:300])

    def hide_confirm(self) -> None:
        """Thread-safe: take the gate down."""
        self._win._confirm_hide_sig.emit()

    def request_earth_command(self, command: str, origin: str = "",
                              destination: str = "") -> None:
        """Thread-safe Earth controls for action handlers running off the UI thread."""
        self._win.request_earth_command(command, origin, destination)

    @property
    def get_plugins(self):
        return self._win.get_plugins

    @get_plugins.setter
    def get_plugins(self, cb):
        self._win.get_plugins = cb

    @property
    def get_plugin_settings(self):
        return self._win.get_plugin_settings

    @get_plugin_settings.setter
    def get_plugin_settings(self, cb):
        self._win.get_plugin_settings = cb

    @property
    def ptt_hold(self):
        return self._win.ptt_hold

    @ptt_hold.setter
    def ptt_hold(self, cb):
        self._win.ptt_hold = cb

    @property
    def on_push_to_talk(self):
        return self._win.on_push_to_talk

    @on_push_to_talk.setter
    def on_push_to_talk(self, cb):
        self._win.on_push_to_talk = cb

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)
        # Face unlock (only active on setups with a suitable camera) holds the
        # assistant back until the user has been recognised.
        while not self._win._face_gate.wait(0.1):
            pass

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def show_quiz(self, topic: str, questions, grade=None) -> None:
        """Thread-safe: put an interactive quiz on the board.

        `grade(question, given)` decides each answer — the caller supplies it
        so the marking rules live with the questions rather than being
        duplicated here. Returning None from it means "AURA should judge this
        one", which is how open answers and near-miss gap-fills are handled.

        Returns immediately: the user answers at their own pace and the
        finished result is delivered back through on_text_command.
        """
        self._win._quiz_sig.emit(str(topic or ""), list(questions or []), grade)

    def hide_quiz(self) -> None:
        """Thread-safe: clear any quiz currently on the board."""
        self._win._quiz_hide_sig.emit()

    def show_review(self, title: str, summary: str, findings, unclear=None) -> None:
        """Thread-safe: lay a document review into the panel below the HUD.

        `findings` is a list of {heading, detail, severity, quote, suggestion};
        severity is one of 'serious' / 'caution' / 'note' and decides colour
        and order here, so the caller supplies no styling of its own.
        """
        self._win._review_sig.emit(str(title or ""), str(summary or ""),
                                   list(findings or []), list(unclear or []))

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        """Thread-safe: show a webcam frame in the small overlay (screen captures)."""
        self._win._camera_sig.emit(img_bytes)

    def show_video(self, source: str, title: str = "", muted: bool = True,
                   audio_source: str = "") -> None:
        """Thread-safe: play a video where the central ball normally is.

        `source` is a local file path or a direct URL. `audio_source` is an
        optional separate soundtrack URL for sites that serve picture and sound
        as two streams. It starts muted, and while its sound is on the
        microphone is closed so the assistant does not answer the film.
        """
        self._win._video_open_sig.emit(str(source or ""), str(title or ""),
                                       bool(muted), str(audio_source or ""))

    def stop_video(self) -> None:
        """Thread-safe: close the video and give the HUD back to the ball."""
        self._win._video_close_sig.emit()

    def set_video_muted(self, muted: bool) -> None:
        """Thread-safe: turn the video's sound on or off."""
        self._win._video_mute_sig.emit(bool(muted))

    def video_is_playing(self) -> bool:
        return bool(self._win.video_is_playing())

    def set_audio_level(self, level: float) -> None:
        """Thread-safe: feed the live output loudness (0.0–1.0) to the HUD so
        the central ball pulses with the assistant's voice."""
        try:
            self._win.hud.set_audio_level(level)
        except Exception:
            pass

    def push_visemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: schedule per-frame loudness, timed to when the audio
        will actually sound. See HudCanvas.push_visemes()."""
        try:
            self._win.hud.push_visemes(frames, hop, at)
        except Exception:
            pass

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Accepted for compatibility; the central ball has no eyes."""
        try:
            self._win.hud.glance(dx, dy, hold)
        except Exception:
            pass

    def start_camera_stream(self) -> None:
        """Thread-safe: start live camera feed in the full HUD area."""
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        """Thread-safe: stop the live camera feed."""
        self._win.stop_camera_stream()

    def pause_room_watch(self) -> None:
        """Thread-safe: briefly release the camera for a one-off capture."""
        self._win.pause_room_watch()

    def resume_room_watch(self) -> None:
        """Thread-safe: resume the always-on room watch."""
        self._win.resume_room_watch()

    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")