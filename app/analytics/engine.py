"""CVD, venue delta and open interest in bounded memory, without recording or signals."""
import time
from collections import defaultdict, deque

from app.analytics.derivs import CcxtDerivFeed, DemoDerivFeed

STEP = 5
WINDOWS = ((60, "1m"), (300, "5m"), (900, "15m"), (3600, "1h"))
KEEP = 3610
OI_TTL = 60


class Analytics:
    def __init__(self, demo: bool = False):
        self.demo = demo
        self.feeds: dict = {}
        self.reset("")

    def reset(self, coin: str) -> None:
        self.shutdown()
        self.coin = coin
        self.started_at = time.time()
        self.flows = defaultdict(dict)
        self.last = {}
        self.oi_last = {}
        self.oi_grid = defaultdict(deque)
        self.cvd = {"spot": 0.0, "perp": 0.0}
        self.cvd_points = deque(maxlen=360)
        self._grid_at = self._minute_at = -1
        self._price = None

    def shutdown(self) -> None:
        for feed in self.feeds.values():
            feed.stop()
        self.feeds.clear()

    def drop_stream(self, key: str) -> None:
        feed = self.feeds.pop(key, None)
        if feed:
            feed.stop()
        for data in (self.flows, self.last, self.oi_last, self.oi_grid):
            data.pop(key, None)

    def on_trades(self, stream, trades: list, live: bool = True) -> None:
        buckets = self.flows[stream.key]
        for t in trades:
            if t.side not in ("buy", "sell"):
                continue
            cell = buckets.setdefault(int(t.ts) // STEP, [0.0, 0.0])
            cell[t.side == "sell"] += t.usd
            if live or t.ts >= self.started_at:  # include reconnect backfill, exclude startup history
                self.cvd[stream.kind] += t.usd if t.side == "buy" else -t.usd
            if t.ts >= self.last.get(stream.key, (0, 0))[0]:
                self.last[stream.key] = (t.ts, t.price)

    def last_price(self, key: str) -> float | None:
        row = self.last.get(key)
        return row[1] if row else None

    def flow(self, key: str, sec: int) -> float:
        cutoff = int(time.time() - sec) // STEP
        return sum(b - s for i, (b, s) in self.flows.get(key, {}).items() if i >= cutoff)

    def oi(self, key: str, ts: float, coins: float, usd: float | None) -> None:
        self.oi_last[key] = (ts, coins, usd)

    def sync_feeds(self, streams: dict) -> None:
        from app.collectors.ccxt_stream import CcxtStream

        for key in list(self.feeds):
            if key not in streams:
                self.drop_stream(key)
        for key, stream in streams.items():
            if stream.kind != "perp" or key in self.feeds or stream.status not in ("live", "polling"):
                continue
            if self.demo:
                feed = DemoDerivFeed(stream, self)
            elif isinstance(stream, CcxtStream) and stream.ex is not None and stream.market:
                feed = CcxtDerivFeed(stream, self)
            else:
                continue
            self.feeds[key] = feed
            feed.start()

    def tick(self, now: float, consensus: float | None, streams: dict) -> None:
        self.sync_feeds(streams)
        self._price = consensus
        grid = int(now) // STEP
        if grid != self._grid_at:
            self._grid_at = grid
            for key, (ts, coins, _) in self.oi_last.items():
                if now - ts < OI_TTL and streams.get(key) and streams[key].status in ("live", "polling"):
                    self.oi_grid[key].append((grid * STEP, coins))
            for points in self.oi_grid.values():
                while points and points[0][0] < now - KEEP:
                    points.popleft()
            cutoff = int(now - KEEP) // STEP
            for buckets in self.flows.values():
                for i in [i for i in buckets if i < cutoff]:
                    del buckets[i]
        minute = int(now) // 60
        if minute != self._minute_at:
            self._minute_at = minute
            self.cvd_points.append([now, self.cvd["spot"], self.cvd["perp"], consensus])

    def feed_status(self, streams: dict, now: float) -> list[dict]:
        return [{"key": key, "oi": self.feeds[key].status if key in self.feeds else "нет публичного ОИ"
                 if s.status in ("live", "polling") else "ждём подключения",
                 "oi_age": round(now - self.oi_last[key][0]) if key in self.oi_last else None}
                for key, s in streams.items() if s.kind == "perp"]

    def snapshot(self, now: float, streams: dict) -> dict:
        venues = {label: [] for _, label in WINDOWS}
        for key, buckets in self.flows.items():
            ordered = sorted(buckets.items(), reverse=True)
            pos = 0
            buy = sell = 0.0
            for sec, label in WINDOWS:
                cutoff = int(now - sec) // STEP
                while pos < len(ordered) and ordered[pos][0] > cutoff:
                    b, s = ordered[pos][1]
                    buy += b
                    sell += s
                    pos += 1
                if buy + sell:
                    venues[label].append({"key": key, "buy": buy, "sell": sell, "delta": buy - sell})
        rows = []
        for status in self.feed_status(streams, now):
            key = status["key"]
            last = self.oi_last.get(key)
            fresh = bool(last and now - last[0] < OI_TTL and streams[key].status in ("live", "polling"))
            r = {**status, "coins": last[1] if fresh else None,
                 "usd": last[2] if fresh else None, "d": {}, "pct": {}, "share": {}}
            if fresh:
                for sec, label in WINDOWS:
                    target = now - sec
                    old = next(((ts, v) for ts, v in reversed(self.oi_grid[key]) if ts <= target), None)
                    if old and target - old[0] <= 2 * STEP and old[1] > 0:
                        r["d"][label] = last[1] - old[1]
                        r["pct"][label] = (last[1] / old[1] - 1) * 100
            rows.append(r)
        totals = {"coins": sum(r["coins"] or 0 for r in rows),
                  "usd": sum(r["usd"] or 0 for r in rows), "pct": {}, "d": {}}
        for _, label in WINDOWS:
            matched = [r for r in rows if label in r["d"]]
            change = sum(r["d"][label] for r in matched)
            base = sum(r["coins"] - r["d"][label] for r in matched)
            if base > 0:
                totals["d"][label] = change
                totals["pct"][label] = change / base * 100
            absolute = sum(abs(r["d"][label]) for r in matched)
            for r in matched:
                r["share"][label] = r["d"][label] / absolute if absolute else None
        rows.sort(key=lambda r: -(r["usd"] or 0))
        return {"type": "analytics", "coin": self.coin, "ts": now,
                "delta": {"windows": [{"w": label} for _, label in WINDOWS], "venues": venues},
                "cvd": list(self.cvd_points) + [[now, self.cvd["spot"], self.cvd["perp"], self._price]],
                "oi": {"venues": rows, "totals": totals}}
