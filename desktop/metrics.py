"""Native analytics overlays sharing the main feed; no web views or exchange clients."""
import copy
import math

from PySide6.QtCore import QByteArray, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QScrollArea,
                               QSizeGrip, QSlider, QSpinBox, QVBoxLayout, QWidget)

from desktop.overlay import Header, MONO, fmt_qty, fmt_usd, split_key
from desktop.theme import THEMES

NAMES = {"delta":"Вклад бирж в дельту", "oi":"Открытый интерес", "cvd":"CVD: спот и фьючерсы"}
WINDOWS = {"1m":"1м", "5m":"5м", "15m":"15м", "1h":"1ч"}


class MetricView(QWidget):
    def __init__(self, overlay):
        super().__init__(overlay)
        self.overlay = overlay
        self.rows = []

    def refresh(self):
        ov = self.overlay
        snap = ov.snapshot or {}
        if ov.kind == "delta":
            self.rows = sorted(snap.get("delta", {}).get("venues", {}).get(ov.settings["window"], []),
                               key=lambda r: -abs(r["delta"]))
        elif ov.kind == "oi":
            self.rows = snap.get("oi", {}).get("venues", [])
        font = QFont()
        font.setFamilies(MONO)
        font.setPointSizeF(ov.settings["font_size"])
        height = QFontMetrics(font).height()
        self.setMinimumHeight(max(190, height * 5) if ov.kind == "cvd" else max(90, height + 32 + len(self.rows) * (height + 14)))
        self.update()

    def paintEvent(self, event):
        ov = self.overlay
        c = THEMES[ov.theme]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setFamilies(MONO)
        font.setPointSizeF(ov.settings["font_size"])
        p.setFont(font)
        fm = QFontMetrics(font)
        lh = fm.height() + 6
        w = self.width()
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        def text(x, y, width, label, color="text", align=left):
            p.setPen(QColor(c[color]))
            label = fm.elidedText(str(label), Qt.TextElideMode.ElideRight, max(1, int(width)))
            p.drawText(QRectF(x, y, width, lh), align, label)

        if not ov.connected:
            text(10, 16, w - 20, "Нет связи с сервером", "muted")
            return
        if not ov.snapshot:
            text(10, 16, w - 20, "Ждём данные основной монеты", "muted")
            return
        snap = ov.snapshot
        if ov.kind == "cvd":
            self._cvd(p, c, lh, text)
            return
        if ov.kind == "oi":
            total = snap.get("oi", {}).get("totals", {})
            unit = ov.settings["unit"]
            fmt = fmt_usd if unit == "usd" else fmt_qty
            value = fmt(total[unit]) if total.get("count") and total.get(unit) is not None else "—"
            text(10, 4, w * .65, f"Всего {value}")
            text(w * .65, 4, w * .35 - 10, f"{total.get('count', 0)} бирж", "muted", right)
        else:
            delta = sum(r["delta"] for r in self.rows)
            text(10, 4, w - 20, f"Δ {WINDOWS[ov.settings['window']]}  {'+' if delta > 0 else '−' if delta < 0 else ''}{fmt_usd(abs(delta))}", "buy" if delta >= 0 else "sell")
        y = lh + 13
        if not self.rows:
            text(10, y, w - 20, "Нет доступных данных", "muted")
            return
        max_delta = max(1, *(abs(r["delta"]) for r in self.rows)) if ov.kind == "delta" else 1
        # QScrollArea only repaints exposed rows, even for coins listed on many venues.
        exposed = event.rect()
        for r in self.rows:
            height = lh + 8
            if y + height < exposed.top():
                y += height
                continue
            if y > exposed.bottom():
                break
            venue, kind = split_key(r["key"])
            text(10, y, w * .43 - 12, venue + (" F" if kind == "perp" else " S"))
            if ov.kind == "delta":
                delta = r["delta"]
                color = "buy" if delta >= 0 else "sell"
                tint = QColor(c[color]); tint.setAlpha(38)
                center = w * .55
                bar = (w * .44 - 12) * abs(delta) / max_delta
                p.fillRect(QRectF(center if delta >= 0 else center - bar, y + lh - 3, bar, 3), tint)
                text(w * .43, y, w * .57 - 10, ("+" if delta > 0 else "−" if delta < 0 else "") + fmt_usd(abs(delta)), color, right)
            else:
                value = r.get(ov.settings["unit"])
                text(w * .43, y, w * .34, fmt(value) if value is not None else "устарело", "text" if value is not None else "muted", right)
                change = r.get("pct", {}).get(ov.settings["window"])
                text(w * .77, y, w * .23 - 10, f"{change:+.2f}%" if change is not None else "—",
                     "buy" if change is not None and change > 0 else "sell" if change is not None and change < 0 else "muted", right)
            y += height

    def _cvd(self, p, c, lh, text):
        points = self.overlay.snapshot.get("cvd") or []
        if not points:
            text(10, 12, self.width() - 20, "Ждём CVD", "muted")
            return
        last = points[-1]
        w, h = self.width(), self.height()
        for i, (name, color) in enumerate((("Спот", "spot"), ("Фьючерсы", "perp")), start=1):
            value = last[i]
            text(10, (i - 1) * lh, w - 20, f"{name}: {'+' if value > 0 else '−' if value < 0 else ''}{fmt_usd(abs(value))}", color)
        seconds = self.overlay.settings["seconds"]
        end = last[0]
        rows = [r for r in points if r[0] >= end - seconds]
        if len(rows) < 2:
            return
        top, bottom, left, right = lh * 2 + 12, h - 25, 10, w - 70
        if bottom <= top or right <= left:
            return
        lo = min(0, *(r[i] for r in rows for i in (1, 2)))
        hi = max(0, *(r[i] for r in rows for i in (1, 2)))
        pad = max((hi - lo) * .06, 1)
        lo -= pad; hi += pad
        xs = lambda t: left + (t - rows[0][0]) / max(1, end - rows[0][0]) * (right - left)
        ys = lambda v: bottom - (v - lo) / (hi - lo) * (bottom - top)
        p.setPen(QPen(QColor(c["line"]), 1))
        for value in (lo, (lo + hi) / 2, hi):
            y = ys(value)
            p.drawLine(int(left), int(y), int(right), int(y))
            text(right + 5, y - lh / 2, w - right - 8, fmt_usd(value), "muted")
        for index, color in ((1, "spot"), (2, "perp")):
            path = QPainterPath()
            previous = None
            for row in rows:
                if previous is None or row[0] - previous > 90:
                    path.moveTo(xs(row[0]), ys(row[index]))
                else:
                    path.lineTo(xs(row[0]), ys(row[index]))
                previous = row[0]
            p.setPen(QPen(QColor(c[color]), 1.6))
            p.drawPath(path)
        text(10, h - 24, w - 20, "CVD с момента выбора монеты · USD", "muted")


class MetricOverlay(QWidget):
    settings_requested = Signal()
    lock_requested = Signal()
    hide_requested = Signal()
    coin_requested = Signal()
    changed = Signal()

    def __init__(self, kind: str, saved: dict, theme: str):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.kind = kind
        self.theme = theme if theme in THEMES else "dark"
        self.snapshot = None
        self.connected = False
        self.locked = False
        self.coin = ""
        self.settings = {"opacity":.92, "font_size":10, "window":"5m", "unit":"usd", "seconds":3600}
        for key, default in list(self.settings.items()):
            value = saved.get(key)
            if isinstance(value, type(default)) and (not isinstance(value, float) or math.isfinite(value)):
                self.settings[key] = value
        self.settings["opacity"] = min(1, max(.1, self.settings["opacity"]))
        self.settings["font_size"] = min(24, max(8, self.settings["font_size"]))
        if self.settings["window"] not in WINDOWS: self.settings["window"] = "5m"
        if self.settings["unit"] not in ("coins", "usd"): self.settings["unit"] = "usd"
        if self.settings["seconds"] not in (900,3600,14400,86400): self.settings["seconds"] = 3600
        self.setWindowTitle(NAMES[kind])
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(280, 120)
        self.resize(420, 300)
        self.header = Header(self)
        self.header.flow.hide()
        self.header.info.setToolTip(NAMES[kind])
        self.view = MetricView(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self.view)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.view.setAutoFillBackground(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(self.scroll, 1)
        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(14, 14)
        self.settings_requested.connect(self.configure)
        self.apply_theme(theme)

    def set_coin(self, coin):
        if coin != self.coin:
            self.coin, self.snapshot = coin, None
            self.header.coin.setText(coin or "—")
            self.view.refresh()

    def set_snapshot(self, snapshot):
        if snapshot.get("coin") != self.coin:
            return
        self.snapshot = snapshot
        self.view.refresh()

    def set_connected(self, ok):
        self.connected = ok
        self.view.update()

    def apply_theme(self, theme):
        self.theme = theme if theme in THEMES else "dark"
        self.header.apply_theme()
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget {background:transparent;border:0;}")
        self.header.info.setText(NAMES[self.kind])
        self.view.refresh()
        self.update()

    def set_locked(self, locked):
        if self.locked == locked: return
        self.locked = locked
        visible, geometry = self.isVisible(), self.geometry()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, locked)
        self.setGeometry(geometry)
        for button in self.header.buttons: button.setVisible(not locked)
        self.header.lock_mark.setVisible(locked)
        self.grip.setVisible(not locked)
        if visible: self.show()

    def configure(self):
        dlg = QDialog(self, Qt.WindowType.WindowStaysOnTopHint)
        dlg.setWindowTitle(NAMES[self.kind])
        layout = QFormLayout(dlg)
        opacity = QSlider(Qt.Orientation.Horizontal, minimum=10, maximum=100, value=int(self.settings["opacity"] * 100))
        font = QSpinBox(minimum=8, maximum=24, value=self.settings["font_size"])
        window = QComboBox()
        for key, label in WINDOWS.items(): window.addItem(label, key)
        window.setCurrentIndex(window.findData(self.settings["window"]))
        unit = QComboBox()
        unit.addItem("USD", "usd"); unit.addItem("Монеты", "coins")
        unit.setCurrentIndex(unit.findData(self.settings["unit"]))
        seconds = QComboBox()
        for value, label in ((900,"15 минут"),(3600,"1 час"),(14400,"4 часа"),(86400,"24 часа")): seconds.addItem(label, value)
        seconds.setCurrentIndex(seconds.findData(self.settings["seconds"]))
        layout.addRow("Непрозрачность", opacity); layout.addRow("Размер шрифта", font)
        if self.kind != "cvd": layout.addRow("Окно изменения", window)
        if self.kind == "oi": layout.addRow("Единицы OI", unit)
        if self.kind == "cvd": layout.addRow("История на графике", seconds)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
        layout.addRow(buttons)
        if dlg.exec():
            self.settings.update(opacity=opacity.value()/100, font_size=font.value(), window=window.currentData(),
                                 unit=unit.currentData(), seconds=seconds.currentData())
            self.view.refresh(); self.update(); self.changed.emit()

    def save(self):
        return {**copy.deepcopy(self.settings), "geometry":self.saveGeometry().toHex().data().decode(), "visible":self.isVisible()}

    def restore(self, geometry):
        return bool(geometry) and self.restoreGeometry(QByteArray.fromHex(str(geometry).encode()))

    def resizeEvent(self, event):
        self.grip.move(self.width() - 14, self.height() - 14)
        super().resizeEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide_requested.emit()

    def paintEvent(self, _event):
        c = THEMES[self.theme]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(c["panel"]); bg.setAlphaF(self.settings["opacity"])
        p.setBrush(bg); p.setPen(QPen(QColor(c["line"]), 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5), 6, 6)
