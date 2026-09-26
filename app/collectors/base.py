"""Base class for one trade stream (one venue x one market kind)."""
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

    # --- to implement -------------------------------------------------
    async def resolve(self) -> None:
        """Find the symbol; raise NotListed if the coin is not traded here."""
        raise NotImplementedError

    async def seed(self) -> None:
        """Emit recent trades (REST) so the baseline is warm right away."""

    async def stream(self) -> None:
        """Emit trades forever; return/raise to reconnect."""
        raise NotImplementedError

    # --- shared ---------------------------------------------------------
    def emit_seed(self, trades: list[Trade], drop_replays: bool = True) -> None:
        """Emit REST history (stats only, not the tape).

        With drop_replays, later trades not newer than the seed are dropped: many
        WS feeds start with a snapshot of the same recent trades.
        """
        if trades:
            self.on_trades(self, aggregate_fills(trades), False)
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
        self.on_trades(self, aggregate_fills(trades), True)

    async def run(self) -> None:
        backoff = 2.0
        resolved = False
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

    def info(self) -> dict:
        return {
            "key": self.key,
            "venue": self.venue,
            "kind": self.kind,
            "symbol": self.symbol,
            "status": self.status,
            "error": self.error,
            "transport": self.transport,
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
