"""Simulated trades (DEMO=1): try the dashboard and the overlay without exchange access.

Every stream trades around one shared random-walk price. From time to time a
stream runs an "algorithm": equal-size orders on one side at a steady pace,
which is exactly what the detector and the tape highlighting look for.
"""
import asyncio
import math
import random
import time

from app.collectors.base import NotListed, Stream, TradesCallback
from app.models import Trade

TICK = 0.25


class _Market:
    """One random-walk USD price per coin, shared by all demo streams."""

    def __init__(self) -> None:
        self._price: dict[str, tuple[float, float]] = {}

    def price(self, coin: str, ts: float) -> float:
        """Walks forward in time only; a timestamp in the past (history) gets the current price."""
        p, last = self._price.get(coin, (0.0, ts))
        if not p:
            known = {"BTC": 65_000.0, "ETH": 3_200.0, "SOL": 150.0}
            p = known.get(coin) or 10 ** random.Random(coin).uniform(-5, 2)
        if ts > last or not self._price.get(coin):
            p *= math.exp(random.gauss(0, 0.0003 * math.sqrt(max(0.0, ts - last))))
            self._price[coin] = (p, max(ts, last))
        return p


MARKET = _Market()


def _poisson(rate: float, dt: float) -> int:
    n, t = 0, random.expovariate(rate)
    while t < dt:
        n += 1
        t += random.expovariate(rate)
    return n


class DemoStream(Stream):
    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback):
        super().__init__(venue, kind, coin, on_trades)
        self.transport = "ws"
        rnd = random.Random(f"{venue}:{kind}:{coin}")
        self.listed = rnd.random() > 0.12
        self.rate = rnd.choice([0.3, 0.6, 1.0, 2.0, 4.0]) * (1.6 if kind == "perp" else 1.0)
        self.median_usd = rnd.choice([80, 200, 500, 1200])
        self.premium = rnd.gauss(0, 3) / 1e4
        self.algo: dict | None = None
        self.algo_at = time.time() + rnd.uniform(15, 300)

    async def resolve(self) -> None:
        await asyncio.sleep(random.uniform(0.2, 1.5))
        if not self.listed:
            raise NotListed
        self.symbol = f"{self.coin}/USDT" + (":USDT" if self.kind == "perp" else "")

    def _trade(self, ts: float, usd: float, side: str) -> Trade:
        p = MARKET.price(self.coin, ts) * (1 + self.premium)
        return Trade(ts, p, usd / p, usd, side)

    def _background(self, t0: float, dt: float) -> list[Trade]:
        out = []
        for _ in range(_poisson(self.rate, dt)):
            usd = self.median_usd * math.exp(random.gauss(0, 1.3))
            out.append(self._trade(t0 + random.random() * dt, usd, random.choice(("buy", "sell"))))
        return out

    async def seed(self) -> None:
        now = time.time()
        self.emit_seed(self._background(now - 600, 600))

    def _algo_prints(self, now: float) -> list[Trade]:
        if self.algo is None:
            if now < self.algo_at:
                return []
            self.algo = {
                "side": random.choice(("buy", "sell")),
                "usd": random.choice([1500, 3000, 5000, 8000, 15000]),
                "every": random.uniform(0.8, 4.0),
                "next": now,
                "end": now + random.uniform(25, 80),
            }
        a = self.algo
        out = []
        while a["next"] <= now:
            out.append(self._trade(a["next"], a["usd"] * random.uniform(0.99, 1.01), a["side"]))
            a["next"] += a["every"] * random.uniform(0.85, 1.15)
        if now > a["end"]:
            self.algo = None
            self.algo_at = now + random.uniform(120, 600)
        return out

    async def stream(self) -> None:
        while True:
            await asyncio.sleep(TICK)
            now = time.time()
            self.emit(self._background(now - TICK, TICK) + self._algo_prints(now))
