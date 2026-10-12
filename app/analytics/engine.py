"""Analysis modules of the main coin: М1 delta, М2 open interest, М3 regime, М4 funding /
basis / liquidations, signals with hysteresis and the М15 journal.

Everything is per venue first and summed only afterwards (ТЗ, "чего не делать"), for
every connected venue: spot and perp delta from all trade streams, OI / funding /
liquidations from every perp venue that publishes them.

tick() runs once a second from the main loop and returns the dashboard snapshot and
the signals that just turned on. Grids (price, OI, depth) are sampled every 5 s,
baseline samples are stored once a minute.
"""
import asyncio
import logging
import math
import time
from collections import defaultdict, deque
from statistics import median

from app.analytics.book import BUCKET_PCT, BookAnalyzer
from app.analytics.borrow import BorrowTracker
from app.analytics.config import ModulesConfig, window_label
from app.analytics.derivs import (CcxtDerivFeed, DemoDerivFeed, annual_pct, basis_pct, f8, premium_pct)
from app.analytics.hyper import HyperTracker, liq_map
from app.analytics.index import IndexTracker
from app.analytics.leadlag import half_cross, xcorr_lead
from app.analytics.onchain import DexTracker, explorer_tx
from app.analytics.quality import VenueQuality
from app.analytics.journal import Journal
from app.analytics import labdata
from app.analytics.labmodel import Lab
from app.analytics.recorder import Recorder
from app.analytics.regime import RegimeTracker, classify, sign
from app.analytics.signals import SPECS, SignalBoard
from app.analytics.stats import pct_rank, z_of
from app.analytics.store import AnalyticsStore, Baselines
from app.config import VENUES
from app.detector import fmt_usd
from app.models import Trade

log = logging.getLogger(__name__)
DEX_VENUES = {v.name for v in VENUES if v.spot is not None and v.spot.id.startswith("dex_")}

STEP = 5  # s: flow buckets, common grid of price / OI / depth
KEEP = 3600 + 900  # s of 5-second history
CVD_POINTS = 360  # minutes on the CVD chart
REGIME_WINDOWS = (60, 300, 900)
LIQ_FEED = 200


def depth_1pct(bids: list, asks: list) -> tuple[float | None, float | None]:
    """(USD within ±1 % of the mid price, mid) of a book given as (price, usd) levels."""
    if not bids or not asks:
        return None, None
    mid = (bids[0][0] + asks[0][0]) / 2
    if mid <= 0:
        return None, None
    lo, hi = mid * 0.99, mid * 1.01
    return sum(u for p, u in bids if p >= lo) + sum(u for p, u in asks if p <= hi), mid


def _fmt_pct(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%"


def _ru_window(sec: int) -> str:
    return window_label(sec).replace("m", "м").replace("h", "ч").replace("s", "с")


def _margin_above(x: float | None, st: dict | None, q: str = "p90") -> float | None:
    """>= 1 when x is above the q percentile (distance measured from the median)."""
    if x is None or not st:
        return None
    span = st[q] - st["p50"]
    if span <= 1e-12:
        return None
    return (x - st["p50"]) / span


def _margin_below(x: float | None, st: dict | None, q: str = "p10") -> float | None:
    if x is None or not st:
        return None
    span = st["p50"] - st[q]
    if span <= 1e-12:
        return None
    return (st["p50"] - x) / span


def _mins(*vals) -> float | None:
    """Strength of an AND of conditions: the weakest one (None = cannot be judged)."""
    if any(v is None for v in vals):
        return None
    return max(0.0, min(3.0, *vals))


class _Grid:
    """Values on a common 5-second grid (forward filled by the caller)."""

    def __init__(self) -> None:
        self.points: deque[tuple[float, float]] = deque()

    def add(self, ts: float, v: float) -> None:
        self.points.append((ts, v))
        while self.points and self.points[0][0] < ts - KEEP:
            self.points.popleft()

    def last(self) -> float | None:
        return self.points[-1][1] if self.points else None

    def at(self, ts: float) -> float | None:
        """Latest value at or before ts (not older than 2 steps before it)."""
        for t, v in reversed(self.points):
            if t <= ts + 0.5:
                return v if ts - t <= 2 * STEP + 0.5 else None
        return None


class Analytics:
    def __init__(self, cfg: ModulesConfig, store: AnalyticsStore | None, recorder: Recorder | None,
                 demo: bool = False, session=None, api_key: str = "", api_secret: str = "", clock=None,
                 lab_dir=None):
        self.book = BookAnalyzer(cfg)  # М5
        self.borrow = BorrowTracker(cfg, session, api_key, api_secret, clock, store, demo)  # М6
        self.index = IndexTracker(cfg, session, demo)  # М7
        self.dex = DexTracker(cfg, session, demo)  # М8
        self.hl = HyperTracker(cfg, session, demo)  # М9
        self.quality = VenueQuality(cfg)  # М12
        self.lab = Lab(cfg, store, recorder, lab_dir)  # own model on the recorded history
        self.cfg = cfg
        self.store = store
        self.recorder = recorder
        self.demo = demo
        self.baselines = Baselines(store, tuple(cfg.get("baseline_days")), int(cfg.get("min_baseline_samples")))
        self.journal = Journal(store)
        self.board = SignalBoard()
        self.regimes = RegimeTracker()
        self.coin = ""
        self.feeds: dict[str, object] = {}
        self._reset_state()

    # ---- lifecycle ---------------------------------------------------------
    def _reset_state(self) -> None:
        self.flows: defaultdict[str, dict[int, list[float]]] = defaultdict(dict)  # key -> idx -> [buy, sell]
        self.kinds: dict[str, str] = {}
        self.last: dict[str, tuple[float, float]] = {}  # key -> (ts, USD price) of the last trade
        self.mid: dict[str, tuple[float, float]] = {}  # key -> (ts, mid) of the last book
        self.depth: defaultdict[str, deque] = defaultdict(lambda: deque(maxlen=KEEP // STEP))
        self._depth_last: dict[str, float] = {}
        self.price = _Grid()
        self.oi_last: dict[str, tuple[float, float, float | None]] = {}  # key -> (ts, coins, usd)
        self.oi_grid: defaultdict[str, _Grid] = defaultdict(_Grid)
        self.fund: dict[str, dict] = {}
        self.liq_buckets: defaultdict[str, dict[int, list[float]]] = defaultdict(dict)  # idx -> [long, short]
        self.liq_feed: deque[dict] = deque(maxlen=LIQ_FEED)
        self.basis_grid = _Grid()
        self.prem_grid = _Grid()
        self.cvd = {"spot": 0.0, "perp": 0.0}
        self.cvd_points: deque[list] = deque(maxlen=CVD_POINTS)
        self._grid_at = 0
        self._minute_at = 0
        self.snapshot: dict = {}
        # М8: CEX prints for arbitrage / whale matching, by second: sec -> [[ts, key, side, usd, used], ...]
        self.cex_secs: dict[int, list[list]] = defaultdict(list)
        self.swaps: deque[dict] = deque(maxlen=300)
        self.swap_sizes: deque[float] = deque(maxlen=5000)
        self.arb_events: deque[dict] = deque(maxlen=200)
        self.whale_pending: list[dict] = []
        self.dex_whales: deque[dict] = deque(maxlen=50)
        self.lead_cex: deque = deque()
        self.lead_dex: deque = deque()
        self.lead: dict = {}
        self.lead_events: deque[dict] = deque(maxlen=50)
        self._lead_at = 0.0
        self._rpc_last = None

    def reset(self, coin: str) -> None:
        for f in self.feeds.values():
            f.stop()
        self.feeds = {}
        self.coin = coin
        self._reset_state()
        self.baselines.reset(coin)
        self.journal.reset(coin)
        self.board.reset()
        self.regimes.reset()
        self.book.reset()
        self.borrow.reset(coin)
        self.index.reset(coin)
        self.dex.reset(coin)
        self.hl.reset(coin)
        self.quality.reset()
        self.lab.reset(coin)
        if self.store is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.borrow.load_history(coin))
                loop.create_task(self._load_quality(coin))
            except RuntimeError:  # no loop (tests)
                pass

    async def _load_quality(self, coin: str) -> None:
        """Last stored venue scores: an excluded venue stays excluded after a restart."""
        try:
            for metric in await self.store.metrics(coin):
                if metric.startswith("quality:"):
                    rows = await self.store.samples(coin, metric, time.time() - 2 * 86400)
                    if rows and coin == self.coin:
                        self.quality.loaded[metric.split(":", 1)[1]] = rows[-1][1]
        except Exception as e:  # noqa: BLE001
            log.info("stored venue quality not loaded: %s", e)

    def shutdown(self) -> None:
        for f in self.feeds.values():
            f.stop()
        self.feeds = {}

    def wants_books(self) -> bool:
        return self.cfg.on("delta") or self.cfg.on("funding") or self.cfg.on("orderbook") or self.cfg.on(
            "index") or (self.cfg.on("recorder")
                                                                   and bool(self.cfg.get("record.books")))

    # ---- inputs ------------------------------------------------------------
    def on_trades(self, stream, trades: list) -> None:
        key = stream.key
        self.kinds[key] = stream.kind
        b = self.flows[key]
        w = self._wkey(key)
        for t in trades:
            if t.side not in ("buy", "sell"):
                continue
            i = int(t.ts) // STEP
            cell = b.get(i)
            if cell is None:
                cell = b[i] = [0.0, 0.0]
            if t.side == "buy":
                cell[0] += t.usd
                self.cvd[stream.kind] += t.usd * w
            else:
                cell[1] += t.usd
                self.cvd[stream.kind] -= t.usd * w
            if t.ts >= self.last.get(key, (0.0, 0.0))[0]:
                self.last[key] = (t.ts, t.price)
        if self.cfg.on("orderbook"):
            self.book.on_trades(key, trades)
        if self.cfg.on("quality"):
            self.quality.on_trades(key, trades)
        if self.cfg.on("dex"):
            if stream.venue in DEX_VENUES:
                self._on_dex_swaps(stream, trades)
            else:
                for t in trades:
                    if t.side in ("buy", "sell"):
                        self.cex_secs[int(t.ts)].append([t.ts, key, t.side, t.usd, False])
        if stream.venue == "Hyperliquid" and self.cfg.on("hyperliquid"):
            self.hl.on_trades(trades)

    def on_book(self, stream, ts: float, bids: list, asks: list) -> None:
        d, mid = depth_1pct(bids, asks)
        if mid is None:
            return
        key = stream.key
        self.kinds[key] = stream.kind
        self.mid[key] = (ts, mid)
        if ts - self._depth_last.get(key, 0.0) >= STEP:
            self._depth_last[key] = ts
            self.depth[key].append((ts, d))
        if self.cfg.on("orderbook") or self.cfg.on("index"):
            self.book.on_book(key, stream.kind, ts, bids, asks)
        if self.recorder:
            self.recorder.book(stream, ts, bids, asks)

    # sink of the derivative feeds
    def oi(self, key: str, ts: float, coins: float, usd: float | None) -> None:
        self.oi_last[key] = (ts, coins, usd)
        if self.recorder:
            self.recorder.event(self.coin, "oi", key, {"ts": ts, "oi_coins": coins, "oi_usd": usd})

    def funding(self, key: str, data: dict) -> None:
        self.fund[key] = data
        if self.recorder:
            self.recorder.event(self.coin, "funding", key, data)

    def liq(self, key: str, ev: dict) -> None:
        i = int(ev["ts"]) // STEP
        cell = self.liq_buckets[key].get(i)
        if cell is None:
            cell = self.liq_buckets[key][i] = [0.0, 0.0]
        cell[0 if ev["side"] == "long" else 1] += ev["usd"]
        venue = key.split(":", 1)[0]
        self.liq_feed.appendleft({**ev, "venue": venue})
        if self.recorder:
            self.recorder.event(self.coin, "liq", key, ev)

    def seed_history(self, metric: str, points: list[tuple[float, float]]) -> None:
        """Free history from the venue (e.g. 30 days of 5-minute OI): baselines without waiting a month."""
        for ts, v in points:
            self.baselines.record(metric, v, ts)
        self.baselines.request_refresh()

    def last_price(self, key: str) -> float | None:
        v = self.last.get(key)
        return v[1] if v else None

    def flow(self, key: str, sec: int) -> float:
        buy, sell = self._window(self.flows.get(key, {}), time.time(), sec)
        return buy - sell

    # ---- helpers -----------------------------------------------------------
    def _weight(self, venue: str) -> float:
        w = (self.cfg.get("delta.venue_weights") or {}).get(venue)
        try:
            return float(w) if w is not None else 1.0
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _window(b: dict, now: float, sec: int) -> tuple[float, float]:
        lo = (int(now) - sec) // STEP
        buy = sell = 0.0
        for i, (bb, ss) in b.items():
            if i > lo:
                buy += bb
                sell += ss
        return buy, sell

    def _venue_price(self, key: str, now: float) -> float | None:
        m = self.mid.get(key)
        if m and now - m[0] < 30:
            return m[1]
        t = self.last.get(key)
        if t and now - t[0] < 120:
            return t[1]
        return None

    def _market_depth(self, kind: str, now: float) -> float | None:
        total, n = 0.0, 0
        for key, dq in self.depth.items():
            if self.kinds.get(key) != kind or not dq or now - dq[-1][0] > 300:
                continue
            vals = [d for ts, d in dq if ts >= now - 3600]
            if vals:
                total += median(vals)
                n += 1
        return total if n else None

    def _prune(self, now: float) -> None:
        lo = (int(now) - KEEP) // STEP
        for b in list(self.flows.values()) + list(self.liq_buckets.values()):
            for i in [i for i in b if i < lo]:
                del b[i]

    def _ret(self, now: float, sec: int) -> float | None:
        p, p0 = self.price.last(), self.price.at(now - sec)
        return (p / p0 - 1) * 100 if p and p0 else None

    # ---- feeds -------------------------------------------------------------
    def sync_feeds(self, streams: dict) -> None:
        if not (self.cfg.on("open_interest") or self.cfg.on("funding") or self.cfg.on("liquidations")
                or self.cfg.on("regime")):
            return
        from app.collectors.ccxt_stream import CcxtStream  # here: avoids an import cycle

        for key in [k for k in self.feeds if k not in streams]:
            self.feeds.pop(key).stop()
        for key, s in streams.items():
            if s.kind != "perp" or key in self.feeds or s.status not in ("live", "polling"):
                continue
            if self.demo:
                feed = DemoDerivFeed(s, self.cfg, self)
            elif isinstance(s, CcxtStream) and s.ex is not None and s.market:
                feed = CcxtDerivFeed(s, self.cfg, self)
            else:
                continue
            self.feeds[key] = feed
            feed.start()

    def feed_status(self, streams: dict) -> list[dict]:
        out = []
        for key, s in streams.items():
            if s.kind != "perp" or s.status in ("na", "noapi"):
                continue
            f = self.feeds.get(key)
            st = f.status if f else {"oi": "нет данных (биржа не через ccxt)" if s.status in ("live", "polling")
                                     else "ждём подключения", "funding": "", "liq": ""}
            oi = self.oi_last.get(key)
            fd = self.fund.get(key)
            out.append({"key": key, **st, "oi_age": round(time.time() - oi[0]) if oi else None,
                        "funding_age": round(time.time() - fd["ts"]) if fd else None})
        return out

    # ---- tick --------------------------------------------------------------
    def tick(self, now: float, consensus: float | None, streams: dict) -> tuple[dict, list[dict]]:
        cfg = self.cfg
        self.sync_feeds(streams)
        grid_due = int(now) // STEP != self._grid_at
        if grid_due:
            self._grid_at = int(now) // STEP
            gts = self._grid_at * STEP
            if consensus:
                self.price.add(gts, consensus)
            for key, (ts, coins, _usd) in self.oi_last.items():
                if now - ts < max(60.0, 3 * float(cfg.get("open_interest.poll_sec"))):
                    self.oi_grid[key].add(gts, coins)
            self._prune(now)

        windows = cfg.windows()
        need = sorted(set(windows) | set(REGIME_WINDOWS) | {cfg.seconds("delta.signal_window"),
                                                            cfg.seconds("regime.window"), 300, 900})
        delta = self._delta(now, need)
        oi = self._open_interest(now, need)
        fund = self._funding(now)
        liq = self._liquidations(now, need, delta)

        if grid_due:
            self._observe(now, delta, oi, fund, liq)
            self.baselines.refresh_session(now)
        minute = int(now) // 60
        minute_due = minute != self._minute_at
        if minute_due:
            self._minute_at = minute
            self._record(now, delta, oi, fund, liq)
            self.cvd_points.append([minute * 60, round(self.cvd["spot"]), round(self.cvd["perp"]), consensus])

        p2 = self._phase2(now, consensus, delta, fund, streams)
        if grid_due:
            for k, v in self._metrics2(p2).items():
                self.baselines.observe(k, v, now)
        if minute_due:
            for k, v in self._metrics2(p2).items():
                self.baselines.record(k, v, now)
        p3 = self._phase3(now, consensus, streams, p2)
        regime = self._regime(now, delta, oi, liq) if cfg.on("regime") else None
        alerts = self._signals(now, consensus, delta, oi, fund, liq, p2, p3)
        if minute_due and cfg.on("lab"):
            self._record_lab(now, consensus, delta, oi, fund, liq, p2, p3, regime)
        low = self.baselines.low_history(now)
        self.snapshot = {
            "type": "analytics",
            "coin": self.coin,
            "ts": now,
            "price": consensus,
            "modules": {m: cfg.on(m) for m in ("delta", "open_interest", "regime", "funding", "liquidations",
                                                 "orderbook", "borrow", "index", "dex", "hyperliquid", "quality",
                                                 "lab", "journal", "recorder")},
            "collect_only": cfg.collect_only,
            "history_days": round(self.baselines.history_days(now), 2),
            "low_history": low,
            "windows": [window_label(w) for w in windows],
            "delta": self._delta_public(delta, windows) if cfg.on("delta") else None,
            "cvd": list(self.cvd_points) + [[now, round(self.cvd["spot"]), round(self.cvd["perp"]), consensus]]
            if cfg.on("delta") else None,
            "oi": self._oi_public(oi, windows) if cfg.on("open_interest") else None,
            "funding": fund if cfg.on("funding") else None,
            "liq": self._liq_public(liq) if cfg.on("liquidations") else None,
            "regime": regime,
            "book": p2["book_public"],
            "borrow": p2["borrow"],
            "index": p2["index_public"],
            "dex": p3["dex"],
            "hl": p3["hl"],
            "quality": p3["quality"],
            "lab": self.lab.live if cfg.on("lab") else None,
            "signals": self.board.active(),
        }
        return self.snapshot, alerts

    # ---- М1 ----------------------------------------------------------------
    def _delta(self, now: float, windows: list[int]) -> dict:
        depth = {"spot": self._market_depth("spot", now), "perp": self._market_depth("perp", now)}
        out = {"depth": depth, "w": {}}
        for w in windows:
            per = []
            agg = {"spot": [0.0, 0.0], "perp": [0.0, 0.0]}
            for key, b in self.flows.items():
                kind = self.kinds.get(key, "spot")
                buy, sell = self._window(b, now, w)
                if buy + sell <= 0:
                    continue
                wt = self._wkey(key)
                agg[kind][0] += buy * wt
                agg[kind][1] += sell * wt
                per.append({"key": key, "buy": round(buy), "sell": round(sell), "delta": round(buy - sell)})
            lbl = window_label(w)
            res = {"venues": per, "ret": self._ret(now, w)}
            for kind in ("spot", "perp"):
                buy, sell = agg[kind]
                res[f"delta_{kind}"] = buy - sell
                res[f"vol_{kind}"] = buy + sell
                d = depth[kind]
                if d:
                    res[f"nd_{kind}"], res[f"norm_{kind}"] = (buy - sell) / d, "depth_1%"
                else:
                    st = self.baselines.stats(f"vol_{kind}_{lbl}")
                    med = st["p50"] if st else None
                    res[f"nd_{kind}"] = (buy - sell) / med if med else None
                    res[f"norm_{kind}"] = "объём окна (7 дн.)" if med else None
            dsum = (depth["spot"] or 0) + (depth["perp"] or 0)
            res["nd_all"] = (res["delta_spot"] + res["delta_perp"]) / dsum if dsum else (
                None if res["nd_spot"] is None or res["nd_perp"] is None else res["nd_spot"] + res["nd_perp"])
            res["div"] = (res["nd_perp"] - res["nd_spot"]) if res["nd_perp"] is not None and res[
                "nd_spot"] is not None else None
            nd = res["nd_all"]
            res["eff"] = (res["ret"] / abs(nd)) if res["ret"] is not None and nd and abs(nd) > 1e-9 else None
            for kind in ("spot", "perp", "all"):
                st = self.baselines.stats(f"nd_{kind}_{lbl}")
                res[f"z_{kind}"] = z_of(res[f"nd_{kind}"], st)
                res[f"pr_{kind}"] = pct_rank(res[f"nd_{kind}"], st["sorted"]) if st and res[
                    f"nd_{kind}"] is not None and "sorted" in st else None
            out["w"][w] = res
        return out

    def _delta_public(self, delta: dict, windows: list[int]) -> dict:
        rows = []
        for w in windows:
            r = delta["w"][w]
            rows.append({"w": window_label(w), **{k: v for k, v in r.items() if k != "venues"}})
        return {"depth": delta["depth"], "windows": rows,
                "venues": {window_label(w): sorted(delta["w"][w]["venues"], key=lambda v: -abs(v["delta"]))
                           for w in windows}}

    # ---- М2 ----------------------------------------------------------------
    def _open_interest(self, now: float, windows: list[int]) -> dict:
        venues = {}
        for key, g in self.oi_grid.items():
            cur = g.last()
            last = self.oi_last.get(key)
            if cur is None or not last:
                continue
            v = {"coins": cur, "usd": last[2], "age": round(now - last[0]), "d": {}, "pct": {}}
            for w in windows:
                then = g.at(now - w)
                if then is not None and then > 0:
                    v["d"][w] = cur - then
                    v["pct"][w] = (cur / then - 1) * 100
            venues[key] = v
        totals = {"coins": sum(v["coins"] for v in venues.values()) or None, "d": {}, "pct": {}}
        for w in windows:
            ds = [(v["d"][w], v["coins"] - v["d"][w]) for v in venues.values() if w in v["d"]]
            if ds:
                d = sum(x for x, _ in ds)
                base = sum(b for _, b in ds)
                totals["d"][w], totals["pct"][w] = d, (d / base * 100 if base else None)
            absum = sum(abs(v["d"][w]) for v in venues.values() if w in v["d"])
            for v in venues.values():
                if w in v["d"]:
                    v.setdefault("share", {})[w] = v["d"][w] / absum if absum else None
        for key, v in venues.items():
            v["z5"] = z_of(v["pct"].get(300), self.baselines.stats(f"doi5:{key.split(':', 1)[0]}"))
        price = self.price.last()
        dspot = self._market_depth("spot", now)
        d5 = totals["d"].get(300)
        totals["d5_depth"] = d5 * price / dspot if d5 is not None and price and dspot else None
        return {"venues": venues, "totals": totals}

    def _oi_public(self, oi: dict, windows: list[int]) -> dict:
        def pub(v: dict) -> dict:
            return {
                "coins": v.get("coins"), "usd": v.get("usd"), "age": v.get("age"), "z5": v.get("z5"),
                "d": {window_label(w): x for w, x in v.get("d", {}).items() if w in windows},
                "pct": {window_label(w): x for w, x in v.get("pct", {}).items() if w in windows},
                "share": {window_label(w): x for w, x in (v.get("share") or {}).items() if w in windows},
            }
        rows = [{"key": k, **pub(v)} for k, v in oi["venues"].items()]
        rows.sort(key=lambda r: -(r["usd"] or 0))
        return {"venues": rows, "totals": {**pub(oi["totals"]), "d5_depth": oi["totals"].get("d5_depth")}}

    # ---- М4 ----------------------------------------------------------------
    def _funding(self, now: float) -> dict:
        rows = []
        stale = max(120.0, 3 * float(self.cfg.get("funding.poll_sec")))
        for key, fd in self.fund.items():
            if now - fd["ts"] > stale:
                continue
            r = {"key": key, "rate": fd.get("rate"), "interval_h": fd.get("interval_h"),
                 "interval_assumed": fd.get("interval_assumed"), "next_ts": fd.get("next_ts"),
                 "mark": fd.get("mark"), "index": fd.get("index")}
            if r["rate"] is not None and r["interval_h"]:
                r["f8"] = f8(r["rate"], r["interval_h"]) * 100
                r["annual"] = annual_pct(r["rate"], r["interval_h"])
            if r["mark"] and r["index"]:
                r["premium"] = premium_pct(r["mark"], r["index"])
            rows.append(r)
        by_venue: dict[str, dict] = {}
        for key in set(self.kinds) | {r["key"] for r in rows}:
            venue, kind = key.split(":", 1)
            p = self._venue_price(key, now)
            if p:
                by_venue.setdefault(venue, {})[kind] = p
        fund_by = {r["key"].split(":", 1)[0]: r for r in rows}
        for venue, prices in by_venue.items():
            if "spot" in prices and "perp" in prices:
                fund_by.setdefault(venue, {"key": f"{venue}:perp"})["basis"] = basis_pct(prices["perp"], prices["spot"])
        rows = list(fund_by.values())
        f8s = [r["f8"] for r in rows if r.get("f8") is not None]
        # cross basis: Binance perp (or the most traded perp) against the volume-weighted spot of all venues
        vols = {k: sum(a + b for a, b in self.flows[k].values()) for k in self.flows}
        perps = [k for k in vols if self.kinds.get(k) == "perp" and self._venue_price(k, now)]
        ref = "Binance:perp" if "Binance:perp" in perps else (max(perps, key=vols.get) if perps else None)
        spots = [(self._venue_price(k, now), vols[k]) for k in vols if self.kinds.get(k) == "spot"]
        spots = [(p, v) for p, v in spots if p and v > 0]
        cross = None
        if ref and spots:
            ws = sum(v for _, v in spots)
            spot_px = sum(p * v for p, v in spots) / ws
            cross = basis_pct(self._venue_price(ref, now), spot_px)
        prem_ref = fund_by.get(ref.split(":", 1)[0], {}).get("premium") if ref else None
        prems = [r["premium"] for r in rows if r.get("premium") is not None]
        premium = prem_ref if prem_ref is not None else (median(prems) if prems else None)
        return {
            "venues": sorted(rows, key=lambda r: r["key"]),
            "f8_median": median(f8s) if f8s else None,
            "f8_spread": (max(f8s) - min(f8s)) if len(f8s) >= 2 else None,
            "cross_basis": cross,
            "cross_ref": ref.split(":", 1)[0] if ref else None,
            "premium": premium,
        }

    def _liquidations(self, now: float, windows: list[int], delta: dict) -> dict:
        per = {}
        tot = {w: [0.0, 0.0] for w in windows}
        for key, b in self.liq_buckets.items():
            v = {}
            for w in windows:
                lng, sht = self._window(b, now, w)
                v[w] = (lng, sht)
                tot[w][0] += lng
                tot[w][1] += sht
            per[key] = v
        depth = delta["depth"].get("perp")
        hist = []
        if self.liq_buckets:
            m_now = int(now) // 60
            mins = defaultdict(lambda: [0.0, 0.0])
            for b in self.liq_buckets.values():
                for i, (lng, sht) in b.items():
                    m = i * STEP // 60
                    if m > m_now - 60:
                        mins[m][0] += lng
                        mins[m][1] += sht
            hist = [[m * 60, round(mins[m][0]), round(mins[m][1])] for m in range(m_now - 59, m_now + 1)]
        return {"per": per, "tot": tot, "depth": depth, "hist": hist}

    def _liq_public(self, liq: dict) -> dict:
        big = float(self.cfg.get("funding.big_liquidation_usd_depth")) * (liq["depth"] or 0)
        venues = []
        for key, v in liq["per"].items():
            row = {"key": key}
            for w in (60, 300):
                if w in v:
                    row[window_label(w)] = [round(v[w][0]), round(v[w][1])]
            venues.append(row)
        tot = {window_label(w): [round(a), round(b)] for w, (a, b) in liq["tot"].items() if w in (60, 300, 900)}
        return {
            "venues": venues,
            "totals": tot,
            "norm_1m": (sum(liq["tot"][60]) / liq["depth"]) if 60 in liq["tot"] and liq["depth"] else None,
            "feed": [{**e, "big": bool(big) and e["usd"] >= big} for e in list(self.liq_feed)[:100]],
            "hist": liq["hist"],
        }

    # ---- baselines ---------------------------------------------------------
    def _metrics(self, delta: dict, oi: dict, fund: dict, liq: dict) -> dict[str, float | None]:
        m: dict[str, float | None] = {}
        for w, r in delta["w"].items():
            lbl = window_label(w)
            for kind in ("spot", "perp", "all"):
                m[f"nd_{kind}_{lbl}"] = r.get(f"nd_{kind}")
            m[f"vol_spot_{lbl}"] = r["vol_spot"]
            m[f"vol_perp_{lbl}"] = r["vol_perp"]
            m[f"ret_abs_{lbl}"] = abs(r["ret"]) if r["ret"] is not None else None
        for key, v in oi["venues"].items():
            m[f"doi5:{key.split(':', 1)[0]}"] = v["pct"].get(300)
        m["f8_med"] = fund["f8_median"]
        m["basis_x"] = fund["cross_basis"]
        m["d_basis_5m"] = self._grid_change(self.basis_grid, fund["cross_basis"])
        m["d_prem_5m"] = self._grid_change(self.prem_grid, fund["premium"])
        if self.liq_buckets:
            for w, (lng, sht) in liq["tot"].items():
                m[f"liq_{window_label(w)}"] = lng + sht
        return m

    def _grid_change(self, grid: _Grid, cur: float | None) -> float | None:
        then = grid.at(time.time() - 300)
        return cur - then if cur is not None and then is not None else None

    def _observe(self, now: float, delta, oi, fund, liq) -> None:
        if fund["cross_basis"] is not None:
            self.basis_grid.add(now, fund["cross_basis"])
        if fund["premium"] is not None:
            self.prem_grid.add(now, fund["premium"])
        for k, v in self._metrics(delta, oi, fund, liq).items():
            self.baselines.observe(k, v, now)

    def _record(self, now: float, delta, oi, fund, liq) -> None:
        for k, v in self._metrics(delta, oi, fund, liq).items():
            self.baselines.record(k, v, now)

    # ---- М3 ----------------------------------------------------------------
    def _regime(self, now: float, delta: dict, oi: dict, liq: dict) -> dict:
        cfg = self.cfg
        out = {"windows": [], "main": window_label(cfg.seconds("regime.window"))}
        has_oi = bool(oi["venues"])
        for w in sorted(set(REGIME_WINDOWS) | {cfg.seconds("regime.window")}):
            lbl = window_label(w)
            r = delta["w"][w]
            dz = float(cfg.get("regime.price_deadzone_pct"))
            typ = float(cfg.get("regime.price_deadzone_typical"))
            st = self.baselines.stats(f"ret_abs_{lbl}")
            if typ > 0 and st:
                dz = max(dz, typ * st["p50"])
            p = sign(r["ret"], dz)
            o = sign(oi["totals"]["pct"].get(w), float(cfg.get("regime.oi_deadzone_pct"))) if has_oi else None
            dzz = float(cfg.get("regime.delta_deadzone_z"))
            dp, ds = sign(r["z_perp"], dzz), sign(r["z_spot"], dzz)
            lv = liq["tot"].get(w)
            liq_usd = sum(lv) if lv else 0.0
            lz = z_of(liq_usd, self.baselines.stats(f"liq_{lbl}"))
            if lz is not None:
                big_liq = lz >= float(cfg.get("regime.liq_significant_z"))
            else:  # no history yet: a tenth of the perp depth within 1 %
                big_liq = bool(liq["depth"]) and liq_usd >= 0.1 * liq["depth"]
            reg = classify(p, o, dp, ds, big_liq)
            self.regimes.update(lbl, reg, now)
            missing = []
            if p is None:
                missing.append(f"цена: копится история окна {_ru_window(w)}")
            if o is None:
                missing.append("ОИ: нет данных ни с одной биржи" if not has_oi
                               else f"ОИ: копится история окна {_ru_window(w)}")
            for name, kind, v in (("фьючерсов", "perp", dp), ("спота", "spot", ds)):
                if v is None:
                    missing.append(f"дельта {name}: нет стаканов и нормы объёма" if r[f"nd_{kind}"] is None
                                   else f"дельта {name}: норма копится (~5 мин после выбора монеты)")
            out["windows"].append({
                "w": lbl, "code": reg.code, "label": reg.label, "tone": reg.tone,
                "since": self.regimes.since(lbl),
                "inputs": {"P": p, "O": o, "Dp": dp, "Ds": ds, "L": big_liq, "ret": r["ret"],
                           "oi_pct": oi["totals"]["pct"].get(w), "z_perp": r["z_perp"], "z_spot": r["z_spot"],
                           "liq_usd": liq_usd, "price_deadzone": dz},
                "missing": missing,
            })
        out["divergence"] = self.regimes.divergence("1m", "15m")
        out["history"] = list(self.regimes.history)[-50:][::-1]
        return out

    # ---- signals -----------------------------------------------------------
    def _signals(self, now: float, price: float | None, delta: dict, oi: dict, fund: dict, liq: dict,
                 p2: dict | None = None, p3: dict | None = None) -> list[dict]:
        cfg = self.cfg
        on_lvl = float(cfg.get("alerts.hysteresis_on"))
        off_lvl = float(cfg.get("alerts.hysteresis_off"))
        cooldown = float(cfg.get("alerts.cooldown_min")) * 60
        alert_z = float(cfg.get("alert_z"))
        results: list[tuple[str, str, float | None, dict]] = []  # (type, key, strength, payload)

        if cfg.on("delta"):
            results += self._sig_delta(delta)
        if cfg.on("open_interest"):
            results += self._sig_oi(delta, oi, alert_z)
        if cfg.on("funding"):
            results += self._sig_funding(delta, oi, fund, alert_z)
        if cfg.on("liquidations"):
            results += self._sig_liq(liq)
        if p2:
            if cfg.on("orderbook"):
                results += self._sig_book(now, p2)
            if cfg.on("borrow"):
                results += self._sig_borrow(delta, oi, fund, p2)
            if cfg.on("index"):
                results += self._sig_index(p2)
        if p3:
            results += self._sig_phase3(now, p2 or {}, p3)
        if cfg.on("lab"):
            results += self._sig_lab(now)

        seen = {(t, k) for t, k, _, _ in results}
        for (t, k), st in list(self.board.states.items()):
            if st.on and (t, k) not in seen:  # its condition is gone (a level no longer defended, ...)
                results.append((t, k, None, {}))
        alerts = []
        low = self.baselines.low_history(now)
        for type_, key, strength, payload in results:
            if self.board.update(type_, key, strength, payload, now, on_lvl, off_lvl, cooldown):
                spec = SPECS[type_]
                score = SignalBoard.score(strength or 0, on_lvl)
                venue = payload.get("venue") or (key.split(":", 1)[0] if ":" in key else "Все биржи")
                reasons = list(payload.get("reasons") or [])
                if low:
                    reasons.append(f"Мало истории: {self.baselines.history_days(now):.1f} дн. из "
                                   f"{min(cfg.get('baseline_days'))}, нормы по текущей сессии")
                alerts.append({
                    "ts": now, "coin": self.coin, "key": f"{spec.module}:{type_}:{key}", "venue": venue,
                    "kind": payload.get("kind", key.split(":", 1)[1] if ":" in key else ""), "score": round(score, 1),
                    "reasons": [f"{spec.module} · {payload.get('title') or spec.title}"] + reasons,
                    "price": price, "vol_w": None, "ratio": None, "buy_share": None,
                    "module": spec.module, "type": type_, "title": payload.get("title") or spec.title,
                    "direction": payload.get("direction", spec.direction), "low_history": low,
                    "data": {k: v for k, v in payload.items() if k not in ("reasons", "title", "venue", "kind")},
                })
        return alerts

    def _sig_delta(self, delta: dict) -> list:
        cfg = self.cfg
        w = cfg.seconds("delta.signal_window")
        r = delta["w"].get(w)
        if not r:
            return []
        lbl = window_label(w)
        wl = _ru_window(w)
        st = {k: self.baselines.stats(f"nd_{k}_{lbl}") for k in ("spot", "perp", "all")}
        ret = r["ret"]
        thr = float(cfg.get("delta.price_threshold_pct"))
        dz = float(cfg.get("regime.price_deadzone_pct"))
        absorb = float(cfg.get("delta.absorb_price_pct"))
        out = []

        def nd_txt(kind: str) -> str:
            s = st[kind]
            z = r.get(f"z_{kind}")
            name = {"spot": "спот", "perp": "фьючерсы", "all": "все рынки"}[kind]
            d = r["delta_spot"] + r["delta_perp"] if kind == "all" else r[f"delta_{kind}"]
            norm = f"норм. {r[f'nd_{kind}']:+.3f}" if r.get(f"nd_{kind}") is not None else "норм. —"
            ref = f", p10 {s['p10']:+.3f} / p50 {s['p50']:+.3f} / p90 {s['p90']:+.3f}" if s else ""
            ztxt = f", z {z:+.1f}" if z is not None else ""
            return f"дельта {name} {'+' if d >= 0 else '−'}{fmt_usd(abs(d))} ({norm}{ztxt}{ref})"

        # «Рост на плечах»
        up = (ret / thr) if ret is not None and thr > 0 else None
        perp_hi = _margin_above(r.get("nd_perp"), st["perp"])
        below_mid = _margin_below(r.get("nd_spot"), st["spot"])  # >= 0 when spot delta is below its median
        spot_lo = None if below_mid is None else 1 + below_mid
        out.append(("lever_rally", "all", _mins(up, perp_hi, spot_lo), {
            "reasons": [f"За {wl} цена {_fmt_pct(ret)} (порог {thr}%)", nd_txt("perp") + " — выше p90",
                        nd_txt("spot") + " — ниже p50", "Рост идёт на плечах, спот не покупают: хрупко"]}))
        # «Падение на плечах» (зеркально): цена упала, фьючерсная дельта ниже p10, спот выше медианы
        down = (-ret / thr) if ret is not None and thr > 0 else None
        perp_low = _margin_below(r.get("nd_perp"), st["perp"])
        above_mid = _margin_above(r.get("nd_spot"), st["spot"])
        spot_hi_mid = None if above_mid is None else 1 + above_mid
        out.append(("lever_drop", "all", _mins(down, perp_low, spot_hi_mid), {
            "reasons": [f"За {wl} цена {_fmt_pct(ret)} (порог −{thr}%)", nd_txt("perp") + " — ниже p10",
                        nd_txt("spot") + " — выше p50", "Падение идёт на плечах, спот не продают: хрупко"]}))
        # «Рост, подтверждённый спросом»
        spot_hi = _margin_above(r.get("nd_spot"), st["spot"])
        out.append(("demand_rally", "all", _mins(spot_hi, (ret / dz) if ret is not None and dz > 0 else None), {
            "reasons": [nd_txt("spot") + " — выше p90", f"Цена {_fmt_pct(ret)} за {wl}",
                        "Монету реально покупают на споте"]}))
        # «Скрытый покупатель» / «Скрытый продавец»
        nd = r.get("nd_all")
        if ret is not None:
            hold_down = min(3.0, absorb / max(-ret, 1e-9)) if absorb > 0 else None
            hold_up = min(3.0, absorb / max(ret, 1e-9)) if absorb > 0 else None
        else:
            hold_down = hold_up = None
        out.append(("hidden_buyer", "all", _mins(_margin_below(nd, st["all"]), hold_down), {
            "reasons": [nd_txt("all") + " — ниже p10 (сильные продажи)",
                        f"а цена за {wl} {_fmt_pct(ret)} (почти не падает, порог −{absorb}%)",
                        "Кто-то поглощает продажи лимитками или айсбергом на покупку"]}))
        out.append(("hidden_seller", "all", _mins(_margin_above(nd, st["all"]), hold_up), {
            "reasons": [nd_txt("all") + " — выше p90 (сильные покупки)",
                        f"а цена за {wl} {_fmt_pct(ret)} (почти не растёт, порог +{absorb}%)",
                        "Кто-то раздаёт лимитками в покупателей"]}))
        return out

    def _sig_oi(self, delta: dict, oi: dict, alert_z: float) -> list:
        out = []
        venues = oi["venues"]
        zs = {k: v.get("z5") for k, v in venues.items()}
        flow5 = delta["w"].get(300, {})
        for key, v in venues.items():
            z = zs[key]
            others = [abs(x) for k, x in zs.items() if k != key and x is not None]
            if z is None or not others:
                strength = None
            else:
                strength = _mins(z / alert_z, *[1 / max(x, 1e-9) for x in others])
            venue = key.split(":", 1)[0]
            d_perp = self.flow(key, 300)
            out.append(("oi_initiator", key, strength, {
                "title": f"Инициатор на {venue}",
                "direction": 1 if d_perp > 0 else -1 if d_perp < 0 else 0,
                "reasons": [
                    f"ОИ {venue} за 5м {_fmt_pct(v['pct'].get(300))} "
                    f"({'+' if (v['d'].get(300) or 0) >= 0 else ''}{(v['d'].get(300) or 0):,.0f} монет), z {z:+.1f}"
                    if z is not None else f"ОИ {venue}: мало данных",
                    "На остальных биржах ОИ в норме (|z| < 1): " + ", ".join(
                        f"{k.split(':', 1)[0]} {x:+.1f}" for k, x in zs.items() if k != key and x is not None),
                    f"Агрессор на {venue} за 5м: {'покупки' if d_perp > 0 else 'продажи'} {fmt_usd(abs(d_perp))}",
                ]}))
        dz = float(self.cfg.get("regime.oi_deadzone_pct"))
        need = int(self.cfg.get("open_interest.broad_min_venues"))
        rising = sorted(((k.split(":", 1)[0], v["pct"][300]) for k, v in venues.items()
                         if (v["pct"].get(300) or 0) > dz), key=lambda x: -x[1])
        d_perp = flow5.get("delta_perp") or 0
        listed = ", ".join(f"{v} {p:+.2f}%" for v, p in rising[:8]) + (
            f" и ещё {len(rising) - 8}" if len(rising) > 8 else "")
        out.append(("oi_broad", "all", (len(rising) / need) if need and len(venues) >= need else None, {
            "direction": 1 if d_perp > 0 else -1 if d_perp < 0 else 0,
            "reasons": [f"ОИ за 5м растёт одновременно на {len(rising)} из {len(venues)} бирж (нужно {need}): {listed}",
                        "Это общий интерес, а не один участник"]}))
        return out

    def _sig_funding(self, delta: dict, oi: dict, fund: dict, alert_z: float) -> list:
        cfg = self.cfg
        out = []
        r5, r15 = delta["w"].get(300, {}), delta["w"].get(900, {})
        dz = float(cfg.get("regime.price_deadzone_pct"))
        zf = z_of(fund["f8_median"], self.baselines.stats("f8_med"))
        st_b = self.baselines.stats("basis_x")
        basis = fund["cross_basis"]
        b99 = (basis / st_b["p99"]) if basis is not None and st_b and st_b["p99"] > 0 else None
        hot = max([x for x in (zf / alert_z if zf is not None else None, b99) if x is not None], default=None)
        ret5 = r5.get("ret")
        stopped = (min(3.0, dz / max(ret5, 1e-9)) if ret5 is not None else None)
        out.append(("crowd_long", "all", _mins(hot, stopped), {
            "reasons": [f"Фандинг (медиана по биржам, к 8ч) {fund['f8_median']:+.4f}%"
                        + (f", z {zf:+.1f}" if zf is not None else "") if fund["f8_median"] is not None
                        else "Фандинг: нет данных",
                        f"Базис {fund['cross_ref'] or ''} перп к споту {_fmt_pct(basis, 3)}"
                        + (f" (p99 {st_b['p99']:+.3f}%)" if st_b else ""),
                        f"Цена за 5м {_fmt_pct(ret5)} — перестала расти", "Риск слива: толпа в лонгах"]}))
        b1 = (basis / st_b["p1"]) if basis is not None and st_b and st_b["p1"] < 0 else None
        cold = max([x for x in (-zf / alert_z if zf is not None else None, b1) if x is not None], default=None)
        stopped_falling = (min(3.0, dz / max(-ret5, 1e-9)) if ret5 is not None else None)
        out.append(("crowd_short", "all", _mins(cold, stopped_falling), {
            "reasons": [f"Фандинг (медиана по биржам, к 8ч) {fund['f8_median']:+.4f}%"
                        + (f", z {zf:+.1f}" if zf is not None else "") if fund["f8_median"] is not None
                        else "Фандинг: нет данных",
                        f"Базис {fund['cross_ref'] or ''} перп к споту {_fmt_pct(basis, 3)}"
                        + (f" (p1 {st_b['p1']:+.3f}%)" if st_b else ""),
                        f"Цена за 5м {_fmt_pct(ret5)} — перестала падать", "Риск шорт-сквиза: толпа в шортах"]}))
        doi15 = oi["totals"]["pct"].get(900)
        ret15 = r15.get("ret")
        zs15 = r15.get("z_spot")
        farm_max = float(cfg.get("funding.farm_max_move_pct"))
        creeping = (1.0 if ret15 is not None and 0 < ret15 < farm_max else 0.0) if ret15 is not None else None
        out.append(("funding_farm", "all", _mins(
            (doi15 / float(cfg.get("regime.oi_deadzone_pct"))) if doi15 is not None else None,
            zf / alert_z if zf is not None else None,
            (1 / max(abs(zs15), 1e-9)) if zs15 is not None else None, creeping), {
            "reasons": [f"ОИ за 15м {_fmt_pct(doi15)}", f"Фандинг экстремальный: z {zf:+.1f}" if zf is not None
                        else "Фандинг: мало истории",
                        f"Спотовая дельта плоская (z {zs15:+.1f})" if zs15 is not None else "Спот: мало данных",
                        f"Цена ползёт вверх: {_fmt_pct(ret15)} за 15м",
                        "Рост может оплачиваться фандингом, а не спросом"]}))
        st_db, st_dp = self.baselines.stats("d_basis_5m"), self.baselines.stats("d_prem_5m")
        db = self._grid_change(self.basis_grid, basis)
        dp = self._grid_change(self.prem_grid, fund["premium"])
        zb, zp = z_of(db, st_db), z_of(dp, st_dp)
        zs5 = r5.get("z_spot")
        out.append(("perp_leads", "all", _mins(
            zb / alert_z if zb is not None else None,
            *([zp / alert_z] if zp is not None else []),
            (1 / max(abs(zs5), 1e-9)) if zs5 is not None else None), {
            "direction": 1,
            "reasons": [f"Базис за 5м {'+' if (db or 0) >= 0 else ''}{(db or 0):.3f} п.п., z {zb:+.1f}"
                        if zb is not None else "Базис: мало истории",
                        f"Премия mark/index за 5м {'+' if (dp or 0) >= 0 else ''}{(dp or 0):.3f} п.п., z {zp:+.1f}"
                        if zp is not None else "Премия mark/index: нет данных",
                        f"Спотовая дельта плоская (z {zs5:+.1f})" if zs5 is not None else "Спот: мало данных",
                        "Цену тянет перп, спот не участвует"]}))
        return out

    def _sig_liq(self, liq: dict) -> list:
        if 60 not in liq["tot"]:
            return []
        lng, sht = liq["tot"][60]
        total = lng + sht
        st = self.baselines.stats("liq_1m")
        strength = (total / st["p99"]) if st and st["p99"] > 0 and total > 0 else None
        return [("liq_cascade", "all", strength, {
            "direction": 1 if lng > sht else -1,
            "reasons": [f"Ликвидации за 1м {fmt_usd(total)}: лонги {fmt_usd(lng)}, шорты {fmt_usd(sht)}"
                        + (f" (p99 {fmt_usd(st['p99'])})" if st else ""),
                        "Движение принудительное; после конца каскада часто бывает разворот",
                        "Binance шлёт не больше одной ликвидации в секунду на символ — сумма занижена"]})]

    # ---- фаза 2: М5 стакан, М6 займы, М7 индекс ----------------------------------
    def _phase2(self, now: float, price: float | None, delta: dict, fund: dict, streams: dict) -> dict:
        cfg = self.cfg
        out: dict = {"book_public": None, "borrow": None, "index_public": None, "index": []}
        fresh = self.book.fresh(now)
        aggs = {scope: self.book.aggregate(now, scope) for scope in ("all", "spot", "perp")}
        out["aggs"] = aggs
        if cfg.on("orderbook"):
            def ctm(a: dict | None) -> dict | None:
                if not a:
                    return None
                return {"venues": a["venues"], "up": {str(x): v for x, v in a["ask"].items()},
                        "down": {str(x): v for x, v in a["bid"].items()}}
            venues = []
            for key, m in fresh.items():
                venues.append({"key": key, "up": {str(x): v for x, v in m["ask"].items()},
                               "down": {str(x): v for x, v in m["bid"].items()},
                               "spread_bps": m["spread_bps"], "reach_up": m["reach_ask"], "reach_down": m["reach_bid"]})
            venues.sort(key=lambda v: -(v["up"].get("1.0", 0) + v["down"].get("1.0", 0)))
            a = aggs["all"]
            st_up, st_dn = self.baselines.stats("ctm2_up"), self.baselines.stats("ctm2_down")
            out["book_public"] = {
                "all": ctm(a), "spot": ctm(aggs["spot"]), "perp": ctm(aggs["perp"]),
                "profile": None if not a else {
                    "bucket_pct": BUCKET_PCT, "bid": [round(x) for x in a["bid_b"]], "ask": [round(x) for x in a["ask_b"]],
                    "gaps_bid": a["gaps_bid"], "gaps_ask": a["gaps_ask"], "seen_bid": a["bid_seen"],
                    "seen_ask": a["ask_seen"], "median": a["bucket_median"]},
                "norm": {"up": st_up and {"p10": st_up["p10"], "p50": st_up["p50"]},
                         "down": st_dn and {"p10": st_dn["p10"], "p50": st_dn["p50"]}},
                "venues": venues,
                "events": [{k: v for k, v in e.items() if k != "pkey"} for e in self.book.recent(now, window=1800)][-80:][::-1],
                "icebergs": [[e["ts"], e["price"], e["side"]] for e in self.book.recent(now, "iceberg", 4 * 3600)],
                "defended": self.book.defended(now),
                "spoofs": self.book.spoof_counts(now),
            }
        if cfg.on("borrow"):
            seed = self.borrow.take_seed()
            if seed:
                self.seed_history("borrow_rate:Binance", seed)
            vol_h = sum(a + b for k, fl in self.flows.items() for i, (a, b) in fl.items() if i * STEP >= now - 3600)
            rows = []
            for venue, r in self.borrow.rows.items():
                if now - r["ts"] > 15 * 60:
                    continue
                row = dict(r)
                if row.get("available") is not None and price:
                    row["available_usd"] = row["available"] * price
                if row.get("available_usd") is not None and vol_h > 0:
                    row["share_day_volume"] = row["available_usd"] / (vol_h * 24)
                for lbl, sec in (("1h", 3600), ("4h", 4 * 3600), ("24h", 24 * 3600)):
                    row[f"chg_{lbl}"] = self.borrow.change_pct(venue, now, sec)
                st = self.baselines.stats30(f"borrow_rate:{venue}") or self.baselines.stats(f"borrow_rate:{venue}")
                row["rate_norm_apr"] = st["median"] * 24 * 365 * 100 if st and st["median"] else None
                row["rate_ratio"] = (row["rate_h"] / st["median"]) if st and st["median"] and row.get("rate_h") else None
                rows.append(row)
            out["borrow"] = {"rows": sorted(rows, key=lambda r: (r.get("kind") != "CEX", r["venue"])),
                             "status": dict(self.borrow.status), "key": self.borrow.key_state}
        if cfg.on("index"):
            if self.demo:
                self.index.demo_baskets(sorted(k.split(":", 1)[0] for k in self.kinds if self.kinds[k] == "spot"
                                               and self._venue_price(k, now)))
            else:
                self.index.set_ids(streams)

            def price_of(venue: str) -> float | None:
                return self._venue_price(f"{venue}:spot", now)

            def depth_of(venue: str) -> float | None:
                m = fresh.get(f"{venue}:spot")
                return min(m["ask"].get(1.0, 0.0), m["bid"].get(1.0, 0.0)) if m else None

            ex_index = {src: (self.fund.get(f"{src}:perp") or {}).get("index") for src in ("Binance", "OKX", "Bybit")}
            baskets = self.index.compute(now, price_of, depth_of, ex_index)
            out["index"] = baskets
            out["index_public"] = {"baskets": baskets, "status": dict(self.index.status), "ids": dict(self.index.ids)}
        return out

    def _metrics2(self, p2: dict) -> dict[str, float | None]:
        m: dict[str, float | None] = {}
        for scope, suffix in (("all", ""), ("perp", "p")):
            a = (p2.get("aggs") or {}).get(scope)
            if a:
                m[f"ctm2{suffix}_up"] = a["ask"].get(2.0)
                m[f"ctm2{suffix}_down"] = a["bid"].get(2.0)
        for r in (p2.get("borrow") or {}).get("rows") or []:
            m[f"borrow_rate:{r['venue']}"] = r.get("rate_h")
            m[f"borrow_avail:{r['venue']}"] = r.get("available") if r.get("available") is not None \
                else r.get("available_usd")
        return m

    def _sig_book(self, now: float, p2: dict) -> list:
        cfg = self.cfg
        out = []
        a = (p2.get("aggs") or {}).get("all")
        for side, type_, word, book_side in (("up", "void_up", "вверх", "ask"), ("down", "void_down", "вниз", "bid")):
            st = self.baselines.stats(f"ctm2_{side}")
            x = a[book_side].get(2.0) if a else None
            gaps = sum(a[f"gaps_{book_side}"][:20]) if a else 0
            out.append((type_, "all", _margin_below(x, st), {
                "reasons": [f"Цена сдвига {'+' if side == 'up' else '−'}2% по сводному стакану ({a['venues'] if a else 0} бирж): "
                            f"{fmt_usd(x or 0)}" + (f" — ниже p10 {fmt_usd(st['p10'])} (норма {fmt_usd(st['p50'])})" if st else ""),
                            f"Пустых корзин 0.1% в пределах 2% {word}: {gaps}",
                            f"Цена пролетит {word} без сопротивления"]}))
        need = int(cfg.get("orderbook.defended_min_events"))
        for d in self.book.defended(now):
            venue, kind = d["key"].split(":", 1)
            side = "покупку" if d["side"] == "bid" else "продажу"
            out.append(("defended_level", f"{d['key']}|{d['side']}|{d['price']:.10g}", d["count"] / max(1, need), {
                "venue": venue, "kind": kind, "title": f"Защищаемый уровень на {venue}",
                "direction": 1 if d["side"] == "bid" else -1,
                "reasons": [f"Уровень на {side} {d['price']:.6g}: айсбергов {d['icebergs']}, быстрых восстановлений "
                            f"{d['recoveries']} за {int(cfg.get('orderbook.defended_window_min'))} мин",
                            f"Исполнено на этой цене всего {fmt_usd(d['usd'])} — больше, чем было видно в стакане",
                            "Уровень кто-то защищает"]}))
        need_spoof = int(cfg.get("orderbook.spoof_alert_count"))
        for key, c in self.book.spoof_counts(now).items():
            venue, kind = key.split(":", 1)
            for side, other in (("bid", "ask"), ("ask", "bid")):
                n, o = c[side], c[other]
                strength = _mins(n / max(1, need_spoof), (n / (2 * o)) if o else 3.0)
                word = "покупку" if side == "bid" else "продажу"
                out.append(("false_wall", f"{key}|{side}", strength, {
                    "venue": venue, "kind": kind, "title": f"Ложная стена на {venue}",
                    "direction": -1 if side == "bid" else 1,
                    "reasons": [f"За час сняли без исполнения {n} крупных заявок на {word} на {fmt_usd(c['usd_' + side])} "
                                f"(на другой стороне {o})",
                                f"Заявки крупнее p{int(cfg.get('orderbook.spoof_size_pctl'))} уровней стакана, сняты ближе "
                                f"{cfg.get('orderbook.spoof_cancel_distance_pct')}% к цене",
                                "Давление, скорее всего, в обратную сторону: " + ("вниз" if side == "bid" else "вверх")]}))
        return out

    def _sig_borrow(self, delta: dict, oi: dict, fund: dict, p2: dict) -> list:
        cfg = self.cfg
        out = []
        drop_thr = float(cfg.get("borrow.inventory_drop_pct_4h"))
        ratio_thr = float(cfg.get("borrow.rate_ratio_alert"))
        r15 = delta["w"].get(900, {})
        dz = float(cfg.get("regime.price_deadzone_pct"))
        for r in (p2.get("borrow") or {}).get("rows") or []:
            chg4 = r.get("chg_4h")
            parts = [(-chg4 / drop_thr) if chg4 is not None and drop_thr > 0 else None,
                     (r["rate_ratio"] / ratio_thr) if r.get("rate_ratio") is not None and ratio_thr > 0 else None]
            parts = [x for x in parts if x is not None]
            strength = max(parts) if parts else None
            reasons = []
            if chg4 is not None:
                reasons.append(f"Доступно к займу на {r['venue']} за 4ч {chg4:+.0f}% (порог −{drop_thr:.0f}%)")
            if r.get("rate_ratio") is not None:
                reasons.append(f"Ставка {r['rate_apr']:.1f}% годовых — ×{r['rate_ratio']:.1f} к медиане за 30 дней "
                               f"(порог ×{ratio_thr:g})")
            f8m, doi = fund.get("f8_median"), oi["totals"]["pct"].get(900)
            if f8m is not None and f8m < 0 and doi is not None and doi > 0:
                reasons.append("Фандинг отрицательный и ОИ растёт: много шортов — топливо для шорт-сквиза")
            if r15.get("ret") is not None and abs(r15["ret"]) < dz and (r15.get("z_spot") or 0) > 0:
                reasons.append("Цена стоит, а спотовая дельта положительная: похоже на выкуп предложения")
            if r.get("updated"):
                reasons.append("Квоты биржа обновляет не мгновенно — время обновления в таблице займов")
            out.append(("borrow_dry", f"borrow|{r['venue']}", strength, {
                "venue": r["venue"], "kind": "", "title": f"Займ иссякает на {r['venue']}", "reasons": reasons}))
        return out

    def _sig_index(self, p2: dict) -> list:
        from app.analytics.stats import percentile

        cfg = self.cfg
        out = []
        thr = float(cfg.get("index.dev_alert_pct"))
        persist_need = float(cfg.get("index.dev_persist_sec"))
        perp = (p2.get("aggs") or {}).get("perp")
        for b in p2.get("index") or []:
            depths = sorted(r["ctm1"] for r in b["rows"] if r.get("ctm1"))
            p10 = percentile(depths, 10) if len(depths) >= 3 else (depths[0] if depths else None)
            for r in b["rows"]:
                if r.get("dev") is None or not r["weight"]:
                    strength = None
                else:
                    thin = (p10 / r["ctm1"]) if r.get("ctm1") and p10 else None
                    strength = _mins(abs(r["dev"]) / thr, r["persist"] / max(persist_need, 1e-9), thin)
                reasons = []
                if r.get("dev") is not None:
                    up = r["dev"] > 0
                    reasons = [
                        f"{r['venue']} в индексе {b['source']} (вес {r['weight'] * 100:.0f}%): цена {_fmt_pct(r['dev'], 2)} "
                        f"к остальным составляющим, держится {r['persist']:.0f} с (порог {thr}% и {persist_need:.0f} с)",
                        f"Глубина {r['venue']} ±1%: {fmt_usd(r['ctm1'] or 0)}" + (f" — среди самых тонких (p10 {fmt_usd(p10)})" if p10 else ""),
                        f"Вклад в индекс {r['influence']:+.3f}%: индекс и mark тянут {'вверх' if up else 'вниз'}",
                    ]
                    side = "up" if up else "down"
                    st = self.baselines.stats(f"ctm2p_{side}")
                    x = perp["ask" if up else "bid"].get(2.0) if perp else None
                    m = _margin_below(x, st)
                    if strength is not None and m is not None and m >= 1:
                        strength *= 1.3
                        reasons.append(f"Усилено: в стакане перпа пусто {'вверх' if up else 'вниз'} (М5)")
                out.append(("index_pull", f"{b['source']}|{r['venue']}", strength, {
                    "venue": r["venue"], "kind": "spot", "title": f"Индекс {b['source']} тянут через {r['venue']}",
                    "direction": (1 if (r.get("dev") or 0) > 0 else -1), "reasons": reasons}))
        return out

    # ---- фаза 3: М8 DEX, М9 Hyperliquid, М12 качество -----------------------------------
    def _wkey(self, key: str) -> float:
        """Weight of a stream in the aggregates: the manual venue weight x its quality (М12)."""
        w = self._weight(key.split(":", 1)[0])
        if self.cfg.on("quality"):
            w *= self.quality.weight(key)
        return w

    def _on_dex_swaps(self, stream, trades: list, network: str | None = None) -> None:
        """DEX swaps: sandwich filter, large swaps, likely arbitrage on CEX, whales to check later."""
        now = time.time()
        cfg = self.cfg
        if network is None:
            network = (getattr(stream, "pool", None) or {}).get("network") or (self.dex.token or {}).get("chain")
        sides: dict[tuple[str, int], set] = {}
        for t in trades:
            if t.taker and t.block:
                sides.setdefault((t.taker, t.block), set()).add(t.side)
        for s in self.swaps:  # the other leg may have come in an earlier batch
            if s.get("taker") and s.get("block") and (s["taker"], s["block"]) in sides:
                sides[(s["taker"], s["block"])].add(s["side"])
        sizes = sorted(self.swap_sizes)
        thr = max(1000.0, sizes[int(len(sizes) * 0.99) - 1]) if len(sizes) >= 50 else 10000.0
        win = float(cfg.get("dex.arb_match_window_sec"))
        for t in trades:
            if t.usd <= 0 or t.side not in ("buy", "sell"):
                continue
            self.swap_sizes.append(t.usd)
            sandwich = t.taker and t.block and sides.get((t.taker, t.block), set()) >= {"buy", "sell"}
            if sandwich or t.usd < thr:
                continue
            swap = {"ts": t.ts, "venue": stream.venue if stream else "DEX (RPC)", "side": t.side, "usd": t.usd,
                    "price": t.price, "taker": t.taker or None, "block": t.block or None, "tx": t.tid or None,
                    "url": explorer_tx(network or "", t.tid) if t.tid else None}
            # likely arbitrage: CEX prints of the same side within `win` s after the swap, of a comparable size
            hits = [p for sec in range(int(t.ts), int(t.ts + win) + 1) for p in self.cex_secs.get(sec, ())
                    if t.ts <= p[0] <= t.ts + win and p[2] == t.side and not p[4]]
            cex_usd = sum(p[3] for p in hits)
            if hits and 0.3 * t.usd <= cex_usd <= 3 * t.usd:
                swap["arb"] = {"cex_usd": cex_usd, "venues": sorted({p[1].split(":", 1)[0] for p in hits})}
                for p in hits:
                    p[4] = True
                    if cfg.get("dex.exclude_arb_from_delta"):
                        cell = self.flows.get(p[1], {}).get(int(p[0]) // STEP)
                        if cell:
                            j = 0 if p[2] == "buy" else 1
                            cell[j] = max(0.0, cell[j] - p[3])
                self.arb_events.appendleft({**swap, "detected": now})
            self.swaps.appendleft(swap)
            self.whale_pending.append(swap)

    def _check_whales(self, now: float) -> None:
        follow = float(self.cfg.get("dex.whale_follow_sec"))
        keep = []
        for s in self.whale_pending:
            if now < s["ts"] + follow + 2:
                keep.append(s)
                continue
            def flow(t0: float, t1: float) -> float:
                return sum((p[3] if p[2] == "buy" else -p[3])
                           for sec in range(int(t0), int(t1) + 1) for p in self.cex_secs.get(sec, ()) if t0 <= p[0] <= t1)

            after = flow(s["ts"], s["ts"] + follow)
            before = flow(s["ts"] - follow, s["ts"])
            same = after > 0 if s["side"] == "buy" else after < 0
            # the CEX flow turned the swap's way: big enough and clearly stronger than before the swap
            if same and abs(after) >= 0.5 * s["usd"] and abs(after) >= 2 * abs(before):
                self.dex_whales.appendleft({**s, "cex_delta": after, "cex_before": before, "found": now})
        self.whale_pending = keep

    def _lead(self, now: float, consensus: float | None) -> None:
        """1-second grids of the CEX consensus and the DEX price; xcorr every 10 s, move events."""
        sec = int(now)
        dex_px = None
        while self.dex.rpc_swaps:  # level 2: exact swap prices from the node
            s = self.dex.rpc_swaps.popleft()
            if s.get("price"):
                dex_px = s["price"]
                self._rpc_last = (s["ts"], dex_px)
            self._on_dex_swaps(None, [Trade(s["ts"], s["price"] or 0, s["coin"], s["usd"], s["side"],
                                            tid=s.get("tx") or "", block=s.get("block") or 0)],
                               network=(self.dex.token or {}).get("chain"))
        if dex_px is None:
            rl = getattr(self, "_rpc_last", None)
            if rl and now - rl[0] < 30:
                dex_px = rl[1]
            elif self.dex.price and now - self.dex.price_ts < 15:
                dex_px = self.dex.price
        if not self.lead_cex or self.lead_cex[-1][0] != sec:
            self.lead_cex.append((sec, consensus))
            self.lead_dex.append((sec, dex_px))
        window = float(self.cfg.get("dex.leadlag_window_min")) * 60
        max_lag = int(self.cfg.get("dex.leadlag_max_lag_sec"))
        while self.lead_cex and self.lead_cex[0][0] < sec - window - max_lag:
            self.lead_cex.popleft()
            self.lead_dex.popleft()
        if now - self._lead_at >= 10 and len(self.lead_cex) >= 120:
            self._lead_at = now
            lag, corr = xcorr_lead([p for _, p in self.lead_dex], [p for _, p in self.lead_cex], max_lag)
            self.lead = {"lag": lag, "corr": corr, "ts": now,
                         "leader": None if corr is None or corr < 0.1 else
                         ("DEX" if lag >= 2 else "биржи" if lag <= -2 else "одновременно")}
        # event method: a move of move_pct % within move_max_sec on either side
        move = float(self.cfg.get("leadership.move_pct"))
        span = int(self.cfg.get("leadership.move_max_sec"))
        if self.lead_events and now - self.lead_events[0]["ts"] < span:
            return
        cex = [(t, p) for t, p in self.lead_cex if t >= sec - span and p]
        dex = [(t, p) for t, p in self.lead_dex if t >= sec - span and p]
        if len(cex) < span // 2 or len(dex) < 3:
            return
        mv = {name: (ser[-1][1] / ser[0][1] - 1) * 100 for name, ser in (("биржи", cex), ("DEX", dex))}
        name = max(mv, key=lambda k: abs(mv[k]))
        if abs(mv[name]) < move:
            return
        other = "DEX" if name == "биржи" else "биржи"
        if mv[other] * mv[name] <= 0 or abs(mv[other]) < move / 3:
            # only one side moved: it led, the other has not followed (yet)
            leader, tc, td, by = name, None, None, None
        else:
            tc = half_cross(cex, cex[0][1], cex[-1][1])
            td = half_cross(dex, dex[0][1], dex[-1][1])
            if tc is None or td is None:
                return
            leader = "DEX" if td < tc else "биржи" if tc < td else "одновременно"
            by = abs(tc - td)
        self.lead_events.appendleft({"ts": now, "dir": 1 if mv[name] > 0 else -1, "move_pct": mv[name],
                                     "other_pct": mv[other], "cex_t": tc, "dex_t": td, "leader": leader,
                                     "by_sec": by})

    def _depth_from_profile(self, agg: dict | None, side: str, pct: float) -> float | None:
        """Cost to move the aggregated book by pct % (bucket profile of М5)."""
        if not agg:
            return None
        b = agg["ask_b" if side == "up" else "bid_b"]
        n = min(len(b), max(1, int(round(pct / BUCKET_PCT))))
        return sum(b[:n])

    def _phase3(self, now: float, price: float | None, streams: dict, p2: dict) -> dict:
        cfg = self.cfg
        out: dict = {"dex": None, "hl": None, "quality": None}
        agg = (p2.get("aggs") or {}).get("all")
        if cfg.on("quality"):
            if self.quality.due(now):
                def depth_of(key: str) -> float | None:
                    vals = [d for _, d in self.depth.get(key, ())]
                    return median(vals) if vals else None
                minutes = {}
                for ts, p in self.price.points:
                    minutes[int(ts) // 60] = p
                for key, s in self.quality.compute(now, depth_of, minutes).items():
                    if s["score"] is not None:
                        self.baselines.record(f"quality:{key}", s["score"], now)
            out["quality"] = self.quality.public()
        if cfg.on("dex"):
            self._lead(now, price)
            self._check_whales(now)
            for sec in [k for k in self.cex_secs if k < now - 240]:
                del self.cex_secs[sec]
            d = self.dex
            dex_depth = d.depth or d.v3
            cex = {s: {str(x): (agg["ask" if s == "up" else "bid"].get(x) if agg else None) for x in (1.0, 2.0, 5.0)}
                   for s in ("up", "down")}
            out["dex"] = {
                "token": d.token, "pools": d.pools, "price": d.price, "price_ts": d.price_ts or None,
                "premium_pct": (d.price / price - 1) * 100 if d.price and price else None,
                "depth": d.depth, "v3": d.v3, "cex_depth": cex, "rpc": d.rpc_live, "status": dict(d.status),
                "lead": self.lead, "lead_events": list(self.lead_events)[:10],
                "swaps": list(self.swaps)[:60], "arb": list(self.arb_events)[:30], "whales": list(self.dex_whales)[:20],
                "dex_depth": dex_depth,
            }
        if cfg.on("hyperliquid"):
            hl = self.hl
            s = streams.get("Hyperliquid:perp")
            if s is not None and getattr(s, "market", None):
                hl.set_market(s.market, getattr(s, "mult", 1.0))
            elif self.demo and not hl.hl_coin:
                hl.hl_coin, hl.mult = self.coin, 1.0
            whales = hl.whales(now, price)
            lmap = liq_map(hl.positions, price or 0, float(cfg.get("hyperliquid.liq_bucket_pct")))
            reach = []
            dist_max = float(cfg.get("hyperliquid.target_distance_pct"))
            bucket = lmap["bucket_pct"]
            for side, arr, book_side in (("long", lmap["longs"], "down"), ("short", lmap["shorts"], "up")):
                n = int(dist_max / bucket)
                if not any(arr[:n]):
                    continue
                i = max(range(n), key=lambda j: arr[j])
                dist = (i + 0.5) * bucket
                cost = self._depth_from_profile(agg, book_side, dist)
                reach.append({"side": side, "usd": arr[i], "dist_pct": dist, "cost": cost,
                              "reach": (arr[i] / cost) if cost else None})
            biggest = max([r["usd"] for r in reach], default=0.0)
            self.baselines.observe("hl_cluster", biggest, now)
            out["hl"] = {"coin": hl.hl_coin, "whales": whales[:40], "liq_map": lmap, "targets": reach,
                         "events": list(hl.events)[:60], "status": dict(hl.status),
                         "registry": len(hl.registry), "price": price}
        return out

    def _sig_phase3(self, now: float, p2: dict, p3: dict) -> list:
        cfg = self.cfg
        out = []
        dx = p3.get("dex")
        if dx:
            lead = dx.get("lead") or {}
            evs = dx.get("lead_events") or []
            dex_led = sum(1 for e in evs if e["leader"] == "DEX")
            by_events = len(evs) >= 3 and dex_led / len(evs) >= 0.6
            leads = lead.get("leader") == "DEX" or by_events
            ratio = float(cfg.get("dex.cheap_move_ratio"))
            dd = dx.get("dex_depth") or {}
            parts = []
            for side in ("up", "down"):
                dv = (dd.get(side) or {}).get("5.0")
                cv = (dx["cex_depth"].get(side) or {}).get("5.0")
                if dv and cv:
                    parts.append((cv / ratio) / dv)
            cheap = max(parts) if parts else None
            strength = _mins(1.0 if leads else 0.0, cheap) if cheap is not None else None
            reasons = [
                f"Кто ведёт: {lead.get('leader') or 'неясно'}"
                + (f" (сдвиг {lead['lag']:+d} с, корреляция {lead['corr']:.2f})" if lead.get("corr") is not None else "")
                + (f"; по движениям DEX первым в {dex_led} из {len(evs)}" if evs else ""),
            ]
            for side, word in (("up", "вверх"), ("down", "вниз")):
                dv = (dd.get(side) or {}).get("5.0")
                cv = (dx["cex_depth"].get(side) or {}).get("5.0")
                if dv and cv:
                    reasons.append(f"Сдвиг на 5% {word}: DEX {fmt_usd(dv)} против бирж {fmt_usd(cv)} (в {cv / dv:.0f} раз дешевле)")
            reasons.append("Движение дешёвое: высокий риск отката")
            out.append(("dex_leads_thin", "all", strength, {"reasons": reasons}))
            for w in dx.get("whales") or []:
                if now - w["found"] > 300:
                    continue
                key = w.get("tx") or f"{w['ts']:.0f}"
                out.append(("dex_whale", f"dex|{key}", 1.0 + min(2.0, abs(w["cex_delta"]) / max(w["usd"], 1) - 0.5), {
                    "venue": w["venue"], "kind": "spot", "title": f"Кит на DEX ({w['venue']})",
                    "direction": 1 if w["side"] == "buy" else -1,
                    "reasons": [f"Свап на {'покупку' if w['side'] == 'buy' else 'продажу'} {fmt_usd(w['usd'])}"
                                + (f", кошелёк {w['taker'][:10]}…" if w.get("taker") else ""),
                                f"За {cfg.get('dex.whale_follow_sec')} с после него дельта на биржах "
                                f"{'+' if w['cex_delta'] >= 0 else '−'}{fmt_usd(abs(w['cex_delta']))} в ту же сторону "
                                f"(до свапа {'+' if w['cex_before'] >= 0 else '−'}{fmt_usd(abs(w['cex_before']))})",
                                *( [f"Транзакция: {w['url']}"] if w.get("url") else [])]}))
        hl = p3.get("hl")
        if hl:
            add = float(cfg.get("hyperliquid.whale_add_usd"))
            ranked = {w["addr"]: i for i, w in enumerate(hl["whales"])}
            for w in hl["whales"]:
                name = w.get("label") or (w["addr"][:8] + "…")
                c15 = w.get("chg_15m")
                grow = c15 is not None and ((w["side"] == "long" and c15 > 0) or (w["side"] == "short" and c15 < 0))
                started_twap = any(now - t["start"] < 900 for t in w.get("twaps") or [])
                strength = None
                if c15 is not None and grow:
                    strength = abs(c15) / add
                if started_twap:
                    strength = max(strength or 0.0, 1.0)
                twap_txt = [f"Запустил TWAP: {'покупка' if t['side'] == 'buy' else 'продажа'} "
                            f"{t['sz']:,.0f} монет за {t['minutes']:.0f} мин, исполнено {t['progress'] * 100:.0f}%"
                            for t in w.get("twaps") or [] if t.get("progress") is not None]
                out.append(("hl_whale_add", f"hl|{w['addr']}", strength, {
                    "venue": "Hyperliquid", "kind": "perp", "title": f"Кит набирает: {name}",
                    "direction": 1 if w["side"] == "long" else -1,
                    "reasons": [f"{name} ({'лонг' if w['side'] == 'long' else 'шорт'} {fmt_usd(w['usd'])}, "
                                f"плечо {w.get('lev') or '—'}): за 15 мин {'+' if (c15 or 0) >= 0 else '−'}"
                                f"{fmt_usd(abs(c15 or 0))} (порог {fmt_usd(add)})", *twap_txt,
                                f"Адрес: {w['url']}"]}))
                shrink = c15 is not None and not grow and abs(c15) >= add / 2
                big = ranked.get(w["addr"], 99) < 10 or w["usd"] >= 2 * add
                out.append(("hl_whale_unload", f"hl|{w['addr']}", (abs(c15) / (add / 2)) if shrink and big else None, {
                    "venue": "Hyperliquid", "kind": "perp", "title": f"Публичный кит разгружается: {name}",
                    "direction": -1 if w["side"] == "long" else 1,
                    "reasons": [f"{name} сократил {'лонг' if w['side'] == 'long' else 'шорт'} на {fmt_usd(abs(c15 or 0))} "
                                f"за 15 мин, осталось {fmt_usd(w['usd'])}",
                                "Вероятна разгрузка в тех, кто его копирует", f"Адрес: {w['url']}"]}))
            st = self.baselines.stats("hl_cluster")
            reach_min = float(cfg.get("hyperliquid.reach_min"))
            total = {s: sum(w["usd"] for w in hl["whales"] if w["side"] == s) for s in ("long", "short")}
            for t in hl["targets"]:
                p95 = st["p95"] if st else None
                big = (t["usd"] / p95) if p95 else (t["usd"] / (0.25 * total[t["side"]]) if total[t["side"]] else None)
                rch = (t["reach"] / reach_min) if t.get("reach") is not None else None
                word = "лонгов" if t["side"] == "long" else "шортов"
                out.append(("hl_target", f"hl|{t['side']}", _mins(big, rch) if big is not None and rch is not None else None, {
                    "venue": "Hyperliquid", "kind": "perp", "title": f"Мишень рядом: ликвидации {word}",
                    "direction": -1 if t["side"] == "long" else 1,
                    "reasons": [f"Кластер ликвидаций {word} {fmt_usd(t['usd'])} в {t['dist_pct']:.1f}% от цены"
                                + (f" (p95 кластеров {fmt_usd(p95)})" if p95 else ""),
                                f"Дотащить туда цену стоит {fmt_usd(t['cost'] or 0)} по сводному стакану: "
                                f"кластер = {t['reach']:.1f}× этой суммы" if t.get("reach") is not None else "Глубина стакана: нет данных",
                                "Крупные позиции становятся мишенью"]}))
        return out

    # ---- laboratory --------------------------------------------------------
    def _lab_values(self, now: float, price, delta: dict, oi: dict, fund: dict, liq: dict, p2: dict, p3: dict,
                    regime: dict | None) -> dict[str, float | None]:
        """This minute's inputs of the laboratory that are not stored for the baselines already."""
        v: dict[str, float | None] = {"px": price}
        tot = oi["totals"]["pct"]
        for w, lbl in ((300, "5m"), (900, "15m"), (3600, "1h")):
            v[f"oi_pct_{lbl}"] = tot.get(w)
        if oi["venues"]:
            dz = float(self.cfg.get("regime.oi_deadzone_pct"))
            v["oi_up_venues"] = sum(1 for x in oi["venues"].values() if (x["pct"].get(300) or 0) > dz)
            zs = [x["z5"] for x in oi["venues"].values() if x.get("z5") is not None]
            v["oi_z_max"] = max(zs) if zs else None
        v["f8_spread"] = fund.get("f8_spread")
        v["premium"] = fund.get("premium")
        if liq["depth"] and 300 in liq["tot"]:
            lng, sht = liq["tot"][300]
            v["liq_long_5m_n"], v["liq_short_5m_n"] = lng / liq["depth"], sht / liq["depth"]
        if self.cfg.on("orderbook") and self.book.fresh(now):
            sp = self.book.spoof_counts(now)
            v["spoof_bid"] = sum(d["bid"] for d in sp.values())
            v["spoof_ask"] = sum(d["ask"] for d in sp.values())
            v["defended"] = len(self.book.defended(now))
            v["icebergs_30m"] = len(self.book.recent(now, "iceberg", 1800))
        rows = (p2.get("borrow") or {}).get("rows") or []
        ratios = [r["rate_ratio"] for r in rows if r.get("rate_ratio") is not None]
        chg = [r["chg_4h"] for r in rows if r.get("chg_4h") is not None]
        v["borrow_ratio_max"] = max(ratios) if ratios else None
        v["borrow_chg4h_min"] = min(chg) if chg else None
        devs = [r["dev"] for b in p2.get("index") or [] for r in b.get("rows") or [] if r.get("dev") is not None]
        v["index_dev_max"] = max(devs, key=abs) if devs else None
        dx = p3.get("dex")
        if dx:
            v["dex_prem"] = dx.get("premium_pct")
            lead = dx.get("lead") or {}
            v["dex_lead"] = lead.get("lag") if lead.get("leader") else None
            dd = dx.get("dex_depth") or {}
            parts = [cv / dv for side in ("up", "down")
                     if (dv := (dd.get(side) or {}).get("5.0")) and (cv := (dx["cex_depth"].get(side) or {}).get("5.0"))]
            v["dex_cheap5"] = math.log(max(parts)) if parts else None
        hl = p3.get("hl")
        if hl:
            dp = delta["depth"].get("perp")
            net = [w["chg_15m"] for w in hl["whales"] if w.get("chg_15m") is not None]
            v["hl_whale_net15_n"] = sum(net) / dp if net and dp else None
            reach = {t["side"]: t.get("reach") for t in hl["targets"]}
            v["hl_cluster_long_n"] = reach.get("long", 0.0) or 0.0
            v["hl_cluster_short_n"] = reach.get("short", 0.0) or 0.0
            v["hl_twap_net"] = sum((1 if t["side"] == "buy" else -1) for w in hl["whales"] for t in w.get("twaps") or [])
        main = next((w for w in (regime or {}).get("windows", []) if w["w"] == "5m"), None)
        if main:
            for k in ("P", "O", "Dp", "Ds"):
                v[f"regime_{k}"] = main["inputs"].get(k)
        active = [s for s in self.board.active() if not s["type"].startswith("lab_")]
        dirs = [s.get("direction", SPECS[s["type"]].direction) for s in active]
        v["sig_up"] = sum(1 for d in dirs if d > 0)
        v["sig_down"] = sum(1 for d in dirs if d < 0)
        return v

    def _record_lab(self, now: float, price, delta, oi, fund, liq, p2, p3, regime) -> None:
        """Minute row of the laboratory: stored ("lab:*") and kept in memory for the live forecast."""
        lab = self._lab_values(now, price, delta, oi, fund, liq, p2, p3, regime)
        for k, val in lab.items():
            self.baselines.record(f"lab:{k}", val, now)
        stored = {**self._metrics(delta, oi, fund, liq), **self._metrics2(p2)}
        row = {k: stored.get(k) for k in labdata.STORED}
        row.update(lab)
        try:
            self.lab.on_minute(now, row)
        except Exception:  # noqa: BLE001 - a broken model must not stop the dashboard
            log.exception("lab forecast failed")
            self.lab.live = None

    def _sig_lab(self, now: float) -> list:
        live = self.lab.live
        meta = self.lab.meta or {}
        out = []
        for target, type_, word in (("up", "lab_up", "рост"), ("down", "lab_down", "падение")):
            t = (live or {}).get("targets", {}).get(target)
            ok = bool(t) and now - live["ts"] < 120 and t["verdict"] == "ok" and bool(self.cfg.get("lab.alerts"))
            strength = t["strength"] if ok else None
            reasons = []
            if t:
                h, mv = live["horizon_min"], live["move_pct"]
                reasons.append(f"Вероятность: {word} на {mv:g}% за {h} мин — {t['p'] * 100:.0f}% "
                               f"(обычно {t['base'] * 100:.0f}%)")
                if t.get("thr"):
                    reasons.append(f"Порог сигнала: {t['thr'] * 100:.0f}% ({t['frac'] * 100:.0f}% самых уверенных "
                                   f"прогнозов на проверке)")
                if t.get("signal_hit") is not None:
                    reasons.append(f"На проверке после первого такого сигнала {word} случался в "
                                   f"{t['signal_hit'] * 100:.0f}% случаев")
                for r in t["reasons"][:4]:
                    val = "нет данных" if r["value"] is None else f"{r['value']:.3g}"
                    reasons.append(f"{r['ru']} = {val} ({'за' if r['effect'] > 0 else 'против'})")
                if meta.get("trained_at"):
                    reasons.append(f"Модель обучена на {meta.get('days', 0):.1f} дн. истории")
            out.append((type_, "all", strength, {"reasons": reasons, "title": f"Лаборатория: {word}",
                                                 "venue": "Модель"}))
        return out

    # ---- background --------------------------------------------------------
    async def run(self) -> None:
        """Journal outcomes every 5 s, baseline samples every minute, statistics hourly."""
        last_flush = last_prune = 0.0
        while True:
            await asyncio.sleep(STEP)
            now = time.time()
            try:
                await self.journal.on_price(now, self.price.last())
                if now - last_flush >= 60:
                    last_flush = now
                    await self.baselines.flush()
                    await self.baselines.refresh(now)
                if now - last_prune >= 86400:
                    last_prune = now
                    await self.baselines.prune(now, tuple(labdata.STORED), float(self.cfg.get("lab.keep_days")))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("analytics background step failed")

    def health(self, streams: dict, now: float) -> dict:
        return {"feeds": self.feed_status(streams), "baseline_days": round(self.baselines.history_days(now), 2),
                "metrics": len(self.baselines.cache) or len(self.baselines.session_cache),
                "borrow": {"key": self.borrow.key_state, "status": dict(self.borrow.status)},
                "index": {"status": dict(self.index.status), "ids": dict(self.index.ids)},
                "books": len(self.book.fresh(now)),
                "dex": {"status": dict(self.dex.status), "token": self.dex.token, "rpc": self.dex.rpc_live},
                "hl": {"status": dict(self.hl.status), "addresses": len(self.hl.registry),
                       "positions": len(self.hl.positions)},
                "lab": self.lab.health()}

