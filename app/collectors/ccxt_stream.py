"""Streams for the 26 venues supported by ccxt (WebSocket via ccxt.pro, REST fallback)."""
import asyncio
import logging
import time
from collections import defaultdict, deque

import ccxt.pro as ccxtpro

from app.collectors.base import BOOK_DEPTH, NotListed, Stream, TradesCallback, poll_loop
from app.config import AMOUNT_IN_BASE, BOOK_AMOUNT_IN_BASE, CCXT_OPTIONS, QUOTE_OVERRIDE
from app.fx import usd_rate
from app.models import Trade
from app.symbols import pick_ccxt_market

log = logging.getLogger(__name__)

MARKETS_TTL = 3600.0


class CcxtPool:
    """Creates ccxt.pro instances (one per stream) sharing a markets cache.

    Each stream owns its instance, so a stalled connection can be torn down
    without touching the other market of the same exchange. Markets are loaded
    once per exchange id and reused, so switching coins is instant.
    """

    def __init__(self) -> None:
        self._live: set[ccxtpro.Exchange] = set()
        self._markets: dict[str, tuple[float, list, dict]] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def create(self, ex_id: str) -> ccxtpro.Exchange:
        ex = getattr(ccxtpro, ex_id)(
            {
                "enableRateLimit": True,
                "timeout": 30000,
                "options": CCXT_OPTIONS.get(ex_id, {}),
                "has": {"fetchCurrencies": False},  # not needed, slow on many venues
            }
        )
        self._live.add(ex)
        try:
            async with self._locks[ex_id]:
                cached = self._markets.get(ex_id)
                if cached and time.time() - cached[0] < MARKETS_TTL:
                    ex.set_markets(cached[1], cached[2])
                else:
                    await ex.load_markets()
                    self._markets[ex_id] = (time.time(), list(ex.markets.values()), ex.currencies)
        except BaseException:
            await self.release(ex)
            raise
        return ex

    async def release(self, ex: ccxtpro.Exchange | None) -> None:
        if ex is None:
            return
        self._live.discard(ex)
        try:
            await ex.close()
        except Exception:  # noqa: BLE001
            pass

    async def reset(self) -> None:
        for ex in list(self._live):
            await self.release(ex)


class CcxtStream(Stream):
    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback, pool: CcxtPool, ex_id: str):
        super().__init__(venue, kind, coin, on_trades)
        self.pool = pool
        self.ex_id = ex_id
        self.ex: ccxtpro.Exchange | None = None
        self.market: dict = {}
        self.mult = 1.0
        self.quote = ""
        self._trade_times: deque[float] = deque(maxlen=50)
        self._seen: dict[object, None] = {}  # insertion-ordered set of trade keys
        self.ws_book = False

    async def resolve(self) -> None:
        self.ex = await self.pool.create(self.ex_id)
        picked = pick_ccxt_market(self.ex.markets, self.coin, self.kind, QUOTE_OVERRIDE.get(self.ex_id))
        if not picked:
            raise NotListed
        self.market, self.mult = picked
        self.symbol = self.market["symbol"]
        self.quote = self.market.get("quote")
        self.transport = "ws" if self.ex.has.get("watchTrades") else "rest"
        self.ws_book = self.transport == "ws" and bool(self.ex.has.get("watchOrderBook"))

    def _idle_limit(self) -> float:
        """Silence after which the socket is assumed dead: short for busy markets."""
        t = self._trade_times
        if len(t) == t.maxlen and time.time() - t[0] < 300:
            return 90.0
        return 900.0

    def _fresh(self, raw: list[dict]) -> list[dict]:
        """Drop trades already seen: feeds replay a snapshot after every reconnect."""
        out = []
        for t in raw:
            key = t.get("id") or (t.get("timestamp"), t.get("price"), t.get("amount"), t.get("side"))
            if key in self._seen:
                continue
            self._seen[key] = None
            out.append(t)
        while len(self._seen) > 5000:
            del self._seen[next(iter(self._seen))]
        return out

    def _quote_amount(self, price: float, amount: float, in_base: set[str]) -> float:
        """Notional in the quote currency; derivative amounts are usually in contracts."""
        m = self.market
        if not m.get("contract"):
            return amount * price
        cs = 1.0 if self.ex_id in in_base else float(m.get("contractSize") or 1)
        return amount * cs if m.get("inverse") else amount * cs * price

    def convert(self, raw: list[dict]) -> list[Trade]:
        fx = usd_rate(self.quote)
        if fx is None:
            return []
        out = []
        for t in raw:
            price, amount = t.get("price"), t.get("amount")
            if not price or not amount:
                continue
            price, amount = float(price), float(amount)
            quote_amt = self._quote_amount(price, amount, AMOUNT_IN_BASE)
            base_units = quote_amt / price
            side = t.get("side") if t.get("side") in ("buy", "sell") else "?"
            ts = t.get("timestamp")
            out.append(
                Trade(
                    ts=ts / 1000 if ts else time.time(),
                    price=price * fx / self.mult,
                    amount=base_units * self.mult,
                    usd=quote_amt * fx,
                    side=side,
                )
            )
        return out

    def convert_book(self, ob: dict) -> tuple[list, list]:
        fx = usd_rate(self.quote)
        if fx is None:
            return [], []

        def side(levels) -> list[tuple[float, float]]:
            out = []
            for lv in levels[:BOOK_DEPTH]:
                price, amount = float(lv[0]), float(lv[1])
                if price > 0 and amount > 0:
                    out.append((price * fx / self.mult, self._quote_amount(price, amount, BOOK_AMOUNT_IN_BASE) * fx))
            return out

        return side(ob.get("bids") or []), side(ob.get("asks") or [])

    async def fetch_book(self) -> tuple[list, list]:
        if self.ex is None:
            raise ConnectionError("exchange instance is reconnecting")
        return self.convert_book(await asyncio.wait_for(self.ex.fetch_order_book(self.symbol), 20))

    async def _recreate(self) -> ccxtpro.Exchange:
        self.ex = await self.pool.create(self.ex_id)
        return self.ex

    async def book_loop(self) -> None:
        if not self.ws_book:
            await super().book_loop()  # REST polling
            return
        self.book_status = "live"
        while True:
            if not self.books_on():
                await asyncio.sleep(1)
                continue
            ex = self.ex
            if ex is None:  # the trade watchdog is reconnecting the instance
                await asyncio.sleep(1)
                continue
            try:
                ob = await asyncio.wait_for(ex.watch_order_book(self.symbol), 60)
            except asyncio.TimeoutError:
                continue  # a quiet book; ccxt reconnects dropped sockets itself
            if self.book_due():
                self.emit_book(*self.convert_book(ob))

    async def _fetch(self, limit: int | None) -> list[dict]:
        try:
            return await asyncio.wait_for(self.ex.fetch_trades(self.symbol, limit=limit), 20)
        except asyncio.TimeoutError:
            raise
        except Exception:  # noqa: BLE001 - some venues reject big limits
            if limit is None:
                raise
            return await asyncio.wait_for(self.ex.fetch_trades(self.symbol), 20)

    async def seed(self) -> None:
        # REST-polled streams seed themselves with their first poll.
        if self.transport == "ws" and self.ex.has.get("fetchTrades"):
            self.emit_seed(self.convert(self._fresh(await self._fetch(500))))

    async def stream(self) -> None:
        if self.transport == "ws":
            if self.ex is None:  # torn down by the watchdog
                await self._recreate()
            while True:
                try:
                    raw = await asyncio.wait_for(self.ex.watch_trades(self.symbol), self._idle_limit())
                except asyncio.TimeoutError:
                    # ccxt occasionally keeps a dead socket "open" after a network blip
                    await self.pool.release(self.ex)
                    self.ex = None
                    raise ConnectionError("no trades for too long, reconnecting") from None
                raw = self._fresh(raw)
                self._trade_times.extend(time.time() for _ in raw)
                self.emit(self.convert(raw))

        async def fetch() -> list[tuple[object, Trade]]:
            raw = await self._fetch(100)
            rows = []
            for t in raw:
                conv = self.convert([t])
                if conv:
                    key = t.get("id") or (t.get("timestamp"), t.get("price"), t.get("amount"), t.get("side"))
                    rows.append((key, conv[0]))
            return rows

        await poll_loop(fetch, self.emit, interval=2.0, emit_first=self._seed_poll, seen=self.poll_seen)
