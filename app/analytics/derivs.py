"""Open interest only: one poller per enabled perpetual stream, no history downloads."""
import asyncio
import logging
import math
import random
import time

from app.fx import usd_rate

log = logging.getLogger(__name__)
OI_IN_CONTRACTS = {"okx", "bitmex", "delta"}
OI_FROM_VALUE = {"xt", "btse", "bingx", "backpack", "hitbtc"}


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


class CcxtDerivFeed:
    def __init__(self, stream, sink):
        self.stream, self.sink = stream, sink
        self.key = stream.key
        self.status = "ожидание"
        self.tasks = []

    def start(self):
        self.tasks = [asyncio.create_task(self._run(), name=f"{self.key}:oi")]

    def stop(self):
        for task in self.tasks:
            task.cancel()
        self.tasks = []

    async def _run(self):
        backoff = 5.0
        while True:
            ex = self.stream.ex
            if ex is None or self.stream.status not in ("live", "polling"):
                await asyncio.sleep(2)
                continue
            if not ex.has.get("fetchOpenInterest"):
                self.status = "нет публичного ОИ"
                return
            try:
                started = time.monotonic()
                raw = await asyncio.wait_for(ex.fetch_open_interest(self.stream.symbol), 20)
                price = self.sink.last_price(self.key)
                quote_price = price * self.stream.mult / (usd_rate(self.stream.quote) or 1) if price else None
                coins = oi_coins(raw, self.stream.market, self.stream.ex_id, quote_price, self.stream.mult)
                if coins is not None:
                    self.sink.oi(self.key, time.time(), coins, coins * price if price else None)
                    self.status = "ok"
                else:
                    self.status = "биржа не дала размер ОИ"
                backoff = 5.0
                poll = 5 if self.stream.ex_id == "binanceusdm" else 10
                await asyncio.sleep(max(1, poll - (time.monotonic() - started)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status = f"ошибка: {type(exc).__name__}: {exc}"[:160]
                log.info("%s OI: %s", self.key, self.status)
                await asyncio.sleep(backoff)
                backoff = min(300, backoff * 2)


class DemoDerivFeed(CcxtDerivFeed):
    async def _run(self):
        coins = None
        while True:
            await asyncio.sleep(5)
            price = self.sink.last_price(self.key)
            if price:
                coins = (coins or 1e6 / price) * (1 + random.gauss(0, 0.001))
                self.sink.oi(self.key, time.time(), coins, coins * price)
                self.status = "ok"
