"""М8 DEX, М9 Hyperliquid, М12 venue quality (ТЗ, раздел 7: математика тиков Uniswap v3 и др.)."""
import math
import random
import time

import pytest

from app.analytics import uniswap as uni
from app.analytics.config import ModulesConfig
from app.analytics.engine import Analytics
from app.analytics.hyper import active_twaps, liq_map, parse_position
from app.analytics.leadlag import half_cross, xcorr_lead
from app.analytics.onchain import coin_price, explorer_tx, interp_depth, pick_token, select_pools
from app.analytics.quality import VenueQuality, combine
from app.models import Trade


@pytest.fixture
def cfg(tmp_path):
    c = ModulesConfig(tmp_path / "radar.yaml")
    c.load()
    return c


class S:
    def __init__(self, venue, kind="spot", coin="XYZ"):
        self.venue, self.kind, self.coin = venue, kind, coin
        self.key = f"{venue}:{kind}"
        self.pool = {"network": "eth"}


# ---- Uniswap v3 tick math --------------------------------------------------------------
def test_v3_constant_liquidity_amounts():
    L = 1_000_000.0
    a, b = 1.0, math.sqrt(1.01)
    assert uni.amount1_for_up(L, a, b) == pytest.approx(L * (b - a))
    assert uni.amount0_for_down(L, a, 1 / b) == pytest.approx(L * (b - 1))  # 1/√b' - 1/√a with √b' = 1/b
    assert uni.tick_sqrt(0) == 1.0 and uni.tick_sqrt(2) == pytest.approx(1.0001)
    # price of token0 in token1 from sqrtPriceX96, decimals applied (WETH 18 / USDC 6 at 2000)
    sqrt_x96 = int(math.sqrt(2000 * 10 ** (6 - 18)) * uni.Q96)
    assert uni.sqrt_x96_to_price(sqrt_x96, 18, 6) == pytest.approx(2000, rel=1e-9)


def test_v3_walk_across_initialised_ticks():
    L = 1000.0
    tick = 0
    net = {100: 500, -100: 300}  # +500 entering [100, ...) upwards; crossing -100 downwards subtracts 300
    s_target_up = uni.tick_sqrt(200)
    s100 = uni.tick_sqrt(100)
    up = uni.walk_cost(1.0, L, tick, net, s_target_up)
    assert up == pytest.approx(L * (s100 - 1) + (L + 500) * (s_target_up - s100))
    s_target_dn = uni.tick_sqrt(-200)
    sm100 = uni.tick_sqrt(-100)
    down = uni.walk_cost(1.0, L, tick, net, s_target_dn)
    assert down == pytest.approx(L * (1 / sm100 - 1) + (L - 300) * (1 / s_target_dn - 1 / sm100))


def test_v3_cost_to_move_in_usd_both_token_orders():
    L = 10**12
    sqrt_x96 = uni.Q96  # raw price 1
    up0, dn0 = uni.v3_cost_to_move(sqrt_x96, L, 0, {}, 1.0, True, 18, 18, coin_usd=2.0, quote_usd=1.0)
    assert up0 == pytest.approx(L * (math.sqrt(1.01) - 1) / 1e18)  # quote in, $1 each
    assert dn0 == pytest.approx(L * (1 / math.sqrt(0.99) - 1) / 1e18 * 2.0)  # the coin in, $2 each
    up1, _ = uni.v3_cost_to_move(sqrt_x96, L, 0, {}, 1.0, False, 18, 18, coin_usd=2.0, quote_usd=1.0)
    assert up1 == pytest.approx(L * (1 / math.sqrt(1 / 1.01) - 1) / 1e18)  # coin is token1: P goes down


def test_swap_log_decoding():
    data = "0x" + uni.enc_int(-5 * 10**18) + uni.enc_int(10 * 10**6) + uni.enc_int(int(uni.Q96 * math.sqrt(2e-12))) \
        + uni.enc_int(1) + uni.enc_int(0)
    side, coin, quote, price = uni.decode_swap(uni.TOPIC_SWAP_V3, data, True, 18, 6)
    assert (side, coin, quote) == ("buy", 5.0, 10.0) and price == pytest.approx(2.0)
    v2 = "0x" + uni.enc_int(0) + uni.enc_int(300 * 10**6) + uni.enc_int(100 * 10**18) + uni.enc_int(0)
    assert uni.decode_swap(uni.TOPIC_SWAP_V2, v2, True, 18, 6) == ("buy", 100.0, 300.0, None)
    assert uni.signed(2**256 - 1) == -1 and uni.signed(2**24 - 5, 24) == -5


# ---- DEX helpers ------------------------------------------------------------------------
def test_token_and_pools_selection():
    pairs = [
        {"chainId": "ethereum", "pairAddress": "0xp1", "dexId": "uniswap", "priceUsd": "0.00001", "priceNative": "5e-9",
         "baseToken": {"symbol": "XYZ", "address": "0xAAA"}, "quoteToken": {"symbol": "WETH", "address": "0xW"},
         "liquidity": {"usd": 900000}},
        {"chainId": "ethereum", "pairAddress": "0xp2", "dexId": "uniswap", "priceUsd": "2000", "priceNative": "2e8",
         "baseToken": {"symbol": "WETH", "address": "0xW"}, "quoteToken": {"symbol": "XYZ", "address": "0xaaa"},
         "liquidity": {"usd": 100000}},
        {"chainId": "bsc", "pairAddress": "0xp3", "baseToken": {"symbol": "XYZ", "address": "0xFAKE"},
         "quoteToken": {"symbol": "WBNB", "address": "0xB"}, "liquidity": {"usd": 3000}},
    ]
    t = pick_token(pairs, "XYZ")
    assert t["chain"] == "ethereum" and t["address"] == "0xaaa"
    pools = select_pools(pairs, "0xaaa", share=0.8)
    assert [p["address"] for p in pools] == ["0xp1"]  # 90 % of the liquidity is enough
    assert coin_price(pairs[1], "0xaaa") == pytest.approx(2000 / 2e8)  # the coin is the quote there
    assert explorer_tx("eth", "0xabc") == "https://etherscan.io/tx/0xabc"


def test_depth_interpolation():
    pts = [(1000, 0.2), (10000, 1.5), (100000, 6.0)]
    d1 = interp_depth(pts, 1.0)
    assert 1000 < d1 < 10000
    assert interp_depth(pts, 5.0) > 10000 and interp_depth(pts, 7.0) is None
    assert interp_depth([(500, 2.0)], 1.0) == pytest.approx(250)


def test_lead_lag():
    rnd = random.Random(1)
    a = [100.0]
    for _ in range(600):
        a.append(a[-1] * math.exp(rnd.gauss(0, 0.001)))
    b = [a[0]] * 4 + a[:-4]  # b repeats a 4 seconds later
    lag, corr = xcorr_lead(a, b, 10)
    assert lag == 4 and corr > 0.9
    lag2, _ = xcorr_lead(b, a, 10)
    assert lag2 == -4
    series = [(0, 100.0), (1, 100.2), (2, 100.6), (3, 101.0)]
    assert half_cross(series, 100.0, 101.0) == 2


def test_arbitrage_sandwich_and_whale(cfg):
    a = Analytics(cfg, None, None)
    a.reset("XYZ")
    t0 = time.time() - 120
    # CEX buys right after the swap, of a comparable size: likely arbitrage
    a.on_trades(S("Binance"), [Trade(t0 + 1, 1.0, 9000, 9000, "buy"), Trade(t0 + 2, 1.0, 6000, 6000, "buy")])
    a.on_trades(S("Uniswap"), [Trade(t0, 1.0, 12000, 12000, "buy", tid="0xtx1", taker="0xw", block=10)])
    assert a.arb_events and a.arb_events[0]["arb"]["cex_usd"] == 15000
    assert a.arb_events[0]["url"].endswith("0xtx1")
    # a sandwich (same address both ways in one block) is not a "large swap"
    a.on_trades(S("Uniswap"), [Trade(t0 + 5, 1.0, 50000, 50000, "buy", taker="0xmev", block=11),
                               Trade(t0 + 5, 1.0, 50000, 50000, "sell", taker="0xmev", block=11)])
    assert all(s.get("taker") != "0xmev" for s in a.swaps)
    # the whale: CEX flow the same way after the swap, clearly stronger than before
    a._check_whales(time.time())
    assert a.dex_whales and a.dex_whales[0]["tx"] == "0xtx1"


# ---- Hyperliquid -------------------------------------------------------------------------
def test_hyperliquid_parsing():
    state = {"assetPositions": [{"position": {"coin": "kPEPE", "szi": "-2000", "entryPx": "0.012",
                                              "positionValue": "25", "liquidationPx": "0.015",
                                              "leverage": {"type": "cross", "value": 10}, "unrealizedPnl": "-1"}}]}
    p = parse_position(state, "kPEPE", 1000.0)
    assert p["side"] == "short" and p["coins"] == 2_000_000 and p["usd"] == 25
    assert p["entry"] == pytest.approx(0.000012) and p["liq"] == pytest.approx(0.000015) and p["lev"] == 10
    assert parse_position(state, "BTC", 1.0) is None
    hist = [
        {"time": 1, "twapId": 7, "state": {"coin": "kPEPE", "side": "B", "sz": "100", "executedSz": "10",
                                            "minutes": 60, "timestamp": 1000, "randomize": True}, "status": {"status": "activated"}},
        {"time": 2, "twapId": 8, "state": {"coin": "kPEPE", "side": "A", "sz": "50", "executedSz": "50",
                                            "minutes": 30, "timestamp": 2000}, "status": {"status": "activated"}},
        {"time": 3, "twapId": 8, "state": {"coin": "kPEPE", "side": "A", "sz": "50", "executedSz": "50",
                                            "minutes": 30, "timestamp": 2000}, "status": {"status": "finished"}},
    ]
    act = active_twaps(hist, "kPEPE")
    assert len(act) == 1 and act[0]["side"] == "buy" and act[0]["progress"] == pytest.approx(0.1)
    m = liq_map({"a": {"side": "long", "usd": 100.0, "liq": 0.97}, "b": {"side": "short", "usd": 50.0, "liq": 1.012}},
                1.0, 0.5)
    assert m["longs"][6] == 100 and m["shorts"][2] == 50


# ---- М12 --------------------------------------------------------------------------------
def test_mirror_pairs_and_wash_venue_scores_low(cfg):
    q = VenueQuality(cfg)
    rnd = random.Random(3)
    t0 = 1_000_000.0
    market = {}
    price = 1.0
    for m in range(60):
        move = rnd.gauss(0, 0.002)
        price *= 1 + move
        market[int(t0) // 60 + m] = price
        for name in ("A", "B", "C"):  # real venues: flow follows the price, all sizes
            trades = []
            for i in range(20):
                usd = rnd.lognormvariate(5, 1.5)
                side = "buy" if rnd.random() < 0.5 + move * 150 else "sell"
                trades.append(Trade(t0 + m * 60 + i * 3, price, usd / price, usd, side))
            q.on_trades(f"{name}:spot", trades)
        wash = []  # wash venue: buy and sell the same size at the same price within a second, flow unrelated
        for i in range(10):
            ts = t0 + m * 60 + i * 5
            wash += [Trade(ts, price, 5000.0, 5000.0 * price, "buy"), Trade(ts + 0.3, price, 5000.0, 5000.0 * price, "sell")]
        q.on_trades("W:spot", wash)
    scores = q.compute(t0 + 3600, lambda k: 50_000.0, market)
    assert scores["W:spot"]["features"]["mirror"] == pytest.approx(1.0)
    assert scores["W:spot"]["score"] < 0.3 < min(scores[k]["score"] for k in ("A:spot", "B:spot", "C:spot"))
    assert q.weight("W:spot") == 0.0 and q.weight("A:spot") > 0.3 and q.weight("unknown") == 1.0
    assert combine({"a": 1.0, "b": 0.0, "c": None}) == pytest.approx(0.2)


def test_excluded_venue_leaves_the_delta(cfg):
    a = Analytics(cfg, None, None)
    a.reset("XYZ")
    a.quality.loaded["W:spot"] = 0.1  # stored score below 0.3
    now = time.time()
    a.on_trades(S("W"), [Trade(now - 5, 1.0, 1000, 1000, "buy")])
    a.on_trades(S("A"), [Trade(now - 5, 1.0, 300, 300, "buy")])
    d = a._delta(now, [60])
    assert d["w"][60]["delta_spot"] == pytest.approx(300)  # W's buys are not in the sum
    assert {v["key"] for v in d["w"][60]["venues"]} == {"W:spot", "A:spot"}  # but still visible
