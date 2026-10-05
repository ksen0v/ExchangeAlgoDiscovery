"""Calculations of the analysis modules (ТЗ, раздел 7)."""
import asyncio
import gzip
import json
import time

import pytest

from app.analytics.config import ModulesConfig, parse_duration
from app.analytics.derivs import annual_pct, basis_pct, f8, interval_hours, liq_side, liq_usd, oi_coins, premium_pct
from app.analytics.engine import STEP, Analytics, depth_1pct
from app.analytics.journal import Journal, report
from app.analytics.recorder import Recorder
from app.analytics.regime import RegimeTracker, classify, sign
from app.analytics.signals import SignalBoard
from app.analytics.stats import pct_rank, robust_stats, robust_z
from app.analytics.store import AnalyticsStore, Baselines
from app.collectors.base import Stream
from app.models import Trade


class FakeStream(Stream):
    def __init__(self, venue="Binance", kind="spot", coin="XYZ"):
        super().__init__(venue, kind, coin, lambda *a: None)
        self.symbol = f"{coin}/USDT"
        self.quote = "USDT"


@pytest.fixture
def cfg(tmp_path):
    c = ModulesConfig(tmp_path / "radar.yaml")
    c.load()
    return c


# ---- robust z-score / percentiles ----------------------------------------------
def test_robust_z_and_percentiles():
    vals = [1, 2, 3, 4, 100]
    st = robust_stats(vals)
    assert st["median"] == 3 and st["mad"] == 1  # |dev| = 2,1,0,1,97 -> median 1
    assert robust_z(6, st["median"], st["mad"]) == pytest.approx(3 / 1.4826)
    assert robust_z(5, 5, 0) is None  # no spread: no z
    assert st["p50"] == 3 and st["p10"] == pytest.approx(1.4)
    assert pct_rank(3, st["sorted"]) == 50.0
    assert robust_stats([]) is None


# ---- taker side -------------------------------------------------------------------
def test_tick_rule_marks_inferred_side():
    s = FakeStream()
    got = []
    s.on_trades = lambda st, trades, live: got.extend(trades)
    s.emit([Trade(1.0, 10.0, 1, 10, "?"), Trade(2.0, 10.5, 1, 10.5, "?"), Trade(3.0, 10.5, 1, 10.5, "?"),
            Trade(4.0, 10.2, 1, 10.2, "?"), Trade(5.0, 10.3, 1, 10.3, "sell")])
    assert [t.side for t in got] == ["?", "buy", "buy", "sell", "sell"]
    assert [t.inferred for t in got] == [False, True, True, True, False]  # the venue's own side is kept
    assert all(t.ts_local > 0 for t in got)


@pytest.mark.parametrize("ex_id,raw,expected", [
    ("binanceusdm", "sell", "long"),  # forceOrder S=SELL: a long was closed
    ("binanceusdm", "buy", "short"),
    ("bybit", "buy", "long"),  # allLiquidation S=Buy: the long position was liquidated
    ("bybit", "sell", "short"),
    ("okx", "", None),
])
def test_liquidation_side(ex_id, raw, expected):
    assert liq_side(raw, ex_id) == expected


def test_liquidation_usd():
    usd, price = liq_usd({"contracts": 10, "price": 2.0}, {"contractSize": 100}, fx=1.0, mult=1000)
    assert usd == 2000 and price == pytest.approx(0.002)
    assert liq_usd({"quoteValue": 500}, {}, fx=1.1)[0] == pytest.approx(550)


# ---- open interest in coins ----------------------------------------------------------
def test_oi_coins_units():
    m = {"contractSize": 1}
    assert oi_coins({"openInterestAmount": 1000}, m, "binanceusdm", 2.0) == 1000  # Binance: coins
    assert oi_coins({"openInterestAmount": 50}, {"contractSize": 10}, "okx", 2.0) == 500  # OKX: contracts x ctVal
    assert oi_coins({"openInterestValue": 4000}, m, "bingx", 2.0) == 2000  # value only: / price
    assert oi_coins({"openInterestAmount": 7, "openInterestValue": 4000}, m, "xt", 2.0) == 2000
    # 1000PEPE contract: coins of 1000PEPE x 1000 = PEPE
    assert oi_coins({"openInterestAmount": 3}, m, "bybit", 0.012, mult=1000) == 3000
    assert oi_coins({}, m, "bybit", 2.0) is None


# ---- funding / basis --------------------------------------------------------------------
def test_funding_f8_and_basis():
    assert f8(0.0001, 8) == pytest.approx(0.0001)
    assert f8(0.0001, 1) == pytest.approx(0.0008)  # hourly funding (Hyperliquid)
    assert f8(0.0002, 4) == pytest.approx(0.0004)
    assert annual_pct(0.0001, 8) == pytest.approx(10.95)
    assert interval_hours("4h") == 4 and interval_hours(28800) == 8 and interval_hours(None) is None
    assert basis_pct(101, 100) == pytest.approx(1.0)
    assert premium_pct(99.5, 100) == pytest.approx(-0.5)


# ---- delta -----------------------------------------------------------------------------
def test_delta_spot_vs_perp(cfg):
    a = Analytics(cfg, None, None)
    a.reset("XYZ")
    now = 1_000_000.0
    spot, perp = FakeStream("Binance", "spot"), FakeStream("Bybit", "perp")
    a.on_trades(spot, [Trade(now - 30, 1.0, 100, 1000, "buy"), Trade(now - 20, 1.0, 40, 400, "sell")])
    a.on_trades(perp, [Trade(now - 100, 1.0, 500, 500, "sell"), Trade(now - 10, 1.0, 200, 200, "buy")])
    # depth within 1 %: (price, usd) levels around mid 1.0
    a.on_book(spot, now, [(0.999, 5000), (0.98, 99999)], [(1.001, 5000)])
    a.on_book(perp, now, [(0.999, 10000)], [(1.001, 10000)])
    d = a._delta(now, [60, 300])
    w1, w5 = d["w"][60], d["w"][300]
    assert w1["delta_spot"] == 600 and w1["delta_perp"] == 200  # the perp sell is older than 1 min
    assert w5["delta_perp"] == -300
    assert d["depth"] == {"spot": 10000, "perp": 20000}
    assert w1["nd_spot"] == pytest.approx(0.06) and w1["nd_perp"] == pytest.approx(0.01)
    assert w1["div"] == pytest.approx(-0.05)
    assert a.cvd == {"spot": 600, "perp": -300}
    assert {v["key"] for v in w5["venues"]} == {"Binance:spot", "Bybit:perp"}


def test_depth_1pct():
    d, mid = depth_1pct([(99.5, 100), (98, 1000)], [(100.5, 200), (102, 1000)])
    assert mid == 100 and d == 300


def test_open_interest_deltas_and_share(cfg):
    a = Analytics(cfg, None, None)
    a.reset("XYZ")
    t0 = 2_000_000.0
    for i in range(0, 361, STEP):  # 6 minutes of a 5-second grid
        ts = t0 + i
        a.oi("Binance:perp", ts, 1000 + i, None)  # +300 coins over 5 min
        a.oi("OKX:perp", ts, 500.0, None)  # flat
        a.oi_grid["Binance:perp"].add(ts, 1000 + i)
        a.oi_grid["OKX:perp"].add(ts, 500.0)
    oi = a._open_interest(t0 + 360, [60, 300])
    b = oi["venues"]["Binance:perp"]
    assert b["d"][300] == 300 and b["pct"][300] == pytest.approx(300 / 1060 * 100)
    assert b["share"][300] == 1.0 and oi["venues"]["OKX:perp"]["share"][300] == 0.0
    assert oi["totals"]["d"][300] == 300


# ---- regime -----------------------------------------------------------------------
@pytest.mark.parametrize("p,o,dp,ds,liq,code", [
    (1, 1, 1, 1, False, "longs_open_spot"),
    (1, 1, 1, 0, False, "longs_open_fragile"),
    (1, 1, 1, -1, False, "longs_open_fragile"),
    (1, -1, 0, 0, False, "short_squeeze"),
    (1, -1, 0, 0, True, "short_squeeze_liq"),
    (-1, 1, -1, -1, False, "shorts_open_spot"),
    (-1, 1, -1, 0, False, "shorts_open"),
    (-1, -1, 0, 0, True, "longs_close_liq"),
    (-1, -1, 0, 0, False, "longs_close"),
    (0, 1, 1, 0, False, "accumulation_buy"),
    (0, 1, -1, 0, False, "accumulation_sell"),
    (0, -1, 0, 0, False, "exit"),
    (1, 0, 0, 1, False, "spot_rally"),
    (1, 0, 0, 0, False, "undefined"),
    (-1, 1, 1, 0, False, "undefined"),
    (1, None, 1, 1, False, "undefined"),  # no OI data
])
def test_regime_classifier(p, o, dp, ds, liq, code):
    assert classify(p, o, dp, ds, liq).code == code


def test_regime_deadzone_and_divergence():
    assert sign(0.1, 0.15) == 0 and sign(0.2, 0.15) == 1 and sign(-0.2, 0.15) == -1 and sign(None, 1) is None
    tr = RegimeTracker()
    tr.update("1m", classify(1, 1, 1, 1, False), 1.0)
    tr.update("15m", classify(-1, -1, 0, 0, False), 1.0)
    assert "расходятся" in tr.divergence("1m", "15m")
    tr.update("1m", classify(-1, -1, 0, 0, False), 5.0)
    assert tr.divergence("1m", "15m") is None and len(tr.history) == 1


# ---- signals: hysteresis and cooldown ---------------------------------------------
def test_signal_hysteresis_and_cooldown():
    b = SignalBoard()
    up = lambda s, t: b.update("demand_rally", "all", s, {}, t, 70, 50, 300)  # noqa: E731
    assert up(0.9, 0) is False  # below the threshold
    assert up(1.2, 1) is True  # turns on: alert
    assert up(0.8, 2) is False and b.active()  # 0.8*70=56 >= 50: still on
    assert up(0.6, 3) is False and not b.active()  # 42 < 50: off
    assert up(1.5, 10) is False and b.active()  # on again, but within the 5-minute cooldown: no alert
    up(0.1, 11)
    assert up(1.1, 400) is True  # cooldown over


# ---- baselines / journal ---------------------------------------------------------------
def test_session_baseline_and_low_history():
    bl = Baselines(None, (7, 30), min_samples=10)
    bl.reset("XYZ")
    for i in range(20):
        bl.observe("m", float(i), 1000.0 + i * 5)
    bl.refresh_session(1100.0)
    st = bl.stats("m")
    assert st["src"] == "session" and st["n"] == 20 and st["p50"] == pytest.approx(9.5)
    assert bl.low_history(1100.0)


def test_journal_outcomes_and_report(tmp_path):
    async def run():
        store = AnalyticsStore(str(tmp_path / "a.db"))
        await store.open()
        j = Journal(store)
        j.reset("XYZ")
        t0 = 10_000.0
        await j.add({"ts": t0, "coin": "XYZ", "module": "М1", "type": "hidden_buyer", "title": "Скрытый покупатель",
                     "key": "all", "direction": 1, "price": 100.0, "score": 80, "data": {"reasons": ["x"]}})
        for minute, price in [(1, 101.0), (5, 99.0), (15, 102.0), (60, 103.0)]:
            await j.on_price(t0 + minute * 60, price)
        rows = await store.signals("XYZ")
        await store.close()
        return rows

    rows = asyncio.run(run())
    r = rows[0]
    assert r["r1"] == pytest.approx(1.0) and r["r5"] == pytest.approx(-1.0) and r["r15"] == pytest.approx(2.0)
    assert r["r60"] == pytest.approx(3.0) and r["up"] == pytest.approx(3.0) and r["down"] == pytest.approx(-1.0)
    assert r["done"] == 1
    rep = report(rows)
    t = rep["types"][0]
    assert t["count"] == 1 and t["hit_rate"] == 1.0 and t["mfe"] == pytest.approx(3.0) and t["mae"] == pytest.approx(-1.0)
    assert sum(t["hist_r15"]) == 1 and t["hist_r15"][7] == 1  # +2 % is in the ">= 2 %" bucket
    short = report([{**r, "type": "hidden_seller", "direction": -1}])["types"][0]
    assert short["avg_r15"] == pytest.approx(-2.0) and short["hit_rate"] == 0.0


# ---- config ----------------------------------------------------------------------------
def test_config_validation_keeps_defaults(tmp_path):
    p = tmp_path / "radar.yaml"
    c = ModulesConfig(p)
    c.load()
    assert p.exists() and c.get("regime.price_deadzone_pct") == 0.15 and c.get("alerts.cooldown_min") == 5
    p.write_text(p.read_text(encoding="utf-8").replace("alert_z: 3.0", "alert_z: много"), encoding="utf-8")
    c.load()
    assert c.get("alert_z") == 3.0 and any("alert_z" in e for e in c.errors)
    with pytest.raises(ValueError):
        c.update({"modules.delta": "может быть"})
    c.update({"modules.delta": False, "collect_only": True})
    c2 = ModulesConfig(p)
    c2.load()
    assert c2.on("delta") is False and c2.collect_only is True
    assert parse_duration("5m") == 300 and parse_duration("1h") == 3600 and c.windows() == [60, 300, 900, 3600]


# ---- raw data ---------------------------------------------------------------------------
def test_recorder_writes_unified_trades(cfg, tmp_path):
    rec = Recorder(cfg, tmp_path / "raw")
    s = FakeStream("Bybit", "perp")
    ts = time.time()
    rec.trades(s, [Trade(ts, 2.0, 5.0, 10.0, "buy", inferred=True, ts_local=ts + 0.1, tid="42")], True)
    asyncio.run(rec.flush())
    files = list((tmp_path / "raw").rglob("XYZ.trades.jsonl.gz"))
    assert len(files) == 1
    row = json.loads(gzip.open(files[0]).read().decode().splitlines()[0])
    assert row["exchange"] == "Bybit" and row["market"] == "perp" and row["side_inferred"] is True
    assert row["notional_usd"] == 10.0 and row["trade_id"] == "42" and row["ts_local"] - row["ts_exchange"] == 100


# ---- pollers over a ccxt-like instance ---------------------------------------------------
class FakeEx:
    has = {"fetchOpenInterest": True, "fetchFundingRate": True, "fetchFundingInterval": True,
           "watchLiquidations": True, "fetchOpenInterestHistory": True}

    def __init__(self):
        self.liq_sent = False

    async def fetch_open_interest(self, symbol):
        return {"openInterestAmount": 20, "openInterestValue": None}  # contracts on OKX

    async def fetch_open_interest_history(self, symbol, timeframe, since=None, limit=None, params=None):
        t = 1_700_000_000_000
        return [{"timestamp": t, "openInterestAmount": 100}, {"timestamp": t + 300_000, "openInterestAmount": 110},
                {"timestamp": t + 600_000, "openInterestAmount": 99}, {"timestamp": t + 3_600_000, "openInterestAmount": 1}]

    async def fetch_funding_rate(self, symbol):
        return {"fundingRate": 0.0002, "markPrice": 0.0105, "indexPrice": 0.01, "interval": None}

    async def fetch_funding_interval(self, symbol):
        return {"interval": "4h"}

    async def watch_liquidations(self, symbol):
        if self.liq_sent:
            await asyncio.sleep(10)
        self.liq_sent = True
        return [{"timestamp": 1_700_000_000_000, "price": 0.0104, "contracts": 3, "side": "sell"}]


def test_ccxt_deriv_feed_polls_everything(cfg):
    from app.analytics.derivs import CcxtDerivFeed

    a = Analytics(cfg, None, None)
    a.reset("PEPE")
    s = FakeStream("OKX", "perp", "PEPE")
    s.ex_id, s.ex, s.mult = "okx", FakeEx(), 1000.0  # 1000PEPE-style contract
    s.market = {"contractSize": 10}
    a.last["OKX:perp"] = (time.time(), 0.00001)  # USD per PEPE

    async def run():
        feed = CcxtDerivFeed(s, cfg, a)
        feed.start()
        await asyncio.sleep(0.3)
        feed.stop()
        return feed

    feed = asyncio.run(run())
    assert feed.status == {"oi": "ok", "funding": "ok", "liq": "ok"}
    _, coins, usd = a.oi_last["OKX:perp"]
    assert coins == 20 * 10 * 1000 and usd == pytest.approx(2.0)  # 200 contracts of 1000 PEPE, $0.00001 each
    fd = a.fund["OKX:perp"]
    assert fd["interval_h"] == 4 and fd["interval_assumed"] is False
    assert fd["mark"] == pytest.approx(0.0000105) and fd["index"] == pytest.approx(0.00001)
    ev = a.liq_feed[0]
    assert ev["side"] == "long" and ev["usd"] == pytest.approx(3 * 10 * 0.0104)
    # 5-minute OI history -> ΔOI % samples of this venue's baseline (the 55-minute gap is skipped)
    hist = [(m, v) for _, m, _, v in a.baselines.pending if m == "doi5:OKX"]
    assert [round(v, 6) for _, v in hist] == [10.0, -10.0]
