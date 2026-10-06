"""SQLite of the analysis modules (separate from alerts/settings): baseline samples and the signal journal.

Baselines (ТЗ 3.4): every metric is sampled once a minute per coin; median, MAD and
p10/p50/p90/p99 over 7 and 30 days are recomputed hourly (every 5 minutes while the
history is shorter than a day). Until enough minutes are stored, the same statistics
come from 5-second samples of the current session, and alerts say "мало истории".
"""
import asyncio
import json
import logging
import os
from collections import defaultdict, deque

import aiosqlite

from app.analytics.stats import robust_stats

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    coin TEXT NOT NULL, metric TEXT NOT NULL, ts INTEGER NOT NULL, v REAL NOT NULL,
    PRIMARY KEY (coin, metric, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, coin TEXT NOT NULL, module TEXT NOT NULL, type TEXT NOT NULL,
    title TEXT NOT NULL, key TEXT NOT NULL, direction INTEGER NOT NULL, price REAL,
    score REAL, low_history INTEGER NOT NULL DEFAULT 0, collect_only INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL,
    r1 REAL, r5 REAL, r15 REAL, r60 REAL, up REAL, down REAL, done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS journal_ts ON journal(ts);
CREATE INDEX IF NOT EXISTS journal_type ON journal(type, ts);
"""

DAY = 86400
SESSION_KEEP = 3600  # seconds of 5-second session samples
REFRESH_SEC = 3600
REFRESH_YOUNG_SEC = 300
SEEDED_PREFIXES = ("doi5:", "borrow_rate:")  # metrics that can start with history downloaded from the venue


class AnalyticsStore:
    def __init__(self, path: str):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    # ---- samples ---------------------------------------------------------
    async def add_samples(self, rows: list[tuple[str, str, int, float]]) -> None:
        if rows:
            await self.db.executemany("INSERT OR REPLACE INTO samples (coin, metric, ts, v) VALUES (?, ?, ?, ?)", rows)
            await self.db.commit()

    async def samples(self, coin: str, metric: str, since: float) -> list[tuple[int, float]]:
        async with self.db.execute(
            "SELECT ts, v FROM samples WHERE coin = ? AND metric = ? AND ts >= ? ORDER BY ts", (coin, metric, int(since))
        ) as cur:
            return await cur.fetchall()

    async def metrics(self, coin: str) -> list[str]:
        async with self.db.execute("SELECT DISTINCT metric FROM samples WHERE coin = ?", (coin,)) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def prune_samples(self, before: float) -> int:
        cur = await self.db.execute("DELETE FROM samples WHERE ts < ?", (int(before),))
        await self.db.commit()
        return cur.rowcount

    # ---- journal ---------------------------------------------------------
    async def add_signal(self, e: dict) -> int:
        cur = await self.db.execute(
            "INSERT INTO journal (ts, coin, module, type, title, key, direction, price, score, low_history,"
            " collect_only, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (e["ts"], e["coin"], e["module"], e["type"], e["title"], e["key"], e["direction"], e.get("price"),
             e.get("score"), int(bool(e.get("low_history"))), int(bool(e.get("collect_only"))),
             json.dumps(e.get("data") or {}, ensure_ascii=False)),
        )
        await self.db.commit()
        return cur.lastrowid

    async def update_outcome(self, id_: int, fields: dict) -> None:
        cols = [c for c in ("r1", "r5", "r15", "r60", "up", "down", "done") if c in fields]
        if cols:
            await self.db.execute(
                f"UPDATE journal SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                [fields[c] for c in cols] + [id_],
            )
            await self.db.commit()

    async def signals(self, coin: str | None = None, since: float = 0, limit: int = 300,
                      type_: str | None = None) -> list[dict]:
        q = "SELECT * FROM journal WHERE ts >= ?"
        args: list = [since]
        if coin:
            q += " AND coin = ?"
            args.append(coin)
        if type_:
            q += " AND type = ?"
            args.append(type_)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        self.db.row_factory = aiosqlite.Row
        try:
            async with self.db.execute(q, args) as cur:
                rows = [dict(r) async for r in cur]
        finally:
            self.db.row_factory = None
        for r in rows:
            r["data"] = json.loads(r["data"] or "{}")
        return rows

    async def prune_signals(self, before: float) -> None:
        await self.db.execute("DELETE FROM journal WHERE ts < ?", (before,))
        await self.db.commit()


class Baselines:
    """Per-coin metric samples -> robust statistics over 7 / 30 days (+ session warm-up)."""

    def __init__(self, store: AnalyticsStore | None, days: tuple[int, ...] = (7, 30), min_samples: int = 60):
        self.store = store
        self.days = days
        self.min_samples = min_samples
        self.coin = ""
        self.pending: list[tuple[str, str, int, float]] = []
        self.session: defaultdict[str, deque] = defaultdict(lambda: deque(maxlen=SESSION_KEEP // 5 + 10))
        self.cache: dict[str, dict] = {}  # metric -> {"7": stats, "30": stats, "span_days": float}
        self.session_cache: dict[str, dict | None] = {}
        self._refreshed_at = 0.0
        self._session_at = 0.0
        self.oldest: float | None = None  # first stored sample of this coin

    def reset(self, coin: str) -> None:
        self.coin = coin
        self.session.clear()
        self.cache = {}
        self.session_cache = {}
        self._refreshed_at = 0.0
        self.oldest = None

    def observe(self, metric: str, value: float | None, ts: float) -> None:
        """5-second sample (session warm-up statistics)."""
        if value is not None and value == value:
            self.session[metric].append((ts, value))

    def record(self, metric: str, value: float | None, ts: float) -> None:
        """Minute sample: stored for the 7/30-day baselines."""
        if value is not None and value == value and self.coin:
            self.pending.append((self.coin, metric, int(ts // 60 * 60), float(value)))

    def stats(self, metric: str) -> dict | None:
        """Statistics to compare with: 7 days if enough minutes are stored, else this session."""
        c = self.cache.get(metric)
        if c and c.get("7") and c["7"]["n"] >= self.min_samples:
            return {**c["7"], "src": "7d", "span_days": c["span_days"]}
        s = self.session_cache.get(metric)
        if s and s["n"] >= self.min_samples:
            return {**s, "src": "session", "span_days": 0.0}
        return None

    def stats30(self, metric: str) -> dict | None:
        c = self.cache.get(metric)
        return c.get("30") if c else None

    def request_refresh(self) -> None:
        """Recompute the statistics at the next background step (after new history arrived)."""
        self._refreshed_at = 0.0

    def history_days(self, now: float) -> float:
        return (now - self.oldest) / DAY if self.oldest else 0.0

    def low_history(self, now: float) -> bool:
        return self.history_days(now) < min(self.days or (7,))

    def refresh_session(self, now: float) -> None:
        """Every 30 s: statistics of the session samples (last hour)."""
        if now - self._session_at < 30:
            return
        self._session_at = now
        cutoff = now - SESSION_KEEP
        out = {}
        for metric, dq in self.session.items():
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            st = robust_stats([v for _, v in dq])
            if st:
                out[metric] = st
        self.session_cache = out

    async def flush(self) -> None:
        if self.store is None or not self.pending:
            return
        rows, self.pending = self.pending, []
        try:
            await self.store.add_samples(rows)
        except Exception:  # noqa: BLE001
            log.exception("baseline samples not saved")

    async def refresh(self, now: float, force: bool = False) -> None:
        """Recompute 7/30-day statistics from the stored minutes (hourly)."""
        young = self.history_days(now) < 1
        if not force and now - self._refreshed_at < (REFRESH_YOUNG_SEC if young else REFRESH_SEC):
            return
        self._refreshed_at = now
        if self.store is None or not self.coin:
            return
        coin = self.coin
        longest = max(self.days or (30,))
        cache = {}
        oldest = None
        for metric in await self.store.metrics(coin):
            rows = await self.store.samples(coin, metric, now - longest * DAY)
            if not rows:
                continue
            first = rows[0][0]
            if not metric.startswith(SEEDED_PREFIXES):  # downloaded history is not our own record
                oldest = first if oldest is None else min(oldest, first)
            entry = {"span_days": (now - first) / DAY}
            short = min(self.days)
            for d in self.days:
                vals = [v for ts, v in rows if ts >= now - d * DAY]
                st = await asyncio.to_thread(robust_stats, vals)
                if st and d != short:
                    del st["sorted"]  # percentile ranks use the shortest period only
                entry[str(d)] = st
            entry["7"] = entry.get(str(short))  # the period signals compare with
            cache[metric] = entry
            await asyncio.sleep(0)
        if coin == self.coin:
            self.cache = cache
            self.oldest = oldest

    async def prune(self, now: float) -> None:
        if self.store is not None:
            await self.store.prune_samples(now - (max(self.days or (30,)) + 1) * DAY)

