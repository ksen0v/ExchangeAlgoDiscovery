import asyncio

import pytest

from app import fx
from app.collectors import dex, rest_venues
from app.collectors.base import NotListed
from app.symbols import pick_ccxt_market

SEARCH = {
    "data": [
        {"id": "eth_0xaaa", "attributes": {"address": "0xaaa", "name": "PEPE / WETH 0.3%", "volume_usd": {"h24": "9000000"}},
         "relationships": {"dex": {"data": {"id": "uniswap_v3"}}, "base_token": {"data": {"id": "eth_0xpepe"}},
                           "network": {"data": {"id": "eth"}}}},
        {"id": "eth_0xbbb", "attributes": {"address": "0xbbb", "name": "PEPE / WETH", "volume_usd": {"h24": "100"}},
         "relationships": {"dex": {"data": {"id": "uniswap_v2"}}, "base_token": {"data": {"id": "eth_0xpepe"}},
                           "network": {"data": {"id": "eth"}}}},
        {"id": "bsc_0xccc", "attributes": {"address": "0xccc", "name": "PEPE / WBNB", "volume_usd": {"h24": "500000"}},
         "relationships": {"dex": {"data": {"id": "pancakeswap_v3"}}, "base_token": {"data": {"id": "bsc_0xpepe2"}},
                           "network": {"data": {"id": "bsc"}}}},
        {"id": "eth_0xddd", "attributes": {"address": "0xddd", "name": "WETH / PEPE", "volume_usd": {"h24": "99999999"}},
         "relationships": {"dex": {"data": {"id": "uniswap_v3"}}, "base_token": {"data": {"id": "eth_0xweth"}},
                           "network": {"data": {"id": "eth"}}}},
    ],
    "included": [
        {"id": "eth_0xpepe", "type": "token", "attributes": {"symbol": "PEPE"}},
        {"id": "bsc_0xpepe2", "type": "token", "attributes": {"symbol": "PEPE"}},
        {"id": "eth_0xweth", "type": "token", "attributes": {"symbol": "WETH"}},
    ],
}
SWAPS = {"data": [
    {"id": "eth_1_0xh1_0", "attributes": {"kind": "buy", "volume_in_usd": "5000", "to_token_amount": "500000000",
                                          "price_to_in_usd": "0.00001", "block_timestamp": "2026-10-05T10:00:00Z"}},
    {"id": "eth_2_0xh2_0", "attributes": {"kind": "sell", "volume_in_usd": "2000", "from_token_amount": "200000000",
                                          "price_from_in_usd": "0.00001", "block_timestamp": "2026-10-05T10:00:12Z"}},
    {"id": "eth_3_0xh3_0", "attributes": {"kind": "buy", "volume_in_usd": "0", "to_token_amount": "1",
                                          "price_to_in_usd": "1", "block_timestamp": "2026-10-05T10:00:13Z"}},
]}


def test_dex_pool_is_the_most_traded_of_that_dex_with_the_coin_as_base():
    assert dex.pick_pool(SEARCH, "PEPE", dex.DEX_IDS["uniswap"]) == {"network": "eth", "address": "0xaaa",
                                                                     "name": "PEPE / WETH 0.3%"}
    assert dex.pick_pool(SEARCH, "PEPE", dex.DEX_IDS["pancakeswap"])["network"] == "bsc"
    assert dex.pick_pool(SEARCH, "PEPE", dex.DEX_IDS["raydium"]) is None
    assert dex.pick_pool(SEARCH, "WETH", dex.DEX_IDS["uniswap"])["address"] == "0xddd"


def test_dex_swaps_become_buy_and_sell_prints():
    rows = dex.parse_swaps(SWAPS)
    assert [(k, t.side, t.usd, t.amount) for k, t in rows] == [
        ("eth_1_0xh1_0", "buy", 5000.0, 500000000.0), ("eth_2_0xh2_0", "sell", 2000.0, 200000000.0)]


def test_dex_stream_resolves_and_polls(monkeypatch):
    calls = []

    async def gt_get(_session, path, params=None):
        calls.append(path)
        return SEARCH if path == "/search/pools" else SWAPS

    monkeypatch.setattr(dex, "gt_get", gt_get)
    monkeypatch.setattr(dex, "_search", {})
    s = dex.DexStream("Uniswap", "spot", "PEPE", lambda *a: None, session=None, dex="uniswap")
    s2 = dex.DexStream("PancakeSwap", "spot", "PEPE", lambda *a: None, session=None, dex="pancakeswap")
    asyncio.run(s.resolve())
    asyncio.run(s2.resolve())
    assert calls.count("/search/pools") == 1  # one search serves every DEX
    assert len(asyncio.run(s.fetch_trades())) == 2 and calls[-1] == "/networks/eth/pools/0xaaa/trades"
    with pytest.raises(NotListed):
        asyncio.run(dex.DexStream("Orca", "perp", "PEPE", lambda *a: None, session=None, dex="orca").resolve())


def test_rate_limiter_spaces_requests(monkeypatch):
    lim = dex._Limiter(per_min=600)  # 0.1 s apart

    async def go():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for _ in range(4):
            await lim.wait()
        return loop.time() - t0

    assert asyncio.run(go()) >= 0.29


def test_pionex_and_zoomex_parsing(monkeypatch):
    async def get_json(_session, url, params=None):
        if "pionex" in url:
            return {"result": True, "data": {"trades": [
                {"tradeId": "1", "price": "2.5", "size": "100", "side": "SELL", "timestamp": 1_800_000_000_000}]}}
        return {"retCode": 0, "result": {"list": [
            {"execId": "x", "price": "2.5", "size": "10", "side": "Buy", "time": "1800000000000"}]}}

    monkeypatch.setattr(rest_venues, "get_json", get_json)
    p = rest_venues.PionexStream("Pionex", "perp", "wif", lambda *a: None, session=None)
    assert p.symbol == "WIF_USDT_PERP"
    (k, t), = asyncio.run(p.fetch_trades())
    assert (k, t.side, t.usd) == ("1", "sell", 250.0)
    z = rest_venues.ZoomexStream("Zoomex", "perp", "WIF", lambda *a: None, session=None)
    (k, t), = asyncio.run(z.fetch_trades())
    assert (k, t.side, t.usd) == ("x", "buy", 25.0)


def test_coindcx_converts_inr_and_waits_for_the_rate(monkeypatch):
    async def get_json(_session, url, params=None):
        return [{"p": "8500000", "q": "0.01", "T": 1_800_000_000_000, "m": True}]

    monkeypatch.setattr(rest_venues, "get_json", get_json)
    s = rest_venues.CoinDcxStream("CoinDCX", "spot", "BTC", lambda *a: None, session=None)
    monkeypatch.delitem(fx._rates, "INR", raising=False)
    asyncio.run(s.resolve())  # listed even before the INR rate is known
    assert asyncio.run(s.fetch_trades()) == []
    monkeypatch.setitem(fx._rates, "INR", 1 / 85)
    (_, t), = asyncio.run(s.fetch_trades())
    assert t.side == "sell" and round(t.price) == 100_000 and round(t.usd) == 1000


def test_fiat_rates_and_fiat_markets(monkeypatch):
    monkeypatch.setattr(fx, "_rates", dict(fx._rates))
    assert fx.apply_fiat({"JPY": 150.0, "EUR": 0.9, "XYZ": 5, "TRY": 0}) == 2
    assert fx.usd_rate("jpy") == pytest.approx(1 / 150) and fx.usd_rate("TRY") is None
    markets = {"a": {"symbol": "BTC/JPY", "base": "BTC", "quote": "JPY", "spot": True}}
    assert pick_ccxt_market(markets, "BTC", "spot", ["JPY"])[0]["symbol"] == "BTC/JPY"
    assert pick_ccxt_market(markets, "BTC", "spot") is None  # fiat only where configured
