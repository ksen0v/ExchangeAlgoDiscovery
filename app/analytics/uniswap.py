"""Uniswap v2/v3 math and a minimal JSON-RPC client (М8, "точный" level 2).

v3: inside a tick range liquidity L is constant and, in raw units,
    moving √P from a to b (b > a) takes  Δtoken1 = L · (√b − √a)
    moving √P from a to b (b < a) takes  Δtoken0 = L · (1/√b − 1/√a)
where P = token1 / token0 in raw units (decimals not applied) and √P(tick) = 1.0001^(tick/2).
Crossing an initialised tick upwards adds its liquidityNet to L, downwards subtracts it.

Only read calls (eth_call, eth_subscribe "logs") are made: nothing is ever signed or sent.
"""
import math

import aiohttp

Q96 = 2**96
TOPIC_SWAP_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
TOPIC_SWAP_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
SEL = {
    "slot0": "0x3850c7bd", "liquidity": "0x1a686502", "tickSpacing": "0xd0c93a7c", "tickBitmap": "0x5339c296",
    "ticks": "0xf30dba93", "token0": "0x0dfe1681", "token1": "0xd21220a7", "decimals": "0x313ce567",
    "getReserves": "0x0902f1ac",
}
RPC_TIMEOUT = aiohttp.ClientTimeout(total=20)


# ---- price / tick math --------------------------------------------------------------
def sqrt_x96_to_price(sqrt_x96: int, dec0: int, dec1: int) -> float:
    """Human price of token0 in token1."""
    return (sqrt_x96 / Q96) ** 2 * 10 ** (dec0 - dec1)


def tick_sqrt(tick: int) -> float:
    return math.pow(1.0001, tick / 2)


def amount1_for_up(liq: float, sqrt_a: float, sqrt_b: float) -> float:
    return liq * (sqrt_b - sqrt_a)


def amount0_for_down(liq: float, sqrt_a: float, sqrt_b: float) -> float:
    return liq * (1 / sqrt_b - 1 / sqrt_a)


def walk_cost(sqrt_p: float, liq: float, tick: int, net: dict[int, int], target_sqrt: float) -> float:
    """Raw input needed to move √P to target_sqrt across initialised ticks (net = liquidityNet by tick).

    Up (target above): token1 in. Down: token0 in.
    """
    total, cur, L = 0.0, sqrt_p, float(liq)
    if target_sqrt >= sqrt_p:
        for t in sorted(t for t in net if t > tick):
            s = tick_sqrt(t)
            if s >= target_sqrt:
                break
            total += amount1_for_up(L, cur, s)
            L += net[t]
            cur = s
        return total + amount1_for_up(max(L, 0.0), cur, target_sqrt)
    for t in sorted((t for t in net if t <= tick), reverse=True):
        s = tick_sqrt(t)
        if s <= target_sqrt:
            break
        total += amount0_for_down(L, cur, s)
        L -= net[t]
        cur = s
    return total + amount0_for_down(max(L, 0.0), cur, target_sqrt)


def v3_cost_to_move(sqrt_x96: int, liq: int, tick: int, net: dict[int, int], pct: float, coin_is_token0: bool,
                    dec0: int, dec1: int, coin_usd: float, quote_usd: float) -> tuple[float, float]:
    """USD to move the COIN price up and down by pct % (market orders, fees ignored)."""
    sp = sqrt_x96 / Q96
    out = []
    for up in (True, False):
        factor = (1 + pct / 100) if up else (1 - pct / 100)
        # P = token1/token0: the coin price moves with P if the coin is token0, against it otherwise
        p_factor = factor if coin_is_token0 else 1 / factor
        raw = walk_cost(sp, liq, tick, net, sp * math.sqrt(p_factor))
        dec = dec1 if p_factor > 1 else dec0  # √P up takes token1, down takes token0
        # the input token is the quote when the coin goes up, the coin itself when it goes down
        out.append(raw / 10**dec * (quote_usd if up else coin_usd))
    return out[0], out[1]


# ---- ABI ---------------------------------------------------------------------------------
def enc_int(v: int) -> str:
    return format(v % (1 << 256), "064x")


def words(data: str) -> list[int]:
    h = data[2:] if data.startswith("0x") else data
    return [int(h[i:i + 64], 16) for i in range(0, len(h) - 63, 64)]


def signed(v: int, bits: int = 256) -> int:
    v &= (1 << 256) - 1
    if bits < 256:
        v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def decode_swap(topic0: str, data: str, coin_is_token0: bool, dec0: int, dec1: int) -> tuple[str, float, float, float | None] | None:
    """A Swap log -> (side for the coin, coin amount, quote amount, price after (v3) or None)."""
    w = words(data)
    if topic0 == TOPIC_SWAP_V3 and len(w) >= 3:
        a0, a1 = signed(w[0]) / 10**dec0, signed(w[1]) / 10**dec1
        after = sqrt_x96_to_price(w[2], dec0, dec1)
        coin, quote = (a0, a1) if coin_is_token0 else (a1, a0)
        side = "buy" if coin < 0 else "sell"  # the coin left the pool = bought
        price = after if coin_is_token0 else (1 / after if after else None)
        return side, abs(coin), abs(quote), price
    if topic0 == TOPIC_SWAP_V2 and len(w) >= 4:
        in0, in1, out0, out1 = w[0] / 10**dec0, w[1] / 10**dec1, w[2] / 10**dec0, w[3] / 10**dec1
        coin_out, coin_in = (out0, in0) if coin_is_token0 else (out1, in1)
        quote_out, quote_in = (out1, in1) if coin_is_token0 else (out0, in0)
        if coin_out > 0:
            return "buy", coin_out, quote_in, None
        if coin_in > 0:
            return "sell", coin_in, quote_out, None
    return None


# ---- JSON-RPC ------------------------------------------------------------------------------
def http_url(url: str) -> str:
    return "https://" + url[6:] if url.startswith("wss://") else ("http://" + url[5:] if url.startswith("ws://") else url)


async def rpc_batch(session: aiohttp.ClientSession, url: str, calls: list[tuple[str, str]]) -> list[str | None]:
    """eth_call (to, data) in one batch request; None for a failed call."""
    if not calls:
        return []
    body = [{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]}
            for i, (to, data) in enumerate(calls)]
    async with session.post(http_url(url), json=body, timeout=RPC_TIMEOUT) as r:
        res = await r.json(content_type=None)
    if isinstance(res, dict):
        res = [res]
    by_id = {x.get("id"): x.get("result") for x in res if isinstance(x, dict)}
    return [by_id.get(i) for i in range(len(calls))]


async def v3_state(session, url: str, pool: str, max_pct: float = 6.0) -> dict | None:
    """slot0, liquidity, spacing and the initialised ticks within ±max_pct % of the price."""
    slot0, liq, spacing = await rpc_batch(session, url, [(pool, SEL["slot0"]), (pool, SEL["liquidity"]),
                                                          (pool, SEL["tickSpacing"])])
    if not slot0 or not liq or not spacing or len(words(slot0)) < 2:
        return None
    w = words(slot0)
    sqrt_x96, tick = w[0], signed(w[1], 24)
    L, ts = words(liq)[0], signed(words(spacing)[0], 24)
    if ts <= 0:
        return None
    span = int(math.log(1 + max_pct / 100) / math.log(1.0001)) + ts
    lo_c, hi_c = (tick - span) // ts, (tick + span) // ts
    word_ids = list(range(lo_c >> 8, (hi_c >> 8) + 1))[:16]
    bitmaps = await rpc_batch(session, url, [(pool, SEL["tickBitmap"] + enc_int(wd)) for wd in word_ids])
    inited = []
    for wd, bm in zip(word_ids, bitmaps):
        bits = words(bm)[0] if bm and len(bm) > 2 else 0
        for b in range(256):
            if bits >> b & 1:
                c = wd * 256 + b
                if lo_c <= c <= hi_c:
                    inited.append(c * ts)
    inited = inited[:300]
    tick_data = await rpc_batch(session, url, [(pool, SEL["ticks"] + enc_int(t)) for t in inited])
    net = {}
    for t, d in zip(inited, tick_data):
        if d and len(words(d)) >= 2:
            net[t] = signed(words(d)[1], 128)
    return {"sqrt_x96": sqrt_x96, "tick": tick, "liquidity": L, "spacing": ts, "net": net}


async def token_meta(session, url: str, pool: str) -> dict | None:
    """token0 / token1 addresses and decimals of a pool."""
    t0, t1 = await rpc_batch(session, url, [(pool, SEL["token0"]), (pool, SEL["token1"])])
    if not t0 or not t1:
        return None
    a0, a1 = "0x" + t0[-40:], "0x" + t1[-40:]
    d0, d1 = await rpc_batch(session, url, [(a0, SEL["decimals"]), (a1, SEL["decimals"])])
    if not d0 or not d1:
        return None
    return {"token0": a0.lower(), "token1": a1.lower(), "dec0": words(d0)[0], "dec1": words(d1)[0]}
