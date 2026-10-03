"""Large resting orders near the price ("плиты", подставки) found in order-book snapshots.

A price level is a wall when it lies within `band_bps` of the mid price and is at
least `ratio` times the typical (median) level of the same book, and `min_usd`.
Walls are followed from snapshot to snapshot, so the tracker can say what the
participant does with them:

  new    - stood for MIN_LIFE seconds (shorter flashes are ignored)
  moved  - vanished at one price while a similar-size wall appeared at another on
           the same side: re-placed. Towards the market (bid up / ask down) = pushing
  pulled - vanished with no trading at its price and without the price reaching it:
           spoof-like, it was there only to lean on the price
  eaten  - traded through (trades at its price or the price went past it)

Prices are USD per coin and sizes are USD, like trades (see app/models.Trade).
"""
import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from statistics import median

from app.models import Trade

MIN_LIFE = 1.5  # s a wall must stand before it is reported
MOVE_EVERY = 5.0  # s between "moved" events of one wall (it may be re-placed twice a second)
GONE_AFTER = 1.0  # s a wall may be missing (book flicker) before it counts as gone
MOVE_SIZE_TOL = 0.35  # a re-placed wall keeps its size within ±35 %
PRICE_TOL = 0.5e-4  # relative tolerance when comparing price levels (0.5 bps)
DEPTH = 50  # levels per side analysed
BASE_LEVELS = 20  # levels per side for the typical-level median
RECENT_SEC = 120  # window for "pulled lately" counters
STALE_SEC = 15  # no book for this long: no wall metrics
EVENTS_KEEP = 300

Levels = list[tuple[float, float]]  # (price per coin in USD, size in USD), best first


@dataclass
class Wall:
    id: int
    side: str  # "bid" | "ask"
    price: float
    usd: float
    ratio: float
    dist_bps: float
    first_ts: float
    seen_ts: float
    origin: float
    usd_max: float = 0.0
    moves: int = 0
    push: int = 0  # moves towards the market: bid up / ask down
    traded: float = 0.0
    confirmed: bool = False
    moved_at: float = 0.0  # last "moved" event
    moved_from: float = 0.0  # price at that event


@dataclass
class _Book:
    walls: list[Wall] = field(default_factory=list)
    ts: float = 0.0
    mid: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_bps: float = 0.0
    history: deque = field(default_factory=deque)  # (ts, event, usd)


def same_price(a: float, b: float) -> bool:
    return abs(a - b) <= PRICE_TOL * max(a, b)


class WallTracker:
    def __init__(self) -> None:
        self._books: dict[str, _Book] = {}
        self._ids = itertools.count(1)
        self.recent: deque[dict] = deque(maxlen=EVENTS_KEEP)

    def reset(self) -> None:
        self._books = {}
        self.recent.clear()

    # ---- input -----------------------------------------------------------
    def update(self, key: str, ts: float, bids: Levels, asks: Levels,
               min_usd: float, ratio: float, band_bps: float) -> list[dict]:
        bids = sorted((lv for lv in bids if lv[0] > 0 and lv[1] > 0), reverse=True)[:DEPTH]
        asks = sorted(lv for lv in asks if lv[0] > 0 and lv[1] > 0)[:DEPTH]
        if not bids or not asks or asks[0][0] <= bids[0][0]:
            return []  # empty or crossed (stale) book
        st = self._books.setdefault(key, _Book())
        bb, ba = bids[0][0], asks[0][0]
        mid = (bb + ba) / 2
        st.ts, st.mid, st.best_bid, st.best_ask = ts, mid, bb, ba
        st.spread_bps = (ba - bb) / mid * 1e4
        typical = median(u for _, u in bids[:BASE_LEVELS] + asks[:BASE_LEVELS])
        threshold = max(min_usd, ratio * typical)

        found: list[tuple[str, float, float, float]] = []
        for side, levels in (("bid", bids), ("ask", asks)):
            for p, u in levels:
                dist = abs(p - mid) / mid * 1e4
                if dist > band_bps:
                    break
                if u >= threshold:
                    found.append((side, p, u, dist))

        events: list[dict] = []
        used: set[int] = set()
        matched: set[int] = set()

        def refresh(w: Wall, p: float, u: float, dist: float) -> None:
            w.price, w.usd, w.dist_bps, w.seen_ts = p, u, dist, ts
            w.usd_max = max(w.usd_max, u)
            w.ratio = u / typical if typical else 0.0
            matched.add(w.id)

        # 1. the same level is still there
        for i, (side, p, u, dist) in enumerate(found):
            for w in st.walls:
                if w.id not in matched and w.side == side and same_price(w.price, p):
                    refresh(w, p, u, dist)
                    used.add(i)
                    break

        # 2. a wall gone from its price + a similar one at another price = re-placed
        for w in sorted((w for w in st.walls if w.id not in matched), key=lambda w: -w.usd_max):
            cands = [
                (abs(p - w.price), i) for i, (side, p, u, _d) in enumerate(found)
                if i not in used and side == w.side and abs(u - w.usd) <= MOVE_SIZE_TOL * max(u, w.usd)
            ]
            if not cands:
                continue
            i = min(cands)[1]
            side, p, u, dist = found[i]
            old = w.price
            refresh(w, p, u, dist)
            used.add(i)
            w.moves += 1
            towards = p > old if side == "bid" else p < old
            w.push += towards
            if w.confirmed and ts - w.moved_at >= MOVE_EVERY:
                start = w.moved_from or w.origin
                up = p > start
                events.append(self._event(key, "moved", w, ts, from_price=start,
                                          towards=up if side == "bid" else not up))
                w.moved_at, w.moved_from = ts, p

        # 3. still missing: flicker, left the zone, or gone for real
        keep: list[Wall] = []
        for w in st.walls:
            if w.id in matched or ts - w.seen_ts < GONE_AFTER:
                keep.append(w)
                continue
            if not w.confirmed:
                continue
            levels = bids if w.side == "bid" else asks
            rest = next((u for p, u in levels if same_price(p, w.price)), 0.0)
            if rest >= 0.5 * w.usd_max or abs(w.price - mid) / mid * 1e4 > band_bps:
                continue  # still resting (no longer stands out) or the price walked away: not an event
            crossed = bb < w.price * (1 - PRICE_TOL) if w.side == "bid" else ba > w.price * (1 + PRICE_TOL)
            kind = "eaten" if crossed or w.traded >= 0.3 * w.usd_max else "pulled"
            events.append(self._event(key, kind, w, ts))
            st.history.append((ts, kind, w.usd_max))
        st.walls = keep

        # 4. new candidates
        for i, (side, p, u, dist) in enumerate(found):
            if i not in used:
                st.walls.append(Wall(next(self._ids), side, p, u, u / typical if typical else 0.0, dist,
                                     first_ts=ts, seen_ts=ts, origin=p, usd_max=u))

        # 5. stood long enough to be real
        for w in st.walls:
            if not w.confirmed and w.seen_ts == ts and ts - w.first_ts >= MIN_LIFE:
                w.confirmed = True
                events.append(self._event(key, "new", w, ts))

        self.recent.extend(events)
        return events

    def on_trades(self, key: str, trades: list[Trade]) -> None:
        """Trades at or through a wall's price count as execution against it."""
        st = self._books.get(key)
        if not st or not st.walls:
            return
        for t in trades:
            for w in st.walls:
                if t.ts < w.first_ts - 1:
                    continue
                if w.side == "bid" and t.price <= w.price * (1 + PRICE_TOL):
                    w.traded += t.usd
                elif w.side == "ask" and t.price >= w.price * (1 - PRICE_TOL):
                    w.traded += t.usd

    # ---- output ----------------------------------------------------------
    def state(self, key: str, now: float | None = None) -> dict:
        now = now or time.time()
        st = self._books.get(key)
        if not st or now - st.ts > STALE_SEC:
            return {"book": False, "spread_bps": None, "wall": None, "pulls": 0, "pulled_usd": 0.0}
        while st.history and st.history[0][0] < now - RECENT_SEC:
            st.history.popleft()
        pulled = [u for _, kind, u in st.history if kind == "pulled"]
        live = [w for w in st.walls if w.confirmed and w.seen_ts == st.ts]
        top = max(live, key=lambda w: w.usd, default=None)
        wall = None
        if top:
            wall = {
                "side": top.side,
                "usd": top.usd,
                "price": top.price,
                "dist_bps": round(top.dist_bps, 1),
                "ratio": round(top.ratio, 1),
                "age": round(now - top.first_ts, 1),
                "moves": top.moves,
                "push": top.push,
            }
        return {
            "book": True,
            "spread_bps": round(st.spread_bps, 2),
            "wall": wall,
            "pulls": len(pulled),
            "pulled_usd": sum(pulled),
        }

    def _event(self, key: str, kind: str, w: Wall, ts: float, **extra) -> dict:
        return {
            "ts": ts,
            "key": key,
            "event": kind,
            "id": w.id,
            "side": w.side,
            "price": w.price,
            "usd": w.usd_max if kind in ("pulled", "eaten") else w.usd,
            "dist_bps": round(w.dist_bps, 1),
            "ratio": round(w.ratio, 1),
            "age": round(ts - w.first_ts, 1),
            "moves": w.moves,
            "push": w.push,
            **extra,
        }
