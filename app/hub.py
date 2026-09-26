"""Browser / desktop clients over WebSocket: snapshots, alerts and a filtered trade tape.

Every client has its own bounded outbound queue drained by its own writer, so a
slow client never stalls the tick loop; a client that falls too far behind is
disconnected (it reconnects and starts fresh).
"""
import asyncio
import logging

import orjson
from fastapi import WebSocket

log = logging.getLogger(__name__)

TAPE_FLUSH_SEC = 0.25
MAX_TAPE_BUFFER = 5000
CLIENT_QUEUE = 200
LITE_FIELDS = ("key", "venue", "kind", "symbol", "status", "price", "score")


class Client:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.min_usd = 3000.0
        self.keys: frozenset[str] | None = None  # None = every stream
        self.lite = False  # snapshots without per-stream metrics (the overlay needs only statuses)
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.overflow = asyncio.Event()

    def set_filter(self, msg: dict) -> None:
        if "min_usd" in msg:
            self.min_usd = max(0.0, float(msg["min_usd"] or 0))
        if "keys" in msg:
            keys = msg["keys"]
            self.keys = frozenset(str(k) for k in keys) if isinstance(keys, list) and keys else None
        if "lite" in msg:
            self.lite = bool(msg["lite"])

    def wants(self, row: dict) -> bool:
        return row["usd"] >= self.min_usd and (self.keys is None or row["key"] in self.keys)

    def offer(self, text: str) -> None:
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.overflow.set()

    async def writer(self) -> None:
        while True:
            await self.ws.send_text(await self.queue.get())


class Hub:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self._tape: list[dict] = []

    def add(self, ws: WebSocket) -> Client:
        c = Client(ws)
        self.clients.add(c)
        return c

    def remove(self, c: Client) -> None:
        self.clients.discard(c)

    def broadcast(self, msg: dict) -> None:
        if self.clients:
            text = orjson.dumps(msg).decode()
            for c in list(self.clients):
                c.offer(text)

    def broadcast_snapshot(self, msg: dict) -> None:
        if not self.clients:
            return
        full = lite = None
        for c in list(self.clients):
            if c.lite:
                if lite is None:
                    rows = [{k: r.get(k) for k in LITE_FIELDS} for r in msg["streams"]]
                    lite = orjson.dumps({**msg, "streams": rows}).decode()
                c.offer(lite)
            else:
                if full is None:
                    full = orjson.dumps(msg).decode()
                c.offer(full)

    def push_trades(self, rows: list[dict]) -> None:
        if self.clients and len(self._tape) < MAX_TAPE_BUFFER:
            self._tape.extend(rows)

    def flush_tape(self) -> None:
        if not self._tape:
            return
        batch, self._tape = self._tape, []
        batch.sort(key=lambda r: r["ts"])
        for c in list(self.clients):
            rows = [r for r in batch if c.wants(r)]
            if rows:
                c.offer(orjson.dumps({"type": "trades", "rows": rows}).decode())

    async def run_tape(self) -> None:
        while True:
            await asyncio.sleep(TAPE_FLUSH_SEC)
            self.flush_tape()
