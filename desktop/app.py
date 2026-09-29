"""Manipulation Radar desktop: the dashboard in a native window + an always-on-top tape overlay.

Nothing from `app.*` is imported at module level: app.config reads its settings
from the environment once, and the embedded server sets that environment first.
"""
import argparse
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
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QToolBar,
    QWidget,
)

from desktop.backend import EmbeddedServer, free_port
from desktop.feed import Feed
from desktop.hotkeys import Hotkeys
from desktop.overlay import OverlayWindow
from desktop.prefs import APP_NAME, Prefs, data_dir

log = logging.getLogger("desktop")


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
        tb.addAction(ctl.act_overlay)
        tb.addAction(ctl.act_lock)
        tb.addAction(ctl.act_overlay_settings)
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
        profile.setCachePath(str(web_dir / "cache"))
        profile.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        self.web = QWebEngineView()
        self.web.setPage(QWebEnginePage(profile, self.web))
        self.web.loadFinished.connect(self._loaded)
        self.web_ok = False
        self.stack.addWidget(self.web)
        self.setCentralWidget(self.stack)

    def show_message(self, text: str) -> None:
        self.message.setText(text)
        self.stack.setCurrentWidget(self.message)

    def load(self, url: str) -> None:
        self.web.load(QUrl(url))
        self.stack.setCurrentWidget(self.web)

    def _loaded(self, ok: bool) -> None:
        self.web_ok = ok
        if not ok:
            self.statusBar().showMessage("Дашборд не загрузился: проверьте адрес сервера (Подключение…)", 15000)

    def closeEvent(self, e) -> None:
        self.ctl.quit()
        e.accept()


class Controller(QObject):
    def __init__(self, app: QApplication, prefs: Prefs, args: argparse.Namespace, single: QLocalServer | None):
        super().__init__()
        self.app = app
        self.prefs = prefs
        self.args = args
        self.single = single
        self.prefs_path = data_dir() / "desktop.json"
        self.icon = make_icon()
        self.got_trades = 0
        self._quitting = False
        self._lock_hint_shown = False

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
        self.feed = Feed(self.base_url, self.token)
        self.overlay = OverlayWindow(prefs.overlay)
        self.overlay.setWindowIcon(self.icon)
        self.main = MainWindow(self)
        self.main.setWindowIcon(self.icon)
        self.tray = self._make_tray()
        self.hotkeys = Hotkeys()

        self.feed.trades.connect(self._on_trades)
        self.feed.walls.connect(self.overlay.add_walls)
        self.feed.snapshot.connect(self._on_snapshot)
        self.feed.alert.connect(self._on_alert)
        self.feed.coin_changed.connect(self.overlay.set_coin)
        self.feed.connection.connect(self._on_connection)
        self.feed.request_failed.connect(self._notify_error)
        self.overlay.settings_requested.connect(self.open_overlay_settings)
        self.overlay.lock_requested.connect(lambda: self.act_lock.setChecked(True))
        self.overlay.hide_requested.connect(lambda: self.act_overlay.setChecked(False))
        self.overlay.coin_requested.connect(self.ask_coin)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.failed.connect(lambda m: self.main.statusBar().showMessage(m, 15000))
        if single:
            single.newConnection.connect(self._on_second_instance)

        self._restore_geometry()
        self.main.show()
        QTimer.singleShot(0, self._ensure_main_on_screen)
        self.act_overlay.setChecked(prefs.overlay_visible)
        self.act_lock.setChecked(prefs.overlay_locked)
        self.hotkeys.start({"overlay": prefs.hotkey_overlay, "lock": prefs.hotkey_lock})
        self.feed.set_filter(prefs.overlay.min_usd, prefs.overlay.keys, prefs.overlay.show_walls)

        if self.server:
            self.main.show_message("Запуск сервера и подключение к биржам…")
            self.server.start()
            self._wait_timer = QTimer(self, interval=100, timeout=self._wait_backend)
            self._wait_timer.start()
        else:
            self._backend_ready()

    # ---- setup -----------------------------------------------------------
    def _make_actions(self) -> None:
        self.act_overlay = QAction("▣ Оверлей", self, checkable=True)
        self.act_overlay.setToolTip(f"Лента сделок поверх всех окон ({self.prefs.hotkey_overlay})")
        self.act_overlay.toggled.connect(self._set_overlay_visible)
        self.act_lock = QAction("🔒 Сквозные клики", self, checkable=True)
        self.act_lock.setToolTip(f"Клики проходят сквозь оверлей в терминал ({self.prefs.hotkey_lock})")
        self.act_lock.toggled.connect(self._set_locked)
        self.act_overlay_settings = QAction("⚙ Биржи и фильтр оверлея…", self)
        self.act_overlay_settings.triggered.connect(self.open_overlay_settings)
        self.act_browser = QAction("↗ В браузере", self)
        self.act_browser.setToolTip("Открыть дашборд в браузере")
        self.act_browser.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(self.web_url())))
        self.act_connection = QAction("🔌 Подключение…", self)
        self.act_connection.triggered.connect(self.open_connection)

    def _make_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(self.icon, self)
        tray.setToolTip("Manipulation Radar")
        menu = QMenu()
        menu.addAction("Открыть Radar", self.show_main)
        menu.addAction(self.act_overlay)
        menu.addAction(self.act_lock)
        menu.addAction(self.act_overlay_settings)
        menu.addSeparator()
        menu.addAction("Выход", self.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self.show_main() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        tray.show()
        self._tray_menu = menu
        return tray

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
        overlay_ok = bool(self.prefs.overlay_geometry) and self.overlay.restoreGeometry(
            QByteArray.fromHex(self.prefs.overlay_geometry.encode())
        )
        if not overlay_ok or not on_screen(self.overlay.geometry()):
            self.overlay.resize(min(self.overlay.width(), avail.width() // 3), min(460, avail.height() // 2))
            self.overlay.move(avail.right() - self.overlay.width() - 40, avail.top() + 90)

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
        self.feed.start()
        self.main.load(self.web_url())

    # ---- feed ------------------------------------------------------------
    def _on_trades(self, rows: list) -> None:
        self.got_trades += len(rows)
        self.overlay.add_trades(rows)

    def _on_snapshot(self, msg: dict) -> None:
        self.overlay.set_coin(msg.get("coin") or "")
        streams = msg.get("streams") or []
        live = sum(1 for s in streams if s.get("status") in ("live", "polling") and s.get("price") is not None)
        where = "встроенный сервер" if self.server else self.base_url
        self.main.status.setText(f"● {msg.get('coin', '')} · потоков {live}/{len(streams)} · {where}")
        self.main.status.setStyleSheet("color:#2ebd85; padding:0 10px;")

    def _on_connection(self, ok: bool, error: str) -> None:
        self.overlay.set_connected(ok, error)
        if not ok:
            self.main.status.setText(f"● нет связи: {error}")
            self.main.status.setStyleSheet("color:#f6465d; padding:0 10px;")

    def _on_alert(self, alert: dict) -> None:
        if alert.get("coin") and alert["coin"] != self.overlay.coin:
            return
        self.overlay.show_alert(alert)
        if self.prefs.notify_alerts and self.tray:
            kind = "фьючерс" if alert.get("kind") == "perp" else "спот"
            self.tray.showMessage(
                f"{alert.get('coin')} · {alert.get('venue')} {kind} · скор {alert.get('score', 0):.0f}",
                "\n".join(alert.get("reasons") or []),
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )

    def _notify_error(self, text: str) -> None:
        self.main.statusBar().showMessage(text, 10000)
        if self.overlay.isVisible():
            self.overlay.show_message(text)

    # ---- actions ---------------------------------------------------------
    def _set_overlay_visible(self, visible: bool) -> None:
        self.overlay.setVisible(visible)
        self.prefs.overlay_visible = visible

    def _set_locked(self, locked: bool) -> None:
        self.overlay.set_locked(locked)
        self.prefs.overlay_locked = locked
        if locked and not self._lock_hint_shown and self.tray and self.overlay.isVisible():
            self._lock_hint_shown = True
            self.tray.showMessage(
                "Оверлей закреплён",
                f"Клики проходят в терминал. Снять: {self.prefs.hotkey_lock} или меню в трее.",
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )

    def _on_hotkey(self, name: str) -> None:
        (self.act_overlay if name == "overlay" else self.act_lock).toggle()

    def show_main(self) -> None:
        self.main.showNormal()
        self.main.raise_()
        self.main.activateWindow()

    def _on_second_instance(self) -> None:
        sock = self.single.nextPendingConnection()
        if sock:
            sock.close()
        self.show_main()

    def ask_coin(self) -> None:
        dlg = QInputDialog(None, Qt.WindowType.WindowStaysOnTopHint)
        dlg.setWindowTitle("Монета")
        dlg.setLabelText("Тикер, например PEPE:")
        dlg.setTextValue(self.overlay.coin)
        if dlg.exec():
            coin = dlg.textValue().strip().upper()
            if coin.isalnum() and len(coin) <= 20:
                self.feed.set_coin(coin)
            elif coin:
                self._notify_error("Тикер должен состоять из букв и цифр")

    def open_overlay_settings(self) -> None:
        from desktop.dialogs import OverlaySettingsDialog

        original = self.prefs.overlay
        dlg = OverlaySettingsDialog(original, self.feed.last_snapshot)
        dlg.setWindowIcon(self.icon)
        dlg.changed.connect(self._apply_overlay_prefs)  # live preview
        if dlg.exec():
            self._apply_overlay_prefs(dlg.prefs)
            self.save_prefs()
        else:
            self._apply_overlay_prefs(original)

    def _apply_overlay_prefs(self, p) -> None:
        self.prefs.overlay = p
        self.overlay.set_prefs(p)
        self.feed.set_filter(p.min_usd, p.keys, p.show_walls)

    def open_connection(self) -> None:
        from desktop.dialogs import ConnectionDialog

        dlg = ConnectionDialog(self.prefs, self.main)
        if not dlg.exec():
            return
        restart = dlg.needs_restart(self.prefs)
        dlg.prefs.overlay = self.prefs.overlay
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
        self.prefs.overlay_geometry = self.overlay.saveGeometry().toHex().data().decode()
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
        self.overlay.hide()
        if self.tray:
            self.tray.hide()
        self.app.quit()

    def shutdown(self) -> None:
        self.feed.stop()
        if self.server:
            self.server.stop()

    # ---- self test (CI smoke test of the built .exe) ---------------------
    def selftest(self) -> None:
        out = data_dir()
        self.overlay.grab().save(str(out / "selftest-overlay.png"))
        self.main.grab().save(str(out / "selftest-main.png"))
        result = {
            "backend": bool(self.server and self.server.started) or not self.server,
            "feed_trades": self.got_trades,
            "overlay_rows": len(self.overlay.tape.rows),
            "dashboard_loaded": self.main.web_ok,
            "coin": self.overlay.coin,
        }
        ok = result["backend"] and result["feed_trades"] > 0 and result["dashboard_loaded"]
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
