import asyncio

import pytest

from app.collectors import binance_alpha as ba
from app.collectors.base import NotListed

TOKENS = {"code": "000000", "success": True, "data": [
    {"symbol": "KOGE", "alphaId": "ALPHA_22", "volume24h": "100", "chainId": "56"},
    {"symbol": "koge", "alphaId": "ALPHA_99", "volume24h": "5000", "chainId": "1"},
    {"symbol": "OTHER", "alphaId": "ALPHA_1"},
]}
TRADES = {"code": "000000", "success": True, "data": [
    {"a": 7, "p": "48.0", "q": "10", "T": 1_800_000_000_000, "m": False, "side": "SELL"},
    {"a": 8, "p": "48.1", "q": "2", "T": 1_800_000_000_500, "m": True, "side": "BUY"},
    {"a": 9, "p": "0", "q": "2", "T": 1_800_000_000_600, "m": False},
]}


@pytest.fixture
def fake_api(monkeypatch):
    calls = []

    async def get_json(_session, url, params=None):
        calls.append((url, params))
        return TOKENS if url == ba.TOKENS_URL else TRADES

    monkeypatch.setattr(ba, "get_json", get_json)
    monkeypatch.setattr(ba, "_tokens", (0.0, []))
    return calls


def test_resolve_picks_most_traded_chain_and_parses_trades(fake_api):
    s = ba.BinanceAlphaStream("Binance Alpha", "spot", "koge", lambda *a: None, session=None)
    asyncio.run(s.resolve())
    assert s.symbol == "ALPHA_99USDT"
    rows = asyncio.run(s.fetch_trades())
    assert [(k, t.side, t.usd) for k, t in rows] == [(7, "sell", 480.0), (8, "buy", 96.2)]
    assert fake_api[-1] == (ba.TRADES_URL, {"symbol": "ALPHA_99USDT", "limit": 100})


def test_unknown_ticker_is_not_listed(fake_api):
    s = ba.BinanceAlphaStream("Binance Alpha", "spot", "NOPE", lambda *a: None, session=None)
    with pytest.raises(NotListed):
        asyncio.run(s.resolve())


def test_error_payload_raises():
    with pytest.raises(ValueError):
        ba.payload({"code": "100001", "success": False, "message": "symbol not found"})


def test_taker_side_from_any_field_format():
    ts = ba.taker_side
    assert ts({"m": True}) is None and ts({"m": False}) is None
    assert ts({"m": "true"}) is None and ts({"m": "false"}) is None
    assert ts({"isBuyerMaker": 1}) == "sell" and ts({"isBuyerMaker": 0}) == "buy"
    assert ts({"side": "SELL"}) == "sell" and ts({"S": "Buy"}) == "buy"
    assert ts({"isBuy": False}) == "sell"
    assert ts({"p": "1"}) is None


def test_tick_rule_when_feed_has_no_side(fake_api, monkeypatch):
    rows = {"code": "000000", "data": [
        {"a": 1, "p": "10", "q": "1", "T": 1_000},
        {"a": 2, "p": "11", "q": "1", "T": 2_000},  # uptick: buy
        {"a": 3, "p": "9", "q": "1", "T": 3_000},  # downtick: sell
        {"a": 4, "p": "9", "q": "1", "T": 4_000},  # same price: same as before
    ]}

    async def get_json(_session, url, params=None):
        return TOKENS if url == ba.TOKENS_URL else rows

    monkeypatch.setattr(ba, "get_json", get_json)
    s = ba.BinanceAlphaStream("Binance Alpha", "spot", "KOGE", lambda *a: None, session=None)
    asyncio.run(s.resolve())
    got = []
    s.on_trades = lambda stream, trades, live: got.extend(trades)
    s.emit_seed([t for _, t in asyncio.run(s.fetch_trades())])
    sides = [t.side for t in got]
    assert sides[1:] == ["buy", "sell", "sell"]


def test_deprecated_false_flag_with_overlapping_pages(monkeypatch):
    first = [{"a": i, "T": i * 1000, "p": str(p), "q": "1", "m": False}
             for i, p in ((1, 10), (2, 11), (3, 9))]
    new = {"a": 4, "T": 4000, "p": "8", "q": "1", "m": False}
    batches = [first, [*first, new]]

    async def get_json(*args, **kwargs):
        return {"code": "000000", "data": batches.pop(0)}

    monkeypatch.setattr(ba, "get_json", get_json)
    got = []
    s = ba.BinanceAlphaStream("Binance Alpha", "spot", "X", lambda st, ts, live: got.extend((t, live) for t in ts), None)

    async def run():
        seen = set()
        for batch in range(2):
            rows = await s.fetch_trades()
            fresh = [t for key, t in rows if key not in seen]
            seen.update(key for key, _ in rows)
            (s.emit_seed if batch == 0 else s.emit)(fresh)
    asyncio.run(run())
    assert [t.side for t, _ in got] == ["?", "buy", "sell", "sell"]
    assert [live for _, live in got] == [False, False, False, True]
    assert all(t.inferred for t, _ in got[1:])


def test_alpha_keeps_distinct_aggregate_ids_in_same_millisecond(monkeypatch):
    async def get_json(*args, **kwargs):
        return {"code": "000000", "data": [
            {"a": 0, "T": 1000, "p": "10", "q": "1", "side": "BUY"},
            {"a": 1, "T": 1000, "p": "10", "q": "2", "side": "BUY"},
        ]}
    monkeypatch.setattr(ba, "get_json", get_json)
    got = []
    s = ba.BinanceAlphaStream("Binance Alpha", "spot", "X", lambda st, ts, live: got.extend(ts), None)
    rows = asyncio.run(s.fetch_trades())
    s.emit_seed([t for _, t in rows])
    assert rows[0][0] == 0 and len(got) == 2
    assert [t.tid for t in got] == ["0", "1"]
