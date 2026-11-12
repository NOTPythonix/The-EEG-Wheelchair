"""Knight Board monitor, recorder and replay - a working replacement for the
parts of the NeuroPawn EXG Visualizer this project actually uses.

Why it exists: the Visualizer's recorder calls get_current_board_data(500)
every 50 ms and appends all 500 rows, so a 10 second recording becomes ~80,500
rows of one sliding 4 second window repeated ~160 times.  This app reads the
serial port directly, keeps every frame exactly once, and proves it with the
board's own rolling frame counter.

Run:  python knight_monitor/knight_app.py
Deps: PySide6, numpy, scipy, pyserial
"""

from __future__ import annotations

import collections
import csv
import datetime as _dt
import functools
import json
import os
import queue
import sys
import time

import numpy as np
import scipy.signal as sps

from PySide6 import QtCore, QtGui, QtWidgets

import knight_protocol as kp

try:
    import serial
    import serial.tools.list_ports
    HAVE_SERIAL = True
except Exception:                                    # pragma: no cover
    HAVE_SERIAL = False


# --- appearance -----------------------------------------------------------
BG = "#080b0f"           # trace canvas, near black
PANEL = "#0e1319"        # sidebar
PANEL_2 = "#131a22"      # raised controls
PANEL_3 = "#182029"
LINE = "#1e2733"
LINE_SOFT = "#161d26"
TEXT = "#e6edf3"
DIM = "#8494a6"
MUTED = "#5d6b7a"
AXIS = "#7b8c9e"
ACCENT = "#3b82f6"
ACCENT_HI = "#5b9bff"
GOOD = "#4ade80"
WARN = "#fbbf24"
BAD = "#f87171"
REC = "#ef4444"

UI_FONT = "Segoe UI"
MONO_FONT = "Consolas"

TRACE_COLORS = ["#63b3ff", "#6ee7b7", "#fbbf24", "#fb923c",
                "#f87171", "#c084fc", "#22d3ee", "#9aa8b6"]
IMU_COLORS = ["#63b3ff", "#6ee7b7", "#fbbf24"]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECORD_DIR = os.path.join(SCRIPT_DIR, "recordings")
MARKER_FILE = os.path.join(SCRIPT_DIR, "markers.json")
CHANNEL_FILE = os.path.join(SCRIPT_DIR, "channels.json")

FILTER_PRESETS = [
    ("Visualizer 5–40 Hz", "visualizer"),
    ("EEG 1–45 Hz", "eeg145"),
    ("Raw", "raw"),
]
FILTER_SPEC = {
    "visualizer": {"bandpass": (5.0, 40.0, 2), "bandstop": [(48.0, 52.0, 4), (58.0, 62.0, 4)]},
    "eeg145":     {"bandpass": (1.0, 45.0, 2), "bandstop": [(58.0, 62.0, 4)]},
    "raw":        {"bandpass": None, "bandstop": []},
}


@functools.lru_cache(maxsize=32)
def _filters(preset: str, fs: int):
    spec = FILTER_SPEC[preset]
    nyq = fs / 2.0
    out = []
    bp = spec["bandpass"]
    if bp:
        lo, hi, order = bp
        out.append(sps.butter(order, [lo / nyq, hi / nyq], btype="bandpass", output="sos"))
    for lo, hi, order in spec["bandstop"]:
        if hi >= nyq:
            continue
        out.append(sps.butter(order, [lo / nyq, hi / nyq], btype="bandstop", output="sos"))
    return out


def apply_filter(x: np.ndarray, fs: int, preset: str) -> np.ndarray:
    """Zero-phase filtering, matching what the Visualizer does per window."""
    if preset == "raw" or x.size < 64:
        return x - x.mean() if x.size else x
    y = x - x.mean()
    for sos in _filters(preset, fs):
        try:
            y = sps.sosfiltfilt(sos, y)
        except Exception:
            return y
    return y


def nice_range(value: float) -> float:
    """Round a scale up to something readable on an axis."""
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000):
        if value <= step:
            return float(step)
    return float(int(value / 1000 + 1) * 1000)


def channel_name(labels, ch):
    lab = (labels[ch] if ch < len(labels) else "") or ""
    return lab.strip() or f"ch{ch + 1}"


# --------------------------------------------------------------------------
# serial link
# --------------------------------------------------------------------------

class SerialWorker(QtCore.QThread):
    """Owns the port.  Reads bytes, emits frames into a deque, and applies a
    queue of board commands with the pauses the firmware needs."""

    connected_ok = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def __init__(self, port: str, mode: str = "auto", mirror: str = "",
                 parent=None):
        super().__init__(parent)
        self.port = port
        self.mode = mode
        self.mirror = mirror
        self.frames = collections.deque()
        self.frame_length = None
        self.stats = {"fps": 0.0, "total": 0, "gaps": 0, "repeats": 0,
                      "dropped": 0, "frame_bytes": 0, "age": 0.0,
                      "last_counter": None, "running": False}
        self._commands = queue.Queue()
        self._stopping = False
        self._frame_times = collections.deque(maxlen=400)

    # -- commands ----------------------------------------------------------
    def send_command(self, text: str, delay_after: float = 0.6) -> None:
        self._commands.put((text, delay_after))

    def queued_commands(self) -> int:
        return self._commands.qsize()

    def stop(self) -> None:
        self._stopping = True

    # -- thread body -------------------------------------------------------
    def run(self) -> None:
        ser = None
        mir = None
        try:
            ser = serial.Serial(self.port, kp.BAUD, timeout=0.02, write_timeout=0.25)
        except Exception as exc:
            self.failed.emit(f"Could not open {self.port}.\n\n{exc}\n\n"
                             "Another program may already hold the port.")
            return

        if self.mirror:
            try:
                mir = serial.Serial(self.mirror, kp.BAUD, timeout=0.02, write_timeout=0.02)
            except Exception:
                mir = None
                self.failed.emit(
                    f"Connected to {self.port}, but the mirror port {self.mirror} "
                    "could not be opened, so the Visualizer will not see data.\n\n"
                    "Check the com0com pair and that no other app holds the port.")

        parser = kp.FrameParser(self.mode)
        prev = None
        gaps = repeats = dropped = total = 0
        # the firmware silently drops commands for ~2 s after a stream starts
        next_command = time.monotonic() + 2.2
        last_frame = 0.0
        stats_at = time.monotonic()

        self.frame_length = parser.frame_length
        self.connected_ok.emit(
            f"{self.port} @ {kp.BAUD}")
        self.stats["running"] = True

        while not self._stopping:
            now = time.monotonic()

            # board commands, spaced out
            if now >= next_command and not self._commands.empty():
                text, delay = self._commands.get()
                try:
                    ser.write(text.encode("ascii"))
                except Exception:
                    pass
                next_command = time.monotonic() + delay

            # board -> host
            chunk = b""
            try:
                waiting = ser.in_waiting
                chunk = ser.read(waiting if waiting else 1)
            except Exception as exc:
                self.failed.emit(f"Serial read failed: {exc}")
                break

            if chunk:
                if mir is not None:
                    try:
                        mir.write(chunk)          # exact bytes, no re-encoding
                    except Exception:
                        pass
                for fr in parser.feed(chunk):
                    total += 1
                    dropped += parser.dropped
                    parser.dropped = 0
                    self.frames.append((fr.counter, fr.counts, fr.imu))
                    if prev is not None:
                        d = kp.counter_delta(prev, fr.counter)
                        if d == 0:
                            repeats += 1
                        elif d > 1:
                            gaps += d - 1
                        elif d < 0:
                            gaps += -d
                    prev = fr.counter
                    last_frame = time.monotonic()
                    self._frame_times.append(last_frame)
                self.frame_length = parser.frame_length

            # host-side mirror -> board (the Visualizer's chon_/rldadd_ commands)
            if mir is not None:
                try:
                    n = mir.in_waiting
                    if n:
                        ser.write(mir.read(n))
                except Exception:
                    pass

            if not chunk:
                if self._commands.empty():
                    time.sleep(0.002)

            if now - stats_at > 0.4:
                stats_at = now
                cut = now - 1.0
                while self._frame_times and self._frame_times[0] < cut:
                    self._frame_times.popleft()
                self.stats = {
                    "fps": float(len(self._frame_times)),
                    "total": total,
                    "gaps": gaps,
                    "repeats": repeats,
                    "dropped": dropped,
                    "frame_bytes": parser.frame_length or 0,
                    "age": (time.monotonic() - last_frame) if last_frame else 0.0,
                    "last_counter": prev,
                    "running": True,
                }

        self.stats = dict(self.stats, running=False)
        for s in (ser, mir):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# small styled widgets
# --------------------------------------------------------------------------

def app_icon() -> QtGui.QIcon:
    pm = QtGui.QPixmap(64, 64)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(QtGui.QColor(ACCENT))
    p.drawRoundedRect(2, 2, 60, 60, 15, 15)
    pen = QtGui.QPen(QtGui.QColor("#ffffff"), 3.4)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    pts = [(9, 34), (17, 34), (22, 20), (28, 46), (34, 13), (40, 43), (46, 27), (52, 34), (55, 34)]
    path = QtGui.QPainterPath(QtCore.QPointF(*pts[0]))
    for x, y in pts[1:]:
        path.lineTo(x, y)
    p.drawPath(path)
    p.end()
    return QtGui.QIcon(pm)


class Combo(QtWidgets.QComboBox):
    """Combo box with a drawn chevron instead of the stock Qt arrow."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(24)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(5)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        pen = QtGui.QPen(QtGui.QColor(DIM), 1.5)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        x = self.width() - 11.0
        y = self.height() / 2.0 - 1.5
        p.drawLine(QtCore.QPointF(x - 3.6, y), QtCore.QPointF(x, y + 3.6))
        p.drawLine(QtCore.QPointF(x, y + 3.6), QtCore.QPointF(x + 3.6, y))


class Chip(QtWidgets.QAbstractButton):
    """Small checkable pill: 'on', 'rld'.  No stock checkbox look."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        f = self.font()
        f.setPointSizeF(7.6)
        f.setBold(True)
        self.setFont(f)
        self.setFixedHeight(20)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,
                           QtWidgets.QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QtCore.QSize(self.fontMetrics().horizontalAdvance(self.text()) + 15, 20)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        r = QtCore.QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        if self.isChecked():
            p.setPen(QtGui.QPen(QtGui.QColor(ACCENT), 1))
            p.setBrush(QtGui.QColor(ACCENT))
        else:
            p.setPen(QtGui.QPen(QtGui.QColor(LINE), 1))
            p.setBrush(QtGui.QColor(PANEL_3 if self.underMouse() else PANEL_2))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(QtGui.QColor("#ffffff") if self.isChecked() else QtGui.QColor(DIM))
        p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, self.text())


def section_label(text: str) -> QtWidgets.QLabel:
    lab = QtWidgets.QLabel(text.upper())
    f = lab.font()
    f.setPointSizeF(7.6)
    f.setBold(True)
    f.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 1.1)
    lab.setFont(f)
    lab.setStyleSheet(f"color:{MUTED}; background:transparent;")
    return lab


def dim_label(text: str, size=8.4) -> QtWidgets.QLabel:
    lab = QtWidgets.QLabel(text)
    f = lab.font()
    f.setPointSizeF(size)
    lab.setFont(f)
    lab.setStyleSheet(f"color:{DIM}; background:transparent;")
    return lab


def hline() -> QtWidgets.QFrame:
    line = QtWidgets.QFrame()
    line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background:{LINE_SOFT}; border:none;")
    return line


class LanesView(QtWidgets.QWidget):
    """Stacked traces, one lane per channel, labels inside each lane."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.lanes = []            # list of dicts: label, sub, color, curves, dim
        self.span = 4.0
        self.markers = []          # list of (age_seconds, label)
        self.show_axis = True
        self.auto_scale = True
        self.fixed_scale = 100.0
        self.hint = None

    def set_lanes(self, lanes, span, markers=None, hint=None):
        self.lanes = lanes
        self.span = max(0.1, span)
        self.markers = markers or []
        self.hint = hint
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QtGui.QColor(BG))

        lanes = [l for l in self.lanes if l.get("visible", True)]
        if not lanes:
            p.setPen(QtGui.QColor(MUTED))
            f = p.font()
            f.setPointSizeF(9.0)
            p.setFont(f)
            p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                       self.hint or "No signal yet")
            return

        axis_h = 22 if self.show_axis else 6
        usable = self.height() - axis_h
        lane_h = usable / len(lanes)
        left, right = 10.0, self.width() - 10.0

        for idx, lane in enumerate(lanes):
            top = idx * lane_h
            mid = top + lane_h / 2.0
            half = lane_h * 0.38

            if idx % 2:
                p.fillRect(QtCore.QRectF(0, top, self.width(), lane_h),
                           QtGui.QColor("#0a0f15"))
            p.setPen(QtGui.QPen(QtGui.QColor(LINE_SOFT), 1))
            p.drawLine(0, int(top), self.width(), int(top))

            pen = QtGui.QPen(QtGui.QColor("#1b2430"), 1)
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            pen.setDashPattern([2, 4])
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(left, mid), QtCore.QPointF(right, mid))

            curves = lane["curves"]
            peak = 1e-6
            for _color, data in curves:
                if data.size:
                    peak = max(peak, float(np.percentile(np.abs(data), 99.5)))
            scale = (nice_range(peak) if self.auto_scale else self.fixed_scale)
            scale = max(scale, 1e-6)

            faded = bool(lane.get("dim"))
            for color, data in curves:
                if data.size < 2:
                    continue
                n = data.size
                pts = QtGui.QPolygonF()
                for i in range(n):
                    x = left + (right - left) * (i / (n - 1))
                    y = mid - float(np.clip(data[i] / scale, -1.0, 1.0)) * half
                    pts.append(QtCore.QPointF(x, y))
                c = QtGui.QColor(color)
                if faded:
                    c.setAlpha(90)
                p.setPen(QtGui.QPen(c, 1.1))
                p.drawPolyline(pts)

            font = p.font()
            font.setPointSizeF(8.8)
            font.setBold(True)
            p.setFont(font)
            p.setPen(QtGui.QColor(MUTED if faded else TEXT))
            label = lane.get("label", "")
            p.drawText(QtCore.QPointF(left + 2, top + 14), label)

            sub_font = p.font()
            sub_font.setPointSizeF(7.6)
            sub_font.setBold(False)
            p.setFont(sub_font)
            p.setPen(QtGui.QColor(AXIS))
            lw = QtGui.QFontMetrics(font).horizontalAdvance(label)
            p.drawText(QtCore.QPointF(left + 10 + lw, top + 14), lane.get("sub", ""))
            p.drawText(QtCore.QRectF(right - 90, top + 3, 88, 14),
                       QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                       f"±{scale:g} µV")

        if self.show_axis:
            y = usable + 3
            tick_font = p.font()
            tick_font.setPointSizeF(7.6)
            p.setFont(tick_font)
            p.setPen(QtGui.QColor(AXIS))
            for k in range(5):
                x = left + (right - left) * (1 - k / 4.0)
                p.drawLine(QtCore.QPointF(x, usable), QtCore.QPointF(x, usable + 3))
                if k == 0:
                    p.drawText(QtCore.QRectF(x - 42, y, 42, 14),
                               QtCore.Qt.AlignmentFlag.AlignRight, "now")
                else:
                    p.drawText(QtCore.QRectF(x - 22, y, 44, 14),
                               QtCore.Qt.AlignmentFlag.AlignHCenter,
                               f"-{k * self.span / 4:g}s")

        for age, label in self.markers:
            if 0 <= age <= self.span:
                x = left + (right - left) * (1 - age / self.span)
                pen = QtGui.QPen(QtGui.QColor(ACCENT_HI), 1)
                pen.setStyle(QtCore.Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(QtCore.QPointF(x, usable * 0.06), QtCore.QPointF(x, usable))
                fm = p.fontMetrics()
                w = fm.horizontalAdvance(label) + 10
                p.setPen(QtCore.Qt.PenStyle.NoPen)
                p.setBrush(QtGui.QColor(56, 100, 190, 150))
                p.drawRoundedRect(QtCore.QRectF(x + 2, usable * 0.06, w, 14), 3, 3)
                p.setPen(QtGui.QColor("#ffffff"))
                p.drawText(QtCore.QRectF(x + 7, usable * 0.06, w, 14),
                           QtCore.Qt.AlignmentFlag.AlignVCenter, label)

        if self.hint:
            p.setPen(QtGui.QColor(MUTED))
            f = p.font()
            f.setPointSizeF(9.0)
            p.setFont(f)
            p.drawText(QtCore.QRectF(0, 0, self.width(), usable),
                       QtCore.Qt.AlignmentFlag.AlignCenter, self.hint)


class SpectrumView(QtWidgets.QWidget):
    """Welch PSD of the visible window, 0-62.5 Hz."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(130)
        self.items = []            # (color, label, freqs, psd_db)
        self.fmax = 62.5

    def set_items(self, items):
        self.items = items
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QtGui.QColor(BG))

        f = p.font()
        f.setPointSizeF(7.4)
        f.setBold(True)
        f.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 1.0)
        p.setFont(f)
        p.setPen(QtGui.QColor(MUTED))
        p.drawText(10, 14, "SPECTRUM  ·  WELCH 128-PT BLACKMAN-HARRIS  ·  dB RE MAX")

        if not self.items:
            return

        left, right, top, bottom = 40, self.width() - 10, 24, self.height() - 20
        if right <= left or bottom <= top:
            return

        allv = np.concatenate([it[3] for it in self.items if it[3].size])
        if allv.size == 0:
            return
        hi = float(np.max(allv))
        lo = max(hi - 55.0, float(np.min(allv)))

        def xmap(fr):
            return left + (right - left) * (fr / self.fmax)

        def ymap(v):
            return bottom - (bottom - top) * ((v - lo) / max(hi - lo, 1e-9))

        tf = p.font()
        tf.setPointSizeF(7.4)
        tf.setBold(False)
        tf.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 0)
        p.setFont(tf)

        for fr in range(0, int(self.fmax) + 1, 10):
            p.setPen(QtGui.QPen(QtGui.QColor(LINE_SOFT), 1))
            p.drawLine(QtCore.QPointF(xmap(fr), top), QtCore.QPointF(xmap(fr), bottom))
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QtCore.QRectF(xmap(fr) - 12, bottom + 3, 24, 14),
                       QtCore.Qt.AlignmentFlag.AlignHCenter, str(fr))
        for db in (0, -20, -40):
            y = ymap(hi + db)
            p.setPen(QtGui.QPen(QtGui.QColor(LINE_SOFT), 1))
            p.drawLine(QtCore.QPointF(left, y), QtCore.QPointF(right, y))
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QtCore.QRectF(4, y - 7, 32, 14),
                       QtCore.Qt.AlignmentFlag.AlignRight, f"{db}")
        p.setPen(QtGui.QColor(MUTED))
        p.drawText(QtCore.QRectF(right - 18, bottom + 3, 18, 14), "Hz")

        for color, label, freqs, psd in self.items:
            if freqs.size < 2:
                continue
            pts = QtGui.QPolygonF()
            for fr, v in zip(freqs, psd):
                pts.append(QtCore.QPointF(xmap(fr), ymap(float(v))))
            p.setPen(QtGui.QPen(QtGui.QColor(color), 1.0))
            p.drawPolyline(pts)


# --------------------------------------------------------------------------
# dialogs
# --------------------------------------------------------------------------

class RecordDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Record")
        self.setMinimumWidth(440)
        form = QtWidgets.QFormLayout(self)
        form.setContentsMargins(18, 18, 18, 14)
        form.setSpacing(10)

        self.seconds = QtWidgets.QSpinBox()
        self.seconds.setRange(1, 3600)
        self.seconds.setValue(10)
        self.seconds.setSuffix(" s")
        self.seconds.setFixedWidth(110)
        self.seconds.setButtonSymbols(
            QtWidgets.QAbstractSpinBox.ButtonSymbols.PlusMinus)
        form.addRow(dim_label("Duration", 9), self.seconds)

        row = QtWidgets.QHBoxLayout()
        self.folder = QtWidgets.QLineEdit(RECORD_DIR)
        browse = QtWidgets.QPushButton("Browse")
        browse.setFixedHeight(26)
        browse.clicked.connect(self._browse)
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        form.addRow(dim_label("Save to", 9), row)

        self.hint = QtWidgets.QLabel()
        self.hint.setStyleSheet(f"color:{MUTED}; font-size:11px")
        form.addRow("", self.hint)
        self.seconds.valueChanged.connect(self._hint)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._hint()

    def _browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Save Directory",
                                                       self.folder.text())
        if d:
            self.folder.setText(d)

    def _hint(self):
        self.hint.setText(f"writes {self.seconds.value() * kp.SAMPLE_RATE:,} rows, "
                          "one per frame, plus a JSON sidecar")

    def values(self):
        return self.seconds.value(), self.folder.text()


class MarkerDialog(QtWidgets.QDialog):
    """Marker labels and their keys.  Empty by default: you define them."""

    def __init__(self, markers, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Markers")
        self.setMinimumSize(460, 340)
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Label", "Press key"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 180)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)

        for entry in markers:
            self._add_row(entry.get("label", ""), entry.get("key", ""))

        add = QtWidgets.QPushButton("Add marker")
        remove = QtWidgets.QPushButton("Remove selected")
        add.clicked.connect(lambda: self._add_row("", ""))
        remove.clicked.connect(self._remove_selected)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        hint = QtWidgets.QLabel(
            "Add a marker, type its label, then click its key cell and press the keys.\n"
            "Markers are stamped at the current recording time and written to the sidecar.")
        hint.setStyleSheet(f"color:{DIM}")
        layout.addWidget(hint)
        layout.addWidget(self.table, 1)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def _add_row(self, label, key):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(label))
        editor = QtWidgets.QKeySequenceEdit(QtGui.QKeySequence(key))
        editor.setMinimumWidth(150)
        editor.setToolTip("Click, then press the key or combination")
        self.table.setCellWidget(r, 1, editor)

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def markers(self):
        out = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            label = (item.text().strip() if item else "")
            editor = self.table.cellWidget(r, 1)
            key = editor.keySequence().toString() if editor else ""
            if label and key:
                out.append({"label": label, "key": key})
        return out


class ImuWindow(QtWidgets.QWidget):
    """Accelerometer / gyroscope / magnetometer, three lanes of three axes."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("IMU Viewer")
        self.setWindowIcon(app_icon())
        self.resize(860, 520)
        self.view = LanesView()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(self.view)
        self._buf = collections.deque(maxlen=2000)

    def push(self, values):
        self._buf.append(values)

    def refresh(self, span):
        arr = np.array(self._buf, dtype=float)
        if arr.size == 0:
            self.view.set_lanes([], span)
            return
        n = max(1, int(span * kp.SAMPLE_RATE))
        arr = arr[-n:]
        groups = [("accel", 0, "g"), ("gyro", 3, "rad/s"), ("mag", 6, "uT")]
        lanes = []
        for name, off, unit in groups:
            lanes.append({
                "label": name,
                "sub": unit,
                "visible": True,
                "curves": [(IMU_COLORS[i], arr[:, off + i]) for i in range(3)],
            })
        self.view.set_lanes(lanes, span)


class ReplayWindow(QtWidgets.QWidget):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Replay — {os.path.basename(path)}")
        self.setWindowIcon(app_icon())
        self.resize(1060, 700)
        self.path = path
        self.fs = kp.SAMPLE_RATE
        self.span = 4.0

        self.data = None           # (n, 8) uV
        self.counters = None
        self.labels = [f"ch{i}" for i in range(1, 9)]
        self.markers = []
        self.playhead = 0.0
        self.playing = False
        self.speed = 1.0

        self.view = LanesView()
        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self._seek)

        self.play_btn = QtWidgets.QPushButton("Play")
        self.play_btn.setFixedWidth(72)
        self.play_btn.clicked.connect(self._toggle)
        speed = Combo()
        speed.addItems(["1x", "2x", "5x", "10x", "20x"])
        speed.currentTextChanged.connect(lambda t: setattr(self, "speed", float(t.rstrip("x"))))
        win = Combo()
        win.addItems(["1 s", "2 s", "4 s", "10 s", "30 s"])
        win.setCurrentText("4 s")
        win.currentTextChanged.connect(self._set_span)
        self.info = QtWidgets.QLabel()
        self.info.setStyleSheet(f"color:{DIM}; font-family:{MONO_FONT}; font-size:11px")

        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 8)
        bar.setSpacing(8)
        bar.addWidget(self.play_btn)
        bar.addWidget(dim_label("speed", 8.2))
        bar.addWidget(speed)
        bar.addWidget(dim_label("window", 8.2))
        bar.addWidget(win)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.info)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view, 1)
        layout.addLayout(bar)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

        self._load()

    def _load(self):
        with open(self.path, "r", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            rows = [r for r in reader if r]
        if not header:
            raise ValueError("empty file")
        idx = {name: i for i, name in enumerate(header)}
        chans = [f"ch{i}" for i in range(1, 9)]
        if not all(c in idx for c in chans):
            raise ValueError("not a knight_app recording (no ch1..ch8 columns)")
        counts = np.array([[float(r[idx[c]]) for c in chans] for r in rows], dtype=float)
        self.counters = np.array([int(r[idx["counter"]]) for r in rows], dtype=int) \
            if "counter" in idx else None

        gains = {str(i): kp.DEFAULT_GAIN for i in range(1, 9)}
        side = os.path.splitext(self.path)[0] + ".json"
        if os.path.exists(side):
            try:
                with open(side, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
                for i in range(1, 9):
                    gains[str(i)] = int(meta.get("gain", {}).get(str(i), kp.DEFAULT_GAIN))
                self.labels = meta.get("channel_labels") or self.labels
                self.markers = meta.get("markers", [])
            except Exception:
                pass
        self.gains = [gains[str(i)] for i in range(1, 9)]
        self.data = counts * np.array([kp.uv_per_count(g) for g in self.gains])
        self.slider.setRange(0, max(0, len(self.data) - int(self.span * self.fs)))
        self._draw()

    def _set_span(self, text):
        self.span = float(text.split()[0])
        self.slider.setRange(0, max(0, len(self.data) - int(self.span * self.fs)))
        self._draw()

    def _seek(self, value):
        if self.playing:
            self.playhead = float(value)

    def _toggle(self):
        self.playing = not self.playing
        self.play_btn.setText("Pause" if self.playing else "Play")

    def _tick(self):
        if self.playing and self.data is not None:
            self.playhead += self.fs * self.speed * 0.05
            if self.playhead >= len(self.data) - self.span * self.fs:
                self.playhead = 0.0
            self.slider.blockSignals(True)
            self.slider.setValue(int(self.playhead))
            self.slider.blockSignals(False)
            self._draw()

    def _draw(self):
        if self.data is None:
            return
        n = int(self.span * self.fs)
        start = int(max(0, min(len(self.data) - n, self.playhead)))
        chunk = self.data[start:start + n]
        t0 = start / self.fs
        lanes = []
        for ch in range(8):
            raw = chunk[:, ch]
            lanes.append({
                "label": channel_name(self.labels, ch),
                "sub": f"gain {self.gains[ch]}",
                "visible": True,
                "curves": [(TRACE_COLORS[ch], apply_filter(raw, self.fs, "visualizer"))],
            })
        marks = [(t0 + n / self.fs - m.get("t", 0), m.get("label", ""))
                 for m in self.markers if 0 <= t0 + n / self.fs - m.get("t", 0) <= self.span]
        self.view.set_lanes(lanes, self.span, marks)
        self.info.setText(f"{len(self.data):,} rows   {len(self.data) / self.fs:.2f} s   "
                          f"t = {t0:.2f} s")


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Knight Monitor")
        self.setWindowIcon(app_icon())
        self.resize(1180, 820)
        self.setMinimumSize(1000, 700)

        self.worker = None
        self.connected = False
        self.history = collections.deque(maxlen=6000)    # (counter, counts, imu)
        self.markers = self._load_markers()
        self.channel_labels = self._load_channels()
        self.live_markers = []                            # (t, label)
        self.filter_preset = "visualizer"
        self.window_secs = 4.0
        self.imu_window = None
        self._replay_window = None
        self._rec_dirty = False
        self._apply_dirty = False

        self.rec = None
        self._build_ui()

        self.tick = QtCore.QTimer(self)
        self.tick.timeout.connect(self._on_tick)
        self.tick.start(50)

        self.port_timer = QtCore.QTimer(self)
        self.port_timer.timeout.connect(self._refresh_ports)
        self.port_timer.start(1500)
        self._refresh_ports()

    # -- construction ------------------------------------------------------
    def _build_ui(self):
        central = QtWidgets.QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)

        body.addWidget(self._build_sidebar())

        pane = QtWidgets.QFrame()
        pane.setObjectName("pane")
        pl = QtWidgets.QVBoxLayout(pane)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.setSpacing(0)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        split.setHandleWidth(8)
        self.trace = LanesView()
        self.spectrum = SpectrumView()
        split.addWidget(self.trace)
        split.addWidget(self.spectrum)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 2)
        split.setSizes([520, 210])
        pl.addWidget(split)
        body.addWidget(pane, 1)

        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().showMessage("Ready")

    def _build_header(self):
        head = QtWidgets.QWidget()
        head.setFixedHeight(58)
        head.setStyleSheet(f"background:{PANEL}; border-bottom:1px solid {LINE_SOFT};")
        h = QtWidgets.QHBoxLayout(head)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(10)

        icon = QtWidgets.QLabel()
        icon.setPixmap(app_icon().pixmap(30, 30))
        icon.setFixedSize(30, 30)
        h.addWidget(icon)

        names = QtWidgets.QVBoxLayout()
        names.setSpacing(0)
        title = QtWidgets.QLabel("Knight Monitor")
        f = title.font()
        f.setPointSizeF(12.5)
        f.setBold(True)
        title.setFont(f)
        title.setStyleSheet("background:transparent;")
        sub = dim_label("8-channel EEG  ·  125 Hz  ·  raw counts", 8.3)
        names.addWidget(title)
        names.addWidget(sub)
        h.addLayout(names)
        h.addStretch(1)

        self.rec_pill = QtWidgets.QLabel("")
        self.rec_pill.setVisible(False)
        self.rec_pill.setStyleSheet(
            f"background:{REC}; color:white; border-radius:9px; padding:3px 10px;"
            f"font-family:{MONO_FONT}; font-size:11px; font-weight:bold;")
        h.addWidget(self.rec_pill)

        self.stat_label = QtWidgets.QLabel("")
        self.stat_label.setStyleSheet(
            f"color:{MUTED}; font-family:{MONO_FONT}; font-size:11px; background:transparent;")
        h.addWidget(self.stat_label)

        self.pill = QtWidgets.QLabel("offline")
        self.pill.setStyleSheet(
            f"background:{PANEL_2}; color:{DIM}; border-radius:9px; padding:3px 12px;"
            f"font-size:11px; font-weight:bold;")
        h.addWidget(self.pill)
        return head

    def _build_sidebar(self):
        side = QtWidgets.QWidget()
        side.setFixedWidth(282)
        side.setStyleSheet(f"background:{PANEL};")

        scroll = QtWidgets.QScrollArea(side)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")

        inner = QtWidgets.QWidget()
        inner.setStyleSheet(f"background:{PANEL};")
        v = QtWidgets.QVBoxLayout(inner)
        v.setContentsMargins(14, 12, 14, 10)
        v.setSpacing(6)

        # -- connection ----------------------------------------------------
        v.addWidget(section_label("Connection"))
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        self.port_combo = Combo()
        refresh = QtWidgets.QPushButton("⟳")
        refresh.setFixedSize(24, 24)
        refresh.setToolTip("Rescan serial ports")
        refresh.clicked.connect(self._refresh_ports)
        prow = QtWidgets.QHBoxLayout()
        prow.setSpacing(4)
        prow.addWidget(self.port_combo, 1)
        prow.addWidget(refresh)

        self.mode_combo = Combo()
        self.mode_combo.addItems(["Auto detect", "Knight Board (EEG only)", "Knight Board IMU"])
        self.mirror_combo = Combo()
        self.mirror_combo.addItem("Off")

        grid.addWidget(dim_label("Port"), 0, 0)
        grid.addLayout(prow, 0, 1)
        grid.addWidget(dim_label("Board"), 1, 0)
        grid.addWidget(self.mode_combo, 1, 1)
        grid.addWidget(dim_label("Mirror"), 2, 0)
        grid.addWidget(self.mirror_combo, 2, 1)
        v.addLayout(grid)

        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.setFixedHeight(34)
        self.connect_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.connect_btn.clicked.connect(self._toggle_connect)
        self._style_connect(False)
        v.addWidget(self.connect_btn)

        status_row = QtWidgets.QHBoxLayout()
        status_row.setSpacing(6)
        self.conn_dot = QtWidgets.QLabel("●")
        self.conn_dot.setStyleSheet(f"color:{MUTED}; font-size:8px;")
        self.conn_label = dim_label("Disconnected", 8.6)
        status_row.addWidget(self.conn_dot)
        status_row.addWidget(self.conn_label, 1)
        v.addLayout(status_row)

        link = QtWidgets.QLabel('<a href="mirror" style="color:#5b9bff; text-decoration:none;">'
                                'com0com mirror setup</a>')
        link.setTextFormat(QtCore.Qt.TextFormat.RichText)
        link.setStyleSheet("background:transparent; font-size:11px;")
        link.linkActivated.connect(self._mirror_help)
        v.addWidget(link)

        v.addWidget(hline())

        # -- session -------------------------------------------------------
        v.addWidget(section_label("Session"))
        modes = QtWidgets.QGridLayout()
        modes.setHorizontalSpacing(6)
        modes.setVerticalSpacing(6)
        self.imu_btn = QtWidgets.QPushButton("IMU Viewer")
        self.imu_btn.setFixedHeight(30)
        self.imu_btn.clicked.connect(self._open_imu)
        self.record_btn = QtWidgets.QPushButton("Record")
        self.record_btn.setFixedHeight(30)
        self.record_btn.clicked.connect(self._toggle_record)
        self.replay_btn = QtWidgets.QPushButton("Replay…")
        self.replay_btn.setFixedHeight(30)
        self.replay_btn.clicked.connect(self._open_replay)
        self.marker_btn = QtWidgets.QPushButton("Markers…")
        self.marker_btn.setFixedHeight(30)
        self.marker_btn.clicked.connect(self._edit_markers)
        modes.addWidget(self.imu_btn, 0, 0)
        modes.addWidget(self.record_btn, 0, 1)
        modes.addWidget(self.replay_btn, 1, 0)
        modes.addWidget(self.marker_btn, 1, 1)
        v.addLayout(modes)

        v.addWidget(hline())

        # -- channels ------------------------------------------------------
        v.addWidget(section_label("Channels"))
        rows = QtWidgets.QGridLayout()
        rows.setHorizontalSpacing(6)
        rows.setVerticalSpacing(3)
        rows.setColumnStretch(1, 1)
        for col, txt, align in ((0, "EN", QtCore.Qt.AlignmentFlag.AlignHCenter),
                                (1, "LABEL", QtCore.Qt.AlignmentFlag.AlignLeft),
                                (2, "GAIN", QtCore.Qt.AlignmentFlag.AlignHCenter),
                                (3, "RLD", QtCore.Qt.AlignmentFlag.AlignHCenter)):
            lab = dim_label(txt, 7.0)
            if col == 1:
                lab.setContentsMargins(12, 0, 0, 0)
            lab.setAlignment(align)
            rows.addWidget(lab, 0, col)
        self.ch_active = []
        self.ch_label = []
        self.ch_rld = []
        self.ch_gain = []
        self.ch_status = []
        for ch in range(1, 9):
            r = ch                      # header occupies row 0
            dot = QtWidgets.QLabel("")
            dot.setFixedSize(7, 7)
            dot.setStyleSheet(f"background:{MUTED}; border-radius:3px;")
            dot.setToolTip("Green: live  ·  red: all zeros  ·  grey: disabled")

            active = Chip("on")
            active.setChecked(True)
            active.setToolTip(f"Enable channel {ch} on the board")
            active.toggled.connect(self._apply_later)

            label = QtWidgets.QLineEdit(self.channel_labels[ch - 1])
            label.setPlaceholderText(f"ch{ch}")
            label.setMaxLength(12)
            label.setMinimumWidth(64)
            label.setFixedHeight(20)
            label.setToolTip("Name shown on the trace (e.g. CP3)")
            label.textChanged.connect(self._on_label_changed)
            label.editingFinished.connect(self._save_channels)

            gains = Combo()
            gains.setFixedWidth(52)
            gains.addItems([str(g) for g in kp.GAINS])
            gains.setCurrentText(str(kp.DEFAULT_GAIN))
            gains.currentTextChanged.connect(self._apply_later)

            rld = Chip("rld")
            rld.setToolTip(f"Add channel {ch} to the RLD reference")
            rld.toggled.connect(self._apply_later)

            cell = QtWidgets.QHBoxLayout()
            cell.setContentsMargins(0, 0, 0, 0)
            cell.setSpacing(5)
            cell.addWidget(dot)
            cell.addWidget(label, 1)

            rows.addWidget(active, r, 0)
            rows.addLayout(cell, r, 1)
            rows.addWidget(gains, r, 2)
            rows.addWidget(rld, r, 3)

            self.ch_active.append(active)
            self.ch_label.append(label)
            self.ch_rld.append(rld)
            self.ch_gain.append(gains)
            self.ch_status.append(dot)
        v.addLayout(rows)

        self.apply_btn = QtWidgets.QPushButton("Apply channel setup")
        self.apply_btn.setFixedHeight(30)
        self.apply_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.apply_btn.clicked.connect(self._apply_config)
        self._style_apply(False)
        v.addWidget(self.apply_btn)

        v.addWidget(hline())

        # -- display -------------------------------------------------------
        v.addWidget(section_label("Display"))
        dg = QtWidgets.QGridLayout()
        dg.setHorizontalSpacing(8)
        dg.setVerticalSpacing(6)
        dg.setColumnStretch(1, 1)

        self.filter_combo = Combo()
        self.filter_combo.addItems([name for name, _ in FILTER_PRESETS])
        self.filter_combo.setToolTip(
            "Visualizer: 5–40 Hz band-pass order 2, plus 48–52 and 58–62 Hz notches, "
            "zero phase.\nDisplay only — recordings always store raw counts.")
        self.filter_combo.currentIndexChanged.connect(self._set_filter)
        dg.addWidget(dim_label("Filter"), 0, 0)
        dg.addWidget(self.filter_combo, 0, 1)

        self.window_combo = Combo()
        self.window_combo.addItems(["1 s", "2 s", "4 s", "10 s", "30 s"])
        self.window_combo.setCurrentText("4 s")
        self.window_combo.currentTextChanged.connect(self._set_window)
        dg.addWidget(dim_label("Window"), 1, 0)
        dg.addWidget(self.window_combo, 1, 1)

        self.scale_combo = Combo()
        self.scale_combo.addItems(["Auto", "25", "50", "100", "200", "500"])
        self.scale_combo.currentTextChanged.connect(self._set_scale)
        dg.addWidget(dim_label("Scale µV"), 2, 0)
        dg.addWidget(self.scale_combo, 2, 1)
        v.addLayout(dg)

        v.addStretch(1)
        scroll.setWidget(inner)

        wrap = QtWidgets.QVBoxLayout(side)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(scroll)
        side.setStyleSheet(f"background:{PANEL}; border-right:1px solid {LINE_SOFT};")
        return side

    def _style_connect(self, connected):
        if connected:
            self.connect_btn.setStyleSheet(
                f"QPushButton {{ background:transparent; color:{BAD}; border:1px solid {BAD};"
                f" border-radius:6px; font-weight:bold; }}"
                f"QPushButton:hover {{ background:rgba(248,113,113,30); }}")
        else:
            self.connect_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:white; border:1px solid {ACCENT};"
                f" border-radius:6px; font-weight:bold; }}"
                f"QPushButton:hover {{ background:{ACCENT_HI}; }}"
                f"QPushButton:pressed {{ background:#2f6dd0; }}")

    def _style_apply(self, dirty):
        if dirty:
            self.apply_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:white; border:1px solid {ACCENT};"
                f" border-radius:5px; font-weight:bold; }}"
                f"QPushButton:hover {{ background:{ACCENT_HI}; }}")
        else:
            self.apply_btn.setStyleSheet(
                f"QPushButton {{ background:transparent; color:{DIM}; border:1px solid {LINE};"
                f" border-radius:5px; }}"
                f"QPushButton:hover {{ color:{TEXT}; border-color:{ACCENT}; }}")
        self._apply_dirty = dirty

    # -- marker / channel files -------------------------------------------
    def _load_markers(self):
        try:
            with open(MARKER_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return [m for m in data if m.get("label") and m.get("key")]
        except Exception:
            return []

    def _save_markers(self):
        with open(MARKER_FILE, "w", encoding="utf-8") as fh:
            json.dump(self.markers, fh, indent=2)

    def _load_channels(self):
        try:
            with open(CHANNEL_FILE, "r", encoding="utf-8") as fh:
                labels = json.load(fh).get("labels", [])
            labels = [str(x) for x in labels][:8]
            return labels + [""] * (8 - len(labels))
        except Exception:
            return [""] * 8

    def _save_channels(self):
        try:
            with open(CHANNEL_FILE, "w", encoding="utf-8") as fh:
                json.dump({"labels": [w.text().strip() for w in self.ch_label]}, fh, indent=2)
        except Exception:
            pass

    def _on_label_changed(self):
        self._draw()

    def _edit_markers(self):
        dlg = MarkerDialog(self.markers, self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.markers = dlg.markers()
            self._save_markers()
            self.statusBar().showMessage(f"{len(self.markers)} marker hotkeys", 3000)

    def keyPressEvent(self, event):
        combo = QtGui.QKeySequence(event.keyCombination()).toString()
        for m in self.markers:
            if m["key"] == combo:
                label = m["label"]
                if self.rec:
                    t = self._record_elapsed()
                    self.live_markers.append((t, label))
                    self.rec["markers"].append({"t": round(t, 3), "label": label})
                    self.statusBar().showMessage(f"marker '{label}' at {t:.2f}s", 1500)
                else:
                    self.statusBar().showMessage(f"'{label}' — not recording", 1500)
                return
        super().keyPressEvent(event)

    # -- ports / connection ------------------------------------------------
    def _refresh_ports(self):
        if not HAVE_SERIAL:
            return
        ports = [p.device for p in serial.tools.list_ports.comports()]
        for combo, extra in ((self.port_combo, []), (self.mirror_combo, ["Off"])):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(extra + ports)
            if current in extra + ports:
                combo.setCurrentText(current)
            combo.blockSignals(False)

    def _mode(self):
        return ["auto", "eeg", "imu"][self.mode_combo.currentIndex()]

    def _toggle_connect(self):
        if self.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        if not HAVE_SERIAL:
            QtWidgets.QMessageBox.critical(self, "pyserial missing",
                                           "Install it with:  pip install pyserial")
            return
        port = self.port_combo.currentText().strip()
        if not port:
            QtWidgets.QMessageBox.warning(self, "No port", "No serial port selected.")
            return
        mirror = self.mirror_combo.currentText()
        if mirror == "Off" or mirror == port:
            mirror = ""

        self.conn_label.setText("Connecting…")
        self.conn_label.setStyleSheet(f"color:{WARN}; background:transparent;")
        self.conn_dot.setStyleSheet(f"color:{WARN}; font-size:8px;")

        self.worker = SerialWorker(port, self._mode(), mirror)
        self.worker.connected_ok.connect(self._on_connected)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_connected(self, description):
        self.connected = True
        self.connect_btn.setText("Disconnect")
        self._style_connect(True)
        self.conn_label.setText(description)
        self.conn_label.setStyleSheet(f"color:{GOOD}; background:transparent;")
        self.conn_dot.setStyleSheet(f"color:{GOOD}; font-size:8px;")
        self.pill.setText("streaming")
        self.pill.setStyleSheet(
            f"background:rgba(74,222,128,28); color:{GOOD}; border-radius:9px;"
            f" padding:3px 12px; font-size:11px; font-weight:bold;")
        self.statusBar().showMessage(f"Connected: {description}")
        QtCore.QTimer.singleShot(1400, self._apply_config)

    def _on_failed(self, message):
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.stop()
            worker.wait(1500)
        self.connected = False
        self.connect_btn.setText("Connect")
        self._style_connect(False)
        self.conn_label.setText("Disconnected")
        self.conn_label.setStyleSheet(f"color:{DIM}; background:transparent;")
        self.conn_dot.setStyleSheet(f"color:{MUTED}; font-size:8px;")
        self.pill.setText("offline")
        self.pill.setStyleSheet(
            f"background:{PANEL_2}; color:{DIM}; border-radius:9px; padding:3px 12px;"
            f"font-size:11px; font-weight:bold;")
        for dot in self.ch_status:
            dot.setStyleSheet(f"background:{MUTED}; border-radius:3px;")
        QtWidgets.QMessageBox.critical(self, "Connection failed", message)

    def _disconnect(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait(2500)
            self.worker = None
        self.connected = False
        self.connect_btn.setText("Connect")
        self._style_connect(False)
        self.conn_label.setText("Disconnected")
        self.conn_label.setStyleSheet(f"color:{DIM}; background:transparent;")
        self.conn_dot.setStyleSheet(f"color:{MUTED}; font-size:8px;")
        self.pill.setText("offline")
        self.pill.setStyleSheet(
            f"background:{PANEL_2}; color:{DIM}; border-radius:9px; padding:3px 12px;"
            f"font-size:11px; font-weight:bold;")
        self.stat_label.setText("")
        for dot in self.ch_status:
            dot.setStyleSheet(f"background:{MUTED}; border-radius:3px;")

    def _apply_later(self):
        if not self.connected:
            return
        if not self._apply_dirty:
            self.apply_btn.setText("Apply channel setup  •")
            self._style_apply(True)

    def _apply_config(self):
        if not self.worker:
            return
        self.apply_btn.setText("Apply channel setup")
        self._style_apply(False)
        # 2 s after stream start, then ~1 s between channels, as the docs require.
        delay = 1.6
        for ch in range(1, 9):
            gain = int(self.ch_gain[ch - 1].currentText())
            if self.ch_active[ch - 1].isChecked():
                self.worker.send_command(kp.cmd_channel_on(ch, gain), delay_after=1.0)
                cmd = kp.cmd_rld_on(ch) if self.ch_rld[ch - 1].isChecked() else kp.cmd_rld_off(ch)
                self.worker.send_command(cmd, delay_after=0.5)
            else:
                self.worker.send_command(kp.cmd_channel_off(ch), delay_after=0.5)
        self.statusBar().showMessage(
            "Sending channel setup — the board applies one command at a time (~15 s)")

    # -- display config ----------------------------------------------------
    def _set_filter(self, index):
        self.filter_preset = FILTER_PRESETS[index][1]

    def _set_window(self, text):
        self.window_secs = float(text.split()[0])

    def _set_scale(self, text):
        if text == "Auto":
            self.trace.auto_scale = True
        else:
            self.trace.auto_scale = False
            self.trace.fixed_scale = float(text)

    # -- recording ---------------------------------------------------------
    def _toggle_record(self):
        if self.rec:
            self._finish_recording("stopped by user")
            return
        if not self.connected:
            QtWidgets.QMessageBox.warning(self, "Not connected", "Connect the board first.")
            return
        dlg = RecordDialog(self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        duration, folder = dlg.values()
        os.makedirs(folder, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(folder, f"knight_{stamp}.csv")

        gains = {str(i + 1): int(self.ch_gain[i].currentText()) for i in range(8)}
        # frame length is known from the stream itself; stats lag by up to 0.4 s
        detected = getattr(self.worker, "frame_length", None) if self.worker else None
        if detected is None and self.worker:
            detected = self.worker.stats.get("frame_bytes") or None
        has_imu = detected == kp.FRAME_IMU or bool(
            self.history and any(f[2] is not None for f in list(self.history)[-250:]))

        fh = open(path, "w", newline="", encoding="utf-8")
        writer = csv.writer(fh)
        header = ["counter", "t"] + [f"ch{i}" for i in range(1, 9)]
        if has_imu:
            header += list(kp.IMU_AXES)
        writer.writerow(header)
        fh.flush()

        self.rec = {
            "path": path, "fh": fh, "writer": writer, "rows": 0,
            "duration": float(duration), "started": time.monotonic(),
            "gains": gains, "imu": has_imu, "markers": [],
            "counters": [],
        }
        self.live_markers = []
        self.record_btn.setText("Stop recording")
        self.record_btn.setStyleSheet(
            f"QPushButton {{ background:{REC}; color:white; border:1px solid {REC};"
            f" border-radius:5px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:#f25f5f; }}")
        self.rec_pill.setVisible(True)
        self.statusBar().showMessage(f"Recording {duration}s → {path}")

    def _record_elapsed(self):
        return time.monotonic() - self.rec["started"] if self.rec else 0.0

    def _finish_recording(self, reason):
        rec = self.rec
        if not rec:
            return
        self.rec = None
        try:
            rec["fh"].flush()
            rec["fh"].close()
        except Exception:
            pass

        counters = rec["counters"]
        gaps = repeats = 0
        for a, b in zip(counters, counters[1:]):
            d = kp.counter_delta(a, b)
            if d == 0:
                repeats += 1
            elif d != 1:
                gaps += abs(d) - 1 if d > 0 else abs(d)

        meta = {
            "app": "knight_app",
            "created": _dt.datetime.now().isoformat(timespec="seconds"),
            "port": self.port_combo.currentText(),
            "baud": kp.BAUD,
            "frame_bytes": kp.FRAME_IMU if rec["imu"] else kp.FRAME_EEG,
            "sampling_rate": kp.SAMPLE_RATE,
            "gain": rec["gains"],
            "channel_labels": [w.text().strip() for w in self.ch_label],
            "channels_enabled": [i + 1 for i in range(8) if self.ch_active[i].isChecked()],
            "rld": [i + 1 for i in range(8) if self.ch_rld[i].isChecked()],
            "filter": {"preset": self.filter_preset, "display_only": True,
                       "spec": FILTER_SPEC[self.filter_preset]},
            "rows": rec["rows"],
            "duration_requested": rec["duration"],
            "duration_actual": round(rec["rows"] / kp.SAMPLE_RATE, 3),
            "counter_gaps": gaps,
            "counter_repeats": repeats,
            "ended_because": reason,
            "markers": rec["markers"],
        }
        sidecar = os.path.splitext(rec["path"])[0] + ".json"
        with open(sidecar, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)

        self.record_btn.setText("Record")
        self.record_btn.setStyleSheet("")
        self.rec_pill.setVisible(False)
        self.live_markers = []

        expected = int(rec["duration"] * kp.SAMPLE_RATE)
        msg = (f"{rec['rows']:,} rows in {rec['rows'] / kp.SAMPLE_RATE:.2f} s "
               f"(expected {expected:,} for {rec['duration']:.0f} s)\n"
               f"counter gaps: {gaps}   repeats: {repeats}\n\n{rec['path']}")
        self.statusBar().showMessage(f"saved {rec['rows']:,} rows")
        QtWidgets.QMessageBox.information(self, "Recording finished", msg)

    # -- IMU / replay ------------------------------------------------------
    def _open_imu(self):
        if self.imu_window is None:
            self.imu_window = ImuWindow()
        self.imu_window.show()
        self.imu_window.raise_()
        self.imu_window.activateWindow()

    def _open_replay(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open recording", RECORD_DIR, "Knight recordings (*.csv)")
        if not path:
            return
        try:
            win = ReplayWindow(path, self)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Cannot open", str(exc))
            return
        win.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        win.show()
        self._replay_window = win

    def _mirror_help(self):
        QtWidgets.QMessageBox.information(
            self, "com0com mirror",
            "Windows gives a COM port to one program at a time, so to watch the same\n"
            "signal in this app and the Visualizer, use a virtual port pair.\n\n"
            "1. Install com0com (signed driver, needs admin) and open Setup Command Prompt.\n"
            "2. Create a pair:   install PortName=COM10 PortName=COM11\n"
            "3. In this app choose the real board port (e.g. COM9) and set Mirror to COM10.\n"
            "4. Connect the Visualizer to COM11 at 115200.\n\n"
            "This app owns the board, mirrors the exact bytes to COM10, and forwards\n"
            "anything the Visualizer sends back to the board, so both stay in sync.")

    # -- per-tick ----------------------------------------------------------
    def _on_tick(self):
        new = []
        if self.worker:
            while self.worker.frames:
                try:
                    new.append(self.worker.frames.popleft())
                except IndexError:
                    break
        for item in new:
            self.history.append(item)
            if self.imu_window is not None and self.imu_window.isVisible() and item[2]:
                self.imu_window.push(item[2])
            if self.rec:
                self._write_row(item)

        self._update_stats()

        if self.rec:
            elapsed = self._record_elapsed()
            left = max(0.0, self.rec["duration"] - elapsed)
            self.rec_pill.setText(f"REC  {left:4.1f}s   {self.rec['rows']:,} rows")
            if left <= 0:
                self._finish_recording("duration reached")

        self._draw()
        if self.imu_window is not None and self.imu_window.isVisible():
            self.imu_window.refresh(self.window_secs)

    def _write_row(self, item):
        counter, counts, imu = item
        rec = self.rec
        row = [counter, round(rec["rows"] / kp.SAMPLE_RATE, 4)] + list(counts)
        if rec["imu"]:
            row += [round(v, 6) for v in imu] if imu else [0.0] * 9
        try:
            rec["writer"].writerow(row)
            rec["rows"] += 1
            rec["counters"].append(counter)
            if rec["rows"] % 125 == 0:
                rec["fh"].flush()
        except Exception as exc:
            self._finish_recording(f"write error: {exc}")

    def _update_stats(self):
        if not self.worker or not self.connected:
            return
        s = self.worker.stats
        fps = s.get("fps", 0.0)
        gaps, repeats = s.get("gaps", 0), s.get("repeats", 0)
        fbytes = s.get("frame_bytes") or 0
        board = {kp.FRAME_IMU: "IMU 57B", kp.FRAME_EEG: "EEG 21B"}.get(fbytes, "detecting…")

        if gaps or repeats:
            col = BAD
        elif fps < 100:
            col = WARN
        else:
            col = DIM
        self.stat_label.setStyleSheet(
            f"color:{col}; font-family:{MONO_FONT}; font-size:11px; background:transparent;")
        self.stat_label.setText(
            f"{fps:5.1f} fps   {board}   {s.get('total', 0):,} frames"
            f"   gaps {gaps}   repeats {repeats}")

        for ch in range(8):
            dot = self.ch_status[ch]
            recent = [abs(f[1][ch]) for f in list(self.history)[-25:]] if self.history else []
            if not self.ch_active[ch].isChecked():
                dot.setStyleSheet(f"background:{MUTED}; border-radius:3px;")
            elif not recent or max(recent) == 0:
                dot.setStyleSheet(f"background:{BAD}; border-radius:3px;")
            else:
                dot.setStyleSheet(f"background:{GOOD}; border-radius:3px;")

    def _draw(self):
        n = int(self.window_secs * kp.SAMPLE_RATE)
        hist = list(self.history)[-n:]
        if not hist:
            self.trace.set_lanes([], self.window_secs, [],
                                 "Connect the board to see live traces")
            self.spectrum.set_items([])
            return

        labels = [w.text().strip() for w in self.ch_label]
        lanes = []
        for ch in range(8):
            gain = int(self.ch_gain[ch].currentText())
            counts = np.array([f[1][ch] for f in hist], dtype=float)
            uv = counts * kp.uv_per_count(gain)
            filtered = apply_filter(uv, kp.SAMPLE_RATE, self.filter_preset)
            label = channel_name(labels, ch)
            sub = f"gain {gain}  ·  {kp.uv_per_count(gain):.3f} µV/count"
            if labels[ch]:
                sub = f"ch{ch + 1}  ·  " + sub
            lanes.append({
                "label": label,
                "sub": sub,
                "visible": True,
                "dim": not self.ch_active[ch].isChecked(),
                "curves": [(TRACE_COLORS[ch], filtered)],
            })
        marks = []
        if self.rec:
            now = self._record_elapsed()
            marks = [(now - t, label) for t, label in self.live_markers]
        self.trace.set_lanes(lanes, self.window_secs, marks)

        items = []
        if hist:
            for ch in range(8):
                if not self.ch_active[ch].isChecked():
                    continue
                gain = int(self.ch_gain[ch].currentText())
                counts = np.array([f[1][ch] for f in hist], dtype=float)
                uv = apply_filter(counts * kp.uv_per_count(gain), kp.SAMPLE_RATE, self.filter_preset)
                if uv.size >= 128:
                    freqs, psd = sps.welch(uv, fs=kp.SAMPLE_RATE, nperseg=128, noverlap=64,
                                           window="blackmanharris", detrend=False)
                    items.append((TRACE_COLORS[ch], f"ch{ch + 1}", freqs,
                                  10.0 * np.log10(np.maximum(psd, 1e-20))))
        self.spectrum.set_items(items)

    def closeEvent(self, event):
        self._save_channels()
        if self.rec:
            self._finish_recording("app closed")
        if self.worker:
            self.worker.stop()
            self.worker.wait(2000)
        super().closeEvent(event)


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------

def _palette():
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(PANEL))
    pal.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(TEXT))
    pal.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(BG))
    pal.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(PANEL))
    pal.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(TEXT))
    pal.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(PANEL_2))
    pal.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(TEXT))
    pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(ACCENT))
    pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("white"))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(PANEL_2))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(TEXT))
    pal.setColor(QtGui.QPalette.ColorRole.PlaceholderText, QtGui.QColor(MUTED))
    return pal


STYLE = f"""
QWidget {{ background:{PANEL}; color:{TEXT}; }}
QWidget#central {{ background:{PANEL}; }}
QFrame#pane {{ background:{BG}; border:none; }}
QFrame#pane QSplitter::handle {{ background:{BG}; }}
QLabel {{ background:transparent; }}
QPushButton {{
    background:{PANEL_2}; color:{TEXT};
    border:1px solid {LINE}; border-radius:5px;
    padding:4px 10px;
}}
QPushButton:hover {{ background:{PANEL_3}; border-color:#2a3646; }}
QPushButton:pressed {{ background:#0f151c; }}
QComboBox, QSpinBox, QLineEdit, QKeySequenceEdit {{
    background:{PANEL_2}; color:{TEXT};
    border:1px solid {LINE}; border-radius:4px;
    padding:2px 6px; selection-background-color:{ACCENT};
}}
QComboBox:hover, QSpinBox:hover, QLineEdit:hover, QKeySequenceEdit:hover {{
    border-color:#2c3948;
}}
QComboBox:focus, QSpinBox:focus, QLineEdit:focus, QKeySequenceEdit:focus {{
    border-color:{ACCENT};
}}
QComboBox::drop-down {{ border:none; width:18px; }}
QComboBox::down-arrow {{ image:none; width:0px; height:0px; }}
QComboBox QAbstractItemView {{
    background:{PANEL_2}; color:{TEXT};
    border:1px solid {LINE}; outline:0;
    selection-background-color:{ACCENT}; selection-color:white;
    padding:2px;
}}
QLineEdit {{ background:transparent; border:none; border-bottom:1px solid {LINE};
             border-radius:0; padding:1px 2px; }}
QLineEdit:focus {{ border-bottom:1px solid {ACCENT}; }}
QLineEdit:hover {{ border-bottom:1px solid #2c3948; }}
QSpinBox::up-button, QSpinBox::down-button {{ width:14px; background:{PANEL_3};
    border:none; border-radius:2px; }}
QPushButton:disabled {{ color:{MUTED}; }}
QSlider::groove:horizontal {{ height:3px; background:{LINE}; border-radius:2px; }}
QSlider::handle:horizontal {{ background:{ACCENT}; width:12px; height:12px;
    margin:-5px 0; border-radius:6px; }}
QSlider::sub-page:horizontal {{ background:{ACCENT}; border-radius:2px; }}
QScrollArea {{ border:none; }}
QScrollBar:vertical {{ background:transparent; width:8px; margin:0; }}
QScrollBar::handle:vertical {{ background:#243040; border-radius:4px; min-height:24px; }}
QScrollBar::handle:vertical:hover {{ background:#2e3c4f; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:none; }}
QTableWidget {{ background:{BG}; alternate-background-color:{PANEL};
    gridline-color:{LINE_SOFT}; border:1px solid {LINE}; border-radius:4px; }}
QTableWidget::item {{ padding:3px; }}
QTableWidget::item:selected {{ background:{ACCENT}; color:white; }}
QHeaderView::section {{ background:{PANEL_2}; color:{DIM};
    border:none; border-bottom:1px solid {LINE}; padding:4px 6px; }}
QStatusBar {{ background:{PANEL}; color:{MUTED}; border-top:1px solid {LINE_SOFT};
    font-size:11px; }}
QStatusBar::item {{ border:none; }}
QMessageBox, QDialog {{ background:{PANEL}; }}
QDialog QLabel {{ color:{TEXT}; }}
QToolTip {{ background:{PANEL_2}; color:{TEXT}; border:1px solid {LINE}; padding:4px; }}
QSplitter::handle {{ background:{BG}; }}
"""


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Knight Monitor")
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    app.setPalette(_palette())
    app.setFont(QtGui.QFont(UI_FONT, 9))
    app.setStyleSheet(STYLE)

    win = MainWindow()
    win.show()

    if "--selftest" in sys.argv:
        out = os.environ.get("KNIGHT_SMOKE") or os.path.join(
            os.environ.get("TEMP", "."), "knight_app_smoke.png")
        QtCore.QTimer.singleShot(700, lambda: (win.grab().save(out), app.quit()))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
