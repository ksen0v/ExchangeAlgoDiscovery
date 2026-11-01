"""Coverage for the retained in-memory analytics (run by GitHub Actions)."""
from app.analytics.derivs import oi_coins
from app.analytics.engine import Analytics
from app.collectors.base import Stream
from app.models import Trade


class FakeStream(Stream):
    def __init__(self, venue="Binance", kind="spot"):
        super().__init__(venue, kind, "XYZ", lambda *args: None)


def test_tick_rule_marks_inferred_side():
    s = FakeStream()
    got = []
    s.on_trades = lambda st, trades, live: got.extend(trades)
    s.emit([Trade(1.0, 10.0, 1, 10, "?"), Trade(2.0, 10.5, 1, 10.5, "?"), Trade(3.0, 10.5, 1, 10.5, "?"),
            Trade(4.0, 10.2, 1, 10.2, "?"), Trade(5.0, 10.3, 1, 10.3, "sell")])
    assert [t.side for t in got] == ["?", "buy", "buy", "sell", "sell"]
    assert [t.inferred for t in got] == [False, True, True, True, False]  # the venue's own side is kept
    assert all(t.ts_local > 0 for t in got)


def test_oi_coins_units():
    m = {"contractSize": 1}
    assert oi_coins({"openInterestAmount": 1000}, m, "binanceusdm", 2.0) == 1000  # Binance: coins
    assert oi_coins({"openInterestAmount": 50}, {"contractSize": 10}, "okx", 2.0) == 500  # OKX: contracts x ctVal
    assert oi_coins({"openInterestValue": 4000}, m, "bingx", 2.0) == 2000  # value only: / price
    assert oi_coins({"openInterestAmount": 7, "openInterestValue": 4000}, m, "xt", 2.0) == 2000
    # 1000PEPE contract: coins of 1000PEPE x 1000 = PEPE
    assert oi_coins({"openInterestAmount": 3}, m, "bybit", 0.012, mult=1000) == 3000
    assert oi_coins({}, m, "bybit", 2.0) is None


def test_cvd_excludes_seed_but_delta_includes_it():
    a = Analytics()
    a.reset("X")
    s = FakeStream()
    s.status = "live"
    a.on_trades(s, [Trade(1000, 1, 20, 20, "buy")], live=False)
    a.on_trades(s, [Trade(1001, 1, 5, 5, "sell")])
    a.tick(1002, 1, {s.key: s})
    snap = a.snapshot(1002, {s.key: s})
    assert snap["cvd"][-1][1] == -5
    assert snap["delta"]["venues"]["1m"][0]["delta"] == 15


def test_stale_oi_is_excluded_from_totals():
    a = Analytics()
    s = FakeStream(kind="perp")
    s.status = "live"
    a.oi(s.key, 1000, 100, 200)
    assert a.snapshot(1001, {s.key: s})["oi"]["totals"]["coins"] == 100
    stale = a.snapshot(1061, {s.key: s})["oi"]
    assert stale["totals"]["coins"] == 0 and stale["venues"][0]["coins"] is None


def test_history_is_bounded_and_removed_stream_is_forgotten():
    a = Analytics()
    s = FakeStream()
    a.on_trades(s, [Trade(1000, 1, 20, 20, "buy")])
    a.tick(5000, 1, {s.key: s})
    assert not a.flows[s.key]
    a.drop_stream(s.key)
    assert s.key not in a.flows and s.key not in a.last
