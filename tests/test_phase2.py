"""М5 order books, М6 borrowing, М7 index constituents (ТЗ, раздел 7: cost_to_move и др.)."""
import asyncio
import time

import pytest

from app.analytics.book import BookAnalyzer, book_metrics
from app.analytics.borrow import BorrowTracker, apr_from_hourly, key_problems, signed_query
from app.analytics.config import ModulesConfig
from app.analytics.engine import Analytics
from app.analytics.index import model_index, parse_binance, parse_bybit, parse_okx, weighted_median
from app.models import Trade


@pytest.fixture
def cfg(tmp_path):
    c = ModulesConfig(tmp_path / "radar.yaml")
    c.load()
    return c


# ---- М5 -------------------------------------------------------------------------------
def test_cost_to_move_and_buckets():
    bids = [(99.95, 1000), (99.55, 2000), (98.5, 4000), (96.0, 8000)]
    asks = [(100.05, 1000), (100.8, 3000), (101.55, 5000), (104.9, 7000)]
    m = book_metrics(bids, asks, [0.5, 1, 2, 5])
    assert m["mid"] == 100.0
    assert m["ask"][1] == 4000 and m["ask"][2] == 9000 and m["ask"][5] == 16000  # cost to move UP by x %
    assert m["bid"][0.5] == 3000 and m["bid"][2] == 7000 and m["bid"][5] == 15000  # cost to move DOWN
    assert m["bid_b"][0] == 1000 and m["bid_b"][4] == 2000 and m["ask_b"][15] == 5000
    assert m["reach_bid"] == pytest.approx(4.0) and m["reach_ask"] == pytest.approx(4.9)


def _feed(an, key, ts, bids, asks):
    an.on_book(key, "spot", ts, bids, asks)


def test_iceberg_and_fast_recovery(cfg):
    an = BookAnalyzer(cfg)
    asks = [(100.1, 1000.0), (100.2, 1000.0)]
    bids = [(99.9, 1000.0), (99.8, 1000.0)]
    _feed(an, "X:spot", 1000.0, bids, asks)
    _feed(an, "X:spot", 1001.0, bids, asks)
    # 3000 USD bought at 100.1 while only 1000 was ever visible there, the level still stands: iceberg
    an.on_trades("X:spot", [Trade(1001.5, 100.1, 30, 3000, "buy", ts_local=1001.5)])
    _feed(an, "X:spot", 1002.0, bids, asks)
    assert [e["kind"] for e in an.events] == ["iceberg"] and an.events[0]["side"] == "ask"
    # the best bid eaten by market sells, back within a second
    an.on_trades("X:spot", [Trade(1002.5, 99.9, 9, 900, "sell", ts_local=1002.5)])
    _feed(an, "X:spot", 1003.0, [(99.9, 50.0), (99.8, 1000.0)], asks)
    _feed(an, "X:spot", 1004.0, bids, asks)
    assert an.events[-1]["kind"] == "recovery" and an.events[-1]["side"] == "bid"


def test_vanishing_big_order_and_defended_levels(cfg):
    an = BookAnalyzer(cfg)
    v = an._v("X:spot")
    v.sizes.extend([1000.0] * 300)
    base_b, base_a = [(99.95, 1000.0), (99.9, 1000.0)], [(100.05, 1000.0)]
    _feed(an, "X:spot", 10.0, base_b, base_a)
    big = base_b + [(99.85, 50000.0)]  # 0.15 % below the price
    for t in (11.0, 12.0, 13.0, 14.0):
        _feed(an, "X:spot", t, big, base_a)
    _feed(an, "X:spot", 15.0, base_b, base_a)  # pulled, nothing traded there
    spoofs = [e for e in an.events if e["kind"] == "spoof"]
    assert len(spoofs) == 1 and spoofs[0]["side"] == "bid" and spoofs[0]["usd"] == 50000
    assert an.spoof_counts(16.0)["X:spot"]["bid"] == 1
    # three icebergs at one price -> a defended level
    for i in range(3):
        an._event("X:spot", "iceberg", "bid", 99.9, 5000, 20.0 + i)
    d = an.defended(30.0)
    assert d and d[0]["count"] == 3 and d[0]["side"] == "bid"


def test_aggregate_book_and_gaps(cfg):
    an = BookAnalyzer(cfg)
    now = time.time()
    lv = lambda mid, side: [(mid * (1 + side * (i + 0.5) / 1000), 1000.0) for i in range(1, 50) if i not in (20, 21)]  # noqa: E731
    _feed(an, "A:spot", now, [(p, u) for p, u in lv(100, -1)], [(p, u) for p, u in lv(100, 1)])
    _feed(an, "B:perp", now, [(p, u) for p, u in lv(200, -1)], [(p, u) for p, u in lv(200, 1)])
    agg = an.aggregate(now)
    assert agg["venues"] == 2
    assert agg["ask"][1.0] == pytest.approx(2 * 10 * 1000, rel=0.15)  # both venues, each relative to its own mid
    assert any(agg["gaps_ask"][19:22]) and not agg["gaps_ask"][5]


# ---- М6 -------------------------------------------------------------------------------
def test_binance_signature_and_key_rights():
    q = signed_query("secret", {"timestamp": 1, "recvWindow": 5000})
    assert q.startswith("timestamp=1&recvWindow=5000&signature=") and len(q.split("signature=")[1]) == 64
    assert key_problems({"enableReading": True, "ipRestrict": True}) == []
    assert set(key_problems({"enableReading": True, "enableWithdrawals": True, "enableSpotAndMarginTrading": True})) \
        == {"вывод", "торговля спот/маржа"}
    assert apr_from_hourly(0.0001) == pytest.approx(87.6)


def test_trading_key_is_refused(cfg):
    tr = BorrowTracker(cfg, None, "k", "s")

    async def fake(path, params=None):
        assert path == "/sapi/v1/account/apiRestrictions"
        return {"enableReading": True, "enableWithdrawals": True}

    tr._binance = fake
    asyncio.run(tr.check_key())
    assert tr.key_ok is False and "вывод" in tr.key_state


def test_borrow_change_and_signal(cfg):
    a = Analytics(cfg, None, None)
    a.reset("XYZ")
    tr = a.borrow
    tr._polling = "XYZ"
    t0 = 1_000_000.0
    tr._put("Binance", t0, kind="CEX", available=1000.0, rate_h=0.00001, rate_apr=8.76)
    tr._put("Binance", t0 + 4 * 3600, kind="CEX", available=400.0, rate_h=0.00001, rate_apr=8.76)
    assert tr.change_pct("Binance", t0 + 4 * 3600, 4 * 3600) == pytest.approx(-60.0)
    p2 = {"borrow": {"rows": [{**tr.rows["Binance"], "chg_4h": -60.0, "rate_ratio": None}]}}
    sig = a._sig_borrow({"w": {}}, {"totals": {"pct": {}}}, {"f8_median": None}, p2)
    assert sig[0][0] == "borrow_dry" and sig[0][2] == pytest.approx(60 / 50)


# ---- М7 -------------------------------------------------------------------------------
def test_index_parsers_and_model():
    assert parse_binance({"constituents": [{"exchange": "binance", "symbol": "XUSDT", "price": "1.0", "weight": "0.5"}]})[0] \
        == {"exchange": "binance", "symbol": "XUSDT", "api_price": 1.0, "weight": 0.5}
    okx = parse_okx({"data": [{"components": [{"exch": "Gate", "symbol": "X/USDT", "symPx": "2", "cnvPx": "2.01",
                                               "wgt": "0.25"}]}]})
    assert okx[0]["api_price"] == 2.01 and okx[0]["weight"] == 0.25
    byb = parse_bybit({"result": {"components": [{"exchange": "Mexc", "spotPair": "XUSDT", "equivalentPrice": "3",
                                                  "price": "3", "weight": "0.1"}]}})
    assert byb[0]["exchange"] == "Mexc" and byb[0]["weight"] == 0.1
    assert weighted_median([(1, 1), (2, 1), (10, 5)]) == 10
    parts = [(100.0, 1), (100.0, 1), (120.0, 1)]
    assert model_index(parts, "none") == pytest.approx(320 / 3)
    assert model_index(parts, "clamp:5") == pytest.approx((100 + 100 + 105) / 3)  # outlier cut to median +5 %
    assert model_index(parts, "exclude:10") == pytest.approx(100.0)


def test_index_pull_signal(cfg):
    a = Analytics(cfg, None, None)
    a.reset("XYZ")
    tr = a.index
    tr._set("Binance", "XYZUSDT", [{"exchange": e, "symbol": "XYZUSDT", "api_price": None, "weight": w}
                                   for e, w in (("binance", 0.4), ("okex", 0.3), ("mexc", 0.3))])
    prices = {"Binance": 1.0, "OKX": 1.0, "MEXC": 1.01}  # MEXC 1 % above the others
    depth = {"Binance": 1e6, "OKX": 8e5, "MEXC": 5e3}
    out = []
    for t in (0.0, 5.0, 12.0):
        out = tr.compute(1000.0 + t, prices.get, depth.get, {"Binance": 1.003})
    rows = {r["venue"]: r for r in out[0]["rows"]}
    assert rows["MEXC"]["dev"] == pytest.approx(1.0) and rows["MEXC"]["persist"] == pytest.approx(12.0)
    assert rows["MEXC"]["influence"] == pytest.approx(0.3)
    sig = {k: st for t, k, st, _ in a._sig_index({"index": out, "aggs": {}}) if t == "index_pull"}
    assert sig["Binance|MEXC"] >= 1 and (sig["Binance|OKX"] or 0) < 1


def test_mirror_signals(cfg):
    from app.analytics.stats import robust_stats

    a = Analytics(cfg, None, None)
    a.reset("XYZ")
    sym = robust_stats([x / 100 for x in range(-50, 51)])  # p10 -0.4, p50 0, p90 0.4
    for k in ("spot", "perp", "all"):
        a.baselines.session_cache[f"nd_{k}_5m"] = sym
    r = {"ret": -0.6, "nd_perp": -0.8, "nd_spot": 0.1, "nd_all": -0.3, "delta_spot": 1e4, "delta_perp": -9e4,
         "z_spot": 0.2, "z_perp": -2.0, "z_all": -1.0}
    sig = {t: st for t, _, st, _ in a._sig_delta({"w": {300: r}})}
    assert sig["lever_drop"] >= 1 and (sig["lever_rally"] or 0) < 1  # price fell on perp selling, spot did not sell
    a.baselines.session_cache["f8_med"] = robust_stats([0.01 + x / 10000 for x in range(-50, 51)])
    fund = {"f8_median": -0.05, "cross_basis": None, "cross_ref": "Binance", "premium": None}
    out = {t: st for t, _, st, _ in a._sig_funding({"w": {300: {"ret": 0.0}, 900: {}}}, {"totals": {"pct": {}}}, fund, 3.0)}
    assert out["crowd_short"] >= 1 and (out["crowd_long"] or 0) < 1
