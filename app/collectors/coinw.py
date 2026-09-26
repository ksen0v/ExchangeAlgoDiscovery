"""CoinW (not in ccxt). Its public WebSocket is behind a CDN that answers 403 to
non-browser clients, so both markets are polled over REST once per second."""
from datetime import datetime, timedelta, timezone

import aiohttp

from app.collectors.base import NotListed, Stream, TradesCallback, get_json, poll_loop
from app.models import Trade

SPOT_URL = "https://api.coinw.com/api/v1/public"
PERP_URL = "https://api.coinw.com/v1/perpumPublic/trades"
CN_TZ = timezone(timedelta(hours=8))  # spot trade times are UTC+8 strings


class CoinwStream(Stream):
    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback, session: aiohttp.ClientSession):
        super().__init__(venue, kind, coin, on_trades)
        self.session = session
        self.transport = "rest"

    async def _fetch_raw(self) -> list[dict]:
        if self.kind == "spot":
            params = {"command": "returnTradeHistory", "symbol": self.symbol}
            data = await get_json(self.session, SPOT_URL, params)
            if str(data.get("code")) != "200":
                raise ValueError(data.get("msg") or "bad response")
        else:
            data = await get_json(self.session, PERP_URL, {"base": self.symbol})
            if data.get("code") != 0:
                raise ValueError(data.get("msg") or "bad response")
        return data.get("data") or []

    async def resolve(self) -> None:
        self.symbol = f"{self.coin}_USDT" if self.kind == "spot" else self.coin.lower()
        try:
            rows = await self._fetch_raw()
        except ValueError:
            raise NotListed from None
        if not rows:
            raise NotListed

    def _convert(self, t: dict) -> tuple[object, Trade]:
        p = float(t["price"])
        if self.kind == "spot":
            a = float(t["amount"])
            ts = datetime.strptime(t["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ).timestamp()
            side = "buy" if t.get("type") == "BUY" else "sell"
        else:
            a = float(t["quantity"])  # base units
            ts = float(t["createdDate"]) / 1000
            side = "buy" if t.get("direction") == "long" else "sell"
        return t.get("id"), Trade(ts, p, a, p * a, side)

    async def stream(self) -> None:
        async def fetch():
            return [self._convert(t) for t in await self._fetch_raw()]

        await poll_loop(fetch, self.emit, interval=1.0, emit_first=self._seed_poll, seen=self.poll_seen)
