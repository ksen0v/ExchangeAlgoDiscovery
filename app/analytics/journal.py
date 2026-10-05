"""М15 (simple): every signal goes to the journal; what the price did next is added automatically.

Price change after 1 / 5 / 15 / 60 minutes and the largest rise and fall within the hour
are stored as plain price moves in %; the report turns them into "in the signal's
direction" (MFE / MAE, share that worked) using the signal's direction.
Outcomes are followed for the main coin while it stays the main coin.
"""
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, median

from app.analytics.signals import SPECS

log = logging.getLogger(__name__)

HORIZONS = (1, 5, 15, 60)  # minutes


class _Pending:
    __slots__ = ("id", "ts", "coin", "price0", "up", "down", "filled")

    def __init__(self, id_: int, ts: float, coin: str, price0: float) -> None:
        self.id, self.ts, self.coin, self.price0 = id_, ts, coin, price0
        self.up = self.down = 0.0
        self.filled: dict[str, float] = {}


class Journal:
    def __init__(self, store) -> None:
        self.store = store
        self.pending: list[_Pending] = []
        self.coin = ""

    def reset(self, coin: str) -> None:
        """A new main coin: outcomes of the old one can no longer be followed."""
        self.coin = coin
        self.pending = []

    async def add(self, entry: dict) -> int | None:
        if self.store is None:
            return None
        try:
            id_ = await self.store.add_signal(entry)
        except Exception:  # noqa: BLE001
            log.exception("journal write failed")
            return None
        if entry.get("price") and entry["coin"] == self.coin:
            self.pending.append(_Pending(id_, entry["ts"], entry["coin"], entry["price"]))
        return id_

    async def on_price(self, now: float, price: float | None) -> None:
        if not price or not self.pending:
            return
        keep = []
        for p in self.pending:
            move = (price / p.price0 - 1) * 100
            p.up, p.down = max(p.up, move), min(p.down, move)
            changed = {}
            for h in HORIZONS:
                col = f"r{h}"
                if col not in p.filled and now - p.ts >= h * 60:
                    p.filled[col] = changed[col] = move
            done = now - p.ts >= HORIZONS[-1] * 60
            if changed or done:
                changed.update({"up": p.up, "down": p.down})
                if done:
                    changed["done"] = 1
                try:
                    await self.store.update_outcome(p.id, changed)
                except Exception:  # noqa: BLE001
                    log.exception("journal update failed")
            if not done:
                keep.append(p)
        self.pending = keep


def _signed(row: dict, col: str) -> float | None:
    v = row.get(col)
    d = row.get("direction") or 0
    if v is None:
        return None
    return v * d if d else v


def _avg(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return mean(vals) if vals else None


def report(rows: list[dict]) -> dict:
    """Per signal type: count, average move after 1/5/15/60 min (in the signal's direction),
    share that worked after 15 min, MFE / MAE; by coin and by hour of day (UTC)."""
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    types = []
    for type_, items in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        spec = SPECS.get(type_)
        d = spec.direction if spec else 0
        entry = {
            "type": type_,
            "title": spec.title if spec else type_,
            "module": spec.module if spec else "",
            "direction": d,
            "count": len(items),
            "low_history": sum(1 for r in items if r.get("low_history")),
        }
        for h in HORIZONS:
            entry[f"avg_r{h}"] = _avg([_signed(r, f"r{h}") for r in items])
        r15 = [_signed(r, "r15") for r in items if r.get("r15") is not None]
        entry["median_r15"] = median(r15) if r15 else None
        entry["measured"] = len(r15)
        # "worked": moved in the signal's direction after 15 min; for signals without a
        # direction - the move after 15 min was larger than 0.5 %
        entry["hit_rate"] = (sum(1 for v in r15 if (v > 0 if d else abs(v) > 0.5)) / len(r15)) if r15 else None
        if d:
            entry["mfe"] = _avg([(r["up"] if d > 0 else -r["down"]) for r in items if r.get("up") is not None])
            entry["mae"] = _avg([(r["down"] if d > 0 else -r["up"]) for r in items if r.get("up") is not None])
        else:
            entry["mfe"] = _avg([r["up"] for r in items if r.get("up") is not None])
            entry["mae"] = _avg([r["down"] for r in items if r.get("down") is not None])
        hist = [0] * 8  # signed 15-min move, %: <-2, -2..-1, -1..-0.5, -0.5..0, 0..0.5, 0.5..1, 1..2, >=2
        edges = [-2, -1, -0.5, 0, 0.5, 1, 2]
        for v in r15:
            hist[sum(1 for e in edges if v >= e)] += 1
        entry["hist_r15"] = hist
        coins = defaultdict(list)
        for r in items:
            coins[r["coin"]].append(_signed(r, "r15"))
        entry["by_coin"] = [{"coin": c, "count": len(v), "avg_r15": _avg(v)} for c, v in
                            sorted(coins.items(), key=lambda kv: -len(kv[1]))[:10]]
        hours = defaultdict(list)
        for r in items:
            hours[datetime.fromtimestamp(r["ts"], timezone.utc).hour].append(_signed(r, "r15"))
        entry["by_hour"] = [{"hour": h, "count": len(v), "avg_r15": _avg(v)} for h, v in sorted(hours.items())]
        types.append(entry)
    return {"generated": time.time(), "total": len(rows), "types": types}
