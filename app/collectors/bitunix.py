"""Bitunix (not in ccxt): USDT perpetuals over public WebSocket. Spot has no public API."""
import time

import aiohttp

from app.collectors.base import NotListed, Stream, TradesCallback, get_json, ws_loop
from app.models import Trade

PAIRS_URL = "https://fapi.bitunix.com/api/v1/futures/market/trading_pairs"
WS_URL = "wss://fapi.bitunix.com/public/"


class BitunixStream(Stream):
    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback, session: aiohttp.ClientSession):
        super().__init__(venue, kind, coin, on_trades)
        self.session = session
        self.transport = "ws"
        self.mult = 1.0
        self.merge_fills = False  # all trades of a message carry the message timestamp

    async def resolve(self) -> None:
        for prefix in ("", "1000", "10000", "1000000"):
            sym = f"{prefix}{self.coin}USDT"
            data = await get_json(self.session, PAIRS_URL, {"symbols": sym})
            rows = data.get("data") or []
            if data.get("code") == 0 and rows:
                self.symbol = sym
                self.mult = float(prefix or 1)  # "base" field says PEPE even for 1000PEPEUSDT
                return
        raise NotListed

    def _on_msg(self, msg) -> None:
        if not isinstance(msg, dict) or msg.get("ch") != "trade":
            return
        ts = float(msg.get("ts") or time.time() * 1000) / 1000  # per-trade "t" has 1s resolution
        out = []
        for t in msg.get("data") or []:
            p, v = float(t["p"]), float(t["v"])  # v is in base units
            out.append(Trade(ts, p / self.mult, v * self.mult, p * v, t.get("s", "?")))
        self.emit(out)

    async def stream(self) -> None:
        await ws_loop(
            self.session,
            WS_URL,
            [{"op": "subscribe", "args": [{"symbol": self.symbol, "ch": "trade"}]}],
            self._on_msg,
            ping=lambda: f'{{"op":"ping","ping":{int(time.time())}}}',
            ping_interval=20,
        )
