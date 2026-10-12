"""М8: the coin on DEXes - pools, price, depth; the data side of "who leads, DEX or CEX".

Level 1 (no keys): DexScreener (pools, liquidity, price every `dex.price_poll_sec`) and the
aggregators' quote APIs for depth (Solana: Jupiter lite API; EVM: ParaSwap prices API):
a ladder of trade sizes is quoted each way and the size that moves the price 1 / 2 / 5 %
is interpolated.
Level 2 (`dex.rpc` set for the chain, EVM only): Swap logs of the pools over eth_subscribe
(every swap with its own price, seconds after the block) and the exact Uniswap v3 depth by
walking the initialised ticks. Solana level 2 needs a Geyser / logsSubscribe decoder per
program and is not implemented: Solana pools run on level 1 plus the GeckoTerminal swap tape.
"""
import asyncio
import json
import logging
import math
import random
import time
from collections import deque

import aiohttp

from app.analytics import uniswap as uni

log = logging.getLogger(__name__)

DS = "https://api.dexscreener.com"
JUP = "https://lite-api.jup.ag/swap/v1/quote"
PARASWAP = "https://api.paraswap.io/prices"
TIMEOUT = aiohttp.ClientTimeout(total=15)
LADDER_USD = [500, 2000, 5000, 15000, 50000, 150000, 500000, 1500000]
REF_USD = 50
DEPTH_PCTS = (1.0, 2.0, 5.0)

# DexScreener chain id -> GeckoTerminal network, ParaSwap network, explorer, USD stablecoin (address, decimals)
CHAINS = {
    "ethereum": {"gt": "eth", "paraswap": 1, "tx": "https://etherscan.io/tx/",
                 "usd": ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6)},
    "bsc": {"gt": "bsc", "paraswap": 56, "tx": "https://bscscan.com/tx/",
            "usd": ("0x55d398326f99059ff775485246999027b3197955", 18)},
    "base": {"gt": "base", "paraswap": 8453, "tx": "https://basescan.org/tx/",
             "usd": ("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6)},
    "arbitrum": {"gt": "arbitrum", "paraswap": 42161, "tx": "https://arbiscan.io/tx/",
                 "usd": ("0xaf88d065e77c8cc2239327c5edb3a432268e5831", 6)},
    "polygon": {"gt": "polygon_pos", "paraswap": 137, "tx": "https://polygonscan.com/tx/",
                "usd": ("0x3c499c542cef5e3811e1192ce70d8cc03d5c3359", 6)},
    "optimism": {"gt": "optimism", "paraswap": 10, "tx": "https://optimistic.etherscan.io/tx/",
                 "usd": ("0x0b2c639c533813f4aa9d7837caf62653d097ff85", 6)},
    "avalanche": {"gt": "avax", "paraswap": 43114, "tx": "https://snowtrace.io/tx/",
                  "usd": ("0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e", 6)},
    "solana": {"gt": "solana", "jupiter": True, "tx": "https://solscan.io/tx/",
               "usd": ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6)},
}
GT_TO_CHAIN = {v["gt"]: k for k, v in CHAINS.items()}


def explorer_tx(network_or_chain: str, tx: str) -> str | None:
    chain = network_or_chain if network_or_chain in CHAINS else GT_TO_CHAIN.get(network_or_chain)
    return CHAINS[chain]["tx"] + tx if chain and tx else None


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def pick_token(pairs: list[dict], coin: str, min_liq: float = 5000.0) -> dict | None:
    """The coin's token = the (chain, address) with the most pool liquidity among pairs whose base is the coin."""
    tot: dict[tuple[str, str], float] = {}
    for p in pairs or []:
        base = p.get("baseToken") or {}
        if str(base.get("symbol", "")).upper() != coin:
            continue
        k = (str(p.get("chainId")), str(base.get("address", "")).lower() if p.get("chainId") != "solana"
             else str(base.get("address", "")))
        tot[k] = tot.get(k, 0.0) + (_f((p.get("liquidity") or {}).get("usd")) or 0.0)
    if not tot:
        return None
    (chain, addr), liq = max(tot.items(), key=lambda kv: kv[1])
    return {"chain": chain, "address": addr, "symbol": coin, "source": "auto", "liquidity": liq} if liq >= min_liq else None


def coin_price(pair: dict, address: str) -> float | None:
    """USD price of the coin in a pair where it may be the base or the quote."""
    usd, native = _f(pair.get("priceUsd")), _f(pair.get("priceNative"))
    base = str((pair.get("baseToken") or {}).get("address", ""))
    if base.lower() == address.lower():
        return usd
    if usd and native:  # the coin is the quote: price of quote = priceUsd / priceNative
        return usd / native
    return None


def select_pools(pairs: list[dict], address: str, share: float = 0.8, limit: int = 10) -> list[dict]:
    """Largest pools that together hold `share` of the token's liquidity."""
    rows = []
    for p in pairs or []:
        b = str((p.get("baseToken") or {}).get("address", "")).lower()
        q = str((p.get("quoteToken") or {}).get("address", "")).lower()
        if address.lower() not in (b, q):
            continue
        liq = _f((p.get("liquidity") or {}).get("usd")) or 0.0
        other = (p.get("quoteToken") if b == address.lower() else p.get("baseToken")) or {}
        rows.append({"address": p.get("pairAddress"), "dex": p.get("dexId"), "labels": p.get("labels") or [],
                     "liquidity": liq, "price": coin_price(p, address), "quote": other.get("symbol"),
                     "url": p.get("url"), "chain": p.get("chainId")})
    rows.sort(key=lambda r: -r["liquidity"])
    total = sum(r["liquidity"] for r in rows)
    out, acc = [], 0.0
    for r in rows:
        out.append(r)
        acc += r["liquidity"]
        if total and acc / total >= share or len(out) >= limit:
            break
    return out


def weighted_price(pools: list[dict]) -> float | None:
    """Liquidity-weighted median of the pools' prices."""
    pts = sorted((p["price"], p["liquidity"]) for p in pools if p.get("price") and p.get("liquidity"))
    total = sum(w for _, w in pts)
    acc = 0.0
    for v, w in pts:
        acc += w
        if acc >= total / 2:
            return v
    return None


def interp_depth(points: list[tuple[float, float]], pct: float) -> float | None:
    """(usd size, impact %) ladder -> the size that moves the price `pct` % (log-linear between points)."""
    prev = (0.0, 0.0)
    for usd, imp in points:
        if imp >= pct:
            u0, i0 = prev
            if u0 <= 0:
                return usd * pct / imp if imp > 0 else usd
            t = (pct - i0) / (imp - i0) if imp > i0 else 1.0
            return math.exp(math.log(u0) + t * (math.log(usd) - math.log(u0)))
        prev = (usd, imp)
    return None


class DexTracker:
    def __init__(self, cfg, session: aiohttp.ClientSession | None, demo: bool = False) -> None:
        self.cfg = cfg
        self.session = session
        self.demo = demo
        self.coin = ""
        self.reset("")

    def reset(self, coin: str) -> None:
        self.coin = coin
        self.token: dict | None = None
        self.pools: list[dict] = []
        self.price: float | None = None
        self.price_ts = 0.0
        self.depth: dict | None = None
        self.v3: dict | None = None
        self.status: dict[str, str] = {}
        self.decimals: dict[str, int] = {}
        self.rpc_swaps: deque = deque(maxlen=2000)  # level 2 swaps not yet taken by the engine
        self.rpc_live = False
        self._token_at = self._pools_at = self._depth_at = self._price_at = 0.0
        self._rpc_task: asyncio.Task | None = None
        self._meta: dict[str, dict] = {}

    # ---- http --------------------------------------------------------------------------
    async def _get(self, url: str, params: dict | None = None):
        async with self.session.get(url, params=params, timeout=TIMEOUT) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {str(data)[:120]}")
            return data

    # ---- token and pools ---------------------------------------------------------------
    async def resolve(self, coin: str) -> None:
        manual = (self.cfg.get("dex.tokens") or {}).get(coin)
        if isinstance(manual, dict) and manual.get("chain") and manual.get("address"):
            self.token = {"chain": str(manual["chain"]).lower(), "address": str(manual["address"]),
                          "symbol": coin, "source": "config"}
            return
        data = await self._get(f"{DS}/latest/dex/search", {"q": coin})
        self.token = pick_token(data.get("pairs") or [], coin)
        self.status["token"] = "ok" if self.token else "токен на DEX не найден (или ликвидность меньше $5k)"

    async def load_pools(self) -> None:
        t = self.token
        pairs = await self._get(f"{DS}/token-pairs/v1/{t['chain']}/{t['address']}")
        self.pools = select_pools(pairs if isinstance(pairs, list) else [], t["address"],
                                  float(self.cfg.get("dex.pools_share")))
        self.price = weighted_price(self.pools) or self.price
        self.status["pools"] = "ok" if self.pools else "пулов нет"

    async def poll_price(self) -> None:
        t = self.token
        pairs = await self._get(f"{DS}/tokens/v1/{t['chain']}/{t['address']}")
        by_addr = {str(p.get("pairAddress")): p for p in pairs or []} if isinstance(pairs, list) else {}
        for pool in self.pools:
            p = by_addr.get(str(pool["address"]))
            if p:
                pool["price"] = coin_price(p, t["address"]) or pool["price"]
                pool["liquidity"] = _f((p.get("liquidity") or {}).get("usd")) or pool["liquidity"]
        price = weighted_price(self.pools)
        if price:
            self.price, self.price_ts = price, time.time()
        self.status["price"] = "ok"

    # ---- depth through aggregator quotes -----------------------------------------------------
    async def _decimals(self, chain: str, address: str) -> int:
        key = f"{chain}:{address}"
        if key not in self.decimals:
            from app.collectors.dex import gt_get  # the shared GeckoTerminal rate limiter

            d = await gt_get(self.session, f"/networks/{CHAINS[chain]['gt']}/tokens/{address}")
            self.decimals[key] = int(((d.get("data") or {}).get("attributes") or {}).get("decimals"))
        return self.decimals[key]

    async def _quote(self, chain: str, src: tuple[str, int], dst: tuple[str, int], amount_raw: int) -> float | None:
        """Raw output for a raw input, or None."""
        if CHAINS[chain].get("jupiter"):
            d = await self._get(JUP, {"inputMint": src[0], "outputMint": dst[0], "amount": str(amount_raw),
                                      "slippageBps": 100, "restrictIntermediateTokens": "true"})
            return _f(d.get("outAmount"))
        d = await self._get(PARASWAP, {"srcToken": src[0], "srcDecimals": src[1], "destToken": dst[0],
                                       "destDecimals": dst[1], "amount": str(amount_raw), "side": "SELL",
                                       "network": CHAINS[chain]["paraswap"]})
        return _f((d.get("priceRoute") or {}).get("destAmount"))

    async def measure_depth(self) -> None:
        t, price = self.token, self.price
        if not t or not price or t["chain"] not in CHAINS:
            self.status["depth"] = "сеть не поддерживается для котировок" if t else "нет токена"
            return
        chain = t["chain"]
        usd_tok = CHAINS[chain]["usd"]
        tok = (t["address"], await self._decimals(chain, t["address"]))
        result: dict = {"ts": time.time(), "price": price, "source": "Jupiter" if chain == "solana" else "ParaSwap"}
        for side in ("up", "down"):
            def raw_in(usd: float) -> int:
                return int(usd * 10 ** usd_tok[1]) if side == "up" else int(usd / price * 10 ** tok[1])

            src, dst = (usd_tok, tok) if side == "up" else (tok, usd_tok)

            async def rate(usd: float) -> float | None:
                amt = raw_in(usd)
                out = await self._quote(chain, src, dst, amt)
                await asyncio.sleep(0.4)
                return (out / amt) if out and amt else None

            ref = await rate(REF_USD)
            points = []
            if ref:
                for usd in LADDER_USD:
                    r = await rate(usd)
                    if not r:
                        break
                    # buying: fewer tokens per dollar; selling: fewer dollars per token
                    impact = (ref / r - 1) * 100 if side == "up" else (1 - r / ref) * 100
                    points.append((usd, max(0.0, impact)))
                    if impact > 6:
                        break
            result[side] = {str(p): interp_depth(points, p) for p in DEPTH_PCTS}
            result[f"{side}_ladder"] = points
        self.depth = result
        self.status["depth"] = "ok"

    # ---- level 2: RPC --------------------------------------------------------------------------
    def rpc_url(self) -> str | None:
        t = self.token
        url = (self.cfg.get("dex.rpc") or {}).get(t["chain"]) if t else None
        return str(url) if url else None

    async def _pool_meta(self, url: str, pool: str) -> dict | None:
        if pool not in self._meta:
            meta = await uni.token_meta(self.session, url, pool)
            if meta:
                meta["coin_is_token0"] = meta["token0"] == self.token["address"].lower()
            self._meta[pool] = meta or {}
        return self._meta[pool] or None

    async def v3_depth(self) -> None:
        url = self.rpc_url()
        if not url or not self.pools or not self.price:
            return
        for pool in self.pools[:3]:  # the largest concentrated-liquidity pool
            addr = str(pool["address"])
            meta = await self._pool_meta(url, addr)
            if not meta:
                continue
            st = await uni.v3_state(self.session, url, addr)
            if not st:
                continue  # v2-style pool
            coin_usd = self.price
            quote_usd = coin_usd / uni.sqrt_x96_to_price(st["sqrt_x96"], meta["dec0"], meta["dec1"]) \
                if meta["coin_is_token0"] else coin_usd * uni.sqrt_x96_to_price(st["sqrt_x96"], meta["dec0"], meta["dec1"])
            out = {"pool": addr, "dex": pool["dex"], "ts": time.time(), "ticks": len(st["net"])}
            for pct in DEPTH_PCTS:
                up, down = uni.v3_cost_to_move(st["sqrt_x96"], st["liquidity"], st["tick"], st["net"], pct,
                                               meta["coin_is_token0"], meta["dec0"], meta["dec1"], coin_usd, quote_usd)
                out.setdefault("up", {})[str(pct)] = up
                out.setdefault("down", {})[str(pct)] = down
            self.v3 = out
            self.status["v3"] = "ok"
            return

    async def rpc_stream(self, coin: str) -> None:
        """Swap logs of the pools: every swap with its price, seconds after the block."""
        url = self.rpc_url()
        pools = [str(p["address"]).lower() for p in self.pools if str(p.get("address", "")).startswith("0x")]
        if not url or not pools or not url.startswith(("ws://", "wss://")):
            return
        metas = {p: await self._pool_meta(url, p) for p in pools}
        sub = {"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
               "params": ["logs", {"address": pools, "topics": [[uni.TOPIC_SWAP_V3, uni.TOPIC_SWAP_V2]]}]}
        async with self.session.ws_connect(url, heartbeat=20, max_msg_size=0) as ws:
            await ws.send_str(json.dumps(sub))
            self.rpc_live = True
            self.status["rpc"] = "ok"
            async for msg in ws:
                if self.coin != coin:
                    return
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                res = ((json.loads(msg.data).get("params") or {}).get("result")) or {}
                pool = str(res.get("address", "")).lower()
                meta = metas.get(pool)
                topics = res.get("topics") or []
                if not meta or not topics:
                    continue
                dec = uni.decode_swap(topics[0], res.get("data", "0x"), meta["coin_is_token0"], meta["dec0"], meta["dec1"])
                if not dec:
                    continue
                side, coin_amt, quote_amt, price_after = dec
                usd = coin_amt * (self.price or 0)
                exec_px = (usd / coin_amt) if coin_amt else None
                self.rpc_swaps.append({"ts": time.time(), "side": side, "coin": coin_amt, "usd": usd,
                                       "price": exec_px, "tx": res.get("transactionHash"),
                                       "block": int(res.get("blockNumber") or "0x0", 16), "pool": pool,
                                       "quote_price_after": price_after})
        self.rpc_live = False

    # ---- loop ---------------------------------------------------------------------------------
    async def run(self) -> None:
        while True:
            coin = self.coin
            if coin and self.cfg.on("dex") and not self.demo:
                try:
                    await self._step(coin)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.status["error"] = f"{type(e).__name__}: {e}"[:160]
                    log.info("dex %s: %s", coin, e)
                    await asyncio.sleep(10)
            elif self.demo and coin:
                self._demo_step()
            await asyncio.sleep(0.5)

    async def _step(self, coin: str) -> None:
        now = time.time()
        if self.token is None and now - self._token_at > 1800 or now - self._token_at > 6 * 3600:
            self._token_at = now
            await self.resolve(coin)
        if not self.token or coin != self.coin:
            return
        if now - self._pools_at > 300:
            self._pools_at = now
            await self.load_pools()
        if now - self._price_at >= float(self.cfg.get("dex.price_poll_sec")):
            self._price_at = now
            await self.poll_price()
        if now - self._depth_at >= float(self.cfg.get("dex.depth_poll_min")) * 60:
            self._depth_at = now
            for job in (self.measure_depth, self.v3_depth):
                try:
                    await job()
                except Exception as e:  # noqa: BLE001 - depth is optional
                    self.status["depth"] = f"ошибка: {type(e).__name__}: {e}"[:160]
        if self.rpc_url() and (self._rpc_task is None or self._rpc_task.done()):
            self._rpc_task = asyncio.create_task(self._rpc_guard(coin))

    async def _rpc_guard(self, coin: str) -> None:
        try:
            await self.rpc_stream(coin)
        except Exception as e:  # noqa: BLE001
            self.status["rpc"] = f"ошибка: {type(e).__name__}: {e}"[:160]
            self.rpc_live = False
            await asyncio.sleep(30)

    def _demo_step(self) -> None:
        """DEMO=1: a pool that follows the demo price with a lag, and a thin DEX book."""
        from app.collectors.demo import MARKET

        now = time.time()
        if self.token is None:
            self.token = {"chain": "base", "address": "0xdemo", "symbol": self.coin, "source": "демо"}
            self.pools = [{"address": "0xpool1", "dex": "uniswap", "labels": ["v3"], "liquidity": 850_000.0,
                           "price": None, "quote": "WETH", "url": None, "chain": "base"},
                          {"address": "0xpool2", "dex": "aerodrome", "labels": [], "liquidity": 240_000.0,
                           "price": None, "quote": "USDC", "url": None, "chain": "base"}]
            self.status = {"token": "ok", "pools": "ok", "price": "ok", "depth": "ok"}
        if now - self._price_at >= 3:
            self._price_at = now
            p = MARKET.price(self.coin, now - 4) * (1 + random.gauss(0, 0.0005))  # 4 s behind the market
            for pool in self.pools:
                pool["price"] = p * (1 + random.gauss(0, 0.0003))
            self.price, self.price_ts = weighted_price(self.pools), now
        if now - self._depth_at >= 60 and self.price:
            self._depth_at = now
            base = 25_000 * random.uniform(0.8, 1.2)
            self.depth = {"ts": now, "price": self.price, "source": "демо",
                          "up": {str(p): base * p * 1.1 for p in DEPTH_PCTS},
                          "down": {str(p): base * p for p in DEPTH_PCTS}}
