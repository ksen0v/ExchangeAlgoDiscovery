"""М5: order books - depth, cost to move, gaps, icebergs, defended levels, vanishing orders.

Local books are kept by ccxt (snapshot + diff stream with the venue's own sync
procedure, checksums where the venue has them; a broken sequence makes ccxt
re-sync from a snapshot) or polled over REST. Here every book of the main coin
arrives as (USD price of one coin, USD size) levels and is analysed at most once a
`orderbook.snapshot_sec` per venue.

  depth_X%        USD of bids (asks) within X % of the mid
  cost_to_move    USD of market orders that move the price X % up (= asks within X %)
                  or down (= bids within X %)
  buckets         depth by 0.1 % bands up to ±5 %; the aggregated book adds every venue
                  relative to ITS OWN mid (inter-exchange price differences removed)
  iceberg         executed at one price within `iceberg_window_sec` more than
                  `iceberg_exec_to_visible` x the most ever visible there
  fast recovery   the best level was eaten by market orders and >= 50 % of it came back at
                  the same price within `recovery_fast_sec`
  vanishing order a level larger than p99 of this venue's levels pulled without being
                  filled while the price was closer than `spoof_cancel_distance_pct`
"""
import math
import time
from collections import deque

BUCKET_PCT = 0.1
MAX_PCT = 5.0
N_BUCKETS = int(MAX_PCT / BUCKET_PCT)  # per side
NEAR_PCT = 1.0  # icebergs / recoveries are looked for within 1 % of the mid
EVENT_KEEP = 3600.0
SAMPLE_EVERY = 5  # every Nth snapshot feeds the level-size distribution
MIN_STAND = 2.0  # s a big order must stand before its removal counts as "vanishing"


def pkey(price: float) -> int:
    """Price key robust to float noise: 1e-6 relative steps."""
    return int(round(math.log(price) * 1e6))


def book_metrics(bids: list, asks: list, levels_pct: list[float]) -> dict | None:
    """Depth / cost to move per X %, 0.1 % buckets and how far the book reaches."""
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    if mid <= 0:
        return None
    bid_b = [0.0] * N_BUCKETS
    ask_b = [0.0] * N_BUCKETS
    for p, u in bids:
        d = (mid - p) / mid * 100
        if 0 <= d < MAX_PCT:
            bid_b[min(N_BUCKETS - 1, int(d / BUCKET_PCT))] += u
    for p, u in asks:
        d = (p - mid) / mid * 100
        if 0 <= d < MAX_PCT:
            ask_b[min(N_BUCKETS - 1, int(d / BUCKET_PCT))] += u
    depth_bid, depth_ask = {}, {}
    for x in levels_pct:
        depth_bid[x] = sum(u for p, u in bids if p >= mid * (1 - x / 100))
        depth_ask[x] = sum(u for p, u in asks if p <= mid * (1 + x / 100))
    return {
        "mid": mid,
        "spread_bps": (asks[0][0] - bids[0][0]) / mid * 1e4,
        "bid": depth_bid,  # = cost to move DOWN by x %
        "ask": depth_ask,  # = cost to move UP by x %
        "reach_bid": (mid - bids[-1][0]) / mid * 100,
        "reach_ask": (asks[-1][0] - mid) / mid * 100,
        "bid_b": bid_b,
        "ask_b": ask_b,
    }


class _Venue:
    __slots__ = ("prev", "prev_ts", "prev_mid", "metrics", "ts", "execs", "visible", "eaten", "sizes", "n",
                 "ice_seen", "big_since")

    def __init__(self) -> None:
        self.prev: dict[str, dict[int, tuple[float, float]]] = {"bid": {}, "ask": {}}
        self.prev_ts = 0.0
        self.prev_mid = 0.0
        self.metrics: dict | None = None
        self.ts = 0.0
        self.execs: deque[tuple[float, str, int, float]] = deque(maxlen=20000)  # (ts, side hit, key, usd)
        self.visible: dict[tuple[str, int], deque] = {}  # (side, key) -> (ts, usd) seen in snapshots
        self.eaten: dict[tuple[str, int], tuple[float, float, float]] = {}  # -> (ts, usd before, price)
        self.sizes: deque[float] = deque(maxlen=5000)
        self.n = 0
        self.ice_seen: dict[tuple[str, int], float] = {}
        self.big_since: dict[tuple[str, int], float] = {}  # big levels: when they first appeared


class BookAnalyzer:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.venues: dict[str, _Venue] = {}
        self.kinds: dict[str, str] = {}
        self.events: deque[dict] = deque(maxlen=2000)  # icebergs, fast recoveries, vanishing orders

    def reset(self) -> None:
        self.venues = {}
        self.kinds = {}
        self.events.clear()

    def _v(self, key: str) -> _Venue:
        v = self.venues.get(key)
        if v is None:
            v = self.venues[key] = _Venue()
        return v

    # ---- inputs ------------------------------------------------------------
    def on_trades(self, key: str, trades: list) -> None:
        v = self._v(key)
        now = time.time()
        for t in trades:
            if t.side in ("buy", "sell") and t.price > 0:
                # a market buy takes the asks, a market sell the bids
                v.execs.append((t.ts_local or now, "ask" if t.side == "buy" else "bid", pkey(t.price), t.usd))

    def on_book(self, key: str, kind: str, ts: float, bids: list, asks: list) -> None:
        v = self._v(key)
        if ts - v.ts < float(self.cfg.get("orderbook.snapshot_sec") or 1):
            return
        levels = [float(x) for x in self.cfg.get("orderbook.depth_levels_pct")]
        m = book_metrics(bids, asks, sorted(set(levels) | {1.0, 2.0, 5.0}))
        if m is None:
            return
        self.kinds[key] = kind
        cur = {"bid": {pkey(p): (p, u) for p, u in bids}, "ask": {pkey(p): (p, u) for p, u in asks}}
        if v.prev_ts:
            self._compare(key, v, ts, m["mid"], cur)
        v.n += 1
        if v.n % SAMPLE_EVERY == 0:
            lo, hi = m["mid"] * 0.98, m["mid"] * 1.02
            v.sizes.extend(u for p, u in bids if p >= lo)
            v.sizes.extend(u for p, u in asks if p <= hi)
        v.prev, v.prev_ts, v.prev_mid, v.metrics, v.ts = cur, ts, m["mid"], m, ts

    # ---- events ------------------------------------------------------------
    def _exec_since(self, v: _Venue, since: float) -> dict[tuple[str, int], float]:
        out: dict[tuple[str, int], float] = {}
        for ts, side, k, usd in reversed(v.execs):
            if ts <= since:
                break
            out[(side, k)] = out.get((side, k), 0.0) + usd
        return out

    def _event(self, key: str, kind: str, side: str, price: float, usd: float, ts: float, **extra) -> None:
        self.events.append({"ts": ts, "key": key, "kind": kind, "side": side, "price": price, "usd": usd,
                            "pkey": pkey(price), **extra})

    def _compare(self, key: str, v: _Venue, ts: float, mid: float, cur: dict) -> None:
        cfg = self.cfg
        window = float(cfg.get("orderbook.iceberg_window_sec"))
        ratio = float(cfg.get("orderbook.iceberg_exec_to_visible"))
        fast = float(cfg.get("orderbook.recovery_fast_sec"))
        spoof_dist = float(cfg.get("orderbook.spoof_cancel_distance_pct"))
        step = self._exec_since(v, v.prev_ts)
        win = self._exec_since(v, ts - window)
        near_lo, near_hi = mid * (1 - NEAR_PCT / 100), mid * (1 + NEAR_PCT / 100)

        # visible size history of near levels (for icebergs)
        for side in ("bid", "ask"):
            for k, (p, u) in v.prev[side].items():
                if near_lo <= p <= near_hi:
                    dq = v.visible.get((side, k))
                    if dq is None:
                        dq = v.visible[(side, k)] = deque(maxlen=64)
                    dq.append((v.prev_ts, u))
        for sk in [sk for sk, dq in v.visible.items() if dq[-1][0] < ts - window * 3]:
            del v.visible[sk]

        # icebergs: executed > ratio x the most ever visible at that price, and the level still stands
        for (side, k), executed in win.items():
            dq = v.visible.get((side, k))
            if not dq or k not in cur[side]:
                continue
            seen = [u for t, u in dq if t >= ts - window - 5]
            shown = max(seen) if seen else 0.0
            if shown > 0 and executed > ratio * shown and ts - v.ice_seen.get((side, k), 0) > window:
                v.ice_seen[(side, k)] = ts
                self._event(key, "iceberg", side, cur[side][k][0], executed, ts, visible=shown)

        # the best level eaten by market orders -> does it come back fast?
        for side in ("bid", "ask"):
            prev_side = v.prev[side]
            if not prev_side:
                continue
            best_k = max(prev_side, key=lambda kk: prev_side[kk][0]) if side == "bid" else \
                min(prev_side, key=lambda kk: prev_side[kk][0])
            p0, u0 = prev_side[best_k]
            now_u = cur[side].get(best_k, (p0, 0.0))[1]
            if now_u < 0.5 * u0 and step.get((side, best_k), 0.0) >= 0.3 * u0 and (side, best_k) not in v.eaten:
                v.eaten[(side, best_k)] = (ts, u0, p0)
        for sk, (t0, u0, p0) in list(v.eaten.items()):
            side, k = sk
            now_u = cur[side].get(k, (p0, 0.0))[1]
            if t0 < ts and now_u >= 0.5 * u0:
                del v.eaten[sk]
                if ts - t0 <= fast:
                    self._event(key, "recovery", side, p0, u0, ts, seconds=round(ts - t0, 1))
            elif ts - t0 > 30:
                del v.eaten[sk]

        # vanishing big orders: stood at least MIN_STAND seconds, then pulled without fills close to the price
        if len(v.sizes) >= 200:
            big = sorted(v.sizes)[int(len(v.sizes) * float(cfg.get("orderbook.spoof_size_pctl")) / 100) - 1]
            pm = v.prev_mid or mid
            alive = set()
            for side in ("bid", "ask"):
                for k, (p, u) in cur[side].items():
                    if u >= big and abs(p - mid) / mid * 100 <= 2 * spoof_dist:
                        alive.add((side, k))
                        v.big_since.setdefault((side, k), ts)
                for k, (p, u) in v.prev[side].items():
                    since = v.big_since.get((side, k))
                    if u < big or since is None or ts - since < MIN_STAND or abs(p - pm) / pm * 100 > spoof_dist:
                        continue
                    left = cur[side].get(k, (p, 0.0))[1]
                    filled = step.get((side, k), 0.0)
                    if left < 0.1 * u and filled < 0.1 * u:
                        self._event(key, "spoof", side, p, u, ts, dist_pct=round(abs(p - pm) / pm * 100, 3),
                                    stood=round(ts - since, 1))
            v.big_since = {sk: t for sk, t in v.big_since.items() if sk in alive}

    # ---- aggregates --------------------------------------------------------
    def fresh(self, now: float, max_age: float = 30.0) -> dict[str, dict]:
        return {k: v.metrics for k, v in self.venues.items() if v.metrics and now - v.ts <= max_age}

    def aggregate(self, now: float, scope: str = "all") -> dict | None:
        """The aggregated book: every venue relative to its own mid."""
        rows = {k: m for k, m in self.fresh(now).items() if scope == "all" or self.kinds.get(k) == scope}
        if not rows:
            return None
        levels = sorted({x for m in rows.values() for x in m["bid"]})
        out = {
            "venues": len(rows),
            "bid": {x: sum(m["bid"][x] for m in rows.values()) for x in levels},
            "ask": {x: sum(m["ask"][x] for m in rows.values()) for x in levels},
            "bid_b": [sum(m["bid_b"][i] for m in rows.values()) for i in range(N_BUCKETS)],
            "ask_b": [sum(m["ask_b"][i] for m in rows.values()) for i in range(N_BUCKETS)],
            # how many venue books reach each bucket: 0 = no data (not a gap)
            "bid_seen": [sum(1 for m in rows.values() if m["reach_bid"] >= (i + 1) * BUCKET_PCT) for i in range(N_BUCKETS)],
            "ask_seen": [sum(1 for m in rows.values() if m["reach_ask"] >= (i + 1) * BUCKET_PCT) for i in range(N_BUCKETS)],
        }
        seen = [u for u, s in zip(out["bid_b"] + out["ask_b"], out["bid_seen"] + out["ask_seen"]) if s]
        med = sorted(seen)[len(seen) // 2] if seen else 0.0
        share = float(self.cfg.get("orderbook.gap_share_of_median"))
        out["bucket_median"] = med
        out["gaps_bid"] = [bool(s) and u < share * med for u, s in zip(out["bid_b"], out["bid_seen"])]
        out["gaps_ask"] = [bool(s) and u < share * med for u, s in zip(out["ask_b"], out["ask_seen"])]
        return out

    def recent(self, now: float, kind: str | None = None, window: float = EVENT_KEEP) -> list[dict]:
        return [e for e in self.events if e["ts"] >= now - window and (kind is None or e["kind"] == kind)]

    def defended(self, now: float) -> list[dict]:
        """Levels with an iceberg or a fast recovery >= defended_min_events times in the window."""
        win = float(self.cfg.get("orderbook.defended_window_min")) * 60
        need = int(self.cfg.get("orderbook.defended_min_events"))
        groups: dict[tuple, list[dict]] = {}
        for e in self.recent(now, window=win):
            if e["kind"] in ("iceberg", "recovery"):
                groups.setdefault((e["key"], e["side"], e["pkey"]), []).append(e)
        out = []
        for (key, side, _), evs in groups.items():
            if len(evs) >= need:
                out.append({"key": key, "side": side, "price": evs[-1]["price"], "count": len(evs),
                            "icebergs": sum(1 for e in evs if e["kind"] == "iceberg"),
                            "recoveries": sum(1 for e in evs if e["kind"] == "recovery"),
                            "usd": sum(e["usd"] for e in evs), "last": evs[-1]["ts"]})
        return sorted(out, key=lambda d: -d["count"])

    def spoof_counts(self, now: float) -> dict[str, dict[str, int]]:
        """Vanishing big orders per venue and side over the last hour."""
        out: dict[str, dict[str, int]] = {}
        for e in self.recent(now, "spoof"):
            d = out.setdefault(e["key"], {"bid": 0, "ask": 0, "usd_bid": 0.0, "usd_ask": 0.0})
            d[e["side"]] += 1
            d[f"usd_{e['side']}"] += e["usd"]
        return out
