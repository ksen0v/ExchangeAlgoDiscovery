"""SQLite: alert history and runtime settings."""
import json
import os

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    coin TEXT NOT NULL,
    key TEXT NOT NULL,
    score REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.db: aiosqlite.Connection | None = None
        self._settings_lock = None

    async def open(self) -> None:
        import asyncio
        self._settings_lock = asyncio.Lock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    async def add_alert(self, alert: dict) -> int:
        cur = await self.db.execute(
            "INSERT INTO alerts (ts, coin, key, score, data) VALUES (?, ?, ?, ?, ?)",
            (alert["ts"], alert["coin"], alert["key"], alert["score"], json.dumps(alert, ensure_ascii=False)),
        )
        await self.db.commit()
        return cur.lastrowid

    async def alerts(self, coin: str | None = None, limit: int = 200) -> list[dict]:
        q, args = "SELECT id, data FROM alerts", []
        if coin:
            q += " WHERE coin = ?"
            args.append(coin)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        async with self.db.execute(q, args) as cur:
            return [{**json.loads(data), "id": id_} async for id_, data in cur]

    async def prune_alerts(self, before_ts: float) -> int:
        cur = await self.db.execute("DELETE FROM alerts WHERE ts < ?", (before_ts,))
        await self.db.commit()
        return cur.rowcount

    async def get(self, key: str, default=None):
        async with self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else default

    async def set(self, key: str, value) -> None:
        await self.set_many({key:value})

    async def set_many(self, values: dict) -> None:
        async with self._settings_lock:
            await self.db.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(key, json.dumps(value)) for key, value in values.items()],
            )
            await self.db.commit()
