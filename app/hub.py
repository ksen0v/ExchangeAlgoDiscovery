"""Browser / desktop clients over WebSocket: snapshots, alerts and a filtered trade tape.

Every client has its own bounded outbound queue drained by its own writer, so a
slow client never stalls the tick loop; a client that falls too far behind is
disconnected (it reconnects and starts fresh).
"""
import asyncio
import logging
import math

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
        self.min_by_key: dict[str, float] = {}
        self.tape = True
        self.keys: frozenset[str] | None = None  # None = every stream
        self.lite = False  # snapshots without per-stream metrics (the overlay needs only statuses)
        self.walls = True  # order-book wall events
        self.coin: str | None = None  # None = the main coin (follows its changes); else a watched coin
        self.analytics = False  # full module snapshots (dashboard "Анализ рынка")
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.overflow = asyncio.Event()

    def set_filter(self, msg: dict) -> None:
        if "min_usd" in msg:
            value = float(msg["min_usd"] or 0)
            if math.isfinite(value):
                self.min_usd = max(0.0, value)
        if "min_by_key" in msg and isinstance(msg["min_by_key"], dict):
            thresholds = {}
            for key, raw in list(msg["min_by_key"].items())[:300]:
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(key, str) and math.isfinite(value) and 0 <= value <= 1e12:
                    thresholds[key] = value
            self.min_by_key = thresholds
        if "tape" in msg:
            self.tape = bool(msg["tape"])
        if "keys" in msg:
            keys = msg["keys"]
            self.keys = frozenset(str(k) for k in keys) if isinstance(keys, list) and keys else None
        if "lite" in msg:
            self.lite = bool(msg["lite"])
        if "walls" in msg:
            self.walls = bool(msg["walls"])
        if "coin" in msg:
            self.coin = str(msg["coin"] or "").strip().upper() or None
        if "analytics" in msg:
            self.analytics = bool(msg["analytics"])

    def follows(self, coin: str, primary: str) -> bool:
        return (self.coin or primary) == coin

    def wants(self, row: dict, primary: str = "") -> bool:
        return (
            self.tape and row["usd"] >= self.threshold(row["key"])
            and (self.keys is None or row["key"] in self.keys)
            and self.follows(row.get("coin") or primary, primary)
        )

    def threshold(self, key: str) -> float:
        return self.min_by_key.get(key, self.min_usd)

    def wants_wall(self, event: dict, primary: str = "") -> bool:
        return self.walls and self.follows(event.get("coin") or primary, primary) and (self.keys is None or event["key"] in self.keys)

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
        self._walls: list[dict] = []
        self.primary = ""  # the main coin

    def add(self, ws: WebSocket) -> Client:
        c = Client(ws)
        self.clients.add(c)
        return c

    def remove(self, c: Client) -> None:
        self.clients.discard(c)

    def trade_floor(self, coin: str, key: str) -> float:
        """Avoid allocating tape rows that no subscribed client will receive."""
        return min((c.threshold(key) for c in self.clients if c.tape and c.follows(coin, self.primary)
                    and (c.keys is None or key in c.keys)), default=float("inf"))

    def broadcast(self, msg: dict) -> None:
        if self.clients:
            text = orjson.dumps(msg).decode()
            for c in list(self.clients):
                c.offer(text)

    def broadcast_snapshot(self, msg: dict, watched: dict[str, dict] | None = None) -> None:
        snapshots = {**(watched or {}), msg["coin"]: msg}
        encoded = {}
        for c in list(self.clients):
            coin = c.coin or self.primary
            snapshot = snapshots.get(coin)
            if snapshot is None:
                continue
            key = (coin, c.lite)
            if key not in encoded:
                data = {**snapshot, "streams": [{k: row.get(k) for k in LITE_FIELDS} for row in snapshot["streams"]]} if c.lite else snapshot
                encoded[key] = orjson.dumps(data).decode()
            c.offer(encoded[key])

    def broadcast_main(self, msg: dict, analytics_only: bool = False) -> None:
        self.broadcast_coin(msg, self.primary, analytics_only)

    def broadcast_coin(self, msg: dict, coin: str, analytics_only: bool = False) -> None:
        """Serialize a coin's snapshot only if a client actually needs it."""
        text = None
        for c in list(self.clients):
            if not c.follows(coin, self.primary) or (analytics_only and not c.analytics):
                continue
            if text is None:
                text = orjson.dumps(msg).decode()
            c.offer(text)

    def push_trades(self, rows: list[dict]) -> None:
        if self.clients and len(self._tape) < MAX_TAPE_BUFFER:
            self._tape.extend(rows[:MAX_TAPE_BUFFER - len(self._tape)])

    def push_walls(self, events: list[dict]) -> None:
        if self.clients and len(self._walls) < MAX_TAPE_BUFFER:
            self._walls.extend(events[:MAX_TAPE_BUFFER - len(self._walls)])

    def flush_tape(self) -> None:
        if not self._tape and not self._walls:
            return
        batch, self._tape = sorted(self._tape, key=lambda r: r["ts"]), []
        walls, self._walls = self._walls, []
        for c in list(self.clients):
            rows = [r for r in batch if c.wants(r, self.primary)]
            if rows:
                c.offer(orjson.dumps({"type": "trades", "rows": rows}).decode())
            events = [e for e in walls if c.wants_wall(e, self.primary)]
            if events:
                c.offer(orjson.dumps({"type": "walls", "rows": events}).decode())

    async def run_tape(self) -> None:
        while True:
            await asyncio.sleep(TAPE_FLUSH_SEC)
            self.flush_tape()
