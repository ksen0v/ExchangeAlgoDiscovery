"""Manipulation Radar desktop: the dashboard in a native window + an always-on-top tape overlay.

Nothing from `app.*` is imported at module level: app.config reads its settings
from the environment once, and the embedded server sets that environment first.
"""
import argparse
import copy
import json
import logging
import logging.handlers
import os
import sys
import time

from PySide6.QtCore import QByteArray, QObject, QPoint, QRect, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QGuiApplication, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QToolBar,
    QToolButton,
    QWidget,
)

from desktop.backend import EmbeddedServer, free_port
from desktop.feed import Feed
from desktop.hotkeys import Hotkeys
from desktop.overlay import OverlayWindow
from desktop.prefs import APP_NAME, MAX_EXTRA, ExtraOverlay, OverlayPrefs, Prefs, data_dir

log = logging.getLogger("desktop")
LOAD_RETRIES = 10


def make_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = size / 64
        p.setBrush(QColor("#151b23"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(1 * s, 1 * s, 62 * s, 62 * s), 14 * s, 14 * s)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for r, a in ((24, 110), (16, 180)):
            p.setPen(QPen(QColor(240, 185, 11, a), max(1.0, 4 * s)))
            p.drawEllipse(QRectF((32 - r) * s, (32 - r) * s, 2 * r * s, 2 * r * s))
        p.setPen(QPen(QColor("#f0b90b"), max(1.0, 5 * s), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPoint(int(32 * s), int(32 * s)), QPoint(int(50 * s), int(16 * s)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#2ebd85"))
        p.drawEllipse(QRectF(24 * s, 24 * s, 16 * s, 16 * s))
        p.end()
        icon.addPixmap(pm)
    return icon


def dark_palette() -> QPalette:
    pal = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#151b23"),
        (QPalette.ColorRole.WindowText, "#e6edf3"),
        (QPalette.ColorRole.Base, "#1b222c"),
        (QPalette.ColorRole.AlternateBase, "#222a35"),
        (QPalette.ColorRole.Text, "#e6edf3"),
        (QPalette.ColorRole.Button, "#263040"),
        (QPalette.ColorRole.ButtonText, "#e6edf3"),
        (QPalette.ColorRole.ToolTipBase, "#1b222c"),
        (QPalette.ColorRole.ToolTipText, "#e6edf3"),
        (QPalette.ColorRole.Highlight, "#f0b90b"),
        (QPalette.ColorRole.HighlightedText, "#111111"),
        (QPalette.ColorRole.PlaceholderText, "#7d8793"),
        (QPalette.ColorRole.Link, "#8fb3ff"),
    ):
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor("#5c6570"))
    return pal


class MainWindow(QMainWindow):
    def __init__(self, ctl: "Controller"):
        super().__init__()
        self.ctl = ctl
        self.setWindowTitle("Manipulation Radar")
        self.resize(1400, 860)

        tb = QToolBar("Оверлей")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(tb)
        # one button per coin (its overlay), filled by Controller._rebuild_coin_controls
        coins = QWidget()
        self.coin_bar = QHBoxLayout(coins)
        self.coin_bar.setContentsMargins(0, 0, 0, 0)
        self.coin_bar.setSpacing(2)
        tb.addWidget(coins)
        tb.addSeparator()
        tb.addAction(ctl.act_lock)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.status = QLabel("запуск…")
        self.status.setStyleSheet("color:#7d8793; padding:0 10px;")
        tb.addWidget(self.status)
        tb.addAction(ctl.act_browser)
        tb.addAction(ctl.act_connection)

        self.stack = QStackedWidget()
        self.message = QLabel("Запуск сервера…")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color:#aab4bf; font-size:15px; padding:40px;")
        self.stack.addWidget(self.message)

        # Named profile = persistent storage: dashboard settings survive restarts. Owned by the
        # application so it outlives the page (Qt warns if a profile dies before its pages).
        profile = QWebEngineProfile(APP_NAME, QApplication.instance())
        web_dir = data_dir() / "web"
        profile.setPersistentStoragePath(str(web_dir / "storage"))
        # In-memory HTTP cache: a dashboard cached on disk by an older version is never shown.
        # (clearHttpCache() is asynchronous in Qt 6.8 and aborted the first page load on Windows.)
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        profile.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        self.web = QWebEngineView()
        self.web.setPage(QWebEnginePage(profile, self.web))
        self.web.loadFinished.connect(self._loaded)
        self.web_ok = False
        self._url, self._retries = "", 0
        self.stack.addWidget(self.web)
        self.setCentralWidget(self.stack)

    def show_message(self, text: str) -> None:
        self.message.setText(text)
        self.stack.setCurrentWidget(self.message)

    def load(self, url: str) -> None:
        self._url, self._retries = url, 0
        self.web.load(QUrl(url))
        self.stack.setCurrentWidget(self.web)

    def _loaded(self, ok: bool) -> None:
        self.web_ok = ok
        if ok:
            return
        if self._retries < LOAD_RETRIES:  # the server may still be busy starting ~125 streams
            self._retries += 1
            QTimer.singleShot(1500, lambda: self.web.load(QUrl(self._url)))
            return
        self.statusBar().showMessage("Дашборд не загрузился: проверьте адрес сервера (Подключение…)", 15000)

    def closeEvent(self, e) -> None:
        self.ctl.quit()
        e.accept()


class OverlaySlot(QObject):
    """One overlay window with its own server feed: the main coin (coin=None) or an extra coin."""

    def __init__(self, ctl: "Controller", coin: str | None, prefs: OverlayPrefs, extra: ExtraOverlay | None = None):
        super().__init__(ctl)
        self.ctl = ctl
        self.coin = coin
        self.prefs = prefs
        self.extra = extra
        self.got_trades = 0
        self.overlay = OverlayWindow(prefs)
        self.overlay.setWindowIcon(ctl.icon)
        if coin:
            self.overlay.set_coin(coin)
        self.feed = Feed(ctl.base_url, ctl.token, coin=coin)
        self.action = QAction(self._label(), self, checkable=True)
        self.action.toggled.connect(self._visible_changed)

        self.feed.trades.connect(self._on_trades)
        self.feed.connection.connect(self.overlay.set_connected)
        self.feed.request_failed.connect(ctl.notify_error)
        if self.is_main:
            self.feed.walls.connect(self.overlay.add_walls)
        self.overlay.settings_requested.connect(lambda: ctl.open_overlay_settings(self))
        self.overlay.lock_requested.connect(lambda: ctl.act_lock.setChecked(True))
        self.overlay.hide_requested.connect(lambda: self.action.setChecked(False))
        self.overlay.coin_requested.connect(lambda: ctl.ask_coin(self))
        self.apply_filter()

    @property
    def is_main(self) -> bool:
        return self.coin is None

    @property
    def shown_coin(self) -> str:
        return self.coin or self.overlay.coin

    def _label(self) -> str:
        return f"▣ {self.shown_coin or 'Оверлей'}"

    def refresh_label(self) -> None:
        self.action.setText(self._label())
        self.action.setToolTip(
            f"Лента сделок {self.shown_coin} поверх всех окон" + (" (основная монета)" if self.is_main else "")
        )

    def _on_trades(self, rows: list) -> None:
        self.got_trades += len(rows)
        self.overlay.add_trades(rows)

    def _visible_changed(self, visible: bool) -> None:
        self.overlay.setVisible(visible)
        if self.extra:
            self.extra.visible = visible
        else:
            self.ctl.prefs.overlay_visible = visible

    def apply_filter(self) -> None:
        p = self.prefs
        self.feed.set_filter(p.min_usd, p.keys, p.show_walls and self.is_main)

    def set_prefs(self, p: OverlayPrefs) -> None:
        self.prefs = p
        if self.extra:
            self.extra.overlay = p
        else:
            self.ctl.prefs.overlay = p
        self.overlay.set_prefs(p)
        self.apply_filter()

    def change_coin(self, coin: str) -> None:
        """Extra slot only: follow another coin."""
        self.coin = coin
        self.extra.coin = coin
        self.overlay.set_coin(coin)
        self.feed.follow(coin)
        self.refresh_label()

    def geometry_hex(self) -> str:
        return self.overlay.saveGeometry().toHex().data().decode()

    def close(self) -> None:
        self.feed.stop()
        self.overlay.hide()
        self.overlay.deleteLater()


class Controller(QObject):
    def __init__(self, app: QApplication, prefs: Prefs, args: argparse.Namespace, single: QLocalServer | None):
        super().__init__()
        self.app = app
        self.prefs = prefs
        self.args = args
        self.single = single
        self.prefs_path = data_dir() / "desktop.json"
        self.icon = make_icon()
        self._quitting = False
        self._lock_hint_shown = False
        self._started = False
        self._hidden_by_hotkey: list[OverlaySlot] = []

        self.server: EmbeddedServer | None = None
        demo = args.demo or prefs.demo
        if prefs.mode == "local" or args.demo:
            env = {
                "DB_PATH": str(data_dir() / "radar.db"),
                "DEMO": "1" if demo else "0",
                "TELEGRAM_BOT_TOKEN": prefs.telegram_token,
                "TELEGRAM_CHAT_ID": prefs.telegram_chat_id,
                "AUTH_TOKEN": "",
            }
            self.server = EmbeddedServer(free_port(prefs.local_port), env)
            self.base_url, self.token = self.server.url, ""
        else:
            self.base_url, self.token = prefs.remote_url, prefs.remote_token

        self._make_actions()
        self.slots: list[OverlaySlot] = [OverlaySlot(self, None, prefs.overlay)]
        self.slots += [OverlaySlot(self, e.coin, e.overlay, e) for e in prefs.extra]
        self.main = MainWindow(self)
        self.main.setWindowIcon(self.icon)
        self.tray = self._make_tray()
        self.hotkeys = Hotkeys()

        feed = self.main_slot.feed
        feed.snapshot.connect(self._on_snapshot)
        feed.alert.connect(self._on_alert)
        feed.coin_changed.connect(self._on_main_coin)
        feed.connection.connect(self._on_connection)
        feed.watch_changed.connect(self._on_server_watch)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.failed.connect(lambda m: self.main.statusBar().showMessage(m, 15000))
        if single:
            single.newConnection.connect(self._on_second_instance)

        self._restore_geometry()
        self.main.show()
        QTimer.singleShot(0, self._ensure_main_on_screen)
        self.main_slot.action.setChecked(prefs.overlay_visible)
        for s in self.extra_slots:
            s.action.setChecked(s.extra.visible)
        self.act_lock.setChecked(prefs.overlay_locked)
        self._rebuild_coin_controls()
        self.hotkeys.start({"overlay": prefs.hotkey_overlay, "lock": prefs.hotkey_lock})

        if self.server:
            self.main.show_message("Запуск сервера и подключение к биржам…")
            self.server.start()
            self._wait_timer = QTimer(self, interval=100, timeout=self._wait_backend)
            self._wait_timer.start()
        else:
            self._backend_ready()

    @property
    def main_slot(self) -> OverlaySlot:
        return self.slots[0]

    @property
    def extra_slots(self) -> list[OverlaySlot]:
        return self.slots[1:]

    # ---- setup -----------------------------------------------------------
    def _make_actions(self) -> None:
        self.act_lock = QAction("🔒 Сквозные клики", self, checkable=True)
        self.act_lock.setToolTip(f"Клики проходят сквозь оверлеи в терминал ({self.prefs.hotkey_lock})")
        self.act_lock.toggled.connect(self._set_locked)
        self.act_add_coin = QAction("＋ Монета", self)
        self.act_add_coin.triggered.connect(self.add_coin)
        self.act_browser = QAction("↗ В браузере", self)
        self.act_browser.setToolTip("Открыть дашборд в браузере")
        self.act_browser.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(self.web_url())))
        self.act_connection = QAction("🔌 Подключение…", self)
        self.act_connection.triggered.connect(self.open_connection)

    def _slot_menu(self, slot: OverlaySlot, parent: QWidget | None = None) -> QMenu:
        menu = QMenu(parent)
        menu.addAction("⚙ Биржи и фильтр…", lambda: self.open_overlay_settings(slot))
        if slot.is_main:
            menu.addAction("Сменить основную монету…", lambda: self.ask_coin(slot))
        else:
            menu.addAction("Сменить монету…", lambda: self.ask_coin(slot))
            menu.addAction("✕ Убрать монету", lambda: self.remove_coin(slot))
        return menu

    def _rebuild_coin_controls(self) -> None:
        """Toolbar: one button per coin (click = show/hide its overlay, arrow = menu) + "＋ Монета"."""
        bar = self.main.coin_bar
        while bar.count():
            w = bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        for slot in self.slots:
            slot.refresh_label()
            b = QToolButton()
            b.setDefaultAction(slot.action)
            b.setMenu(self._slot_menu(slot, b))
            b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            b.setAutoRaise(True)
            bar.addWidget(b)
        b = QToolButton()
        b.setDefaultAction(self.act_add_coin)
        b.setAutoRaise(True)
        bar.addWidget(b)
        full = len(self.extra_slots) >= MAX_EXTRA
        self.act_add_coin.setEnabled(not full)
        self.act_add_coin.setToolTip(
            f"Можно следить максимум за {MAX_EXTRA + 1} монетами" if full
            else "Ещё одна монета со своим оверлеем (только лента сделок)"
        )
        self._rebuild_tray_menu()

    def _make_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(self.icon, self)
        tray.setToolTip("Manipulation Radar")
        tray.activated.connect(
            lambda reason: self.show_main() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        tray.show()
        self._tray_menu = QMenu()
        tray.setContextMenu(self._tray_menu)
        return tray

    def _rebuild_tray_menu(self) -> None:
        if not self.tray:
            return
        menu = self._tray_menu
        menu.clear()
        menu.addAction("Открыть Radar", self.show_main)
        menu.addSeparator()
        for slot in self.slots:
            menu.addAction(slot.action)
        menu.addAction(self.act_add_coin)
        menu.addAction(self.act_lock)
        menu.addSeparator()
        menu.addAction("Выход", self.quit)

    def _restore_geometry(self) -> None:
        """Saved positions are used only while they are still on a screen (monitor unplugged,
        Windows scaling changed); otherwise the windows are placed to fit the primary screen."""
        avail = QGuiApplication.primaryScreen().availableGeometry()
        main_ok = bool(self.prefs.main_geometry) and self.main.restoreGeometry(
            QByteArray.fromHex(self.prefs.main_geometry.encode())
        )
        if not main_ok or not (self.main.isMaximized() or on_screen(self.main.geometry())):
            self.main.resize(int(avail.width() * 0.9), int(avail.height() * 0.85))
            self.main.move(avail.center() - self.main.rect().center())
        saved = [self.prefs.overlay_geometry] + [e.geometry for e in self.prefs.extra]
        for i, (slot, geo) in enumerate(zip(self.slots, saved, strict=False)):
            self._place_overlay(slot, i, geo)

    def _place_overlay(self, slot: OverlaySlot, index: int, geometry: str = "") -> None:
        ov = slot.overlay
        if geometry and ov.restoreGeometry(QByteArray.fromHex(geometry.encode())) and on_screen(ov.geometry()):
            return
        avail = QGuiApplication.primaryScreen().availableGeometry()
        ov.resize(min(ov.width(), avail.width() // 3), min(460, avail.height() // 2))
        # side by side from the right edge; wrap below when the screen is too narrow
        x = avail.right() - (ov.width() + 12) * (index + 1) - 28
        y = avail.top() + 90
        if x < avail.left():
            x, y = avail.right() - ov.width() - 40, min(avail.bottom() - ov.height(), y + 60 * index)
        ov.move(x, y)

    def _ensure_main_on_screen(self) -> None:
        """The title bar and toolbar must be visible: pull the window back if Windows placed it too high."""
        if self.main.isMaximized() or self.main.isFullScreen():
            return
        frame = self.main.frameGeometry()
        screen = QGuiApplication.screenAt(frame.center()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        if frame.top() >= avail.top() and frame.bottom() <= avail.bottom() and frame.height() <= avail.height():
            return
        border = frame.height() - self.main.height()
        self.main.resize(min(self.main.width(), avail.width()), min(self.main.height(), avail.height() - border))
        frame = self.main.frameGeometry()
        x = min(max(frame.left(), avail.left()), avail.right() - frame.width())
        self.main.move(x, avail.top())

    def web_url(self) -> str:
        return self.base_url + (f"/?token={self.token}" if self.token else "/")

    # ---- backend ---------------------------------------------------------
    def _wait_backend(self) -> None:
        if self.server.started:
            self._wait_timer.stop()
            self._backend_ready()
        elif self.server.failed:
            self._wait_timer.stop()
            err = self.server.error
            self.main.show_message(
                "Не удалось запустить встроенный сервер.\n\n"
                f"{type(err).__name__}: {err}\n\nПодробности в журнале: {data_dir() / 'radar.log'}"
            )

    def _backend_ready(self) -> None:
        self._started = True
        for slot in self.slots:
            slot.feed.start()
        self.main.load(self.web_url())

    def _sync_watch(self) -> None:
        """The desktop owns the list of extra coins: tell the server (also after reconnects)."""
        self.main_slot.feed.put_watch([s.coin for s in self.extra_slots])

    # ---- main feed -------------------------------------------------------
    def _on_snapshot(self, msg: dict) -> None:
        self._on_main_coin(msg.get("coin") or "")
        streams = msg.get("streams") or []
        live = sum(1 for s in streams if s.get("status") in ("live", "polling") and s.get("price") is not None)
        where = "встроенный сервер" if self.server else self.base_url
        extra = "".join(f" + {s.coin}" for s in self.extra_slots)
        self.main.status.setText(f"● {msg.get('coin', '')}{extra} · потоков {live}/{len(streams)} · {where}")
        self.main.status.setStyleSheet("color:#2ebd85; padding:0 10px;")

    def _on_main_coin(self, coin: str) -> None:
        main = self.main_slot
        if not coin or coin == main.overlay.coin:
            return
        main.overlay.set_coin(coin)
        # the server drops a watched coin that became the main one: drop its overlay too
        for slot in [s for s in self.extra_slots if s.coin == coin]:
            self._drop_slot(slot)
        self._rebuild_coin_controls()

    def _on_server_watch(self, coins: list) -> None:
        mine = [s.coin for s in self.extra_slots]
        if coins != mine and set(mine) - set(coins) - {self.main_slot.overlay.coin}:
            self._sync_watch()  # the server lost some (restart, another client): put them back

    def _on_connection(self, ok: bool, error: str) -> None:
        if ok:
            self._sync_watch()
        else:
            self.main.status.setText(f"● нет связи: {error}")
            self.main.status.setStyleSheet("color:#f6465d; padding:0 10px;")

    def _on_alert(self, alert: dict) -> None:
        main = self.main_slot.overlay
        if alert.get("coin") and alert["coin"] != main.coin:
            return
        main.show_alert(alert)
        if self.prefs.notify_alerts and self.tray:
            kind = "фьючерс" if alert.get("kind") == "perp" else "спот"
            self.tray.showMessage(
                f"{alert.get('coin')} · {alert.get('venue')} {kind} · скор {alert.get('score', 0):.0f}",
                "\n".join(alert.get("reasons") or []),
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )

    def notify_error(self, text: str) -> None:
        self.main.statusBar().showMessage(text, 10000)
        for slot in self.slots:
            if slot.overlay.isVisible():
                slot.overlay.show_message(text)
                break

    # ---- coins -----------------------------------------------------------
    def _ask_ticker(self, title: str, current: str = "") -> str:
        dlg = QInputDialog(None, Qt.WindowType.WindowStaysOnTopHint)
        dlg.setWindowTitle(title)
        dlg.setLabelText("Тикер, например PEPE:")
        dlg.setTextValue(current)
        if not dlg.exec():
            return ""
        coin = dlg.textValue().strip().upper()
        if coin and not (coin.isalnum() and len(coin) <= 20):
            self.notify_error("Тикер должен состоять из букв и цифр")
            return ""
        return coin

    def _taken(self, coin: str, slot: OverlaySlot | None = None) -> bool:
        coins = {s.shown_coin for s in self.slots if s is not slot}
        if coin in coins:
            self.notify_error(f"{coin} уже на экране")
            return True
        return False

    def add_coin(self) -> None:
        if len(self.extra_slots) >= MAX_EXTRA:
            self.notify_error(f"Можно следить максимум за {MAX_EXTRA + 1} монетами")
            return
        coin = self._ask_ticker("Ещё одна монета")
        if not coin or self._taken(coin):
            return
        # start from the main overlay's venues and filter; it can be tuned separately afterwards
        extra = ExtraOverlay(coin=coin, overlay=copy.deepcopy(self.main_slot.prefs))
        self.prefs.extra.append(extra)
        slot = OverlaySlot(self, coin, extra.overlay, extra)
        self.slots.append(slot)
        self._place_overlay(slot, len(self.slots) - 1)
        slot.overlay.set_locked(self.act_lock.isChecked())
        if self._started:
            slot.feed.start()
        slot.action.setChecked(True)
        self._rebuild_coin_controls()
        self._sync_watch()
        self.save_prefs()

    def remove_coin(self, slot: OverlaySlot) -> None:
        self._drop_slot(slot)
        self._rebuild_coin_controls()
        self._sync_watch()
        self.save_prefs()

    def _drop_slot(self, slot: OverlaySlot) -> None:
        if slot.is_main:
            return
        self.slots.remove(slot)
        if slot.extra in self.prefs.extra:
            self.prefs.extra.remove(slot.extra)
        slot.close()

    def ask_coin(self, slot: OverlaySlot) -> None:
        if slot.is_main:
            coin = self._ask_ticker("Основная монета", slot.overlay.coin)
            if coin and coin != slot.overlay.coin:
                slot.feed.set_coin(coin)  # the server switches; a watched coin moves to the main slot
            return
        coin = self._ask_ticker("Монета оверлея", slot.coin)
        if coin and coin != slot.coin and not self._taken(coin, slot):
            slot.change_coin(coin)
            self._rebuild_coin_controls()
            self._sync_watch()
            self.save_prefs()

    # ---- actions ---------------------------------------------------------
    def _set_locked(self, locked: bool) -> None:
        for slot in self.slots:
            slot.overlay.set_locked(locked)
        self.prefs.overlay_locked = locked
        visible = any(s.overlay.isVisible() for s in self.slots)
        if locked and not self._lock_hint_shown and self.tray and visible:
            self._lock_hint_shown = True
            self.tray.showMessage(
                "Оверлеи закреплены",
                f"Клики проходят в терминал. Снять: {self.prefs.hotkey_lock} или меню в трее.",
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )

    def _toggle_overlays(self) -> None:
        """Hotkey: hide every overlay, or bring back the ones it hid (the main one if none)."""
        shown = [s for s in self.slots if s.action.isChecked()]
        if shown:
            self._hidden_by_hotkey = shown
            for s in shown:
                s.action.setChecked(False)
        else:
            for s in [s for s in self._hidden_by_hotkey if s in self.slots] or [self.main_slot]:
                s.action.setChecked(True)

    def _on_hotkey(self, name: str) -> None:
        if name == "overlay":
            self._toggle_overlays()
        else:
            self.act_lock.toggle()

    def show_main(self) -> None:
        self.main.showNormal()
        self.main.raise_()
        self.main.activateWindow()

    def _on_second_instance(self) -> None:
        sock = self.single.nextPendingConnection()
        if sock:
            sock.close()
        self.show_main()

    def open_overlay_settings(self, slot: OverlaySlot | None = None) -> None:
        from desktop.dialogs import OverlaySettingsDialog

        slot = slot or self.main_slot
        original = slot.prefs
        dlg = OverlaySettingsDialog(original, slot.feed.last_snapshot, coin=slot.shown_coin, walls=slot.is_main)
        dlg.setWindowIcon(self.icon)
        dlg.changed.connect(slot.set_prefs)  # live preview
        if dlg.exec():
            slot.set_prefs(dlg.prefs)
            self.save_prefs()
        else:
            slot.set_prefs(original)

    def open_connection(self) -> None:
        from desktop.dialogs import ConnectionDialog

        dlg = ConnectionDialog(self.prefs, self.main)
        if not dlg.exec():
            return
        restart = dlg.needs_restart(self.prefs)
        dlg.prefs.overlay, dlg.prefs.extra = self.prefs.overlay, self.prefs.extra
        self.prefs = dlg.prefs
        self.save_prefs()
        self.hotkeys.start({"overlay": self.prefs.hotkey_overlay, "lock": self.prefs.hotkey_lock})
        if restart and QMessageBox.question(
            self.main, "Перезапуск", "Новые настройки подключения вступят в силу после перезапуска. Перезапустить сейчас?"
        ) == QMessageBox.StandardButton.Yes:
            self.restart()

    def restart(self) -> None:
        from PySide6.QtCore import QProcess

        if getattr(sys, "frozen", False):
            program, argv = sys.executable, sys.argv[1:]
        else:
            program, argv = sys.executable, ["-m", "desktop", *sys.argv[1:]]
        if self.single:
            self.single.close()  # let the new instance start
        QProcess.startDetached(program, argv)
        self.quit()

    # ---- shutdown --------------------------------------------------------
    def save_prefs(self) -> None:
        self.prefs.main_geometry = self.main.saveGeometry().toHex().data().decode()
        self.prefs.overlay_geometry = self.main_slot.geometry_hex()
        for slot in self.extra_slots:
            slot.extra.geometry = slot.geometry_hex()
        try:
            self.prefs.save(self.prefs_path)
        except OSError as e:
            log.warning("cannot save preferences: %s", e)

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.save_prefs()
        self.hotkeys.stop()
        for slot in self.slots:
            slot.overlay.hide()
        if self.tray:
            self.tray.hide()
        self.app.quit()

    def shutdown(self) -> None:
        for slot in self.slots:
            slot.feed.stop()
        if self.server:
            self.server.stop()

    # ---- self test (CI smoke test of the built .exe) ---------------------
    def selftest(self) -> None:
        out = data_dir()
        main = self.main_slot
        main.overlay.grab().save(str(out / "selftest-overlay.png"))
        for slot in self.extra_slots:
            slot.overlay.grab().save(str(out / f"selftest-overlay-{slot.coin}.png"))
        self.main.grab().save(str(out / "selftest-main.png"))
        result = {
            "backend": bool(self.server and self.server.started) or not self.server,
            "feed_trades": main.got_trades,
            "overlay_rows": len(main.overlay.tape.rows),
            "dashboard_loaded": self.main.web_ok,
            "coin": main.overlay.coin,
            "extra": {s.coin: {"trades": s.got_trades, "rows": len(s.overlay.tape.rows)} for s in self.extra_slots},
        }
        ok = result["backend"] and result["feed_trades"] > 0 and result["dashboard_loaded"]
        ok = ok and all(v["trades"] > 0 for v in result["extra"].values())
        result["ok"] = ok
        (out / "selftest.json").write_text(json.dumps(result, indent=2), "utf-8")
        log.info("selftest %s", result)
        self._quitting = True
        self.app.exit(0 if ok else 1)


def on_screen(rect: QRect) -> bool:
    """Top edge (where the title bar / overlay header is) inside some screen, and not taller than it."""
    for screen in QGuiApplication.screens():
        g = screen.availableGeometry()
        grab = QPoint(rect.left() + min(80, rect.width() // 2), rect.top() + 5)
        if g.contains(grab) and rect.height() <= g.height() and rect.bottom() <= g.bottom() + 40:
            return True
    return False


def setup_logging() -> None:
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(data_dir() / "radar.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("ccxt").setLevel(logging.WARNING)


def single_instance(app: QApplication) -> QLocalServer | None | bool:
    """False if another instance is running (it is asked to show itself)."""
    name = f"{APP_NAME}-{os.environ.get('USERNAME') or os.environ.get('USER') or 'user'}"
    sock = QLocalSocket()
    sock.connectToServer(name)
    if sock.waitForConnected(300):
        sock.disconnectFromServer()
        return False
    QLocalServer.removeServer(name)
    server = QLocalServer(app)
    return server if server.listen(name) else None


def main(argv: list[str] | None = None) -> int:
    # A windowed .exe has no console: libraries that print must not crash.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115
    parser = argparse.ArgumentParser(prog="ManipulationRadar")
    parser.add_argument("--demo", action="store_true", help="сгенерированные сделки вместо бирж")
    parser.add_argument("--selftest", type=float, metavar="SEC", help="проверить запуск и выйти через SEC секунд")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    setup_logging()
    log.info("starting, data dir %s", data_dir())
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setQuitOnLastWindowClosed(False)

    single = None if args.selftest else single_instance(app)
    if single is False:
        log.info("already running, activated the other instance")
        return 0

    prefs = Prefs.load(data_dir() / "desktop.json")
    if args.selftest:
        prefs.overlay_visible = True
    ctl = Controller(app, prefs, args, single or None)
    if args.selftest:
        QTimer.singleShot(int(args.selftest * 1000), ctl.selftest)
    started = time.monotonic()
    rc = app.exec()
    ctl.shutdown()
    log.info("stopped after %.0f s, code %s", time.monotonic() - started, rc)
    return rc
