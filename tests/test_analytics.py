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


def test_oi_chart_preserves_coins_usd_and_observation_times():
    a = Analytics()
    a.reset("X")
    a.oi("A:perp", 1000, 100, 200)
    a.oi("A:perp", 1010, 120, 250)
    a.oi("B:perp", 1005, 500, 900)
    data = a.oi_series("A:perp", 1011, 900)
    assert data["coin"] == "X" and data["key"] == "A:perp"
    assert data["points"] == [(1000, 100, 200), (1010, 120, 250)]
    assert data["last_ts"] == 1010 and not data["stale"]
    assert a.oi_series("A:perp", 1080, 900)["stale"]


def test_oi_chart_downsamples_old_history_and_marks_gaps():
    a = Analytics()
    for ts in (0, 5, 10, 65, 70, 5000):
        a.oi("A:perp", ts, ts + 1, None)
    # Tick prunes the dense hour; older values survive at one point per minute.
    a.tick(5001, None, {})
    short = a.oi_series("A:perp", 5001, 3600)
    long = a.oi_series("A:perp", 5001, 14400)
    assert short["points"] == [(5000, 5001, None)]
    assert long["points"] == [(10, 11, None), (70, 71, None), (5000, 5001, None)]
    assert 5000 - 70 > long["max_gap_sec"]
    a.drop_stream("A:perp")
    assert not a.oi_series("A:perp", 5001, 14400)["points"]


def test_oi_history_is_bounded_and_out_of_order_values_are_ignored():
    a = Analytics()
    for minute in range(1500):
        a.oi("A:perp", minute * 60, 100, 200)
    a.oi("A:perp", 5, 999, 999)
    assert len(a.oi_history["A:perp"]) <= 1441
    assert len(a.oi_grid["A:perp"]) <= 724
    assert a.oi_last["A:perp"][1] == 100


def test_oi_table_discovers_any_connected_venue_without_empty_templates():
    a = Analytics()
    streams = {f"Venue{i}:perp": FakeStream(f"Venue{i}", "perp") for i in range(12)}
    for s in streams.values():
        s.status = "live"
    for key in list(streams)[:8]:
        a.oi(key, 1000, 10, 20)
    streams["Venue0:perp"].status = "connecting"
    oi = a.snapshot(1001, streams)["oi"]
    assert {r["key"] for r in oi["venues"]} == {f"Venue{i}:perp" for i in range(1, 8)}
    assert oi["totals"]["count"] == 7 and oi["totals"]["coins"] == 70
    assert len(a.feed_status(streams, 1001)) == 12  # diagnostics retain failed / waiting venues


def test_total_oi_history_marks_changes_in_exchange_coverage():
    a = Analytics()
    one, two = FakeStream("One", "perp"), FakeStream("Two", "perp")
    one.status = two.status = "live"
    streams = {one.key:one, two.key:two}
    a.oi(one.key, 1000, 10, 20)
    a.tick(1001, 2, streams)
    a.oi(two.key, 1005, 50, 100)
    a.tick(1006, 2, streams)
    points = a.oi_series("__total__", 1007, 900)["points"]
    assert [p[1] for p in points] == [10, 60]
    assert points[0][3] != points[1][3]
    a.tick(1070, 2, streams)  # stale observations never become a new zero-valued sample
    assert a.oi_series("__total__", 1070, 900)["stale"]


def test_missing_oi_price_does_not_turn_a_partial_usd_total_into_full_coverage():
    a = Analytics()
    s = FakeStream(kind="perp")
    s.status = "live"
    a.oi(s.key, 1000, 100, None)
    total = a.snapshot(1001, {s.key:s})["oi"]["totals"]
    assert total["coins"] == 100 and total["usd"] is None
