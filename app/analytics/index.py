"""М7: index constituents of the perps (mark / index price).

A perp's index (and its mark, by which positions are liquidated) is a weighted price of a
basket of spot venues. A thin venue in the basket can be moved cheaply - the index moves,
liquidations fire, while nobody bought on the perp itself.

Sources (public; checked against the venues' docs, see the README):
  Binance  GET https://fapi.binance.com/fapi/v1/constituents?symbol=   (weight 2)
           constituents: exchange, symbol (+ price, weight where the API gives them;
           the documented example has neither - then all weights are equal)
  OKX      GET https://www.okx.com/api/v5/market/index-components?index=COIN-USDT
           components: exch, symbol, symPx, wgt, cnvPx
  Bybit    GET https://api.bybit.com/v5/market/index-price-components?indexName=
           components: exchange, spotPair, equivalentPrice, multiplier, price, weight
           (the spec expected a manual list for Bybit - the venue now publishes it)

Live prices of the constituents come from our own spot streams of those venues (the basket
venues are almost all connected); the API price is used where we have no stream.
"""
import asyncio
import logging
import time
from collections import deque

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=20)
DEV_KEEP = 1800  # seconds of deviation history for the chart

# exchange names used by the index APIs -> our venue names
EXCHANGE_MAP = {
    "binance": "Binance", "okex": "OKX", "okx": "OKX", "huobi": "HTX", "htx": "HTX", "gateio": "Gate",
    "gate": "Gate", "gate.io": "Gate", "kucoin": "KuCoin", "mexc": "MEXC", "bybit": "Bybit",
    "coinbase": "Coinbase", "coinbasepro": "Coinbase", "bitget": "Bitget", "kraken": "Kraken",
    "bitmart": "BitMart", "bitstamp": "Bitstamp", "upbit": "Upbit", "bithumb": "Bithumb",
    "crypto.com": "Crypto.com", "cryptocom": "Crypto.com", "bitfinex": "Bitfinex", "lbank": "LBank",
    "whitebit": "WhiteBIT", "poloniex": "Poloniex", "bingx": "BingX", "coinex": "CoinEx", "binanceus": "Binance US",
    "gemini": "Gemini", "bitvavo": "Bitvavo", "xt": "XT.com", "htx global": "HTX", "hyperliquid": "Hyperliquid",
    "phemex": "Phemex", "bitrue": "Bitrue", "digifinex": "DigiFinex", "bitflyer": "bitFlyer",
}


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v > 0 else None


def weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """(value, weight) -> the value where the cumulative weight crosses half."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in pairs)
    if not pairs or total <= 0:
        return None
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def parse_protection(rule: str) -> tuple[str, float]:
    """'clamp:5' / 'exclude:10' / 'none' -> (mode, pct)."""
    try:
        mode, pct = str(rule).split(":", 1)
        return mode.strip().lower(), float(pct)
    except ValueError:
        return "none", 0.0


def model_index(parts: list[tuple[float, float]], rule: str) -> float | None:
    """Weighted index of (price, weight) with the venue's protection against outliers."""
    pairs = [(p, w) for p, w in parts if p and w > 0]
    if not pairs:
        return None
    mode, pct = parse_protection(rule)
    med = weighted_median(pairs)
    if mode in ("clamp", "exclude") and med and pct > 0:
        lo, hi = med * (1 - pct / 100), med * (1 + pct / 100)
        if mode == "clamp":
            pairs = [(min(hi, max(lo, p)), w) for p, w in pairs]
        else:
            pairs = [(p, w) for p, w in pairs if lo <= p <= hi] or pairs
    total = sum(w for _, w in pairs)
    return sum(p * w for p, w in pairs) / total


def parse_binance(data: dict) -> list[dict]:
    out = []
    for c in data.get("constituents") or []:
        out.append({"exchange": str(c.get("exchange") or ""), "symbol": str(c.get("symbol") or ""),
                    "api_price": _f(c.get("price")), "weight": _f(c.get("weight"))})
    return out


def parse_okx(data: dict) -> list[dict]:
    d = (data.get("data") or [{}])[0] or {}
    return [{"exchange": str(c.get("exch") or ""), "symbol": str(c.get("symbol") or ""),
             "api_price": _f(c.get("cnvPx")) or _f(c.get("symPx")), "weight": _f(c.get("wgt"))}
            for c in d.get("components") or []]


def parse_bybit(data: dict) -> list[dict]:
    r = data.get("result") or {}
    return [{"exchange": str(c.get("exchange") or ""), "symbol": str(c.get("spotPair") or ""),
             "api_price": _f(c.get("equivalentPrice")) or _f(c.get("price")), "weight": _f(c.get("weight"))}
            for c in r.get("components") or []]


class Basket:
    def __init__(self, source: str, name: str) -> None:
        self.source = source  # Binance | OKX | Bybit
        self.name = name  # index / symbol it was fetched for
        self.parts: list[dict] = []
        self.fetched = 0.0
        self.equal_weights = False
        self.persist: dict[str, float] = {}  # venue -> since when |dev| is above the threshold
        self.dev_hist: dict[str, deque] = {}


class IndexTracker:
    def __init__(self, cfg, session: aiohttp.ClientSession | None, demo: bool = False) -> None:
        self.cfg = cfg
        self.session = session
        self.demo = demo
        self.coin = ""
        self.baskets: dict[str, Basket] = {}
        self.status: dict[str, str] = {}
        self.ids: dict[str, str] = {}  # source -> the symbol / index name to ask for

    def reset(self, coin: str) -> None:
        self.coin = coin
        self.baskets, self.status, self.ids = {}, {}, {}

    def set_ids(self, streams: dict) -> None:
        """Index names from our perp markets of the main coin (1000PEPEUSDT and such)."""
        for source, key in (("Binance", "Binance:perp"), ("OKX", "OKX:perp"), ("Bybit", "Bybit:perp")):
            s = streams.get(key)
            m = getattr(s, "market", None) or {}
            if source in self.ids or not m.get("id"):
                continue
            if source == "OKX":
                info = m.get("info") or {}
                self.ids[source] = info.get("uly") or info.get("instFamily") or f"{self.coin}-USDT"
            else:
                self.ids[source] = m["id"]

    async def _get(self, url: str, params: dict):
        async with self.session.get(url, params=params, timeout=TIMEOUT) as r:
            return await r.json(content_type=None)

    async def fetch(self, source: str, name: str) -> list[dict]:
        if source == "Binance":
            return parse_binance(await self._get("https://fapi.binance.com/fapi/v1/constituents", {"symbol": name}))
        if source == "OKX":
            return parse_okx(await self._get("https://www.okx.com/api/v5/market/index-components", {"index": name}))
        return parse_bybit(await self._get("https://api.bybit.com/v5/market/index-price-components",
                                           {"indexName": name}))

    def _set(self, source: str, name: str, parts: list[dict]) -> None:
        b = self.baskets.get(source) or Basket(source, name)
        b.name = name
        b.equal_weights = not any(p["weight"] for p in parts)
        for p in parts:
            p["venue"] = EXCHANGE_MAP.get(p["exchange"].lower().replace(" ", ""), EXCHANGE_MAP.get(
                p["exchange"].lower(), p["exchange"]))
            if b.equal_weights:
                p["weight"] = 1.0 / len(parts)
        b.parts, b.fetched = parts, time.time()
        self.baskets[source] = b
        self.status[source] = "ok" if parts else "биржа не дала состав индекса"

    async def run(self) -> None:
        while True:
            coin = self.coin
            if coin and self.cfg.on("index") and not self.demo:
                every = float(self.cfg.get("index.constituents_refresh_min")) * 60
                for source, name in list(self.ids.items()):
                    b = self.baskets.get(source)
                    if b and time.time() - b.fetched < every:
                        continue
                    try:
                        parts = await self.fetch(source, name)
                        if coin == self.coin:
                            self._set(source, name, parts)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        self.status[source] = f"ошибка: {type(e).__name__}: {e}"[:160]
                        log.info("index %s %s: %s", source, name, e)
            for _ in range(15):  # a new coin gets its baskets at once
                # never tried ids (the perp market just resolved): fetch at once; failures wait the full pause
                if self.coin != coin or any(src not in self.baskets and src not in self.status for src in self.ids):
                    break
                await asyncio.sleep(1)

    def demo_baskets(self, venues: list[str]) -> None:
        """DEMO=1: baskets made of the demo spot venues."""
        if self.baskets or len(venues) < 3:
            return
        import random

        rnd = random.Random(self.coin)
        for source in ("Binance", "OKX", "Bybit"):
            pick = rnd.sample(venues, min(len(venues), rnd.randint(4, 7)))
            ws = [rnd.uniform(0.5, 2) for _ in pick]
            self._set(source, f"{self.coin}USDT", [{"exchange": v, "symbol": f"{self.coin}USDT", "api_price": None,
                                                     "weight": w / sum(ws)} for v, w in zip(pick, ws)])

    # ---- computation -----------------------------------------------------------
    def compute(self, now: float, price_of, depth_of, exchange_index: dict[str, float | None]) -> list[dict]:
        """Per basket: constituents with deviation, persistence, depth, influence; our index vs the venue's."""
        thr = float(self.cfg.get("index.dev_alert_pct"))
        out = []
        for source, b in self.baskets.items():
            rows = []
            for p in b.parts:
                live = price_of(p["venue"])
                price = live or p.get("api_price")
                rows.append({"exchange": p["exchange"], "venue": p["venue"], "symbol": p["symbol"],
                             "weight": p["weight"] or 0.0, "price": price, "live": bool(live),
                             "ctm1": depth_of(p["venue"])})
            for r in rows:
                others = [(o["price"], o["weight"] or 1e-9) for o in rows if o is not r and o["price"]]
                cons = weighted_median(others)
                r["dev"] = (r["price"] / cons - 1) * 100 if r["price"] and cons else None
                r["influence"] = r["weight"] * r["dev"] if r["dev"] is not None else None
                if r["dev"] is not None and abs(r["dev"]) > thr:
                    since = b.persist.setdefault(r["venue"], now)
                else:
                    b.persist.pop(r["venue"], None)
                    since = None
                r["persist"] = round(now - since, 1) if since else 0.0
                dq = b.dev_hist.setdefault(r["venue"], deque())
                if r["dev"] is not None and (not dq or now - dq[-1][0] >= 5):
                    dq.append((now, round(r["dev"], 4)))
                while dq and dq[0][0] < now - DEV_KEEP:
                    dq.popleft()
            ours = model_index([(r["price"], r["weight"]) for r in rows],
                               str(self.cfg.get(f"index.protection_{source.lower()}") or "none"))
            theirs = exchange_index.get(source)
            out.append({
                "source": source, "name": b.name, "equal_weights": b.equal_weights, "fetched": b.fetched,
                "rows": sorted(rows, key=lambda r: -(r["weight"] or 0)),
                "index_model": ours, "index_exchange": theirs,
                "model_error_pct": (ours / theirs - 1) * 100 if ours and theirs else None,
                "dev_hist": {v: list(dq) for v, dq in b.dev_hist.items()},
            })
        return out
