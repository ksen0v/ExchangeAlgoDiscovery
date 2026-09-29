"""Base class for one stream (one venue x one market kind): trades, plus its order book."""
import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

import aiohttp

from app.models import Trade, aggregate_fills

log = logging.getLogger(__name__)

# Per-request timeout for REST calls. The session itself has no total timeout,
# otherwise aiohttp would kill long-lived WebSocket connections.
REST_TIMEOUT = aiohttp.ClientTimeout(total=15)

TradesCallback = Callable[["Stream", list[Trade], bool], None]  # (stream, trades, live)
# (stream, ts, bids, asks); levels are (USD price of one coin, USD size), best first
BookCallback = Callable[["Stream", float, list[tuple[float, float]], list[tuple[float, float]]], None]

BOOK_EVERY = 0.5  # s between processed book snapshots (walls need no 100 ms resolution)
BOOK_POLL = 2.0  # s between REST depth requests where there is no WebSocket book
BOOK_DEPTH = 50  # levels per side
PRICE_KEYS = ("price", "p", "px")
AMOUNT_KEYS = ("amount", "qty", "quantity", "size", "vol", "volume", "q", "v", "m", "sz")


def parse_levels(raw, amount_mult: float = 1.0, price_div: float = 1.0) -> list[tuple[float, float]]:
    """Depth levels as [price, amount, ...] or {"price":..,"qty":..} -> (price per coin, USD size).

    amount_mult turns contracts into base units; price_div removes 1000x-contract prefixes.
    Quotes are USDT/USDC, taken 1:1 as USD.
    """
    out = []
    for lv in (raw or [])[:BOOK_DEPTH]:
        if isinstance(lv, dict):
            p = next((lv[k] for k in PRICE_KEYS if k in lv), None)
            a = next((lv[k] for k in AMOUNT_KEYS if k in lv), None)
        else:
            p, a = lv[0], lv[1]
        p, a = float(p), float(a) * amount_mult
        if p > 0 and a > 0:
            out.append((p / price_div, p * a))
    return out


class NotListed(Exception):
    """The coin is not traded on this venue/market."""


class Stream:
    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback):
        self.venue = venue
        self.kind = kind  # "spot" | "perp"
        self.coin = coin.upper()
        self.key = f"{venue}:{kind}"
        self.on_trades = on_trades
        self.status = "init"  # init | connecting | live | polling | na | error
        self.error = ""
        self.symbol = ""
        self.transport = ""  # ws | rest
        self._seed_until = 0.0
        self.connected_at = 0.0
        self.poll_seen: dict[object, float] = {}
        # Fills of one taker order share a millisecond timestamp and get merged.
        # Feeds with coarse timestamps (1 s, or one time per message) must not merge:
        # distinct orders would be glued into one print.
        self.merge_fills = True
        # Order book: set by the manager; books_on() lets the detector switch analysis off.
        self.on_book: BookCallback | None = None
        self.books_on: Callable[[], bool] = lambda: False
        self.book_status = ""  # "" | live | polling | error | none
        self.book_error = ""
        self._book_at = 0.0

    # --- to implement -------------------------------------------------
    async def resolve(self) -> None:
        """Find the symbol; raise NotListed if the coin is not traded here."""
        raise NotImplementedError

    async def seed(self) -> None:
        """Emit recent trades (REST) so the baseline is warm right away."""

    async def stream(self) -> None:
        """Emit trades forever; return/raise to reconnect."""
        raise NotImplementedError

    async def fetch_book(self) -> tuple[list, list]:
        """REST depth -> (bids, asks) via parse_levels. Venues without it have no book."""
        raise NotImplementedError

    async def book_loop(self) -> None:
        """Emit book snapshots forever; default: poll fetch_book(). Return/raise to restart."""
        if type(self).fetch_book is Stream.fetch_book:
            self.book_status = "none"
            return
        self.book_status = "polling"
        while True:
            started = time.monotonic()
            if self.books_on():
                self.emit_book(*await self.fetch_book())
            await asyncio.sleep(max(0.2, BOOK_POLL - (time.monotonic() - started)))

    # --- shared ---------------------------------------------------------
    def emit_seed(self, trades: list[Trade], drop_replays: bool = True) -> None:
        """Emit REST history (stats only, not the tape).

        With drop_replays, later trades not newer than the seed are dropped: many
        WS feeds start with a snapshot of the same recent trades.
        """
        if trades:
            self.on_trades(self, self._prints(trades), False)
            if drop_replays:
                self._seed_until = max(t.ts for t in trades)

    def _seed_poll(self, trades: list[Trade]) -> None:
        self.emit_seed(trades, drop_replays=False)

    def emit(self, trades: list[Trade]) -> None:
        if self._seed_until:
            trades = [t for t in trades if t.ts > self._seed_until]
        if not trades:
            return
        if self.status in ("connecting", "error"):
            self.status = "polling" if self.transport == "rest" else "live"
        self.error = ""
        self.on_trades(self, self._prints(trades), True)

    def _prints(self, trades: list[Trade]) -> list[Trade]:
        return aggregate_fills(trades) if self.merge_fills else sorted(trades, key=lambda t: t.ts)

    def book_due(self) -> bool:
        return time.monotonic() - self._book_at >= BOOK_EVERY

    def emit_book(self, bids: list, asks: list, ts: float | None = None) -> None:
        self._book_at = time.monotonic()
        if self.book_status == "error":
            self.book_status = "polling" if self.transport == "rest" else "live"
        self.book_error = ""
        if self.on_book and bids and asks:
            self.on_book(self, ts or time.time(), bids, asks)

    async def _book_runner(self) -> None:
        """Order book next to the trades: its failures never touch the trade stream."""
        backoff = 2.0
        while True:
            try:
                await self.book_loop()
                if self.book_status == "none":
                    return
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.book_status = "error"
                self.book_error = f"{type(e).__name__}: {e}"[:200]
                log.info("%s book error: %s", self.key, self.book_error)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def run(self) -> None:
        backoff = 2.0
        resolved = False
        book_task: asyncio.Task | None = None
        try:
            while True:
                try:
                    self.status = "connecting"
                    if not resolved:
                        await self.resolve()
                        resolved = True
                        try:
                            await self.seed()
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:  # noqa: BLE001 - seed is best effort
                            log.info("%s seed failed: %s", self.key, e)
                    if book_task is None and self.on_book:
                        book_task = asyncio.create_task(self._book_runner(), name=f"{self.key}:book")
                    self.status = "polling" if self.transport == "rest" else "live"
                    self.connected_at = self.connected_at or time.time()
                    await self.stream()
                    backoff = 2.0
                except asyncio.CancelledError:
                    raise
                except NotListed:
                    self.status, self.error = "na", ""
                    return
                except Exception as e:  # noqa: BLE001 - any network/parse error -> reconnect
                    self.status = "error"
                    self.error = f"{type(e).__name__}: {e}"[:200]
                    log.info("%s error: %s", self.key, self.error)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
        finally:
            if book_task:
                book_task.cancel()

    def info(self) -> dict:
        return {
            "key": self.key,
            "venue": self.venue,
            "kind": self.kind,
            "symbol": self.symbol,
            "status": self.status,
            "error": self.error,
            "transport": self.transport,
            "book_status": self.book_status,
            "book_error": self.book_error,
        }


async def ws_loop(
    session: aiohttp.ClientSession,
    url: str,
    subscribe: list,
    on_message: Callable[[object], None],
    ping: Callable[[], str] | None = None,
    ping_interval: float = 15.0,
    decode: Callable[[bytes], str] | None = None,
    idle_timeout: float = 90.0,
) -> None:
    """Connect, subscribe, dispatch JSON messages; returns/raises on disconnect."""
    async with session.ws_connect(url, heartbeat=None, timeout=15, max_msg_size=0) as ws:
        for msg in subscribe:
            await ws.send_str(msg if isinstance(msg, str) else json.dumps(msg))

        async def pinger() -> None:
            while True:
                await asyncio.sleep(ping_interval)
                await ws.send_str(ping())

        ping_task = asyncio.create_task(pinger()) if ping else None
        try:
            while True:
                msg = await asyncio.wait_for(ws.receive(), idle_timeout)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    raw = msg.data
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    raw = decode(msg.data) if decode else msg.data.decode()
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise ConnectionError(f"websocket closed: {msg.type.name}")
                else:
                    continue
                if not raw or raw[0] not in "{[":
                    continue  # "pong" and friends
                on_message(json.loads(raw))
        finally:
            if ping_task:
                ping_task.cancel()


async def poll_loop(
    fetch: Callable[[], Awaitable[list[tuple[object, Trade]]]],
    emit: Callable[[list[Trade]], None],
    interval: float = 1.0,
    emit_first: Callable[[list[Trade]], None] | None = None,
    seen: dict[object, float] | None = None,
) -> None:
    """Poll a REST endpoint returning (dedup_key, trade) pairs and emit only new trades.

    The very first batch is history and goes to `emit_first` (if given). Pass a
    stream-owned `seen` so dedup survives reconnects.
    """
    seen = {} if seen is None else seen
    first = not seen
    while True:
        started = time.monotonic()
        rows = await fetch()
        new = [t for k, t in rows if k not in seen]
        now = time.time()
        for k, _ in rows:
            seen[k] = now
        if len(seen) > 5000:
            cutoff = now - 600
            for k in [k for k, v in seen.items() if v <= cutoff]:
                del seen[k]
        (emit_first if first and emit_first else emit)(new)
        first = False
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))


async def get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None):
    async with session.get(url, params=params, timeout=REST_TIMEOUT) as r:
        return await r.json(content_type=None)


def http_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 manipulation-radar"},
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=15),
    )
