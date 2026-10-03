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
    {"a": 7, "p": "48.0", "q": "10", "T": 1_800_000_000_000, "m": True},
    {"a": 8, "p": "48.1", "q": "2", "T": 1_800_000_000_500, "m": False},
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
