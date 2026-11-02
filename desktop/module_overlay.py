"""A workspace module in a reusable always-on-top window, with its own fixed instrument."""
import copy
import json

from PySide6.QtCore import QByteArray, QEvent, Qt, QUrl
from PySide6.QtGui import QCursor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSizeGrip, QToolButton, QVBoxLayout, QWidget

TYPES = {"tape", "streams", "cvd", "delta", "oi", "oiChart", "walls", "alerts"}
NAMES = {"tape":"Лента сделок", "streams":"Биржи", "cvd":"CVD", "delta":"Вклад в дельту",
         "oi":"Открытый интерес", "oiChart":"График OI", "walls":"Плиты", "alerts":"Алерты"}


def overlay_request(raw: str) -> dict:
    if len(raw) > 128 * 1024:
        raise ValueError("Слишком большие настройки модуля")
    value = json.loads(raw)
    coin = value["coin"].strip().upper()
    if not coin.isascii() or not coin.isalnum() or len(coin) > 20:
        raise ValueError("Неверный инструмент")
    # Reuse the workspace schema; keep the desktop startup free of server-config imports.
    from app.workspaces import Module
    spec = Module.model_validate(value["module"]).model_dump(mode="json")
    return {"coin": coin, "module": spec}


class ModuleOverlay(QWidget):
    def __init__(self, ctl, key: str, record: dict):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.ctl, self.key, self.record = ctl, key, record
        self.web = None
        self.setWindowIcon(ctl.icon)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(240, 160)
        self.resize(480, 330)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(1, 1, 1, 1)
        self.layout.setSpacing(0)
        self.header = QWidget()
        self.header.setObjectName("moduleOverlayHeader")
        bar = QHBoxLayout(self.header)
        bar.setContentsMargins(10, 3, 4, 3)
        self.label = QLabel(f"{record['coin']} · {record['module'].get('title') or NAMES[record['module']['type']]}")
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        bar.addWidget(self.label)
        bar.addStretch()
        self.menu_button = QToolButton()
        self.menu_button.setText("⋯")
        self.menu_button.setAccessibleName("Меню оверлея")
        self.menu_button.clicked.connect(self.menu)
        bar.addWidget(self.menu_button)
        close = QToolButton()
        close.setText("×")
        close.setAccessibleName("Закрыть оверлей")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        self.layout.addWidget(self.header)
        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(12, 12)
        self.header.installEventFilter(self)
        self.apply_theme(ctl.prefs.theme)
        geometry = record.get("geometry", "")
        if isinstance(geometry, str) and geometry:
            self.restoreGeometry(QByteArray.fromHex(geometry.encode()))
        from desktop.app import on_screen
        if not on_screen(self.geometry()):
            available = ctl.app.primaryScreen().availableGeometry()
            self.resize(min(480, available.width()), min(330, available.height()))
            self.move(available.left() + 30, available.top() + 60)
        self.set_locked(bool(record.get("locked")), persist=False)

    def eventFilter(self, obj, event):
        if obj is self.header and event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            if self.windowHandle():
                self.windowHandle().startSystemMove()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.grip.move(self.width() - self.grip.width(), self.height() - self.grip.height())
        self.grip.raise_()

    def show_overlay(self):
        if self.web is None:
            from desktop.app import DashboardPage
            self.web = QWebEngineView()
            self.web.setPage(DashboardPage(self.ctl.main.profile, self.web, self.ctl, overlay=self))
            self.layout.addWidget(self.web, 1)
            self.web.load(QUrl(self.ctl.web_url(desktop=True)))
        self.record["visible"] = True
        self.show()
        self.raise_()
        self.grip.raise_()
        self.ctl.overlay_state_changed()

    def closeEvent(self, event):
        self.record["geometry"] = self.saveGeometry().toHex().data().decode()
        self.record["visible"] = False
        if self.web is not None:
            # Closing releases the WS subscription, chart timers and the page, rather than hiding work.
            page = self.web.page()
            self.layout.removeWidget(self.web)
            self.web.stop()
            self.web.hide()
            self.web.page().setWebChannel(None)
            self.web.deleteLater()
            page.deleteLater()
            self.web = None
        self.ctl.overlay_state_changed()
        event.accept()

    def set_locked(self, locked: bool, persist=True):
        visible, geometry = self.isVisible(), self.geometry()
        self.record["locked"] = bool(locked)
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, bool(locked))
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, bool(locked))
        self.setGeometry(geometry)
        self.grip.setVisible(not locked)
        if visible:
            self.show()
        if persist:
            self.ctl.overlay_state_changed()

    def apply_theme(self, theme):
        bg, fg, border = ("#181a1d", "#e4e6ea", "#32363b") if theme == "dark" else ("#fafbfd", "#26334a", "#d3d9e2")
        self.setStyleSheet(f"ModuleOverlay {{background:{bg};border:1px solid {border};}} #moduleOverlayHeader {{background:{bg};}} QLabel,QToolButton {{color:{fg};background:transparent;}} QToolButton {{border:0;padding:3px 7px;}}")
        if self.web:
            self.web.page().bridge.themeChanged.emit(theme)

    def menu(self):
        menu = QMenu(self)
        action = menu.addAction("Сквозные клики")
        action.setCheckable(True)
        action.setChecked(bool(self.record.get("locked")))
        action.toggled.connect(self.set_locked)
        menu.addAction("Открыть рабочий стол", self.ctl.show_main)
        menu.addSeparator()
        menu.addAction("Закрыть оверлей", self.close)
        menu.exec(QCursor.pos())

    def save_module(self, raw: str):
        value = overlay_request(json.dumps({"coin": self.record["coin"], "module": json.loads(raw)}))
        if value["module"]["type"] != self.record["module"]["type"] or value["module"]["id"] != self.record["module"]["id"]:
            raise ValueError("Неверный модуль оверлея")
        self.record["module"] = copy.deepcopy(value["module"])
        self.ctl.save_prefs()

    def save(self):
        self.record["geometry"] = self.saveGeometry().toHex().data().decode()
        return copy.deepcopy(self.record)
