"""М9: Hyperliquid - addresses, positions, liquidation map, TWAP.

Every Hyperliquid print carries both addresses (users = [buyer, seller]); the aggressor is
taken in the trade feed (app/collectors/ccxt_stream.py). Here:

  registry     aggressive volume per address over 1 h / 24 h, and how regular its trades are
  positions    POST https://api.hyperliquid.xyz/info {"type": "clearinghouseState", "user": ...}
               (weight 2) for the top addresses of the coin and the watchlist, every
               `hyperliquid.positions_poll_sec`: side, size, entry, liquidation price, leverage
  TWAP         {"type": "twapHistory", "user": ...}: orders whose latest status is "activated"
  liq map      position USD by liquidation price, 0.5 % buckets, longs below and shorts above

Everything stays under the IP budget of 1200 weight a minute (a shared token bucket); no
per-user WebSocket subscriptions are used, so the 10-address limit does not apply.
The field layout of twapHistory was checked against third-party references only (the
official docs were not reachable from the build machine).
"""
import asyncio
import logging
import random
import time
from collections import defaultdict, deque

import aiohttp

log = logging.getLogger(__name__)

INFO = "https://api.hyperliquid.xyz/info"
EXPLORER = "https://app.hyperliquid.xyz/explorer/address/"
TIMEOUT = aiohttp.ClientTimeout(total=15)
WEIGHT_PER_MIN = 1200
BUDGET_SHARE = 0.5  # use at most half of the IP budget: the trade feed shares the same IP
W_STATE, W_TWAP = 2, 20
REG_KEEP = 86400
POS_KEEP = 2 * 3600
EVENTS = 300


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


class WeightLimiter:
    def __init__(self, per_min: float) -> None:
        self.rate = per_min / 60.0
        self.tokens = per_min
        self.cap = per_min
        self.at = time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self, w: float) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.at) * self.rate)
                self.at = now
                if self.tokens >= w:
                    self.tokens -= w
                    return
                await asyncio.sleep((w - self.tokens) / self.rate)


LIMITER = WeightLimiter(WEIGHT_PER_MIN * BUDGET_SHARE)


def parse_position(state: dict, hl_coin: str, mult: float) -> dict | None:
    """The coin's position from clearinghouseState (sizes and prices per ONE coin)."""
    for ap in state.get("assetPositions") or []:
        p = ap.get("position") or {}
        if str(p.get("coin")) != hl_coin:
            continue
        szi = _f(p.get("szi"))
        if not szi:
            return None
        lev = p.get("leverage") or {}
        liq = _f(p.get("liquidationPx"))
        entry = _f(p.get("entryPx"))
        return {
            "side": "long" if szi > 0 else "short",
            "coins": abs(szi) * mult,
            "usd": abs(_f(p.get("positionValue")) or 0.0),
            "entry": entry / mult if entry else None,
            "liq": liq / mult if liq else None,
            "lev": _f(lev.get("value")),
            "lev_type": lev.get("type"),
            "upnl": _f(p.get("unrealizedPnl")),
        }
    return None


def active_twaps(history: list, hl_coin: str) -> list[dict]:
    """TWAP orders of the coin whose latest status is "activated"."""
    latest: dict = {}
    for h in history or []:
        st = h.get("state") or {}
        if str(st.get("coin")) != hl_coin:
            continue
        key = h.get("twapId") or (st.get("timestamp"), st.get("side"), st.get("sz"))
        if key not in latest or (h.get("time") or 0) >= (latest[key].get("time") or 0):
            latest[key] = h
    out = []
    for h in latest.values():
        status = (h.get("status") or {}).get("status") if isinstance(h.get("status"), dict) else h.get("status")
        if status != "activated":
            continue
        st = h["state"]
        sz, done = _f(st.get("sz")) or 0.0, _f(st.get("executedSz")) or 0.0
        start = (_f(st.get("timestamp")) or 0) / 1000
        minutes = _f(st.get("minutes")) or 0.0
        out.append({"side": "buy" if st.get("side") == "B" else "sell", "sz": sz, "done": done,
                    "progress": done / sz if sz else None, "notional_done": _f(st.get("executedNtl")),
                    "minutes": minutes, "start": start, "randomize": bool(st.get("randomize")),
                    "reduce_only": bool(st.get("reduceOnly"))})
    return out


def liq_map(positions: dict[str, dict], price: float, bucket_pct: float, max_pct: float = 20.0) -> dict:
    """Position USD per liquidation-price bucket: longs below the price, shorts above."""
    longs: dict[int, float] = defaultdict(float)
    shorts: dict[int, float] = defaultdict(float)
    for p in positions.values():
        if not p.get("liq") or not price:
            continue
        d = (p["liq"] / price - 1) * 100
        if abs(d) > max_pct:
            continue
        i = int(abs(d) // bucket_pct)
        if p["side"] == "long" and d < 0:
            longs[i] += p["usd"]
        elif p["side"] == "short" and d > 0:
            shorts[i] += p["usd"]
    n = int(max_pct / bucket_pct)
    return {"bucket_pct": bucket_pct, "longs": [round(longs.get(i, 0.0)) for i in range(n)],
            "shorts": [round(shorts.get(i, 0.0)) for i in range(n)]}


class HyperTracker:
    def __init__(self, cfg, session: aiohttp.ClientSession | None, demo: bool = False) -> None:
        self.cfg = cfg
        self.session = session
        self.demo = demo
        self.reset("")

    def reset(self, coin: str) -> None:
        self.coin = coin
        self.hl_coin = ""  # "kPEPE" for 1000x markets
        self.mult = 1.0
        self.registry: dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))  # addr -> (ts, side, usd)
        self.positions: dict[str, dict] = {}
        self.pos_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=400))  # addr -> (ts, signed usd)
        self.twaps: dict[str, list] = {}
        self.events: deque = deque(maxlen=EVENTS)
        self.status: dict[str, str] = {}
        self._twap_at = 0.0
        self._demo_addrs: list[str] = []
        self._whales = None

    def set_market(self, market: dict | None, mult: float) -> None:
        if not market or self.hl_coin:
            return
        info = market.get("info") or {}
        self.hl_coin = str(info.get("name") or market.get("baseName") or market.get("base") or "")
        self.mult = mult or 1.0

    # ---- registry -------------------------------------------------------------------
    def on_trades(self, trades: list) -> None:
        for t in trades:
            addr = t.taker
            if not addr and self.demo and t.usd > 0:
                if not self._demo_addrs:
                    rnd = random.Random(self.coin)
                    self._demo_addrs = ["0x" + "".join(rnd.choice("0123456789abcdef") for _ in range(40))
                                        for _ in range(40)]
                addr = random.choices(self._demo_addrs, weights=[1 / (i + 1) for i in range(40)])[0]
            if addr and t.side in ("buy", "sell"):
                self.registry[addr].append((t.ts, t.side, t.usd))

    def volume(self, addr: str, now: float, sec: float) -> tuple[float, float]:
        buy = sell = 0.0
        for ts, side, usd in reversed(self.registry.get(addr, ())):
            if ts < now - sec:
                break
            if side == "buy":
                buy += usd
            else:
                sell += usd
        return buy, sell

    def regularity(self, addr: str, now: float) -> dict | None:
        """Steady intervals between an address's aggressive trades = an algorithm at work."""
        ts = [t for t, _, _ in self.registry.get(addr, ()) if t >= now - 1800]
        if len(ts) < 8:
            return None
        gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a > 0.2]
        if len(gaps) < 6:
            return None
        mean = sum(gaps) / len(gaps)
        cv = (sum((g - mean) ** 2 for g in gaps) / len(gaps)) ** 0.5 / mean if mean > 0 else None
        return {"step": mean, "cv": cv, "regular": cv is not None and cv < 0.4}

    def top_addresses(self, now: float) -> list[str]:
        n = int(self.cfg.get("hyperliquid.top_addresses_per_coin"))
        for addr in [a for a, dq in self.registry.items() if not dq or dq[-1][0] < now - REG_KEEP]:
            del self.registry[addr]
        ranked = sorted(self.registry, key=lambda a: -sum(self.volume(a, now, REG_KEEP)))
        watch = [str(a).lower() for a in (self.cfg.get("hyperliquid.watchlist") or {})]
        return list(dict.fromkeys(watch + ranked[:n]))

    # ---- polling ------------------------------------------------------------------------
    async def _info(self, body: dict, weight: float):
        await LIMITER.take(weight)
        async with self.session.post(INFO, json=body, timeout=TIMEOUT) as r:
            if r.status == 429:
                raise RuntimeError("Hyperliquid: лимит запросов (429)")
            return await r.json(content_type=None)

    def _update(self, addr: str, pos: dict | None, now: float) -> None:
        old = self.positions.get(addr)
        signed = 0.0 if not pos else (pos["usd"] if pos["side"] == "long" else -pos["usd"])
        self.pos_hist[addr].append((now, signed))
        while self.pos_hist[addr] and self.pos_hist[addr][0][0] < now - POS_KEEP:
            self.pos_hist[addr].popleft()
        if pos:
            self.positions[addr] = {**pos, "ts": now}
        else:
            self.positions.pop(addr, None)
        before = 0.0 if not old else (old["usd"] if old["side"] == "long" else -old["usd"])
        delta = signed - before
        if old is None and pos:
            kind = "открыл"
        elif old and not pos:
            kind = "закрыл"
        elif pos and old and abs(delta) >= 0.05 * max(abs(before), 1):
            kind = "увеличил" if abs(signed) > abs(before) else "сократил"
        else:
            return
        self.events.appendleft({"ts": now, "addr": addr, "kind": kind, "side": (pos or old)["side"],
                                "usd": abs(signed) if pos else abs(before), "delta": delta})

    async def poll(self, coin: str) -> None:
        now = time.time()
        addrs = self.top_addresses(now)
        if not self.hl_coin or not addrs:
            self.status["positions"] = "ждём сделки Hyperliquid по монете" if not self.registry else "нет рынка"
            return
        for addr in addrs:
            if self.coin != coin:
                return
            try:
                st = await self._info({"type": "clearinghouseState", "user": addr}, W_STATE)
            except Exception as e:  # noqa: BLE001
                self.status["positions"] = f"ошибка: {type(e).__name__}: {e}"[:160]
                continue
            self._update(addr, parse_position(st or {}, self.hl_coin, self.mult), time.time())
        self.status["positions"] = f"ok: {len(addrs)} адресов"
        if time.time() - self._twap_at > 120:
            self._twap_at = time.time()
            watch = [str(a).lower() for a in (self.cfg.get("hyperliquid.watchlist") or {})]
            for addr in list(dict.fromkeys(watch + [a for a in addrs if a in self.positions]))[:10]:
                try:
                    hist = await self._info({"type": "twapHistory", "user": addr}, W_TWAP)
                except Exception as e:  # noqa: BLE001
                    self.status["twap"] = f"ошибка: {type(e).__name__}: {e}"[:160]
                    continue
                act = active_twaps(hist if isinstance(hist, list) else [], self.hl_coin)
                was = {(t["side"], t["start"]) for t in self.twaps.get(addr, [])}
                for tw in act:
                    if (tw["side"], tw["start"]) not in was:
                        self.events.appendleft({"ts": time.time(), "addr": addr, "kind": "запустил TWAP",
                                                "side": "long" if tw["side"] == "buy" else "short",
                                                "usd": None, "delta": None, "twap": tw})
                self.twaps[addr] = act
                self.status["twap"] = "ok"

    def _demo_poll(self) -> None:
        from app.collectors.demo import MARKET

        now = time.time()
        rnd = random.Random()
        price = MARKET.price(self.coin, now)
        for i, addr in enumerate(self.top_addresses(now)[:25]):
            old = self.positions.get(addr)
            if old is None and rnd.random() < 0.7:
                side = rnd.choice(("long", "short"))
                lev = rnd.choice((3, 5, 10, 20, 40))
                usd = rnd.lognormvariate(11, 1.1)
                liq = price * (1 - 0.9 / lev) if side == "long" else price * (1 + 0.9 / lev)
                pos = {"side": side, "coins": usd / price, "usd": usd, "entry": price, "liq": liq, "lev": lev,
                       "lev_type": "cross", "upnl": 0.0}
            elif old:
                pos = dict(old)
                pos["usd"] *= rnd.choice((1.0, 1.0, 1.0, 1.25, 0.8))
                if rnd.random() < 0.03:
                    pos = None
            else:
                continue
            self._update(addr, pos, now)
            if pos and i < 2 and addr not in self.twaps:
                self.twaps[addr] = [{"side": "buy" if pos["side"] == "long" else "sell", "sz": pos["coins"],
                                     "done": pos["coins"] * 0.35, "progress": 0.35, "notional_done": pos["usd"] * 0.35,
                                     "minutes": 120, "start": now - 40 * 60, "randomize": True, "reduce_only": False}]
        self.status = {"positions": "ok (демо)", "twap": "ok (демо)"}

    async def run(self) -> None:
        while True:
            coin = self.coin
            if coin and self.cfg.on("hyperliquid"):
                try:
                    if self.demo:
                        self._demo_poll()
                    else:
                        await self.poll(coin)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.status["error"] = f"{type(e).__name__}: {e}"[:160]
                    log.info("hyperliquid %s: %s", coin, e)
            until = time.time() + max(10.0, float(self.cfg.get("hyperliquid.positions_poll_sec")))
            while time.time() < until and self.coin == coin:
                await asyncio.sleep(1)

    # ---- view -----------------------------------------------------------------------------
    def change(self, addr: str, now: float, sec: float) -> float | None:
        dq = self.pos_hist.get(addr)
        if not dq:
            return None
        then = None
        for ts, v in dq:
            if ts <= now - sec:
                then = v
            else:
                break
        if then is None:
            then = dq[0][1] if dq[0][0] <= now - sec / 2 else None
        return dq[-1][1] - then if then is not None else None

    def label(self, addr: str) -> str | None:
        wl = {str(k).lower(): v for k, v in (self.cfg.get("hyperliquid.watchlist") or {}).items()}
        return wl.get(addr)

    def whales(self, now: float, price: float | None) -> list[dict]:
        """The positions table; rebuilt at most every 5 s (positions change once per poll)."""
        cached = getattr(self, "_whales", None)
        if cached and now - cached[0] < 5 and cached[1] == len(self.positions):
            return cached[2]
        rows = []
        for addr, p in self.positions.items():
            buy, sell = self.volume(addr, now, 3600)
            rows.append({
                "addr": addr, "label": self.label(addr), "url": EXPLORER + addr, **p,
                "dist_pct": (p["liq"] / price - 1) * 100 if p.get("liq") and price else None,
                "chg_15m": self.change(addr, now, 900), "chg_1h": self.change(addr, now, 3600),
                "vol_1h": buy + sell, "buy_1h": buy, "sell_1h": sell, "algo": self.regularity(addr, now),
                "twaps": self.twaps.get(addr) or [],
            })
        rows.sort(key=lambda r: -r["usd"])
        self._whales = (now, len(self.positions), rows)
        return rows
