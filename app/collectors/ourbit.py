"""Ourbit (not in ccxt, MEXC-like API): perpetuals over WebSocket, spot over REST polling."""
import aiohttp

from app.collectors.base import NotListed, Stream, TradesCallback, get_json, parse_levels, poll_loop, ws_loop
from app.models import Trade

PERP_REST = "https://futures.ourbit.com/api/v1/contract"
PERP_WS = "wss://futures.ourbit.com/edge"
SPOT_REST = "https://api.ourbit.com/api/v3/trades"
SPOT_BOOK = "https://api.ourbit.com/api/v3/depth"


class OurbitStream(Stream):
    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback, session: aiohttp.ClientSession):
        super().__init__(venue, kind, coin, on_trades)
        self.session = session
        self.transport = "rest" if kind == "spot" else "ws"
        self.contract_size = 1.0

    async def resolve(self) -> None:
        if self.kind == "spot":
            self.symbol = f"{self.coin}USDT"
            if not isinstance(await get_json(self.session, SPOT_REST, {"symbol": self.symbol, "limit": 1}), list):
                raise NotListed
            return
        self.symbol = f"{self.coin}_USDT"
        data = await get_json(self.session, f"{PERP_REST}/detail", {"symbol": self.symbol})
        info = data.get("data")
        if not data.get("success") or not isinstance(info, dict) or not info.get("contractSize"):
            raise NotListed
        self.contract_size = float(info["contractSize"])

    def _perp_trade(self, t: dict) -> Trade:
        p = float(t["p"])
        base = float(t["v"]) * self.contract_size
        return Trade(float(t["t"]) / 1000, p, base, p * base, "buy" if t.get("T") == 1 else "sell")

    async def seed(self) -> None:
        if self.kind == "spot":
            return  # the first REST poll is the seed
        data = await get_json(self.session, f"{PERP_REST}/deals/{self.symbol}")
        self.emit_seed([self._perp_trade(t) for t in data.get("data") or []])

    async def fetch_book(self) -> tuple[list, list]:
        if self.kind == "spot":
            book = await get_json(self.session, SPOT_BOOK, {"symbol": self.symbol, "limit": 50})
            return parse_levels(book.get("bids")), parse_levels(book.get("asks"))
        data = await get_json(self.session, f"{PERP_REST}/depth/{self.symbol}")
        book = data.get("data") or {}  # [price, contracts, orders]
        cs = self.contract_size
        return parse_levels(book.get("bids"), cs), parse_levels(book.get("asks"), cs)

    def _on_msg(self, msg) -> None:
        if not isinstance(msg, dict) or msg.get("channel") != "push.deal":
            return
        data = msg.get("data")
        rows = data if isinstance(data, list) else [data]
        self.emit([self._perp_trade(t) for t in rows if t])

    async def stream(self) -> None:
        if self.kind == "perp":
            await ws_loop(
                self.session,
                PERP_WS,
                [{"method": "sub.deal", "param": {"symbol": self.symbol}}],
                self._on_msg,
                ping=lambda: '{"method":"ping"}',
            )
            return

        async def fetch():
            rows = await get_json(self.session, SPOT_REST, {"symbol": self.symbol, "limit": 100})
            out = []
            for t in rows if isinstance(rows, list) else []:
                p, a = float(t["price"]), float(t["qty"])
                side = "sell" if t.get("isBuyerMaker") else "buy"
                key = (t.get("time"), t.get("price"), t.get("qty"), side)
                out.append((key, Trade(float(t["time"]) / 1000, p, a, p * a, side)))
            return out

        await poll_loop(fetch, self.emit, interval=1.0, emit_first=self._seed_poll, seen=self.poll_seen)
