"""Anomaly detector: per-stream rolling stats, 4 signals, a 0-100 score and alerts.

Signals (all over the last `window_sec`, compared to the same stream's own past):
  1. volume spike   - USD volume rate vs the baseline rate of the previous `baseline_sec`
  2. side imbalance - share of taker buys (or sells) in the window
  3. algo pattern   - many near-equal taker orders on one side (TWAP / iceberg bots)
  4. price lead     - premium to the cross-exchange median price moves away from
                      its usual level (this venue is pulling the price, others follow)
"""
import itertools
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, fields
from statistics import median

from app.models import Trade

SPARK_BUCKETS = 30
SPARK_STEP = 10  # seconds per sparkline bar
RECENT_KEEP = 120  # seconds of individual trades kept for pattern detection

# Allowed ranges of numeric settings: a zero window or tolerance would divide by
# zero in tick() and silently stop every snapshot.
LIMITS: dict[str, tuple[float, float]] = {
    "window_sec": (5, 600),
    "baseline_sec": (60, 3600),
    "spike_ratio": (1.1, 1000),
    "min_window_usd": (0, 1e10),
    "imbalance": (0.5, 0.99),
    "algo_min_repeats": (2, 1000),
    "algo_size_tolerance": (0.001, 0.5),
    "algo_min_trade_usd": (1, 1e10),
    "lead_bps": (1, 10_000),
    "alert_score": (1, 100),
    "alert_cooldown_sec": (0, 86_400),
}


@dataclass
class DetectorConfig:
    window_sec: int = 30
    baseline_sec: int = 600
    spike_ratio: float = 5.0
    min_window_usd: float = 10_000
    imbalance: float = 0.75
    algo_min_repeats: int = 6
    algo_size_tolerance: float = 0.03
    algo_min_trade_usd: float = 100
    lead_bps: float = 25
    alert_score: float = 50
    alert_cooldown_sec: int = 90
    telegram: bool = True

    def update(self, data: dict) -> None:
        """Apply known fields; raises ValueError and changes nothing if any value is invalid."""
        new = {}
        for f in fields(self):
            v = data.get(f.name)
            if v is None:
                continue
            if f.type is bool:
                new[f.name] = v.lower() not in ("0", "false", "no", "") if isinstance(v, str) else bool(v)
                continue
            v = float(v)
            lo, hi = LIMITS[f.name]
            if not lo <= v <= hi:  # also rejects NaN
                raise ValueError(f"{f.name} должно быть от {lo:g} до {hi:g}")
            new[f.name] = int(v) if f.type is int else v
        for k, v in new.items():
            setattr(self, k, v)

    def dict(self) -> dict:
        return asdict(self)


class StreamState:
    __slots__ = (
        "buckets", "recent", "last_price", "last_ts", "first_sec",
        "premium", "last_alert_ts", "last_alert_score", "metrics",
    )

    def __init__(self) -> None:
        # second -> [buy_usd, sell_usd, unknown_usd, trades, last_price, last_price_ts]
        self.buckets: dict[int, list] = {}
        self.recent: deque[Trade] = deque()
        self.last_price: float | None = None
        self.last_ts = 0.0
        self.first_sec: int | None = None
        self.premium: dict[int, float] = {}
        self.last_alert_ts = 0.0
        self.last_alert_score = 0.0
        self.metrics: dict = {}

    def add(self, t: Trade) -> None:
        sec = int(t.ts)
        b = self.buckets.get(sec)
        if b is None:
            b = self.buckets[sec] = [0.0, 0.0, 0.0, 0, None, 0.0]
        b[0 if t.side == "buy" else 1 if t.side == "sell" else 2] += t.usd
        b[3] += 1
        if t.ts >= b[5]:
            b[4], b[5] = t.price, t.ts
        if t.ts >= self.last_ts:
            self.last_ts, self.last_price = t.ts, t.price
        if self.first_sec is None or sec < self.first_sec:
            self.first_sec = sec
        self.recent.append(t)

    def prune(self, now: float, history: int) -> None:
        cutoff = int(now) - history
        if self.first_sec is not None and self.first_sec < cutoff:
            self.first_sec = cutoff
        for s in [s for s in self.buckets if s < cutoff]:
            del self.buckets[s]
        for s in [s for s in self.premium if s < cutoff]:
            del self.premium[s]
        while self.recent and self.recent[0].ts < now - RECENT_KEEP:
            self.recent.popleft()

    def price_at(self, t: float) -> float | None:
        if self.first_sec is None:
            return None
        for s in range(int(t), self.first_sec - 1, -1):
            b = self.buckets.get(s)
            if b and b[4] is not None:
                return b[4]
        return None

    def sums(self, lo: int, hi: int) -> tuple[float, float, float, int]:
        """Sum buckets for seconds in [lo, hi]."""
        buy = sell = unk = 0.0
        n = 0
        for s in range(lo, hi + 1):
            b = self.buckets.get(s)
            if b:
                buy += b[0]
                sell += b[1]
                unk += b[2]
                n += b[3]
        return buy, sell, unk, n


def find_algo(trades: list[Trade], cfg: DetectorConfig) -> dict | None:
    """Largest group of near-equal-size taker orders on one side."""
    step = math.log1p(cfg.algo_size_tolerance)
    best = None
    for side in ("buy", "sell"):
        bins: dict[int, list[Trade]] = {}
        for t in trades:
            if t.side == side and t.usd >= cfg.algo_min_trade_usd:
                bins.setdefault(round(math.log(t.usd) / step), []).append(t)
        for k in bins:
            members = bins[k] + bins.get(k + 1, [])  # neighbours absorb bin-edge jitter
            if best is None or len(members) > len(best[1]):
                best = (side, members)
    if not best or len(best[1]) < cfg.algo_min_repeats:
        return None
    side, members = best
    ts = sorted(t.ts for t in members)
    gaps = [b - a for a, b in itertools.pairwise(ts)]
    mean_gap = sum(gaps) / len(gaps) if gaps else 0.0
    cv = (math.sqrt(sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)) / mean_gap) if mean_gap > 0 else 0.0
    usd = sum(t.usd for t in members)
    return {
        "side": side,
        "count": len(members),
        "avg_usd": usd / len(members),
        "usd": usd,
        "interval": mean_gap,
        "regular": mean_gap > 0 and cv < 0.6,
    }


def fmt_usd(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}k"
    return f"${v:.0f}"


class Detector:
    def __init__(self, cfg: DetectorConfig, history_sec: int = 900):
        self.cfg = cfg
        self.history = history_sec
        self.coin = ""
        self.states: dict[str, StreamState] = {}
        self.consensus: float | None = None

    @property
    def keep_sec(self) -> int:
        """Seconds of history needed: window + baseline, and never less than configured."""
        return max(self.history, int(self.cfg.window_sec + self.cfg.baseline_sec) + 30)

    def reset(self, coin: str) -> None:
        self.coin = coin
        self.states = {}
        self.consensus = None

    def ingest(self, key: str, trades: list[Trade], now: float | None = None) -> list[Trade]:
        now = now or time.time()
        st = self.states.get(key)
        if st is None:
            st = self.states[key] = StreamState()
        kept = []
        for t in trades:
            if t.ts > now + 2:
                t.ts = now  # exchange clock ahead of ours
            if t.ts < now - self.keep_sec or t.usd <= 0 or t.price <= 0:
                continue
            st.add(t)
            kept.append(t)
        return kept

    def mark_connected(self, key: str, since: float) -> None:
        """A live stream with no trades still proves 'nothing happened' since `since`."""
        st = self.states.get(key)
        if st is None:
            st = self.states[key] = StreamState()
        sec = int(since)
        if st.first_sec is None or sec < st.first_sec:
            st.first_sec = sec

    def tick(self, now: float | None = None) -> tuple[dict[str, dict], list[dict]]:
        """Recompute metrics for all streams. Returns (metrics by key, new alerts)."""
        cfg = self.cfg
        now = now or time.time()
        now_sec = int(now)
        w = int(cfg.window_sec)
        w_lo = now_sec - w + 1
        b_hi = w_lo - 1
        b_lo = b_hi - int(cfg.baseline_sec) + 1

        for st in self.states.values():
            st.prune(now, self.keep_sec)

        fresh = [st.last_price for st in self.states.values() if st.last_price and now - st.last_ts < 300]
        cons = median(fresh) if len(fresh) >= 2 else None
        then = [p for st in self.states.values() if (p := st.price_at(now - w))]
        cons_then = median(then) if len(then) >= 2 else None
        cons_ret = (cons / cons_then - 1) * 1e4 if cons and cons_then else None
        self.consensus = cons

        raw = {}
        total_w = total_b = 0.0
        for key, st in self.states.items():
            buy, sell, unk, n = st.sums(w_lo, now_sec)
            bbuy, bsell, bunk, _ = st.sums(b_lo, b_hi)
            vol_w, vol_b = buy + sell + unk, bbuy + bsell + bunk
            total_w += vol_w
            total_b += vol_b
            raw[key] = (buy, sell, unk, n, vol_w, vol_b)

        metrics: dict[str, dict] = {}
        alerts: list[dict] = []
        for key, st in self.states.items():
            buy, sell, unk, n, vol_w, vol_b = raw[key]
            m = self._stream_metrics(st, now, now_sec, b_lo, b_hi, buy, sell, n, vol_w, vol_b, cons, cons_ret)
            m["share"] = vol_w / total_w if total_w else None
            m["share_base"] = vol_b / total_b if total_b else None
            self._score(m)
            st.metrics = m
            metrics[key] = m
            alert = self._maybe_alert(key, st, m, now)
            if alert:
                alerts.append(alert)
        return metrics, alerts

    def _stream_metrics(self, st, now, now_sec, b_lo, b_hi, buy, sell, n, vol_w, vol_b, cons, cons_ret) -> dict:
        cfg = self.cfg
        w = int(cfg.window_sec)
        eff_len = (b_hi - max(b_lo, st.first_sec) + 1) if st.first_sec is not None else 0
        warming = eff_len < 60
        base_rate = vol_b / eff_len if eff_len > 0 else 0.0
        rate_w = vol_w / w
        ratio = None if warming else min(999.0, rate_w / max(base_rate, 1.0))

        fresh = st.last_price is not None and now - st.last_ts < 60
        prem = dev = ret = rel = None
        if fresh and cons:
            prem = (st.last_price / cons - 1) * 1e4
            st.premium[now_sec] = prem
            hist = [v for s, v in st.premium.items() if b_lo <= s <= b_hi]
            if len(hist) >= 10:
                dev = prem - median(hist)
        p_then = st.price_at(now - w)
        if st.last_price and p_then:
            ret = (st.last_price / p_then - 1) * 1e4
            if cons_ret is not None:
                rel = ret - cons_ret

        cutoff = now - w
        algo = find_algo([t for t in st.recent if t.ts >= cutoff], cfg)
        if algo and not (algo["usd"] >= 0.25 * vol_w and algo["usd"] >= 0.3 * cfg.min_window_usd):
            algo = None  # background noise of a liquid book, not one participant

        spark = []
        start = now_sec - SPARK_BUCKETS * SPARK_STEP + 1
        for i in range(SPARK_BUCKETS):
            sb, ss, su, _ = st.sums(start + i * SPARK_STEP, start + (i + 1) * SPARK_STEP - 1)
            spark.append([round(sb + su / 2), round(ss + su / 2)])

        return {
            "price": st.last_price,
            "age": round(now - st.last_ts, 1) if st.last_ts else None,
            "vol_w": vol_w,
            "buy_w": buy,
            "sell_w": sell,
            "n_w": n,
            "base_rate": base_rate,
            "ratio": ratio,
            "warming": warming,
            "buy_share": buy / (buy + sell) if buy + sell > 0 else None,
            "ret_bps": ret,
            "rel_bps": rel,
            "prem_bps": prem,
            "dev_bps": dev,
            "algo": algo,
            "spark": spark,
        }

    def _score(self, m: dict) -> None:
        cfg = self.cfg
        score = 0.0
        reasons: list[str] = []
        active = m["vol_w"] >= cfg.min_window_usd
        w = int(cfg.window_sec)

        ratio = m["ratio"]
        if active and ratio is not None:
            if ratio >= cfg.spike_ratio:
                score += 25 + 15 * min(1.0, math.log(ratio / cfg.spike_ratio) / math.log(10))
                base = "база ≈0" if m["base_rate"] < 1 else f"база {fmt_usd(m['base_rate'] * w)}"
                reasons.append(f"Объём ×{ratio:.0f}: {fmt_usd(m['vol_w'])} за {w}с ({base})")
            elif ratio >= cfg.spike_ratio / 2:
                score += 10 * (ratio - cfg.spike_ratio / 2) / (cfg.spike_ratio / 2)

        bs = m["buy_share"]
        if active and bs is not None:
            dom = max(bs, 1 - bs)
            if dom >= cfg.imbalance:
                score += 12 + 8 * (dom - cfg.imbalance) / max(1e-9, 1 - cfg.imbalance)
                reasons.append(f"{'Покупки' if bs >= 0.5 else 'Продажи'} {dom * 100:.0f}% объёма")

        algo = m["algo"]
        if algo:
            k = min(1.0, (algo["count"] - cfg.algo_min_repeats) / max(1, cfg.algo_min_repeats))
            score += 15 + 10 * k
            side = "покупок" if algo["side"] == "buy" else "продаж"
            step = f", шаг ~{algo['interval']:.1f}с" if algo["regular"] else ""
            reasons.append(f"Алго: {algo['count']} {side} ≈{fmt_usd(algo['avg_usd'])}{step}")

        dev = m["dev_bps"]
        if dev is not None and abs(dev) >= cfg.lead_bps and m["vol_w"] >= 0.3 * cfg.min_window_usd:
            score += 8 + 7 * min(1.0, (abs(dev) - cfg.lead_bps) / cfg.lead_bps)
            reasons.append(f"Цена {'выше' if dev > 0 else 'ниже'} рынка на {abs(dev):.0f} bps (лидирует)")

        share, share_base = m.get("share"), m.get("share_base")
        if active and share and share >= 0.1 and share_base is not None and share >= 3 * max(share_base, 0.01):
            reasons.append(f"Доля объёма всех бирж {share * 100:.0f}% (обычно {share_base * 100:.0f}%)")

        m["score"] = round(min(100.0, score), 1)
        m["reasons"] = reasons

    def _maybe_alert(self, key: str, st: StreamState, m: dict, now: float) -> dict | None:
        cfg = self.cfg
        if m["score"] < cfg.alert_score:
            return None
        cooling = now - st.last_alert_ts < cfg.alert_cooldown_sec
        if cooling and m["score"] < st.last_alert_score + 20:
            return None
        st.last_alert_ts, st.last_alert_score = now, m["score"]
        venue, kind = key.split(":", 1)
        return {
            "ts": now,
            "coin": self.coin,
            "key": key,
            "venue": venue,
            "kind": kind,
            "score": m["score"],
            "reasons": m["reasons"],
            "price": m["price"],
            "vol_w": m["vol_w"],
            "ratio": m["ratio"],
            "buy_share": m["buy_share"],
        }
