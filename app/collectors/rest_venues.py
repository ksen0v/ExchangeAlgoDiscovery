"""Exchanges outside ccxt with simple public REST trade endpoints, polled once a second.

Pionex: spot COIN_USDT and perpetuals COIN_USDT_PERP.
CoinDCX: its own INR market I-COIN_INR (the B-... USDT pairs only mirror Binance).
Zoomex: Bybit-style v3 API, USDT perpetuals.
"""
import aiohttp

from app.collectors.base import NotListed, Stream, TradesCallback, get_json, poll_loop
from app.fx import usd_rate
from app.models import Trade


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


class NoPublicApi(Stream):
    """A market that trades on the venue's site, but whose data the venue does not publish
    (no public API): shown as such instead of silently missing or "not listed"."""

    REASON = {
        "BYDFi": "BYDFi публикует рыночные данные только по фьючерсам; по споту публичного API нет",
        "Bitunix": "у спота Bitunix нет публичного API (только фьючерсы)",
    }

    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback, session=None):
        super().__init__(venue, kind, coin, on_trades)

    async def run(self) -> None:
        self.status = "noapi"
        self.error = self.REASON.get(self.venue, "у биржи нет публичного API для этого рынка")


class _RestTrades(Stream):
    interval = 1.0

    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback, session: aiohttp.ClientSession):
        super().__init__(venue, kind, coin, on_trades)
        self.session = session
        self.transport = "rest"

    async def fetch_trades(self) -> list[tuple[object, Trade]]:
        raise NotImplementedError

    async def resolve(self) -> None:
        try:
            rows = await self.fetch_trades()
        except (ValueError, KeyError, TypeError):
            raise NotListed from None
        if not rows:
            raise NotListed

    async def stream(self) -> None:
        await poll_loop(self.fetch_trades, self.emit, interval=self.interval,
                        emit_first=self._seed_poll, seen=self.poll_seen)


class PionexStream(_RestTrades):
    URL = "https://api.pionex.com/api/v1/market/trades"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.symbol = f"{self.coin}_USDT" + ("_PERP" if self.kind == "perp" else "")

    async def fetch_trades(self) -> list[tuple[object, Trade]]:
        data = await get_json(self.session, self.URL, {"symbol": self.symbol, "limit": 100})
        if not data.get("result"):
            raise ValueError(data.get("message") or "bad response")
        out = []
        for t in (data.get("data") or {}).get("trades") or []:
            p, q = _f(t.get("price")), _f(t.get("size"))
            if p > 0 and q > 0:
                side = "buy" if str(t.get("side")).upper() == "BUY" else "sell"
                out.append((t.get("tradeId"), Trade(_f(t.get("timestamp")) / 1000, p, q, p * q, side)))
        return out


class CoinDcxStream(_RestTrades):
    URL = "https://public.coindcx.com/market_data/trade_history"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.symbol = f"I-{self.coin}_INR"
        self.quote = "INR"

    async def _raw(self) -> list:
        rows = await get_json(self.session, self.URL, {"pair": self.symbol, "limit": 50})
        if not isinstance(rows, list):
            raise ValueError("unexpected response")
        return rows

    async def resolve(self) -> None:
        # checked on the raw answer: the INR rate may not be loaded yet right after start
        if self.kind != "spot" or not await self._raw():
            raise NotListed

    async def fetch_trades(self) -> list[tuple[object, Trade]]:
        rows = await self._raw()
        fx = usd_rate("INR")  # None until FX rates arrive: nothing emitted, nothing marked seen
        out = []
        for t in rows:
            p, q = _f(t.get("p")), _f(t.get("q"))
            if p > 0 and q > 0 and fx:
                side = "sell" if t.get("m") else "buy"  # m = buyer is maker
                ts = _f(t.get("T")) / 1000
                out.append(((t.get("T"), t.get("p"), t.get("q"), side), Trade(ts, p * fx, q, p * q * fx, side)))
        return out


class ZoomexStream(_RestTrades):
    URL = "https://openapi.zoomex.com/cloud/trade/v3/market/recent-trade"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.symbol = f"{self.coin}USDT"

    async def resolve(self) -> None:
        if self.kind != "perp":
            raise NotListed
        await super().resolve()

    async def fetch_trades(self) -> list[tuple[object, Trade]]:
        data = await get_json(self.session, self.URL, {"category": "linear", "symbol": self.symbol, "limit": 100})
        if data.get("retCode") not in (0, "0"):
            raise ValueError(data.get("retMsg") or "bad response")
        out = []
        for t in (data.get("result") or {}).get("list") or []:
            p, q = _f(t.get("price")), _f(t.get("size"))
            if p > 0 and q > 0:
                side = "buy" if str(t.get("side")).lower() == "buy" else "sell"
                out.append((t.get("execId"), Trade(_f(t.get("time")) / 1000, p, q, p * q, side)))
        return out
