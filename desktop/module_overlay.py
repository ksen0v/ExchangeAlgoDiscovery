"""A workspace module over the terminal, with an adjustable background and reachable controls."""
import copy
import json
import math

from PySide6.QtCore import QByteArray, QPoint, QRect, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                              QMenu, QSizeGrip, QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget)

TYPES = {"tape", "streams", "cvd", "delta", "oi", "oiChart", "walls", "alerts"}
NAMES = {"tape":"Лента сделок", "streams":"Биржи", "cvd":"CVD", "delta":"Вклад в дельту",
         "oi":"Открытый интерес", "oiChart":"График OI", "walls":"Плиты", "alerts":"Алерты"}


def overlay_request(raw: str) -> dict:
    if len(raw) > 128 * 1024:
        raise ValueError("Слишком большие настройки модуля")
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("coin"), str):
        raise ValueError("Неверные настройки оверлея")
    coin = value["coin"].strip().upper()
    if not coin.isascii() or not coin.isalnum() or len(coin) > 20:
        raise ValueError("Неверный инструмент")
    from app.workspaces import Module
    spec = Module.model_validate(value["module"]).model_dump(mode="json")
    return {"coin": coin, "module": spec}


def background_opacity(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.25
    return min(0.9, max(0.0, float(value)))


class OverlayHeader(QWidget):
    """A separate input window keeps controls usable while the content passes clicks through."""
    def __init__(self, overlay, floating=False):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint) if floating else Qt.WindowType.Widget
        super().__init__(overlay, flags)
        self.overlay, self.floating, self.drag = overlay, floating, None
        self.setObjectName("moduleOverlayHeader")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar = QHBoxLayout(self)
        bar.setContentsMargins(10, 3, 4, 3)
        record = overlay.record
        self.label = QLabel(f"{record['coin']} · {record['module'].get('title') or NAMES[record['module']['type']]}")
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        bar.addWidget(self.label)
        bar.addStretch()
        self.lock_button = QToolButton()
        self.lock_button.clicked.connect(lambda: overlay.set_locked(not overlay.record["locked"]))
        bar.addWidget(self.lock_button)
        self.menu_button = QToolButton()
        self.menu_button.setText("⋯")
        self.menu_button.setAccessibleName("Меню оверлея")
        self.menu_button.clicked.connect(overlay.menu)
        bar.addWidget(self.menu_button)
        close = QToolButton()
        close.setText("×")
        close.setAccessibleName("Закрыть оверлей")
        close.clicked.connect(overlay.close)
        bar.addWidget(close)
        self.refresh()

    def refresh(self):
        locked = self.overlay.record["locked"]
        self.lock_button.setText("🔓" if locked else "🔒")
        self.lock_button.setAccessibleName("Выключить сквозные клики" if locked else "Включить сквозные клики")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag = event.globalPosition().toPoint() - self.overlay.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.overlay.move(event.globalPosition().toPoint() - self.drag)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag = None
        event.accept()


class ModuleOverlay(QWidget):
    def __init__(self, ctl, key: str, record: dict):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.ctl, self.key, self.record = ctl, key, record
        self.web, self.locked_header = None, None
        self.record["locked"] = record.get("locked") is True
        self.record["transparent"] = record.get("transparent") is True or self.record["locked"]
        self.record["background_opacity"] = background_opacity(record.get("background_opacity", 0.25))
        self._theme = ctl.prefs.theme
        # Keep the native alpha surface for both modes; switching never recreates the page.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowIcon(ctl.icon)
        self.setMinimumSize(240, 160)
        self.resize(480, 330)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(1, 1, 1, 1)
        self.layout.setSpacing(0)
        self.header = OverlayHeader(self)
        self.label, self.menu_button = self.header.label, self.header.menu_button
        self.layout.addWidget(self.header)
        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(12, 12)
        self.apply_theme(ctl.prefs.theme)
        geometry = record.get("geometry", "")
        restored = isinstance(geometry, str) and bool(geometry) and self.restoreGeometry(QByteArray.fromHex(geometry.encode()))
        from desktop.app import on_screen
        if not restored or not on_screen(self.geometry()):
            available = ctl.app.primaryScreen().availableGeometry()
            self.resize(min(480, available.width()), min(330, available.height()))
            offset = len(getattr(ctl, "module_overlays", {})) % 6 * 24
            self.move(available.left() + min(30 + offset, max(0, available.width() - self.width())),
                      available.top() + min(60 + offset, max(0, available.height() - self.height())))
        self.set_locked(self.record["locked"], persist=False)

    def _sync_locked_header(self):
        if not self.isVisible() or not self.record["locked"]:
            if self.locked_header:
                self.locked_header.hide()
            return
        if self.locked_header is None:
            self.locked_header = OverlayHeader(self, floating=True)
        self.locked_header.refresh()
        self.locked_header.setGeometry(QRect(self.header.mapToGlobal(QPoint()), self.header.size()))
        self.locked_header.show()
        self.locked_header.raise_()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_locked_header)

    def hideEvent(self, event):
        if self.locked_header:
            self.locked_header.hide()
        super().hideEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        self._sync_locked_header()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "grip"):
            self.grip.move(self.width() - self.grip.width(), self.height() - self.grip.height())
            self.grip.raise_()
        QTimer.singleShot(0, self._sync_locked_header)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        color = Qt.GlobalColor.transparent if self.record["transparent"] else QColor("#181a1d" if self._theme == "dark" else "#fafbfd")
        painter.fillRect(self.rect(), color)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setPen(QColor("#32363b" if self._theme == "dark" else "#d3d9e2"))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def show_overlay(self):
        if self.web is None:
            from desktop.app import DashboardPage
            self.web = QWebEngineView()
            self.web.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.web.setPage(DashboardPage(self.ctl.main.profile, self.web, self.ctl, overlay=self))
            self.layout.addWidget(self.web, 1)
            self.web.load(QUrl(self.ctl.web_url(desktop=True)))
        self.record["visible"] = True
        self.show()
        self.raise_()
        self.grip.raise_()
        self._sync_locked_header()
        self.ctl.overlay_state_changed()

    def closeEvent(self, event):
        self.record["geometry"] = self.saveGeometry().toHex().data().decode()
        self.record["visible"] = False
        self.hide()
        if self.web is not None:
            page = self.web.page()
            self.layout.removeWidget(self.web)
            self.web.stop()
            self.web.hide()
            page.setWebChannel(None)
            self.web.deleteLater()
            page.deleteLater()
            self.web = None
        self.ctl.overlay_state_changed()
        event.accept()

    def set_locked(self, locked: bool, persist=True):
        visible, geometry = self.isVisible(), self.geometry()
        self.record["locked"] = bool(locked)
        if locked:
            self.record["transparent"] = True
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, bool(locked))
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, bool(locked))
        self.setGeometry(geometry)
        self.grip.setVisible(not locked)
        if visible:
            self.show()
        self.header.refresh()
        self._refresh_appearance()
        self._sync_locked_header()
        if persist:
            self.ctl.overlay_state_changed()

    def appearance(self):
        return {"transparent": self.record["transparent"], "background_opacity": self.record["background_opacity"]}

    def _refresh_appearance(self):
        self.update()
        if self.web:
            self.web.page().bridge.overlayAppearanceChanged.emit(json.dumps(self.appearance()))

    def set_appearance(self, transparent: bool, opacity: float, persist=True):
        self.record["transparent"] = bool(transparent)
        self.record["background_opacity"] = background_opacity(opacity)
        if not transparent and self.record["locked"]:
            self.set_locked(False, persist=False)
        self._refresh_appearance()
        if persist:
            self.ctl.overlay_state_changed()

    def apply_theme(self, theme):
        self._theme = theme
        bg, fg = ("#181a1d", "#e4e6ea") if theme == "dark" else ("#fafbfd", "#26334a")
        self.setStyleSheet(f"#moduleOverlayHeader {{background:{bg};border:0;}} QLabel,QToolButton {{color:{fg};background:transparent;}} QToolButton {{border:0;padding:3px 7px;}}")
        self.update()
        if self.web:
            self.web.page().bridge.themeChanged.emit(theme)

    def menu(self):
        menu = QMenu(self)
        menu.addAction("Настройки оверлея…", self.open_settings)
        menu.addSeparator()
        action = menu.addAction("Сквозные клики")
        action.setCheckable(True)
        action.setChecked(self.record["locked"])
        action.toggled.connect(self.set_locked)
        menu.addAction("Открыть рабочий стол", self.ctl.show_main)
        menu.addSeparator()
        menu.addAction("Закрыть оверлей", self.close)
        menu.exec(QCursor.pos())

    def open_settings(self):
        original = {**self.appearance(), "locked": self.record["locked"]}
        dialog = QDialog(self, Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
        dialog.setWindowTitle("Настройки оверлея")
        dialog.setMinimumWidth(360)
        form = QVBoxLayout(dialog)
        form.setSpacing(12)
        transparent = QCheckBox("Прозрачный фон")
        transparent.setChecked(self.record["transparent"])
        form.addWidget(transparent)
        value = QLabel()
        form.addWidget(value)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 90)
        slider.setValue(round(self.record["background_opacity"] * 100))
        slider.setAccessibleName("Плотность фона в процентах")
        form.addWidget(slider)
        hint = QLabel("Текст остаётся чётким. Непрозрачный фон отключает сквозные клики.")
        hint.setWordWrap(True)
        form.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Готово")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addWidget(buttons)
        def preview():
            enabled = transparent.isChecked()
            slider.setEnabled(enabled)
            value.setText(f"Плотность фона: {slider.value()}%" if enabled else "Непрозрачный фон")
            self.set_appearance(enabled, slider.value() / 100, persist=False)
        transparent.toggled.connect(preview)
        slider.valueChanged.connect(preview)
        preview()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.set_appearance(original["transparent"], original["background_opacity"], persist=False)
            self.set_locked(original["locked"], persist=False)
        self.ctl.overlay_state_changed()
        dialog.deleteLater()

    def save_module(self, raw: str):
        value = overlay_request(json.dumps({"coin": self.record["coin"], "module": json.loads(raw)}))
        if value["module"]["type"] != self.record["module"]["type"] or value["module"]["id"] != self.record["module"]["id"]:
            raise ValueError("Неверный модуль оверлея")
        self.record["module"] = copy.deepcopy(value["module"])
        self.ctl.save_prefs()

    def save(self):
        self.record["geometry"] = self.saveGeometry().toHex().data().decode()
        return copy.deepcopy(self.record)
