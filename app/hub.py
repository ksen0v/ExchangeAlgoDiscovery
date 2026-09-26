"""Browser clients over WebSocket: snapshots, alerts and a USD-filtered trade tape."""
import asyncio
import logging

import orjson
from fastapi import WebSocket

log = logging.getLogger(__name__)

TAPE_FLUSH_SEC = 0.25
MAX_TAPE_BUFFER = 5000


class Client:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.min_usd = 3000.0


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

    async def _send(self, c: Client, payload: bytes) -> None:
        try:
            await c.ws.send_text(payload.decode())
        except Exception:  # noqa: BLE001 - client went away
            self.remove(c)

    async def broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        payload = orjson.dumps(msg)
        await asyncio.gather(*(self._send(c, payload) for c in list(self.clients)))

    def push_trades(self, rows: list[dict]) -> None:
        if self.clients and len(self._tape) < MAX_TAPE_BUFFER:
            self._tape.extend(rows)

    async def run_tape(self) -> None:
        while True:
            await asyncio.sleep(TAPE_FLUSH_SEC)
            if not self._tape:
                continue
            batch, self._tape = self._tape, []
            batch.sort(key=lambda r: r["ts"])
            for c in list(self.clients):
                rows = [r for r in batch if r["usd"] >= c.min_usd]
                if rows:
                    await self._send(c, orjson.dumps({"type": "trades", "rows": rows}))
