"""Owns the running streams: the main coin (full analysis) and up to MAX_WATCH extra coins.

Extra ("watched") coins only feed the trade tape of their own overlays: trades with
repeated-size tagging, no detector, no order books, so three coins cost little more
than one.
"""
import asyncio
import logging
import time
from functools import partial

import aiohttp

from app.collectors import CcxtPool, Stream, make_stream
from app.config import VENUES
from app.detector import Detector
from app.hub import Hub
from app.models import Trade
from app.repeats import RepeatTracker

log = logging.getLogger(__name__)

MAX_WATCH = 2  # extra coins next to the main one: three in total
WALL_LEVELS = 50  # book levels the wall detector looks at (analysis modules get the whole book)


def trade_row(coin: str, key: str, t: Trade, rep: tuple[int, int] | None) -> dict:
    row = {
        "ts": t.ts,
        "coin": coin,
        "key": key,
        "side": t.side,
        "price": t.price,
        "amount": t.amount,
        "usd": t.usd,
        "fills": t.fills,
    }
    if rep:
        row["grp"], row["rep"] = rep
    return row


class Tape:
    """A watched coin: trade streams only."""

    def __init__(self, coin: str) -> None:
        self.coin = coin
        self.streams: dict[str, Stream] = {}
        self.tasks: list[asyncio.Task] = []
        self.repeats = RepeatTracker()
        self.last: dict[str, tuple[float, float]] = {}  # key -> (ts, price) of the latest trade


class Manager:
    def __init__(self, detector: Detector, hub: Hub, session: aiohttp.ClientSession, analytics=None, recorder=None):
        self.detector = detector
        self.analytics = analytics  # app.analytics.engine.Analytics: М1-М4 of the main coin
        self.recorder = recorder  # app.analytics.recorder.Recorder: raw data on disk
        self.hub = hub
        self.session = session
        self.pool = CcxtPool()
        self.coin = ""
        self.streams: dict[str, Stream] = {}
        self._tasks: list[asyncio.Task] = []
        self._marked: set[str] = set()
        self._lock = asyncio.Lock()
        self.repeats = RepeatTracker()
        self.watch: dict[str, Tape] = {}

    # ---- trades / books --------------------------------------------------
    def _tune(self, repeats: RepeatTracker) -> None:
        cfg = self.detector.cfg
        repeats.tolerance = cfg.algo_size_tolerance
        repeats.min_usd = cfg.algo_min_trade_usd

    def on_trades(self, stream: Stream, trades: list[Trade], live: bool) -> None:
        kept = self.detector.ingest(stream.key, trades)
        if self.analytics:
            self.analytics.on_trades(stream, kept)
        self._tune(self.repeats)
        rows = []
        for t in kept:
            rep = self.repeats.observe(stream.key, t)
            if live:  # REST history only warms the stats
                rows.append(trade_row(self.coin, stream.key, t, rep))
        if rows:
            self.hub.push_trades(rows)

    def on_watch_trades(self, tape: Tape, stream: Stream, trades: list[Trade], live: bool) -> None:
        now = time.time()
        self._tune(tape.repeats)
        rows = []
        for t in trades:
            if t.usd <= 0 or t.price <= 0:
                continue
            if t.ts > now + 2:
                t.ts = now  # exchange clock ahead of ours
            rep = tape.repeats.observe(stream.key, t)
            if t.ts >= tape.last.get(stream.key, (0.0, 0.0))[0]:
                tape.last[stream.key] = (t.ts, t.price)
            if live:
                rows.append(trade_row(tape.coin, stream.key, t, rep))
        if rows:
            self.hub.push_trades(rows)

    def on_book(self, stream: Stream, ts: float, bids: list, asks: list) -> None:
        events = self.detector.ingest_book(stream.key, ts, bids[:WALL_LEVELS], asks[:WALL_LEVELS])
        if self.analytics:
            self.analytics.on_book(stream, ts, bids, asks)
        if events:
            self.hub.push_walls(events)

    # ---- lifecycle -------------------------------------------------------
    def _launch(self, coin: str, on_trades, books: bool) -> tuple[dict[str, Stream], list[asyncio.Task]]:
        streams: dict[str, Stream] = {}
        tasks: list[asyncio.Task] = []
        for venue in VENUES:
            for kind in ("spot", "perp"):
                source = getattr(venue, kind)
                if source is None:
                    continue
                s = make_stream(venue.name, kind, source, coin, on_trades, self.pool, self.session)
                if self.recorder:
                    s.on_raw = self.recorder.trades
                if books:
                    s.on_book = self.on_book
                    s.books_on = self._books_on
                streams[s.key] = s
                tasks.append(asyncio.create_task(s.run(), name=f"{coin}:{s.key}"))
        return streams, tasks

    def _books_on(self) -> bool:
        return self.detector.cfg.walls or bool(self.analytics and self.analytics.wants_books())

    @staticmethod
    async def _halt(streams: dict[str, Stream], tasks: list[asyncio.Task]) -> None:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(s.close() for s in streams.values()), return_exceptions=True)

    async def set_coin(self, coin: str) -> None:
        coin = coin.strip().upper()
        async with self._lock:
            if coin in self.watch:  # a watched coin becomes the main one
                tape = self.watch.pop(coin)
                await self._halt(tape.streams, tape.tasks)
            await self._halt(self.streams, self._tasks)
            self.coin = self.hub.primary = coin
            self.detector.reset(coin)
            if self.analytics:
                self.analytics.reset(coin)
            self.repeats.reset()
            self._marked = set()
            self.streams, self._tasks = self._launch(coin, self.on_trades, books=True)
            log.info("monitoring %s on %d streams", coin, len(self.streams))

    async def set_watch(self, coins: list[str]) -> list[str]:
        """Extra coins for their own overlays (tape only), at most MAX_WATCH."""
        wanted: list[str] = []
        for c in coins:
            c = str(c).strip().upper()
            if c and c != self.coin and c not in wanted:
                wanted.append(c)
        wanted = wanted[:MAX_WATCH]
        async with self._lock:
            for coin in [c for c in self.watch if c not in wanted]:
                tape = self.watch.pop(coin)
                await self._halt(tape.streams, tape.tasks)
                log.info("stopped watching %s", coin)
            for coin in wanted:
                if coin not in self.watch:
                    tape = Tape(coin)
                    tape.streams, tape.tasks = self._launch(coin, partial(self.on_watch_trades, tape), books=False)
                    self.watch[coin] = tape
                    log.info("watching %s (tape only) on %d streams", coin, len(tape.streams))
            # keep the requested order
            self.watch = {c: self.watch[c] for c in wanted if c in self.watch}
        return list(self.watch)

    async def shutdown(self) -> None:
        async with self._lock:
            for tape in self.watch.values():
                await self._halt(tape.streams, tape.tasks)
            self.watch = {}
            if self.analytics:
                self.analytics.shutdown()
            await self._halt(self.streams, self._tasks)
            self.streams, self._tasks = {}, []
            await self.pool.reset()

    # ---- state -----------------------------------------------------------
    def sync_connected(self) -> None:
        """Tell the detector since when each live stream has been watching."""
        for key, s in self.streams.items():
            if s.connected_at and key not in self._marked:
                self.detector.mark_connected(key, s.connected_at)
                self._marked.add(key)

    def infos(self) -> list[dict]:
        return [s.info() for s in self.streams.values()]

    def health(self, now: float) -> list[dict]:
        """Connection health of the main coin's streams (ТЗ 3.5)."""
        return [s.health(now) for s in self.streams.values()]

    def watch_snapshots(self, now: float) -> dict[str, dict]:
        """Per watched coin: stream statuses and last prices (what its overlay needs)."""
        out = {}
        for coin, tape in self.watch.items():
            rows = []
            for key, s in tape.streams.items():
                ts, price = tape.last.get(key, (0.0, None))
                rows.append({**s.info(), "price": price if now - ts < 300 else None, "score": 0})
            out[coin] = {"type": "snapshot", "coin": coin, "ts": now, "watch": True, "streams": rows}
        return out
