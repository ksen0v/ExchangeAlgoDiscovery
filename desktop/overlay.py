"""Always-on-top translucent trade tape to keep over a trading terminal.

The window is frameless, never takes keyboard focus from the terminal, can be
dragged by its header and resized by the corner grip. "Locked" makes it
click-through: the mouse goes to the window underneath (unlock by hotkey,
tray or main window).
"""
import math
import time
from collections import deque
from itertools import islice
from dataclasses import dataclass

from PySide6.QtCore import QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizeGrip, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from desktop.prefs import OverlayPrefs

# Same palette as static/app.js: one colour per repeating-size series.
REP_COLORS = ["#f0b90b", "#8fb3ff", "#c38fff", "#4fd1d9", "#ff9f43", "#ff7eb6", "#b5e853", "#e6edf3"]
BUY = QColor("#2ebd85")
SELL = QColor("#f6465d")
TEXT = QColor("#e6edf3")
TEXT_2 = QColor("#aab4bf")
MUTED = QColor("#7d8793")
ACCENT = QColor("#f0b90b")
PERP = QColor("#8fb3ff")
BG = (13, 17, 23)
MONO = ["Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", "Menlo", "Courier New"]
FLOW_SEC = 60
# regime badge: (text, background) per tone
REGIME_COLORS = {
    "bull": ("#5fe0aa", "rgba(46,189,133,45)"),
    "bear": ("#ff8494", "rgba(246,70,93,45)"),
    "warn": ("#ffc15c", "rgba(240,160,32,45)"),
    "flat": ("#a9c4ff", "rgba(143,179,255,40)"),
    "none": ("#7d8793", "rgba(255,255,255,15)"),
}
# short badge texts: the overlay is narrow, the full label is in the tooltip
REGIME_SHORT = {
    "Открываются лонги": "лонги+",
    "Закрываются шорты (сквиз)": "сквиз",
    "Открываются шорты": "шорты+",
    "Закрываются лонги": "лонги−",
    "Набор позиций в боковике": "набор",
    "Выход из позиций": "выход",
    "Спотовый рост без плеча": "спот↑",
    "Неопределённо": "—",
}


def fmt_usd(v: float) -> str:
    a = abs(v)
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e3:
        return f"${v / 1e3:.{0 if a >= 1e5 else 1}f}k"
    return f"${v:.0f}"


def fmt_qty(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e4:
        return f"{v / 1e3:.1f}k"
    if a >= 100:
        return f"{v:.0f}"
    return f"{v:.2f}" if a >= 1 else f"{v:.4g}"


def fmt_price(p: float) -> str:
    """Six significant digits without an exponent: 65432.1, 3.21457, 0.0000123456."""
    if p <= 0:
        return "—"
    decimals = max(0, 5 - math.floor(math.log10(p)))
    return f"{p:,.{decimals}f}"


def fmt_time(ts: float, ms: bool) -> str:
    s = time.strftime("%H:%M:%S", time.localtime(ts))
    return f"{s}.{int(ts * 1000) % 1000:03d}" if ms else s


def split_key(key: str) -> tuple[str, str]:
    venue, _, kind = key.rpartition(":")
    return venue, kind


@dataclass(slots=True)
class Row:
    ts: float
    venue: str
    kind: str
    side: str
    price: float
    amount: float
    usd: float
    grp: int = 0
    rep: int = 0


def row_from(d: dict) -> Row:
    venue, kind = split_key(d["key"])
    return Row(d["ts"], venue, kind, d.get("side", "?"), d["price"], d["amount"], d["usd"], d.get("grp", 0), d.get("rep", 0))


@dataclass(slots=True)
class WallRow:
    """A large resting order near the price: put, re-placed, pulled or eaten (app/walls.py)."""

    ts: float
    venue: str
    kind: str
    side: str  # bid | ask
    price: float
    usd: float
    dist_bps: float
    event: str  # new | moved | pulled | eaten
    towards: bool = False


def wall_from(d: dict) -> WallRow:
    venue, kind = split_key(d["key"])
    return WallRow(d["ts"], venue, kind, d["side"], d["price"], d["usd"], d.get("dist_bps", 0.0), d["event"],
                   bool(d.get("towards")))


def wall_text(w: WallRow) -> str:
    """What happened goes first: on a narrow overlay the tail is cut off."""
    bid = w.side == "bid"
    what = {"new": "плита", "moved": "переставили", "pulled": "СНЯЛИ", "eaten": "съели"}.get(w.event, w.event)
    if w.event == "moved" and w.towards:
        what += "⇡" if bid else "⇣"
    return f"{'▲' if bid else '▼'} {what} {fmt_usd(w.usd)} @{fmt_price(w.price)} · {w.dist_bps / 100:.2f}%"


class TapeView(QWidget):
    def __init__(self, parent: QWidget, prefs: OverlayPrefs):
        super().__init__(parent)
        self.prefs = prefs
        self.rows: deque[Row] = deque(maxlen=prefs.max_rows)
        self.empty_text = ""
        self.apply_prefs()

    def apply_prefs(self) -> None:
        f = QFont()
        f.setFamilies(MONO)
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSizeF(self.prefs.font_size)
        self.font_ = f
        self.bold = QFont(f)
        self.bold.setBold(True)
        self.small = QFont(f)
        self.small.setPointSizeF(max(6.0, self.prefs.font_size * 0.8))
        self.fm = QFontMetrics(f)
        if self.rows.maxlen != self.prefs.max_rows:
            self.rows = deque(self.rows, maxlen=self.prefs.max_rows)
        self.update()

    def add(self, rows: list[Row]) -> None:
        for r in rows:
            self.rows.appendleft(r)
        self.update()

    def clear(self) -> None:
        self.rows.clear()
        self.update()

    def _columns(self, width: int) -> dict[str, tuple[int, int]]:
        """name -> (x, w). Optional columns are dropped when the window is too narrow."""
        adv = self.fm.horizontalAdvance
        gap = adv(" ") + 3
        recent = list(islice(self.rows, 60))
        want = {
            "time": adv("00:00:00.000") if self.prefs.show_time else 0,
            "venue": max((adv(r.venue) for r in recent), default=adv("Binance")) + adv(" F"),
            "price": max((adv(fmt_price(r.price)) for r in recent), default=0) if self.prefs.show_price else 0,
            "qty": adv("000.00k") if self.prefs.show_qty else 0,
            "usd": QFontMetrics(self.bold).horizontalAdvance("$000.0k"),
            "rep": adv("×99") if self.prefs.highlight_repeats else 0,
        }
        for drop in ("qty", "price", "time"):
            if sum(want.values()) + gap * sum(1 for v in want.values() if v) + 8 <= width:
                break
            want[drop] = 0
        cols: dict[str, tuple[int, int]] = {}
        x = 7
        for name in ("time", "venue"):
            if want[name]:
                cols[name] = (x, want[name])
                x += want[name] + gap
        right = width - 5
        for name in ("rep", "usd", "qty", "price"):
            if want[name]:
                cols[name] = (right - want[name], want[name])
                right -= want[name] + gap
        return cols

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        prefs = self.prefs
        if not self.rows:
            p.setFont(self.small)
            p.setPen(MUTED)
            p.drawText(self.rect().adjusted(10, 10, -10, -10), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       self.empty_text)
            return
        lh = self.fm.height() + 3
        width = self.width()
        cols = self._columns(width)
        shadow = prefs.opacity < 0.6
        min_usd = max(prefs.min_usd, 1.0)
        big_usd = min_usd * max(prefs.big_mult, 1.0)
        y = 0

        def text(col: tuple[int, int], s: str, color: QColor, align=Qt.AlignmentFlag.AlignRight, font=None):
            rect = QRect(col[0], y, col[1], lh)
            p.setFont(font or self.font_)
            if shadow:
                p.setPen(QColor(0, 0, 0, 200))
                p.drawText(rect.translated(1, 1), align | Qt.AlignmentFlag.AlignVCenter, s)
            p.setPen(color)
            p.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, s)

        for r in self.rows:
            if y > self.height():
                break
            if isinstance(r, WallRow):
                self._paint_wall(p, r, y, lh, width, cols, text)
                y += lh
                continue
            side = BUY if r.side == "buy" else SELL if r.side == "sell" else MUTED
            big = r.usd >= big_usd
            bar = QColor(side)
            bar.setAlpha(70 if big else 38)
            p.fillRect(QRect(0, y, int(width * min(1.0, r.usd / (min_usd * 10))), lh - 1), bar)
            rep_color = QColor(REP_COLORS[r.grp % len(REP_COLORS)]) if r.rep and prefs.highlight_repeats else None
            if rep_color:
                p.fillRect(QRect(0, y, 3, lh - 1), rep_color)
            left = Qt.AlignmentFlag.AlignLeft
            if "time" in cols:
                text(cols["time"], fmt_time(r.ts, True), TEXT_2, left)
            vx, vw = cols["venue"]
            name_w = min(vw, self.fm.horizontalAdvance(r.venue + " "))
            text((vx, name_w), r.venue, TEXT, left)
            perp = r.kind == "perp"
            text((vx + name_w, max(0, vw - name_w)), "F" if perp else "S", PERP if perp else MUTED, left, self.small)
            if "price" in cols:
                text(cols["price"], fmt_price(r.price), TEXT)
            if "qty" in cols:
                text(cols["qty"], fmt_qty(r.amount), TEXT_2)
            text(cols["usd"], fmt_usd(r.usd), side.lighter(125) if big else side, font=self.bold if big else self.font_)
            if rep_color and "rep" in cols:
                text(cols["rep"], f"×{r.rep}", rep_color, font=self.bold)
            y += lh

    def _paint_wall(self, p: QPainter, w: WallRow, y: int, lh: int, width: int, cols: dict, text) -> None:
        color = BUY if w.side == "bid" else SELL
        tint = QColor(color)
        tint.setAlpha(45)
        p.fillRect(QRect(0, y, width, lh - 1), tint)
        frame = ACCENT if w.event == "pulled" else color
        p.setPen(QPen(frame, 1))
        p.drawRect(QRect(1, y, width - 3, lh - 2))
        p.fillRect(QRect(0, y, 3, lh - 1), ACCENT)
        left = Qt.AlignmentFlag.AlignLeft
        if "time" in cols:
            text(cols["time"], fmt_time(w.ts, True), TEXT_2, left)
        vx, vw = cols["venue"]
        name_w = min(vw, self.fm.horizontalAdvance(w.venue + " "))
        text((vx, name_w), w.venue, TEXT, left)
        perp = w.kind == "perp"
        text((vx + name_w, max(0, vw - name_w)), "F" if perp else "S", PERP if perp else MUTED, left, self.small)
        x0 = vx + vw + 4
        fm = QFontMetrics(self.bold)
        msg = fm.elidedText(wall_text(w), Qt.TextElideMode.ElideRight, max(10, width - x0 - 6))
        text((x0, width - x0 - 6), msg, ACCENT if w.event == "pulled" else color.lighter(115), left, self.bold)


class Header(QWidget):
    def __init__(self, overlay: "OverlayWindow"):
        super().__init__(overlay)
        self.overlay = overlay
        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 3, 4, 3)
        lay.setSpacing(6)
        self.coin = QLabel("—")
        self.coin.setStyleSheet(f"color:{ACCENT.name()}; font-weight:700;")
        self.coin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.coin.setToolTip("Сменить монету")
        self.coin.mousePressEvent = lambda _e: overlay.coin_requested.emit()
        self.flow = QLabel()
        self.flow.setToolTip(f"Сумма показанных сделок за {FLOW_SEC} с: покупки / продажи")
        self.regime = QLabel()  # М3 market regime of the main window (5 min by default)
        self.regime.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.regime.hide()
        self.info = QLabel()
        self.info.setStyleSheet(f"color:{MUTED.name()};")
        self.info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lock_mark = QLabel("🔒")
        self.lock_mark.setToolTip("Сквозные клики включены")
        self.lock_mark.hide()
        lay.addWidget(self.coin)
        lay.addWidget(self.regime)
        lay.addWidget(self.flow)
        lay.addWidget(self.info, 1)
        lay.addWidget(self.lock_mark)
        self.buttons = []
        for label, tip, signal in (
            ("⚙", "Настройки оверлея", overlay.settings_requested),
            ("🔓", "Сквозные клики: мышь проходит в терминал под оверлеем", overlay.lock_requested),
            ("✕", "Скрыть оверлей", overlay.hide_requested),
        ):
            b = QToolButton()
            b.setText(label)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QToolButton{color:#aab4bf;border:0;padding:0 4px;background:transparent;}"
                "QToolButton:hover{color:#e6edf3;background:rgba(255,255,255,30);border-radius:4px;}"
            )
            b.clicked.connect(signal.emit)
            lay.addWidget(b)
            self.buttons.append(b)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.window().windowHandle():
            self.window().windowHandle().startSystemMove()


class OverlayWindow(QWidget):
    settings_requested = Signal()
    lock_requested = Signal()
    hide_requested = Signal()
    coin_requested = Signal()

    def __init__(self, prefs: OverlayPrefs):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.prefs = prefs
        self.locked = False
        self.connected = False
        self.coin = ""
        self.flow: deque[tuple[float, str, float]] = deque()
        self.setWindowTitle("Radar Tape")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(200, 110)
        self.resize(470, 460)

        self.header = Header(self)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background:rgba(240,185,11,215);color:#111;padding:3px 8px;font-weight:600;")
        self.banner.hide()
        self.tape = TapeView(self, prefs)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.header)
        lay.addWidget(self.banner)
        lay.addWidget(self.tape, 1)
        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(14, 14)
        self.grip.setToolTip("Потяни, чтобы изменить размер")

        self._banner_timer = QTimer(self, singleShot=True, interval=10_000, timeout=self.banner.hide)
        self._flow_timer = QTimer(self, interval=1000, timeout=self._update_flow)
        self._flow_timer.start()
        self.apply_prefs()

    # ---- state -----------------------------------------------------------
    def set_prefs(self, prefs: OverlayPrefs) -> None:
        self.prefs = self.tape.prefs = prefs
        self.tape.rows = deque(filter(self.accepts, self.tape.rows), maxlen=prefs.max_rows)
        self.apply_prefs()

    def apply_prefs(self) -> None:
        f = self.font()
        f.setPointSizeF(max(8.0, self.prefs.font_size - 1))
        self.header.setFont(f)
        self.banner.setFont(f)
        self.tape.apply_prefs()
        self._update_info()
        self.update()

    def set_coin(self, coin: str) -> None:
        if coin and coin != self.coin:
            self.coin = coin
            self.header.coin.setText(coin)
            self.header.regime.hide()
            self.tape.clear()
            self.flow.clear()
            self._update_flow()
            self._update_info()

    def set_regime(self, msg: dict) -> None:
        """Badge of the market regime (М3): the main window's regime, all three in the tooltip."""
        if msg.get("coin") != self.coin:
            return
        windows = msg.get("windows") or []
        main = next((w for w in windows if w.get("w") == msg.get("main")), None)
        if not main:
            self.header.regime.hide()
            return
        fg, bg = REGIME_COLORS.get(main.get("tone"), REGIME_COLORS["none"])
        full = str(main.get("label") or "").split(" · ")[0]
        self.header.regime.setText(REGIME_SHORT.get(full, full))
        self.header.regime.setStyleSheet(
            f"color:{fg}; background:{bg}; border-radius:4px; padding:0 5px; font-weight:600;")
        ru = {"1m": "1м", "5m": "5м", "15m": "15м", "1h": "1ч"}
        tip = "\n".join(f"{ru.get(w.get('w'), w.get('w'))}: {w.get('label')}" for w in windows)
        if msg.get("divergence"):
            tip += "\n⚠ " + msg["divergence"]
        self.header.regime.setToolTip(f"Режим рынка (М3), окно {ru.get(main.get('w'), main.get('w'))}\n" + tip)
        self.header.regime.show()

    def set_connected(self, ok: bool, message: str = "") -> None:
        self.connected = ok
        self.header.coin.setStyleSheet(f"color:{(ACCENT if ok else MUTED).name()}; font-weight:700;")
        self.header.coin.setToolTip("Сменить монету" if ok else f"Нет связи с сервером. {message}".strip())
        self._update_info(message)

    def set_locked(self, locked: bool) -> None:
        if locked == self.locked:
            return
        self.locked = locked
        visible = self.isVisible()
        geo = self.geometry()
        # Changing window flags recreates the native window, so geometry is restored after.
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, locked)
        self.setGeometry(geo)
        for b in self.header.buttons:
            b.setVisible(not locked)
        self.header.lock_mark.setVisible(locked)
        self.grip.setVisible(not locked)
        if visible:
            self.show()

    def show_alert(self, alert: dict) -> None:
        kind = {"perp": " фьючерс", "spot": " спот"}.get(alert.get("kind") or "", "")
        reasons = "; ".join(alert.get("reasons") or [])
        self.show_message(f"⚠ {alert.get('venue')}{kind} · скор {alert.get('score', 0):.0f} — {reasons}")

    def show_message(self, text: str) -> None:
        self.banner.setText(text)
        self.banner.show()
        self._banner_timer.start()

    # ---- data ------------------------------------------------------------
    def accepts(self, r: Row | WallRow) -> bool:
        pr = self.prefs
        if isinstance(r, WallRow):
            return pr.show_walls and r.usd >= pr.wall_min_usd and (
                not pr.keys or f"{r.venue}:{r.kind}" in pr.keys)
        if r.usd < pr.min_usd:
            return False
        if pr.keys and f"{r.venue}:{r.kind}" not in pr.keys:
            return False
        if pr.side != "all" and r.side != pr.side:
            return False
        return not pr.only_repeats or r.rep > 0

    def add_trades(self, rows: list[dict]) -> None:
        now = time.time()
        shown = [r for r in map(row_from, rows) if self.accepts(r)]
        for r in shown:
            self.flow.append((now, r.side, r.usd))
        if shown:
            self.tape.add(shown)

    def add_walls(self, events: list[dict]) -> None:
        shown = [w for w in map(wall_from, events) if self.accepts(w)]
        if shown:
            self.tape.add(shown)

    def _update_flow(self) -> None:
        cutoff = time.time() - FLOW_SEC
        while self.flow and self.flow[0][0] < cutoff:
            self.flow.popleft()
        buy = sum(u for _, s, u in self.flow if s == "buy")
        sell = sum(u for _, s, u in self.flow if s == "sell")
        self.header.flow.setText(
            f'<span style="color:{BUY.name()}">▲{fmt_usd(buy)}</span>&nbsp;'
            f'<span style="color:{SELL.name()}">▼{fmt_usd(sell)}</span>'
        )

    def _update_info(self, message: str = "") -> None:
        pr = self.prefs
        if not pr.keys:
            where = "все биржи"
        else:
            names = [f"{v} {'F' if k == 'perp' else 'S'}" for v, k in map(split_key, pr.keys)]
            where = ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")
        extra = " · только повторы" if pr.only_repeats else ""
        extra += {"buy": " · покупки", "sell": " · продажи"}.get(pr.side, "")
        self.header.info.setText(f"от {fmt_usd(pr.min_usd)} · {where}{extra}")
        self.header.info.setToolTip(", ".join(pr.keys) or "Все биржи")
        if not self.connected:
            self.tape.empty_text = "Нет связи с сервером…" + (f"\n{message}" if message else "")
        else:
            self.tape.empty_text = f"Ждём сделки от {fmt_usd(pr.min_usd)}\n{where}{extra}"
        self.tape.update()

    # ---- painting / geometry --------------------------------------------
    def resizeEvent(self, e) -> None:
        self.grip.move(self.width() - self.grip.width(), self.height() - self.grip.height())
        super().resizeEvent(e)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha = int(255 * min(1.0, max(0.0, self.prefs.opacity)))
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        p.fillPath(path, QColor(*BG, alpha))
        p.save()
        p.setClipPath(path)
        p.fillRect(self.header.geometry(), QColor(27, 34, 44, min(255, alpha + 40)))
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawPath(path)
        if not self.locked:  # grip hint
            p.setPen(QPen(MUTED, 1))
            w, h = self.width(), self.height()
            for i in (4, 8):
                p.drawLine(w - 3, h - 3 - i, w - 3 - i, h - 3)
