"""BitMart (not in ccxt): spot + USDT perpetuals over public WebSocket."""
import time
import zlib
from datetime import datetime

import aiohttp

from app.collectors.base import NotListed, Stream, TradesCallback, get_json, parse_levels, ws_loop
from app.models import Trade

SPOT_REST = "https://api-cloud.bitmart.com/spot/quotation/v3/trades"
SPOT_BOOK = "https://api-cloud.bitmart.com/spot/quotation/v3/books"
SPOT_WS = "wss://ws-manager-compress.bitmart.com/api?protocol=1.1"
PERP_REST = "https://api-cloud-v2.bitmart.com/contract/public"
PERP_WS = "wss://openapi-ws-v2.bitmart.com/api?protocol=1.1"


def _inflate(data: bytes) -> str:
    return zlib.decompress(data, -zlib.MAX_WBITS).decode()


def _iso_ts(s: str) -> float:
    # "2026-09-23T11:43:03.362923323Z" -> trim nanoseconds for fromisoformat
    head, _, frac = s.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{head}.{(frac or '0')[:6]}+00:00").timestamp()


class BitmartStream(Stream):
    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback, session: aiohttp.ClientSession):
        super().__init__(venue, kind, coin, on_trades)
        self.session = session
        self.transport = "ws"
        self.contract_size = 1.0
        self.mult = 1.0

    async def resolve(self) -> None:
        if self.kind == "spot":
            sym = f"{self.coin}_USDT"
            data = await get_json(self.session, SPOT_REST, {"symbol": sym, "limit": 50})
            if data.get("code") != 1000:
                raise NotListed
            self.symbol = sym
            self._seed_rows = data.get("data") or []
            return
        for prefix in ("", "1000", "10000", "1000000"):
            sym = f"{prefix}{self.coin}USDT"
            data = await get_json(self.session, f"{PERP_REST}/details", {"symbol": sym})
            symbols = (data.get("data") or {}).get("symbols") or []
            if data.get("code") == 1000 and symbols:
                info = symbols[0]
                self.symbol = sym
                self.contract_size = float(info.get("contract_size") or 1)
                self.mult = float(prefix or 1)
                return
        raise NotListed

    async def seed(self) -> None:
        trades = []
        if self.kind == "spot":
            for _sym, ts, price, size, side in self._seed_rows:
                p, a = float(price), float(size)
                trades.append(Trade(int(ts) / 1000, p, a, p * a, side))
        else:
            data = await get_json(self.session, f"{PERP_REST}/market-trade", {"symbol": self.symbol, "limit": 100})
            for t in data.get("data") or []:
                p, q = float(t["price"]), float(t["qty"])  # qty is in base units
                side = "sell" if t.get("is_buyer_maker") else "buy"
                trades.append(Trade(float(t["time"]), p / self.mult, q * self.mult, p * q, side))
        self.emit_seed(trades)

    async def fetch_book(self) -> tuple[list, list]:
        if self.kind == "spot":
            data = await get_json(self.session, SPOT_BOOK, {"symbol": self.symbol, "limit": 50})
            book = data.get("data") or {}
            return parse_levels(book.get("bids")), parse_levels(book.get("asks"))
        data = await get_json(self.session, f"{PERP_REST}/depth", {"symbol": self.symbol})
        book = data.get("data") or {}  # volumes in contracts
        cs, mult = self.contract_size, self.mult
        return parse_levels(book.get("bids"), cs, mult), parse_levels(book.get("asks"), cs, mult)

    def _on_spot(self, msg) -> None:
        if not isinstance(msg, dict) or msg.get("table") != "spot/trade":
            return
        out = []
        for t in msg.get("data") or []:
            p, a = float(t["price"]), float(t["size"])
            out.append(Trade(t.get("ms_t", time.time() * 1000) / 1000, p, a, p * a, t.get("side", "?")))
        self.emit(out)

    def _on_perp(self, msg) -> None:
        if not isinstance(msg, dict) or not str(msg.get("group", "")).startswith("futures/trade"):
            return
        out = []
        for t in msg.get("data") or []:
            p = float(t["deal_price"])
            base = float(t["deal_vol"]) * self.contract_size
            side = "sell" if t.get("m") else "buy"  # m = buyer is maker
            ts = _iso_ts(t["created_at"]) if t.get("created_at") else time.time()
            out.append(Trade(ts, p / self.mult, base * self.mult, p * base, side))
        self.emit(out)

    async def stream(self) -> None:
        if self.kind == "spot":
            await ws_loop(
                self.session,
                SPOT_WS,
                [{"op": "subscribe", "args": [f"spot/trade:{self.symbol}"]}],
                self._on_spot,
                ping=lambda: "ping",
                decode=_inflate,
            )
        else:
            await ws_loop(
                self.session,
                PERP_WS,
                [{"action": "subscribe", "args": [f"futures/trade:{self.symbol}"]}],
                self._on_perp,
                ping=lambda: '{"action":"ping"}',
            )
