"""On-chain DEX swaps (Uniswap, PancakeSwap, Raydium, ...) through the public GeckoTerminal API.

For a coin the most traded pool of the venue's DEX (any chain) is found by a pool
search; its swaps are polled. GeckoTerminal allows ~30 requests a minute in total,
so every DEX stream shares one rate limiter: a DEX tape lags by 10-30 s, which is
about one block-explorer refresh anyway. Each swap is its own print (no merging),
"buy" = the coin was bought out of the pool.
"""
import asyncio
import time
from datetime import datetime

import aiohttp

from app.collectors.base import NotListed, Stream, TradesCallback, get_json, poll_loop
from app.models import Trade

GT = "https://api.geckoterminal.com/api/v2"
SEARCH_TTL = 600.0
POLL_SEC = 15.0
REQS_PER_MIN = 25

# venue -> GeckoTerminal dex id prefixes (uniswap_v2, uniswap_v3, uniswap-v4-base, ...)
DEX_IDS: dict[str, tuple[str, ...]] = {
    "uniswap": ("uniswap",),
    "pancakeswap": ("pancakeswap",),
    "raydium": ("raydium",),
    "aerodrome": ("aerodrome",),
    "orca": ("orca",),
    "meteora": ("meteora",),
    "pumpswap": ("pumpswap", "pump-swap"),
}


class _Limiter:
    """At most `per_min` requests a minute across all DEX streams."""

    def __init__(self, per_min: int) -> None:
        self.gap = 60.0 / per_min
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.gap


LIMITER = _Limiter(REQS_PER_MIN)
_search: dict[str, tuple[float, dict]] = {}  # coin -> (ts, response): one search serves every DEX
_search_locks: dict[str, asyncio.Lock] = {}


async def gt_get(session: aiohttp.ClientSession, path: str, params: dict | None = None):
    await LIMITER.wait()
    return await get_json(session, GT + path, params)


async def search_pools(session: aiohttp.ClientSession, coin: str) -> dict:
    lock = _search_locks.setdefault(coin, asyncio.Lock())
    async with lock:
        cached = _search.get(coin)
        if cached and time.time() - cached[0] < SEARCH_TTL:
            return cached[1]
        data = await gt_get(session, "/search/pools", {"query": coin, "include": "base_token,dex"})
        _search[coin] = (time.time(), data)
        return data


def pick_pool(data: dict, coin: str, prefixes: tuple[str, ...]) -> dict | None:
    """Most traded pool of this DEX whose base token is the coin: {"network", "address", "name"}."""
    symbols = {
        t.get("id"): str((t.get("attributes") or {}).get("symbol") or "").upper()
        for t in data.get("included") or []
        if t.get("type") == "token"
    }
    best, best_vol = None, -1.0
    for pool in data.get("data") or []:
        attrs = pool.get("attributes") or {}
        rel = pool.get("relationships") or {}
        dex = str(((rel.get("dex") or {}).get("data") or {}).get("id") or "")
        if not dex.startswith(prefixes):
            continue
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id")
        base = symbols.get(base_id) or str(attrs.get("name") or "").split(" / ")[0].strip().upper()
        if base != coin:
            continue
        try:
            vol = float((attrs.get("volume_usd") or {}).get("h24") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        network = ((rel.get("network") or {}).get("data") or {}).get("id") or str(pool.get("id", "")).split("_")[0]
        address = attrs.get("address")
        if address and network and vol > best_vol:
            best, best_vol = {"network": network, "address": address, "name": attrs.get("name") or ""}, vol
    return best


def parse_swaps(data: dict) -> list[tuple[object, Trade]]:
    out = []
    for item in data.get("data") or []:
        a = item.get("attributes") or {}
        kind = a.get("kind")
        try:
            usd = float(a.get("volume_in_usd") or 0)
            if kind == "buy":  # the coin left the pool
                amount, price = float(a.get("to_token_amount") or 0), float(a.get("price_to_in_usd") or 0)
            elif kind == "sell":
                amount, price = float(a.get("from_token_amount") or 0), float(a.get("price_from_in_usd") or 0)
            else:
                continue
            ts = datetime.fromisoformat(str(a.get("block_timestamp")).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        if usd > 0 and amount > 0 and price > 0:
            out.append((item.get("id") or a.get("tx_hash"), Trade(ts, price, amount, usd, kind)))
    return out


class DexStream(Stream):
    def __init__(self, venue: str, kind: str, coin: str, on_trades: TradesCallback,
                 session: aiohttp.ClientSession, dex: str = ""):
        super().__init__(venue, kind, coin, on_trades)
        self.session = session
        self.transport = "rest"
        self.merge_fills = False  # one swap = one print
        self.prefixes = DEX_IDS[dex]
        self.pool: dict = {}

    async def resolve(self) -> None:
        if self.kind != "spot":
            raise NotListed
        pool = pick_pool(await search_pools(self.session, self.coin), self.coin, self.prefixes)
        if not pool:
            raise NotListed
        self.pool = pool
        self.symbol = f"{pool['name']} ({pool['network']})"

    async def fetch_trades(self) -> list[tuple[object, Trade]]:
        p = self.pool
        return parse_swaps(await gt_get(self.session, f"/networks/{p['network']}/pools/{p['address']}/trades"))

    async def stream(self) -> None:
        await poll_loop(self.fetch_trades, self.emit, interval=POLL_SEC, emit_first=self._seed_poll, seen=self.poll_seen)
