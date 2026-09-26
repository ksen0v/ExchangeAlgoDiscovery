from app.detector import Detector, DetectorConfig, find_algo
from app.models import Trade, aggregate_fills
from app.symbols import base_multiplier, pick_ccxt_market

NOW = 1_800_000_000.0
PERP = {"swap": True, "linear": True}


def quiet_then_burst(det: Detector, key: str, burst: list[Trade]) -> None:
    # 10 minutes of small two-sided trades, then the burst
    quiet = [Trade(NOW - 650 + i * 5, 1.0, 50, 50.0, "buy" if i % 2 else "sell") for i in range(125)]
    det.ingest(key, quiet, now=NOW)
    det.ingest(key, burst, now=NOW)


def test_base_multiplier():
    assert base_multiplier("PEPE", "pepe") == 1
    assert base_multiplier("1000PEPE", "PEPE") == 1000
    assert base_multiplier("kPEPE", "PEPE") == 1000
    assert base_multiplier("1MBABYDOGE", "BABYDOGE") == 1_000_000
    assert base_multiplier("WBTC", "BTC") is None


def test_pick_market_prefers_usdt_linear_and_overrides():
    markets = {
        "a": {"symbol": "X/USDC", "base": "X", "quote": "USDC", "spot": True},
        "b": {"symbol": "X/USDT", "base": "X", "quote": "USDT", "spot": True},
        "c": {"symbol": "X/KRW", "base": "X", "quote": "KRW", "spot": True},
        "d": {"symbol": "1000X/USDT:USDT", "base": "1000X", "quote": "USDT", "settle": "USDT", **PERP},
        "e": {"symbol": "X/USDC:USDT", "base": "X", "quote": "USDC", "settle": "USDT", **PERP},
    }
    assert pick_ccxt_market(markets, "X", "spot")[0]["symbol"] == "X/USDT"
    assert pick_ccxt_market(markets, "X", "spot", ["KRW"])[0]["symbol"] == "X/KRW"
    m, mult = pick_ccxt_market(markets, "X", "perp")
    assert m["symbol"] == "1000X/USDT:USDT" and mult == 1000
    assert pick_ccxt_market(markets, "Y", "spot") is None


def test_aggregate_fills_merges_same_ms_same_side():
    fills = [Trade(NOW, 1.0, 10, 10, "buy"), Trade(NOW, 1.02, 10, 10.2, "buy"), Trade(NOW + 1, 1.0, 5, 5, "sell")]
    out = aggregate_fills(fills)
    assert len(out) == 2
    assert out[0].fills == 2 and abs(out[0].usd - 20.2) < 1e-9


def test_volume_spike_with_buy_imbalance_alerts():
    det = Detector(DetectorConfig())
    det.reset("X")
    burst = [Trade(NOW - 20 + i, 1.0, 2000, 2000.0 + i * 37, "buy") for i in range(15)]
    quiet_then_burst(det, "Tiny:spot", burst)
    metrics, alerts = det.tick(now=NOW)
    m = metrics["Tiny:spot"]
    assert m["ratio"] > 50
    assert m["buy_share"] == 1.0
    assert m["score"] >= 50
    assert alerts and alerts[0]["key"] == "Tiny:spot"
    # cooldown: same score again is not re-alerted
    _, again = det.tick(now=NOW + 1)
    assert not again


def test_algo_pattern_detects_equal_sized_orders():
    cfg = DetectorConfig()
    trades = [Trade(NOW + i * 2.0, 1.0, 5000, 5000.0 * (1 + (i % 3) * 0.005), "buy") for i in range(10)]
    trades += [Trade(NOW + i, 1.0, 1, 300.0 + i * 97, "sell") for i in range(10)]
    algo = find_algo(trades, cfg)
    assert algo and algo["side"] == "buy" and algo["count"] == 10
    assert algo["regular"] and abs(algo["interval"] - 2.0) < 0.01


def test_quiet_stream_is_not_flagged():
    det = Detector(DetectorConfig())
    det.reset("X")
    quiet_then_burst(det, "Tiny:spot", [])
    metrics, alerts = det.tick(now=NOW)
    assert metrics["Tiny:spot"]["score"] == 0 and not alerts


def test_dead_market_awakening_uses_connection_time():
    """No trades at all for 10 min, then a burst: baseline is zero, not 'warming'."""
    det = Detector(DetectorConfig())
    det.reset("X")
    det.mark_connected("Dead:perp", NOW - 700)
    burst = [Trade(NOW - 10 + i, 1.0, 3000, 3000.0, "buy") for i in range(8)]
    det.ingest("Dead:perp", burst, now=NOW)
    metrics, alerts = det.tick(now=NOW)
    m = metrics["Dead:perp"]
    assert not m["warming"] and m["ratio"] >= 500  # $24k in 30s over a zero baseline
    assert alerts


def test_price_leader_deviation():
    det = Detector(DetectorConfig())
    det.reset("X")
    for k in ("A:spot", "B:spot", "C:spot"):
        det.ingest(k, [Trade(NOW - 600 + i, 100.0, 1, 100.0, "buy") for i in range(0, 600, 2)], now=NOW)
    for t in range(-40, 0):  # build premium history at 0 bps
        det.tick(now=NOW - 600 + 590 + t)
    # C runs +1% ahead of the market with buys
    det.ingest("C:spot", [Trade(NOW - 5 + i, 101.0, 60, 6060.0, "buy") for i in range(5)], now=NOW)
    metrics, _ = det.tick(now=NOW)
    assert metrics["C:spot"]["prem_bps"] > 90
    assert metrics["C:spot"]["dev_bps"] > 90
    assert metrics["A:spot"]["dev_bps"] < 10
