"""Binance Alpha (not in ccxt): early-stage tokens traded inside the Binance app.

Public market endpoints of Alpha (www.binance.com/bapi/defi): the token list maps a
ticker to its alphaId (e.g. ALPHA_175), aggregated trades of "<alphaId>USDT" are
polled once a second. Alpha has no public order book here, so no walls for it.
"""
import time

import aiohttp

from app.collectors.base import NotListed, Stream, TradesCallback, get_json, poll_loop
from app.models import Trade

BASE = "https://www.binance.com/bapi/defi/v1/public"
TOKENS_URL = f"{BASE}/wallet-direct/buw/wallet/cex/alpha/all/token/list"
TRADES_URL = f"{BASE}/alpha-trade/agg-trades"
TOKENS_TTL = 600.0

_tokens: tuple[float, list[dict]] = (0.0, [])  # shared by all coins: one request per 10 min


def payload(data):
    """bapi wraps results: {"code": "000000", "success": true, "data": ...}."""
    if not isinstance(data, dict):
        return data
    if data.get("success") is False or str(data.get("code") or "000000") != "000000":
        raise ValueError(data.get("message") or f"code {data.get('code')}")
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("list"), list):
        return inner["list"]
    return inner


def num(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


async def alpha_tokens(session: aiohttp.ClientSession) -> list[dict]:
    global _tokens
    ts, rows = _tokens
    if rows and time.time() - ts < TOKENS_TTL:
        return rows
    rows = payload(await get_json(session, TOKENS_URL))
    if not isinstance(rows, list):
        raise ValueError("unexpected Alpha token list")
    _tokens = (time.time(), rows)
    return rows


class BinanceAlphaStream(Stream):
    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback, session: aiohttp.ClientSession):
        super().__init__(venue, kind, coin, on_trades)
        self.session = session
        self.transport = "rest"

    async def resolve(self) -> None:
        if self.kind != "spot":
            raise NotListed
        tokens = await alpha_tokens(self.session)
        same = [t for t in tokens if str(t.get("symbol") or "").upper() == self.coin and t.get("alphaId")]
        if not same:
            raise NotListed
        # one ticker can exist on several chains: take the most traded
        best = max(same, key=lambda t: (num(t.get("volume24h")), num(t.get("liquidity"))))
        self.symbol = f"{best['alphaId']}USDT"

    async def fetch_trades(self) -> list[tuple[object, Trade]]:
        rows = payload(await get_json(self.session, TRADES_URL, {"symbol": self.symbol, "limit": 100})) or []
        out = []
        for t in rows:
            p, q = num(t.get("p")), num(t.get("q"))
            if p <= 0 or q <= 0:
                continue
            side = "sell" if t.get("m") else "buy"  # m = buyer is maker
            ts = num(t.get("T")) / 1000 or time.time()
            key = t.get("a") or (t.get("T"), t.get("p"), t.get("q"), side)
            out.append((key, Trade(ts, p, q, p * q, side)))
        return out

    async def stream(self) -> None:
        await poll_loop(self.fetch_trades, self.emit, interval=1.0, emit_first=self._seed_poll, seen=self.poll_seen)
