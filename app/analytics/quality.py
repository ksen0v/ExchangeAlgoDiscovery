"""М12: venue quality - a filter for painted (wash) volume.

Per venue and market of the main coin, over the last 24 h (recomputed every
`venue_quality.recalc_min`), five signs, each scored 0..1 (1 = looks real):

  impact      price impact per unit of normalised delta (|Δprice| / (|delta| / depth_1%)),
              compared with the other venues: huge volume that does not move the price is suspicious
  turnover    volume per hour / depth_1%: hundreds of times the book with a quiet price
  mirror      share of "mirror pairs": a buy and a sell of the same size at the same price within 1 s
  sizes       too few small trades, or the same few sizes over and over
  link        the venue's delta does not go along with the price of the market

quality_score = 0.6 x the weakest sign + 0.4 x their mean. Below `venue_quality.exclude_below`
the venue leaves the aggregates (М1, М3, later М14); its own data stays visible.
"""
import math
from collections import Counter, defaultdict, deque
from statistics import median

DAY = 86400
MIN_MINUTES = 30  # active minutes before a venue gets a score
SMALL_USD = 100.0  # "small" trade
MIRROR_SEC = 1.0


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _corr(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy)


def combine(parts: dict[str, float | None]) -> float | None:
    vals = [v for v in parts.values() if v is not None]
    if not vals:
        return None
    return round(0.6 * min(vals) + 0.4 * sum(vals) / len(vals), 3)


class _Minute:
    __slots__ = ("buy", "sell", "n", "small", "mirrored", "first", "last", "sizes")

    def __init__(self) -> None:
        self.buy = self.sell = 0.0
        self.n = self.small = self.mirrored = 0
        self.first = self.last = 0.0
        self.sizes: Counter = Counter()


class VenueQuality:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.minutes: defaultdict[str, dict[int, _Minute]] = defaultdict(dict)
        self.recent: defaultdict[str, dict] = defaultdict(dict)  # (price, size) -> deque of (ts, side)
        self.scores: dict[str, dict] = {}
        self.loaded: dict[str, float] = {}  # last stored score per venue (until a new one is computed)
        self._computed = 0.0

    def reset(self) -> None:
        self.minutes.clear()
        self.recent.clear()
        self.scores = {}
        self.loaded = {}
        self._computed = 0.0

    # ---- input ---------------------------------------------------------------------
    def on_trades(self, key: str, trades: list) -> None:
        mins = self.minutes[key]
        rec = self.recent[key]
        for t in trades:
            if t.side not in ("buy", "sell") or t.usd <= 0:
                continue
            m_id = int(t.ts) // 60
            m = mins.get(m_id)
            if m is None:
                m = mins[m_id] = _Minute()
                m.first = t.price
            m.last = t.price
            if t.side == "buy":
                m.buy += t.usd
            else:
                m.sell += t.usd
            m.n += 1
            if t.usd < SMALL_USD:
                m.small += 1
            size = t.amount  # bots repeat the very same size: exact repeats are what counts
            if size in m.sizes or len(m.sizes) < 300:  # bounded memory on very busy venues
                m.sizes[size] += 1
            # a mirror: the opposite side, same size and price, within a second
            k = (t.price, t.amount)
            dq = rec.get(k)
            if dq is None:
                dq = rec[k] = deque()
            while dq and dq[0][0] < t.ts - MIRROR_SEC:
                dq.popleft()
            for i, (ts, side) in enumerate(dq):
                if side != t.side:
                    m.mirrored += 2
                    del dq[i]
                    break
            else:
                dq.append((t.ts, t.side))
            if len(rec) > 5000:  # forget prices that have not traded for a while
                for kk in [kk for kk, v in rec.items() if not v or v[-1][0] < t.ts - MIRROR_SEC]:
                    del rec[kk]

    def prune(self, now: float) -> None:
        lo = int(now - DAY) // 60
        for mins in self.minutes.values():
            for k in [k for k in mins if k < lo]:
                del mins[k]

    # ---- scoring --------------------------------------------------------------------
    def due(self, now: float) -> bool:
        every = float(self.cfg.get("venue_quality.recalc_min") or 60) * 60
        if now - self._computed >= every:
            return True
        # venues still without a score are scored as soon as they have enough minutes
        return now - self._computed >= 300 and any(
            k not in self.scores and len(m) >= MIN_MINUTES for k, m in self.minutes.items())

    def compute(self, now: float, depth_of, market_minutes: dict[int, float]) -> dict[str, dict]:
        """depth_of(key) -> median depth_1% of the venue; market_minutes: minute -> consensus price."""
        self._computed = now
        self.prune(now)
        feats: dict[str, dict] = {}
        for key, mins in self.minutes.items():
            active = [(k, m) for k, m in sorted(mins.items()) if m.n]
            if len(active) < MIN_MINUTES:
                continue
            depth = depth_of(key)
            f: dict = {"minutes": len(active)}
            n = sum(m.n for _, m in active)
            vol = sum(m.buy + m.sell for _, m in active)
            span_h = max(1 / 60, (active[-1][0] - active[0][0] + 1) / 60)
            # impact per unit of normalised delta
            if depth:
                ratios = []
                for _, m in active:
                    nd = abs(m.buy - m.sell) / depth
                    if nd >= 0.02 and m.first > 0:
                        ratios.append(abs(m.last / m.first - 1) * 100 / nd)
                f["impact"] = median(ratios) if len(ratios) >= 10 else None
                f["turnover"] = vol / span_h / depth
            f["mirror"] = sum(m.mirrored for _, m in active) / n if n else None
            f["small"] = sum(m.small for _, m in active) / n if n else None
            sizes: Counter = Counter()
            for _, m in active:
                sizes.update(m.sizes)
            f["top5"] = sum(c for _, c in sizes.most_common(5)) / n if n >= 50 else None
            # the venue's delta against the market price change in the same minute
            xs, ys = [], []
            for k, m in active:
                p0, p1 = market_minutes.get(k - 1), market_minutes.get(k)
                if p0 and p1:
                    xs.append(m.buy - m.sell)
                    ys.append(p1 / p0 - 1)
            f["corr"] = _corr(xs, ys)
            feats[key] = f

        def med(name: str) -> float | None:
            vals = [f[name] for f in feats.values() if f.get(name) is not None]
            return median(vals) if len(vals) >= 3 else None

        m_impact, m_turn, m_mirror = med("impact"), med("turnover"), med("mirror")
        m_small, m_top5, m_corr = med("small"), med("top5"), med("corr")
        out = {}
        for key, f in feats.items():
            parts: dict[str, float | None] = {"impact": None, "turnover": None, "mirror": None, "sizes": None,
                                              "link": None}
            if f.get("impact") is not None and m_impact:
                parts["impact"] = _clamp(f["impact"] / (0.25 * m_impact))
            if f.get("turnover") is not None and m_turn:
                excess = f["turnover"] / m_turn
                parts["turnover"] = 1.0 if f["turnover"] < 100 or excess <= 3 else _clamp(1 - math.log10(excess / 3) / 1.5)
            if f.get("mirror") is not None:
                parts["mirror"] = _clamp(1 - max(0.0, f["mirror"] - (m_mirror or 0)) / 0.15)
            size_parts = []
            if f.get("small") is not None and m_small and m_small > 0.05:
                size_parts.append(_clamp(f["small"] / (0.25 * m_small)))
            if f.get("top5") is not None and m_top5 is not None:
                size_parts.append(_clamp(1 - max(0.0, f["top5"] - m_top5) / 0.4))
            if size_parts:
                parts["sizes"] = min(size_parts)
            if f.get("corr") is not None and m_corr and m_corr >= 0.1:
                parts["link"] = _clamp(f["corr"] / (0.5 * m_corr))
            out[key] = {"score": combine(parts), "parts": parts, "features": f, "ts": now}
        self.scores = out
        return out

    # ---- use -------------------------------------------------------------------------
    def score(self, key: str) -> float | None:
        s = self.scores.get(key)
        if s and s["score"] is not None:
            return s["score"]
        return self.loaded.get(key)

    def weight(self, key: str) -> float:
        """Weight in the aggregates: the score itself, 0 below the exclusion level, 1 without a score."""
        s = self.score(key)
        if s is None:
            return 1.0
        return 0.0 if s < float(self.cfg.get("venue_quality.exclude_below")) else s

    def public(self) -> dict:
        thr = float(self.cfg.get("venue_quality.exclude_below"))
        rows = []
        for key in set(self.scores) | set(self.loaded):
            s = self.scores.get(key)
            score = self.score(key)
            rows.append({"key": key, "score": score, "excluded": score is not None and score < thr,
                         "parts": s["parts"] if s else None,
                         "features": {k: v for k, v in (s["features"] if s else {}).items()},
                         "stored": s is None})
        rows.sort(key=lambda r: (r["score"] is None, r["score"] if r["score"] is not None else 2))
        return {"rows": rows, "exclude_below": thr, "computed": self._computed or None}
