from app.models import Trade
from app.walls import WallTracker

T0 = 1_800_000_000.0
KEY = "Binance:spot"
ARGS = {"min_usd": 10_000, "ratio": 8, "band_bps": 50}


def book(mid=100.0, wall=None, level=1_000.0):
    """20 levels a side, 1 bps apart; wall = (side, price, usd) replaces a level."""
    bids = [(round(mid * (1 - (i + 0.5) / 1e4), 6), level) for i in range(20)]
    asks = [(round(mid * (1 + (i + 0.5) / 1e4), 6), level) for i in range(20)]
    if wall:
        side, price, usd = wall
        levels = bids if side == "bid" else asks
        levels.append((price, usd))
    return bids, asks


def run(tr, ts, **kw):
    return tr.update(KEY, ts, *book(**kw), **ARGS)


def test_wall_reported_only_after_it_stands():
    tr = WallTracker()
    w = ("bid", 99.97, 50_000)
    assert run(tr, T0, wall=w) == []
    assert run(tr, T0 + 1, wall=w) == []
    ev = run(tr, T0 + 2, wall=w)
    assert [e["event"] for e in ev] == ["new"]
    assert ev[0]["side"] == "bid" and ev[0]["ratio"] == 50 and ev[0]["dist_bps"] == 3.0
    st = tr.state(KEY, T0 + 2)
    assert st["wall"]["usd"] == 50_000 and st["book"]


def test_flash_shorter_than_min_life_is_ignored():
    tr = WallTracker()
    run(tr, T0, wall=("ask", 100.05, 60_000))
    for t in (1, 2, 3):
        assert run(tr, T0 + t) == []
    assert tr.state(KEY, T0 + 3)["pulls"] == 0


def test_small_levels_and_far_walls_are_not_walls():
    tr = WallTracker()
    for t in range(4):
        assert run(tr, T0 + t, wall=("bid", 99.97, 5_000)) == []  # below min_usd
        assert run(tr, T0 + t, wall=("bid", 99.0, 90_000)) == []  # 100 bps away


def test_replaced_towards_the_market_counts_as_push():
    tr = WallTracker()
    for t in range(3):
        run(tr, T0 + t, wall=("bid", 99.97, 50_000))
    ev = run(tr, T0 + 3, mid=100.02, wall=("bid", 99.99, 52_000))
    assert [(e["event"], e["towards"]) for e in ev] == [("moved", True)]
    w = tr.state(KEY, T0 + 3)["wall"]
    assert w["push"] == 1 and w["moves"] == 1 and w["price"] == 99.99


def test_pulled_without_trades_is_spoof_like():
    tr = WallTracker()
    for t in range(3):
        run(tr, T0 + t, wall=("ask", 100.04, 80_000))
    assert run(tr, T0 + 2.5) == []  # missing briefly: flicker grace
    ev = run(tr, T0 + 3.5)
    assert [e["event"] for e in ev] == ["pulled"]
    st = tr.state(KEY, T0 + 5)
    assert st["pulls"] == 1 and st["pulled_usd"] == 80_000 and st["wall"] is None


def test_traded_through_is_eaten():
    tr = WallTracker()
    for t in range(3):
        run(tr, T0 + t, wall=("bid", 99.97, 50_000))
    tr.on_trades(KEY, [Trade(T0 + 2.2, 99.97, 400, 40_000, "sell")])
    assert run(tr, T0 + 2.5) == []
    ev = run(tr, T0 + 3.5)
    assert [e["event"] for e in ev] == ["eaten"]
    assert tr.state(KEY, T0 + 5)["pulls"] == 0


def test_stale_book_has_no_metrics():
    tr = WallTracker()
    for t in range(3):
        run(tr, T0 + t, wall=("bid", 99.97, 50_000))
    assert tr.state(KEY, T0 + 60) == {"book": False, "spread_bps": None, "wall": None, "pulls": 0, "pulled_usd": 0.0, "mm": None}


def test_detector_scores_a_pushing_wall_and_explains_it():
    from app.detector import Detector, DetectorConfig

    det = Detector(DetectorConfig())
    det.reset("X")
    for t in range(3):
        det.ingest_book(KEY, T0 + t, *book(wall=("bid", 99.97, 50_000)))
    det.ingest_book(KEY, T0 + 3, *book(mid=100.02, wall=("bid", 99.99, 50_000)))
    metrics, _ = det.tick(now=T0 + 3)
    m = metrics[KEY]
    assert m["wall"]["push"] == 1
    assert m["score"] >= 17
    assert any(r.startswith("Плита на покупку $50.0k") and "переставляют за ценой" in r for r in m["reasons"])
    det.cfg.update({"walls": False})
    assert det.ingest_book(KEY, T0 + 4, *book()) == []
    assert det.tick(now=T0 + 4)[0][KEY]["wall"] is None


def test_ccxt_book_is_converted_like_trades():
    from app.collectors.ccxt_stream import CcxtStream
    from app.fx import _rates

    s = CcxtStream("Gate", "perp", "PEPE", lambda *a: None, pool=None, ex_id="gate")
    s.quote, s.mult = "USDT", 1000.0
    s.market = {"contract": True, "contractSize": 10.0, "linear": True}
    bids, asks = s.convert_book({"bids": [[0.01, 5]], "asks": [[0.011, 2]]})  # 1000PEPE at $0.01, 5 contracts
    assert bids == [(0.01 / 1000, 0.01 * 5 * 10)] and asks[0][1] == 0.011 * 2 * 10
    s.quote, s.mult, s.market = "KRW", 1.0, {}
    _rates["KRW"] = 1 / 1000
    bids, _ = s.convert_book({"bids": [[1000.0, 3]], "asks": []})
    assert bids == [(1.0, 3.0)]


def test_parse_levels_accepts_lists_and_dicts():
    from app.collectors.base import parse_levels

    assert parse_levels([["2", "3", "9"], ["0", "1"]]) == [(2.0, 6.0)]
    assert parse_levels([{"price": "2", "qty": "3"}], amount_mult=10, price_div=1000) == [(0.002, 60.0)]


def test_hub_sends_walls_to_clients_that_want_them():
    from app.hub import Hub

    hub = Hub()
    a, b = hub.add(ws=None), hub.add(ws=None)
    b.set_filter({"keys": ["OKX:spot"]})
    hub.push_walls([{"key": KEY, "usd": 1.0, "event": "new"}])
    hub.flush_tape()
    assert "walls" in a.queue.get_nowait() and b.queue.empty()
    a.set_filter({"walls": False})
    hub.push_walls([{"key": KEY, "usd": 1.0, "event": "new"}])
    hub.flush_tape()
    assert a.queue.empty()


def test_market_maker_pair_is_not_a_wall():
    tr = WallTracker()
    both = book()
    bids, asks = both
    bids.append((99.96, 60_000))
    asks.append((100.04, 50_000))
    events = []
    for t in range(0, 40, 2):
        events += tr.update(KEY, T0 + t, list(bids), list(asks), **ARGS, ignore_mm=True)
    st = tr.state(KEY, T0 + 38, ignore_mm=True)
    assert st["wall"] is None and st["mm"] == {"bid": 60_000, "ask": 50_000}
    # pulling a market maker's quote is not a spoof
    bids.remove((99.96, 60_000))
    for t in (40, 42):
        events += tr.update(KEY, T0 + t, list(bids), list(asks), **ARGS, ignore_mm=True)
    assert not [e for e in events if e["event"] == "pulled"]
    assert tr.state(KEY, T0 + 42, ignore_mm=True)["pulls"] == 0
    # without the option the same book reports walls
    tr2 = WallTracker()
    for t in range(0, 40, 2):
        tr2.update(KEY, T0 + t, *book(wall=("bid", 99.96, 60_000)), **ARGS)
    assert tr2.state(KEY, T0 + 38)["wall"]["usd"] == 60_000


def test_one_sided_wall_is_still_a_wall_with_mm_option():
    tr = WallTracker()
    for t in range(0, 40, 2):
        tr.update(KEY, T0 + t, *book(wall=("bid", 99.97, 50_000)), **ARGS, ignore_mm=True)
    assert tr.state(KEY, T0 + 38, ignore_mm=True)["wall"]["usd"] == 50_000
