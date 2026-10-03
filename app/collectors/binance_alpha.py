"""Binance Alpha (not in ccxt): early-stage tokens traded inside the Binance app.

Public market endpoints of Alpha (www.binance.com/bapi/defi): the token list maps a
ticker to its alphaId (e.g. ALPHA_175), aggregated trades of "<alphaId>USDT" are
polled once a second. Alpha has no public order book here, so no walls for it.
"""
import logging
import time

import aiohttp

from app.collectors.base import NotListed, Stream, TradesCallback, get_json, poll_loop
from app.models import Trade

BASE = "https://www.binance.com/bapi/defi/v1/public"
TOKENS_URL = f"{BASE}/wallet-direct/buw/wallet/cex/alpha/all/token/list"
TRADES_URL = f"{BASE}/alpha-trade/agg-trades"
TOKENS_TTL = 600.0

log = logging.getLogger(__name__)
MAKER_KEYS = ("m", "isBuyerMaker", "buyerMaker", "isBuyerMarker")  # true = buyer is maker -> taker sold
SIDE_KEYS = ("side", "S", "takerSide", "direction", "type")
BUY_WORDS = {"buy", "b", "bid", "long", "1"}
SELL_WORDS = {"sell", "s", "ask", "short", "2", "-1"}

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


def flag(v) -> bool | None:
    """true/false that may come as a bool, a number or a string."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return bool(v)
    if isinstance(v, str):
        v = v.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
    return None


def taker_side(t: dict) -> str | None:
    """Aggressor side from whichever field the feed uses; None when it has none."""
    for k in MAKER_KEYS:
        if k in t and (maker := flag(t[k])) is not None:
            return "sell" if maker else "buy"
    for k in SIDE_KEYS:
        v = str(t.get(k) or "").strip().lower()
        if v in BUY_WORDS:
            return "buy"
        if v in SELL_WORDS:
            return "sell"
    if "isBuy" in t and (buy := flag(t["isBuy"])) is not None:
        return "buy" if buy else "sell"
    return None


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
        self._last_price = 0.0
        self._last_side = "?"
        self._logged_sample = False

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
        if rows and not self._logged_sample:
            self._logged_sample = True
            log.info("%s %s trade sample: %s", self.key, self.symbol, rows[0])
        out = []
        for t in sorted(rows, key=lambda r: num(r.get("T"))):
            p, q = num(t.get("p") or t.get("price")), num(t.get("q") or t.get("qty"))
            if p <= 0 or q <= 0:
                continue
            side = taker_side(t)
            if side is None:  # no side in the feed: tick rule (uptick = buy, downtick = sell)
                if self._last_price and p != self._last_price:
                    side = "buy" if p > self._last_price else "sell"
                else:
                    side = self._last_side
            self._last_price, self._last_side = p, side
            ts = num(t.get("T")) / 1000 or time.time()
            key = t.get("a") or (t.get("T"), t.get("p"), t.get("q"))
            out.append((key, Trade(ts, p, q, p * q, side)))
        return out

    async def stream(self) -> None:
        await poll_loop(self.fetch_trades, self.emit, interval=1.0, emit_first=self._seed_poll, seen=self.poll_seen)
