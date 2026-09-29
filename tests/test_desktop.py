"""Desktop app logic that runs without a display (Qt offscreen platform)."""
import json
import os

import pytest

pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from desktop.feed import Feed  # noqa: E402
from desktop.hotkeys import parse  # noqa: E402
from desktop.overlay import OverlayWindow, fmt_price, fmt_usd, row_from  # noqa: E402
from desktop.prefs import OverlayPrefs, Prefs  # noqa: E402

NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def trade(key="KuCoin:spot", usd=2000.0, side="buy", **kw):
    return {"ts": NOW, "key": key, "side": side, "price": 1.0, "amount": usd, "usd": usd, **kw}


def test_prefs_roundtrip_and_tolerant_load(tmp_path):
    path = tmp_path / "desktop.json"
    p = Prefs()
    p.overlay.keys = ["KuCoin:spot"]
    p.overlay.min_usd = 1500
    p.save(path)
    assert Prefs.load(path).overlay.keys == ["KuCoin:spot"]
    raw = json.loads(path.read_text("utf-8"))
    raw.update(local_port="oops", demo=1, overlay={**raw["overlay"], "font_size": 14, "opacity": "x"})
    path.write_text(json.dumps(raw), "utf-8")
    q = Prefs.load(path)
    assert q.local_port == 8765 and q.demo is False  # wrong types ignored
    assert q.overlay.font_size == 14 and q.overlay.opacity == OverlayPrefs().opacity
    path.write_text("{broken", "utf-8")
    assert Prefs.load(path) == Prefs()


def test_hotkey_parse():
    assert parse("ctrl+alt+t") == (0x3, ord("T"))
    assert parse("Ctrl + Shift + F9") == (0x6, 0x78)
    assert parse("t") is None and parse("ctrl+hyper+t") is None and parse("ctrl+alt+??") is None


def test_formatting():
    assert fmt_price(65432.1) == "65,432.1"
    assert fmt_price(3.214571) == "3.21457"
    assert fmt_price(0.0000123456) == "0.0000123456"
    assert fmt_usd(1234) == "$1.2k" and fmt_usd(250_000) == "$250k" and fmt_usd(3_100_000) == "$3.10M"


def test_overlay_filters_and_refilter_on_prefs_change(qapp):
    ov = OverlayWindow(OverlayPrefs(keys=["KuCoin:spot", "MEXC:spot"], min_usd=1000))
    ov.add_trades([
        trade(),
        trade(usd=500),  # below the minimum
        trade(key="Binance:spot"),  # venue not selected
        trade(key="MEXC:spot", side="sell", grp=3, rep=4),
    ])
    assert [(r.venue, r.side) for r in ov.tape.rows] == [("MEXC", "sell"), ("KuCoin", "buy")]
    ov.set_prefs(OverlayPrefs(keys=["KuCoin:spot", "MEXC:spot"], min_usd=1000, only_repeats=True))
    assert [(r.venue, r.rep) for r in ov.tape.rows] == [("MEXC", 4)]


def test_overlay_drops_optional_columns_when_narrow(qapp):
    ov = OverlayWindow(OverlayPrefs(show_qty=True))
    ov.tape.add([row_from(trade(usd=12345.0))])
    wide = ov.tape._columns(900)
    assert {"time", "venue", "price", "qty", "usd"} <= wide.keys()
    narrow = ov.tape._columns(220)
    assert "venue" in narrow and "usd" in narrow and "qty" not in narrow
    x, w = narrow["usd"]
    assert x + w <= 220


def test_overlay_lock_is_click_through_and_keeps_geometry(qapp):
    ov = OverlayWindow(OverlayPrefs())
    ov.setGeometry(100, 120, 400, 300)
    ov.show()
    ov.set_locked(True)
    assert ov.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert ov.isVisible() and ov.geometry().topLeft().x() == 100
    assert all(not b.isVisible() for b in ov.header.buttons)
    ov.set_locked(False)
    assert not ov.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert ov.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    ov.hide()


def test_feed_ws_url():
    assert Feed("http://1.2.3.4:8000").ws_url() == "ws://1.2.3.4:8000/ws"
    assert Feed("https://radar.example.com/", "t k").ws_url() == "wss://radar.example.com/ws?token=t+k"


def test_on_screen_rejects_windows_above_or_taller_than_screen(qapp):
    from desktop.app import on_screen

    g = qapp.primaryScreen().availableGeometry()
    assert on_screen(QRect(g.left() + 50, g.top() + 50, 400, 300))
    assert not on_screen(QRect(g.left() + 50, g.top() - 200, 400, 300))  # title bar above the screen
    assert not on_screen(QRect(g.left() + 50, g.top(), 400, g.height() + 200))  # taller than the screen
    assert not on_screen(QRect(g.right() + 500, g.top() + 50, 400, 300))  # on an unplugged monitor


def test_overlay_shows_wall_events_with_filters(qapp):
    ov = OverlayWindow(OverlayPrefs(keys=["KuCoin:spot"], wall_min_usd=30_000))
    ev = {"ts": NOW, "key": "KuCoin:spot", "side": "bid", "price": 1.0, "usd": 50_000.0, "dist_bps": 3.0,
          "event": "moved", "towards": True}
    ov.add_walls([ev, {**ev, "usd": 10_000.0}, {**ev, "key": "OKX:spot"}])
    assert len(ov.tape.rows) == 1
    ov.resize(470, 300)
    assert not ov.tape.grab().isNull()  # paints a wall row
    ov.set_prefs(OverlayPrefs(keys=["KuCoin:spot"], show_walls=False))
    assert not ov.tape.rows


def test_wall_text_starts_with_what_happened():
    from desktop.overlay import WallRow, wall_text

    w = WallRow(NOW, "KuCoin", "spot", "ask", 65_000.0, 251_000.0, 5.0, "pulled")
    assert wall_text(w).startswith("▼ СНЯЛИ $251k @65,000.0")
