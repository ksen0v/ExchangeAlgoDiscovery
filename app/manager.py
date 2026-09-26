"""Owns the set of running streams for the current coin."""
import asyncio
import logging

import aiohttp

from app.collectors import CcxtPool, Stream, make_stream
from app.config import VENUES
from app.detector import Detector
from app.hub import Hub
from app.models import Trade

log = logging.getLogger(__name__)


class Manager:
    def __init__(self, detector: Detector, hub: Hub, session: aiohttp.ClientSession):
        self.detector = detector
        self.hub = hub
        self.session = session
        self.pool = CcxtPool()
        self.coin = ""
        self.streams: dict[str, Stream] = {}
        self._tasks: list[asyncio.Task] = []
        self._marked: set[str] = set()
        self._lock = asyncio.Lock()

    def on_trades(self, stream: Stream, trades: list[Trade], live: bool) -> None:
        kept = self.detector.ingest(stream.key, trades)
        if live and kept:
            self.hub.push_trades(
                [
                    {
                        "ts": t.ts,
                        "key": stream.key,
                        "side": t.side,
                        "price": t.price,
                        "amount": t.amount,
                        "usd": t.usd,
                        "fills": t.fills,
                    }
                    for t in kept
                ]
            )

    async def set_coin(self, coin: str) -> None:
        coin = coin.strip().upper()
        async with self._lock:
            await self._stop()
            self.coin = coin
            self.detector.reset(coin)
            self._marked = set()
            for venue in VENUES:
                for kind in ("spot", "perp"):
                    source = getattr(venue, kind)
                    if source is None:
                        continue
                    s = make_stream(venue.name, kind, source, coin, self.on_trades, self.pool, self.session)
                    self.streams[s.key] = s
                    self._tasks.append(asyncio.create_task(s.run(), name=s.key))
            log.info("monitoring %s on %d streams", coin, len(self.streams))

    async def _stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.streams = {}
        await self.pool.reset()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop()

    def sync_connected(self) -> None:
        """Tell the detector since when each live stream has been watching."""
        for key, s in self.streams.items():
            if s.connected_at and key not in self._marked:
                self.detector.mark_connected(key, s.connected_at)
                self._marked.add(key)

    def infos(self) -> list[dict]:
        return [s.info() for s in self.streams.values()]
