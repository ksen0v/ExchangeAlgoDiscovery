"""М2 / М4 data: open interest, funding with mark/index, liquidations of every perp stream.

Polled through the stream's own ccxt instance (its rate limiter is shared with the
trade feed). Every venue whose ccxt class can give the data takes part; the others
show "нет данных" on the health screen. Units differ between venues, see oi_coins().
"""
import asyncio
import logging
import math
import random
import time
from collections.abc import Callable

from app.fx import usd_rate

log = logging.getLogger(__name__)

# ccxt's openInterestAmount is in contracts on these (multiply by the contract size) ...
OI_IN_CONTRACTS = {"okx", "bitmex", "delta"}
# ... and on these the USD value divided by the price is the reliable figure
OI_FROM_VALUE = {"xt", "btse", "bingx", "backpack", "hitbtc"}
# liquidation "side" = side of the liquidated POSITION (buy = long); elsewhere = side of the
# liquidation ORDER (sell = a long was closed)
LIQ_SIDE_IS_POSITION = {"bybit"}
BINANCE_FUTURES = {"binanceusdm"}


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def oi_coins(oi: dict, market: dict, ex_id: str, price: float | None, mult: float = 1.0) -> float | None:
    """Open interest in coins of the base asset (1000PEPE contracts -> PEPE).

    price: price of one contract's base unit in the quote currency (not divided by mult).
    """
    amount = _num(oi.get("openInterestAmount"))
    value = _num(oi.get("openInterestValue"))
    cs = _num(market.get("contractSize")) or 1.0
    coins = None
    if ex_id in OI_FROM_VALUE and value and price:
        coins = value / price
    elif ex_id in OI_IN_CONTRACTS and amount is not None:
        coins = amount * cs
    elif amount is not None and not market.get("inverse"):
        coins = amount
    elif value and price:
        coins = value / price
    if coins is None or coins < 0:
        return None
    return coins * mult


def interval_hours(raw) -> float | None:
    """'8h' / '4h' / '1h' / 8 -> hours."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    try:
        if s.endswith("h"):
            return float(s[:-1])
        if s.endswith("m"):
            return float(s[:-1]) / 60
        v = float(s)
        return v / 3600 if v > 1000 else v  # seconds or hours
    except ValueError:
        return None


def f8(rate: float, hours: float) -> float:
    """Funding brought to 8 hours: rate * 8 / interval."""
    return rate * 8 / hours


def annual_pct(rate: float, hours: float) -> float:
    return rate * (24 / hours) * 365 * 100


def basis_pct(perp: float, spot: float) -> float:
    return (perp - spot) / spot * 100


def premium_pct(mark: float, index: float) -> float:
    return (mark - index) / index * 100


def liq_side(raw_side: str | None, ex_id: str) -> str | None:
    """'long' when a long position was liquidated, 'short' otherwise."""
    s = (raw_side or "").lower()
    if s not in ("buy", "sell"):
        return None
    if ex_id in LIQ_SIDE_IS_POSITION:
        return "long" if s == "buy" else "short"
    return "long" if s == "sell" else "short"


def liq_usd(liq: dict, market: dict, fx: float, mult: float = 1.0) -> tuple[float | None, float | None]:
    """(USD size, USD price of one coin) of a ccxt liquidation."""
    price = _num(liq.get("price"))
    quote = _num(liq.get("quoteValue"))
    base = _num(liq.get("baseValue"))
    contracts = _num(liq.get("contracts"))
    cs = _num(liq.get("contractSize")) or _num(market.get("contractSize")) or 1.0
    if quote is None and price:
        if base is not None:
            quote = base * price
        elif contracts is not None:
            quote = contracts * cs * price
    if not quote or quote <= 0:
        return None, None
    return quote * fx, (price * fx / mult if price else None)


Sink = Callable[..., None]


class CcxtDerivFeed:
    """Pollers of one perp CcxtStream. Sink methods: oi(key, ts, coins, usd), funding(key, dict), liq(key, dict)."""

    def __init__(self, stream, cfg, sink) -> None:
        self.stream = stream
        self.cfg = cfg
        self.sink = sink
        self.key = stream.key
        self.ex_id = stream.ex_id
        self.tasks: list[asyncio.Task] = []
        self.status = {"oi": "ожидание", "funding": "ожидание", "liq": "ожидание"}
        self._interval_h: float | None = None
        self._interval_at = 0.0
        self._liq_seen: dict = {}

    def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self._loop("oi", self._poll_oi), name=f"{self.key}:oi"),
            asyncio.create_task(self._loop("funding", self._poll_funding), name=f"{self.key}:funding"),
            asyncio.create_task(self._loop("liq", self._watch_liqs), name=f"{self.key}:liq"),
        ]

    def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        self.tasks = []

    async def _ex(self):
        while self.stream.ex is None:  # the trade watchdog is re-creating the instance
            await asyncio.sleep(1)
        return self.stream.ex

    def _has(self, ex, method: str) -> bool:
        return bool(ex.has.get(method))

    async def _loop(self, name: str, body) -> None:
        backoff = 5.0
        while True:
            try:
                done = await body()
                if done:  # not supported by this venue
                    return
                backoff = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.status[name] = f"ошибка: {type(e).__name__}: {e}"[:160]
                log.info("%s %s error: %s", self.key, name, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)

    def _enabled(self, module: str) -> bool:
        return self.cfg.on(module)

    async def _poll_oi(self) -> bool:
        ex = await self._ex()
        if not self._has(ex, "fetchOpenInterest"):
            self.status["oi"] = "нет данных у биржи"
            return True
        poll = self.cfg.get("open_interest.binance_poll_sec" if self.ex_id in BINANCE_FUTURES
                            else "open_interest.poll_sec")
        while True:
            started = time.monotonic()
            if self._enabled("open_interest") or self._enabled("regime"):
                ex = await self._ex()
                oi = await asyncio.wait_for(ex.fetch_open_interest(self.stream.symbol), 20)
                coins = oi_coins(oi, self.stream.market, self.ex_id, self.stream_price_quote(), self.stream.mult)
                if coins is not None:
                    usd_price = self.sink.last_price(self.key)  # USD per coin
                    self.sink.oi(self.key, time.time(), coins, coins * usd_price if usd_price else None)
                    self.status["oi"] = "ok"
                else:
                    self.status["oi"] = "биржа не дала размер ОИ"
            await asyncio.sleep(max(1.0, float(poll) - (time.monotonic() - started)))

    def stream_price_quote(self) -> float | None:
        """Last trade price in the market's own quote units (1000x contracts not removed)."""
        p = self.sink.last_price(self.key)
        fx = usd_rate(self.stream.quote) or 1.0
        return p * self.stream.mult / fx if p else None

    async def _interval(self, ex, fr: dict) -> tuple[float, bool]:
        """(hours, assumed): from the funding answer, else fetchFundingInterval hourly, else 8 h."""
        h = interval_hours(fr.get("interval"))
        if h:
            return h, False
        checked = False
        if self._has(ex, "fetchFundingInterval") and time.time() - self._interval_at > 3600:
            self._interval_at = time.time()
            try:
                info = await asyncio.wait_for(ex.fetch_funding_interval(self.stream.symbol), 20)
                self._interval_h = interval_hours((info or {}).get("interval"))
                checked = True
            except Exception as e:  # noqa: BLE001
                log.info("%s funding interval: %s", self.key, e)
        if self._interval_h:
            return self._interval_h, False
        if self.ex_id == "hyperliquid":
            return 1.0, False
        # Binance /fapi/v1/fundingInfo lists only the symbols whose interval is not 8 h
        if self.ex_id in BINANCE_FUTURES and (checked or self._interval_at):
            return 8.0, False
        return 8.0, True

    async def _poll_funding(self) -> bool:
        ex = await self._ex()
        single = self._has(ex, "fetchFundingRate")
        if not single and not self._has(ex, "fetchFundingRates"):
            self.status["funding"] = "нет данных у биржи"
            return True
        while True:
            started = time.monotonic()
            if self._enabled("funding") or self._enabled("regime"):
                ex = await self._ex()
                sym = self.stream.symbol
                if single:
                    fr = await asyncio.wait_for(ex.fetch_funding_rate(sym), 20)
                else:
                    fr = (await asyncio.wait_for(ex.fetch_funding_rates([sym]), 30)).get(sym) or {}
                rate = _num(fr.get("fundingRate"))
                fx = usd_rate(self.stream.quote) or 1.0
                mult = self.stream.mult
                mark, index = _num(fr.get("markPrice")), _num(fr.get("indexPrice"))
                hours, assumed = await self._interval(ex, fr)
                self.sink.funding(self.key, {
                    "ts": time.time(),
                    "rate": rate,
                    "interval_h": hours,
                    "interval_assumed": assumed,
                    "mark": mark * fx / mult if mark else None,
                    "index": index * fx / mult if index else None,
                    "next_ts": (_num(fr.get("fundingTimestamp")) or _num(fr.get("nextFundingTimestamp")) or 0) / 1000
                    or None,
                })
                self.status["funding"] = "ok" if rate is not None else "биржа не дала ставку"
            await asyncio.sleep(max(5.0, float(self.cfg.get("funding.poll_sec")) - (time.monotonic() - started)))

    async def _watch_liqs(self) -> bool:
        ex = await self._ex()
        if not self._has(ex, "watchLiquidations"):
            self.status["liq"] = "нет потока у биржи"
            return True
        self.status["liq"] = "ok"
        while True:
            if not self._enabled("liquidations"):
                await asyncio.sleep(2)
                continue
            ex = await self._ex()
            try:
                rows = await asyncio.wait_for(ex.watch_liquidations(self.stream.symbol), 300)
            except asyncio.TimeoutError:
                continue  # no liquidations for 5 minutes is normal for an altcoin
            fx = usd_rate(self.stream.quote) or 1.0
            for r in rows or []:
                k = (r.get("timestamp"), r.get("price"), r.get("contracts"), r.get("side"))
                if k in self._liq_seen:
                    continue
                self._liq_seen[k] = None
                side = liq_side(r.get("side") or (r.get("info") or {}).get("S"), self.ex_id)
                usd, price = liq_usd(r, self.stream.market, fx, self.stream.mult)
                if side and usd:
                    ts = (_num(r.get("timestamp")) or time.time() * 1000) / 1000
                    self.sink.liq(self.key, {"ts": ts, "side": side, "usd": usd, "price": price})
            while len(self._liq_seen) > 2000:
                del self._liq_seen[next(iter(self._liq_seen))]


class DemoDerivFeed:
    """DEMO=1: simulated OI, funding and liquidations that follow the demo price and flows."""

    def __init__(self, stream, cfg, sink) -> None:
        self.stream = stream
        self.cfg = cfg
        self.sink = sink
        self.key = stream.key
        self.tasks: list[asyncio.Task] = []
        rnd = random.Random(self.key)
        self.has_oi = rnd.random() < 0.8
        self.has_liq = rnd.random() < 0.5
        self.interval = rnd.choice([8.0, 8.0, 8.0, 4.0, 1.0])
        self.base_rate = rnd.gauss(0.0001, 0.00008)
        self.status = {"oi": "ok" if self.has_oi else "нет данных у биржи", "funding": "ok",
                       "liq": "ok" if self.has_liq else "нет потока у биржи"}

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self._run(), name=f"{self.key}:demo-derivs")]

    def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        self.tasks = []

    async def _run(self) -> None:
        coins = None
        last_funding = 0.0
        while True:
            await asyncio.sleep(2)
            price = self.sink.last_price(self.key)
            if not price:
                continue
            now = time.time()
            if self.has_oi:
                if coins is None:
                    coins = random.uniform(2e5, 2e6) / price
                flow = self.sink.flow(self.key, 10)  # recent perp delta drives the OI change
                # recent perp flow opens (mostly) or closes positions: a fraction of it reaches the OI
                coins *= 1 + random.gauss(0, 0.0002) + 0.02 * (abs(flow) / max(price * coins, 1)) * random.choice((1, 1, -1))
                self.sink.oi(self.key, now, coins, coins * price)
            if now - last_funding > 10:
                last_funding = now
                rate = self.base_rate + random.gauss(0, 0.00002)
                mark = price * (1 + random.gauss(0.0002, 0.0003))
                self.sink.funding(self.key, {"ts": now, "rate": rate, "interval_h": self.interval,
                                             "interval_assumed": False, "mark": mark, "index": price,
                                             "next_ts": (now // 3600 + 1) * 3600})
            if self.has_liq and random.random() < 0.08:
                side = random.choice(("long", "short"))
                self.sink.liq(self.key, {"ts": now, "side": side, "usd": random.lognormvariate(7.5, 1.2),
                                         "price": price})
