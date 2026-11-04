"""One retained analytical session per instrument, sharing HTTP and exchange clients."""
import asyncio
import logging
from functools import partial

import aiohttp

from app.analytics.engine import Analytics
from app.collectors import CcxtPool, Stream, make_stream
from app.config import VENUES
from app.detector import Detector
from app.hub import Hub
from app.models import Trade
from app.repeats import RepeatTracker

log = logging.getLogger(__name__)
WALL_LEVELS = 50


def trade_row(coin: str, key: str, t: Trade, rep: tuple[int, int] | None) -> dict:
    row = {"ts": t.ts, "coin": coin, "key": key, "side": t.side, "price": t.price,
           "amount": t.amount, "usd": t.usd, "fills": t.fills, "inferred": t.inferred}
    if rep:
        row["grp"], row["rep"] = rep
    return row


class Instrument:
    def __init__(self, coin: str, detector: Detector, analytics: Analytics | None):
        self.coin, self.detector, self.analytics = coin, detector, analytics
        self.streams: dict[str, Stream] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.marked: set[str] = set()
        self.repeats = RepeatTracker()
        self.snapshot: dict = {"type": "snapshot", "coin": coin, "ts": 0, "streams": []}
        self.pending_start = False
        detector.reset(coin)
        if analytics:
            analytics.reset(coin)


class Manager:
    def __init__(self, detector: Detector, hub: Hub, session: aiohttp.ClientSession, analytics=None, venue_selection=None):
        self._detector, self._analytics = detector, analytics
        self.hub, self.session, self.pool = hub, session, CcxtPool()
        self.coin = ""
        self.instruments: dict[str, Instrument] = {}
        self._lock = asyncio.Lock()
        known = {v.name for v in VENUES}
        self.venue_selection = {str(c).upper(): [v for v in vs if v in known]
                                for c, vs in (venue_selection or {}).items() if isinstance(vs, list)}

    @property
    def detector(self):
        return self.instruments[self.coin].detector if self.coin in self.instruments else self._detector

    @property
    def analytics(self):
        return self.instruments[self.coin].analytics if self.coin in self.instruments else self._analytics

    @property
    def streams(self):
        return self.instruments[self.coin].streams if self.coin in self.instruments else {}

    @property
    def watch(self):
        """Compatibility for older clients; every instrument now has full analytics."""
        return {c: item for c, item in self.instruments.items() if c != self.coin}

    def _add(self, coin: str, deferred=False) -> None:
        if coin in self.instruments:
            return
        if not self.instruments and not self._detector.coin:
            detector, analytics = self._detector, self._analytics
        else:
            detector = Detector(self._detector.cfg, self._detector.history)
            detector.wall_overrides = self._detector.wall_overrides
            analytics = Analytics(demo=self._analytics.demo) if self._analytics else None
        item = Instrument(coin, detector, analytics)
        self.instruments[coin] = item
        item.streams, item.tasks = self._launch(item, start=not deferred)
        item.pending_start = deferred
        log.info("monitoring %s on %d streams", coin, len(item.streams))

    def _on_trades(self, item: Instrument, stream: Stream, trades: list[Trade], live: bool) -> None:
        kept = item.detector.ingest(stream.key, trades)
        if item.analytics:
            item.analytics.on_trades(stream, kept, live=live)
        floor = self.hub.trade_floor(item.coin, stream.key)
        if floor == float("inf"):
            return
        cfg = item.detector.cfg
        item.repeats.tolerance, item.repeats.min_usd = cfg.algo_size_tolerance, cfg.algo_min_trade_usd
        rows = []
        for trade in kept:
            rep = item.repeats.observe(stream.key, trade)
            if live and trade.usd >= floor:
                rows.append(trade_row(item.coin, stream.key, trade, rep))
        if rows:
            self.hub.push_trades(rows)

    def _on_book(self, item: Instrument, stream: Stream, ts: float, bids: list, asks: list) -> None:
        events = item.detector.ingest_book(stream.key, ts, bids[:WALL_LEVELS], asks[:WALL_LEVELS])
        if events:
            self.hub.push_walls([{**event, "coin": item.coin} for event in events])

    def _launch(self, item: Instrument, only=None, start=True) -> tuple[dict, dict]:
        streams, tasks = {}, {}
        selected = self.venue_selection.get(item.coin)
        for venue in VENUES:
            if (selected is not None and venue.name not in selected) or (only is not None and venue.name not in only):
                continue
            for kind in ("spot", "perp"):
                source = getattr(venue, kind)
                if source is None:
                    continue
                stream = make_stream(venue.name, kind, source, item.coin, partial(self._on_trades, item), self.pool, self.session)
                stream.on_book = partial(self._on_book, item)
                stream.books_on = partial(self._books_on, item, stream.key)
                streams[stream.key] = stream
                if start:
                    tasks[stream.key] = asyncio.create_task(stream.run(), name=f"{item.coin}:{stream.key}")
        return streams, tasks

    @staticmethod
    def _books_on(item: Instrument, key: str) -> bool:
        return item.detector.cfg.walls and not item.detector.wall_override(key).get("off", False)

    @staticmethod
    async def _halt(item: Instrument, streams=None, tasks=None) -> None:
        streams = item.streams if streams is None else streams
        tasks = item.tasks if tasks is None else tasks
        for stream in streams.values():
            stream.status = "stopping"
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await asyncio.gather(*(s.close() for s in streams.values()), return_exceptions=True)

    async def set_coin(self, coin: str, deferred=False) -> None:
        """Add/select a coin without restarting collectors or resetting history."""
        coin = coin.strip().upper()
        async with self._lock:
            self._add(coin, deferred=deferred)
            self.coin = self.hub.primary = coin

    async def set_instruments(self, coins: list[str]) -> list[str]:
        wanted = list(dict.fromkeys(c.strip().upper() for c in coins if c.strip()))
        if not wanted:
            raise ValueError("Добавьте хотя бы один инструмент")
        async with self._lock:
            for coin in wanted:
                self._add(coin)
            for coin in list(self.instruments):
                if coin not in wanted:
                    await self._remove(coin)
            self.instruments = {coin: self.instruments[coin] for coin in wanted}
            if self.coin not in self.instruments:
                self.coin = self.hub.primary = wanted[0]
            self.hub._tape = [r for r in self.hub._tape if r.get("coin") in self.instruments]
            self.hub._walls = [r for r in self.hub._walls if r.get("coin") in self.instruments]
        return list(self.instruments)

    async def _remove(self, coin: str) -> None:
        item = self.instruments.pop(coin)
        if item.analytics:
            item.analytics.shutdown()
        await self._halt(item)
        # The first detector also holds the shared configuration template, but must
        # not retain the removed instrument's trade and chart buffers.
        if item.detector is self._detector:
            item.detector.reset("")
            if item.analytics:
                item.analytics.reset("")

    async def remove_instrument(self, coin: str) -> None:
        async with self._lock:
            if coin not in self.instruments:
                raise ValueError("Инструмент уже удалён")
            if len(self.instruments) == 1:
                raise ValueError("Оставьте хотя бы один инструмент")
            await self._remove(coin)
            if self.coin == coin:
                self.coin = self.hub.primary = next(iter(self.instruments))
            for buffer in (self.hub._tape, self.hub._walls):
                buffer[:] = [row for row in buffer if row.get("coin") != coin]

    async def set_watch(self, coins: list[str]) -> list[str]:
        await self.set_instruments([self.coin, *coins])
        return list(self.watch)

    async def ensure_instruments(self, coins: list[str], deferred=False) -> None:
        async with self._lock:
            for coin in coins:
                self._add(coin, deferred=deferred)

    async def start_pending(self) -> None:
        """Called after registration was sent to the client, never before its response."""
        async with self._lock:
            for item in self.instruments.values():
                if item.pending_start:
                    item.pending_start = False
                    for key, stream in item.streams.items():
                        if key not in item.tasks:
                            item.tasks[key] = asyncio.create_task(stream.run(), name=f"{item.coin}:{key}")

    async def set_venues(self, coin: str, venues: list[str] | None) -> None:
        async with self._lock:
            if venues is None:
                self.venue_selection.pop(coin, None)
            else:
                self.venue_selection[coin] = sorted(set(venues))
            item = self.instruments.get(coin)
            if not item:
                return
            selected = set(venues) if venues is not None else {v.name for v in VENUES}
            removed = {k: s for k, s in item.streams.items() if s.venue not in selected}
            removed_tasks = {k: item.tasks.pop(k) for k in removed}
            for key in removed:
                item.streams.pop(key)
                if item.analytics:
                    item.analytics.drop_stream(key)
                item.detector.states.pop(key, None)
                item.detector.walls.forget(key)
                item.marked.discard(key)
            await self._halt(item, removed, removed_tasks)
            item.repeats.reset()
            present = {s.venue for s in item.streams.values()}
            added, tasks = self._launch(item, only=selected - present)
            item.streams.update(added)
            item.tasks.update(tasks)
            for buffer in (self.hub._tape, self.hub._walls):
                buffer[:] = [r for r in buffer if r.get("coin") != coin or r["key"] not in removed]

    def summaries(self) -> list[dict]:
        summaries = []
        for coin, item in self.instruments.items():
            venues = {}
            for stream in item.streams.values():
                venues.setdefault(stream.venue, set()).add(stream.status)
            connected = sum(bool(statuses & {"live", "polling"}) for statuses in venues.values())
            pending = sum(bool(statuses & {"init", "connecting"}) for statuses in venues.values())
            errors = sum("error" in statuses and not statuses & {"live", "polling"} for statuses in venues.values())
            state = ("disabled" if not venues else "connecting" if item.pending_start or pending
                     else "live" if connected else "error" if errors else "unavailable")
            summaries.append({"coin":coin,"price":item.detector.consensus,
                              "live":sum(s.status in ("live", "polling") for s in item.streams.values()),
                              "total":len(item.streams),"state":state,"connected_venues":connected,
                              "total_venues":len(venues),"pending_venues":pending,"error_venues":errors,
                              "starting":item.pending_start})
        return summaries

    def sync_connected(self) -> None:
        for item in self.instruments.values():
            for key, stream in item.streams.items():
                if stream.connected_at and key not in item.marked:
                    item.detector.mark_connected(key, stream.connected_at)
                    item.marked.add(key)

    def infos(self) -> list[dict]:
        return [s.info() for s in self.streams.values()]

    def health(self, now: float) -> list[dict]:
        return [s.health(now) for s in self.streams.values()]

    async def shutdown(self) -> None:
        async with self._lock:
            for item in self.instruments.values():
                if item.analytics:
                    item.analytics.shutdown()
                await self._halt(item)
            self.instruments.clear()
            await self.pool.reset()
