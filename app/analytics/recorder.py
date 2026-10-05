"""Raw data on disk (ТЗ 3.2): one gzip JSON-lines file per day, coin and kind.

    <dir>/2026-10-05/PEPE.trades.jsonl.gz
    <dir>/2026-10-05/PEPE.books.jsonl.gz
    <dir>/2026-10-05/PEPE.oi.jsonl.gz / .funding / .liq

Read with DuckDB without converting anything:
    SELECT * FROM read_json_auto('data/raw/2026-10-05/PEPE.trades.jsonl.gz');
or with pandas: pd.read_json(path, lines=True).

Writes are buffered and appended every few seconds from a worker thread (every append
is a separate gzip member, which gzip/DuckDB/pandas read as one file). Old days are
deleted per kind (trades 30 d, books 7 d, others 90 d) and when the folder grows
beyond max_gb, oldest days first.
"""
import asyncio
import gzip
import logging
import shutil
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import orjson

log = logging.getLogger(__name__)

FLUSH_SEC = 5.0
MAX_BUFFER = 200_000  # rows; beyond that new rows are dropped (and counted)


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def trade_record(coin: str, stream, t) -> dict:
    """The unified trade model of ТЗ 3.1."""
    return {
        "exchange": stream.venue,
        "market": stream.kind,
        "symbol": coin,
        "pair": stream.symbol,
        "quote": getattr(stream, "quote", "") or "",
        "price": t.price,
        "qty": t.amount,
        "notional_usd": t.usd,
        "side": t.side,
        "side_inferred": t.inferred,
        "ts_exchange": int(t.ts * 1000),
        "ts_local": int((t.ts_local or time.time()) * 1000),
        "trade_id": t.tid or None,
        "taker_address": None,
    }


class Recorder:
    def __init__(self, cfg, base_dir: Path):
        self.cfg = cfg
        self.base_dir = base_dir
        self.buf: defaultdict[tuple[str, str, str], list[bytes]] = defaultdict(list)
        self.size = 0
        self.dropped = 0
        self.written = 0
        self.errors = ""
        self._book_at: dict[str, float] = {}
        self.disk_bytes = 0

    @property
    def dir(self) -> Path:
        d = self.cfg.get("record.dir")
        return Path(d) if d else self.base_dir

    def enabled(self, what: str = "") -> bool:
        if not self.cfg.on("recorder"):
            return False
        return bool(self.cfg.get(f"record.{what}")) if what else True

    def _put(self, coin: str, kind: str, ts: float, row: dict) -> None:
        if self.size >= MAX_BUFFER:
            self.dropped += 1
            return
        self.buf[(day_of(ts), coin, kind)].append(orjson.dumps(row))
        self.size += 1

    # ---- hooks -----------------------------------------------------------
    def trades(self, stream, trades, live: bool) -> None:
        if not self.enabled("trades"):
            return
        coin = stream.coin
        for t in trades:
            row = trade_record(coin, stream, t)
            if not live:
                row["history"] = True  # REST history / backfill: may repeat after a restart
            self._put(coin, "trades", t.ts, row)

    def book(self, stream, ts: float, bids: list, asks: list) -> None:
        if not self.enabled("books"):
            return
        every = float(self.cfg.get("record.book_every_sec") or 1)
        if ts - self._book_at.get(stream.key, 0.0) < every:
            return
        self._book_at[stream.key] = ts
        n = int(self.cfg.get("record.book_levels") or 50)
        self._put(stream.coin, "books", ts, {
            "exchange": stream.venue, "market": stream.kind, "symbol": stream.coin, "ts_local": int(ts * 1000),
            # (USD price of one coin, USD size), best first; 8 significant digits and whole dollars keep it small
            "bids": [[float(f"{p:.8g}"), round(u)] for p, u in bids[:n]],
            "asks": [[float(f"{p:.8g}"), round(u)] for p, u in asks[:n]],
        })

    def event(self, coin: str, kind: str, key: str, row: dict) -> None:
        """oi / funding / liq."""
        if not self.enabled():
            return
        venue, market = key.split(":", 1)
        ts = row.get("ts") or time.time()
        self._put(coin, kind, ts, {"exchange": venue, "market": market, "symbol": coin,
                                    **{k: v for k, v in row.items() if k != "ts"}, "ts": int(ts * 1000)})

    # ---- disk ------------------------------------------------------------
    def _write(self, batches: dict) -> int:
        n = 0
        for (day, coin, kind), rows in batches.items():
            folder = self.dir / day
            folder.mkdir(parents=True, exist_ok=True)
            with gzip.open(folder / f"{coin}.{kind}.jsonl.gz", "ab", compresslevel=5) as f:
                f.write(b"\n".join(rows) + b"\n")
            n += len(rows)
        return n

    async def flush(self) -> None:
        if not self.buf:
            return
        batches, self.buf, self.size = self.buf, defaultdict(list), 0
        try:
            self.written += await asyncio.to_thread(self._write, batches)
            self.errors = ""
        except OSError as e:
            self.errors = f"запись не удалась: {e}"
            log.warning("recorder: %s", self.errors)

    async def run(self) -> None:
        cleaned = 0.0
        while True:
            await asyncio.sleep(FLUSH_SEC)
            await self.flush()
            if time.time() - cleaned > 3600:
                cleaned = time.time()
                try:
                    await asyncio.to_thread(self.cleanup)
                except OSError as e:
                    log.warning("recorder cleanup: %s", e)

    def cleanup(self, now: float | None = None) -> None:
        """Delete expired kinds per day, then oldest days while the folder is above max_gb."""
        now = now or time.time()
        root = self.dir
        if not root.exists():
            self.disk_bytes = 0
            return
        keep = {
            "trades": float(self.cfg.get("record.keep_days_trades")),
            "books": float(self.cfg.get("record.keep_days_books")),
        }
        other = float(self.cfg.get("record.keep_days_other"))
        days = sorted(p for p in root.iterdir() if p.is_dir() and len(p.name) == 10)
        for d in days:
            try:
                age = (now - datetime.strptime(d.name, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) / 86400
            except ValueError:
                continue
            for f in d.glob("*.jsonl.gz"):
                kind = f.name.split(".")[-3]
                if age > keep.get(kind, other) + 1:
                    f.unlink(missing_ok=True)
            if not any(d.iterdir()):
                d.rmdir()
        limit = float(self.cfg.get("record.max_gb")) * 1e9
        days = sorted(p for p in root.iterdir() if p.is_dir() and len(p.name) == 10)
        sizes = {d: sum(f.stat().st_size for f in d.glob("*")) for d in days}
        total = sum(sizes.values())
        today = day_of(now)
        for d in days:
            if total <= limit or d.name == today:
                break
            total -= sizes[d]
            shutil.rmtree(d, ignore_errors=True)
            log.warning("recorder: deleted %s, the raw data folder is above %.0f GB", d.name, limit / 1e9)
        self.disk_bytes = total

    def status(self) -> dict:
        return {
            "enabled": self.enabled(),
            "dir": str(self.dir),
            "written": self.written,
            "buffered": self.size,
            "dropped": self.dropped,
            "disk_gb": round(self.disk_bytes / 1e9, 3),
            "error": self.errors,
        }
