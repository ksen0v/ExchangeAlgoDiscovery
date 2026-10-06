"""М6: borrowing for shorts - how much of the coin can be borrowed and at what rate.

Sources (checked against the venues' docs, see the README):
  Binance  GET /sapi/v1/margin/available-inventory?type=MARGIN|ISOLATED  (signed, weight 50)
           GET /sapi/v1/margin/next-hourly-interest-rate?assets=&isIsolated=FALSE (signed)
           GET /sapi/v1/margin/interestRateHistory?asset=  (signed: 30-day baseline at once)
           Needs a READ-ONLY key in .env. Its rights are checked first with
           GET /sapi/v1/account/apiRestrictions: a key that can trade, withdraw or transfer is
           refused and never used.
  OKX      GET /api/v5/public/interest-rate-loan-quota  (public; `rate` is a DAILY rate, `quota`
           is the loan quota of one user, not the venue's inventory)
           GET /api/v5/finance/savings/lending-rate-summary?ccy=  (public; the lending market:
           24h average lent amount and the annual rate, grows with demand to borrow)
  Bybit    GET /v5/spot-margin-trade/data?currency=  (public; hourlyBorrowRate, borrowable,
           maxBorrowingAmount per user; the venue's inventory is private)
  DeFi     DefiLlama yields API (Aave, Morpho, Euler, Compound, Spark, Kamino, MarginFi, ...):
           borrow APY and utilisation = borrowed / supplied.
"""
import asyncio
import hashlib
import hmac
import logging
import time
from collections import deque
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger(__name__)

BINANCE = "https://api.binance.com"
OKX = "https://www.okx.com"
BYBIT = "https://api.bybit.com"
LLAMA_POOLS = "https://yields.llama.fi/pools"
LLAMA_LEND = "https://yields.llama.fi/lendBorrow"
TIMEOUT = aiohttp.ClientTimeout(total=20)
BIG_TIMEOUT = aiohttp.ClientTimeout(total=90)
HISTORY_KEEP = 25 * 3600
# rights a read-only key must NOT have
DANGEROUS_RIGHTS = ("enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer",
                    "enableSpotAndMarginTrading", "enableMargin", "enableFutures", "enableVanillaOptions",
                    "enablePortfolioMarginTrading", "enableFixApiTrade")
RIGHT_NAMES = {
    "enableWithdrawals": "вывод", "enableInternalTransfer": "внутренние переводы",
    "permitsUniversalTransfer": "переводы между счетами", "enableSpotAndMarginTrading": "торговля спот/маржа",
    "enableMargin": "заём и погашение", "enableFutures": "фьючерсы", "enableVanillaOptions": "опционы",
    "enablePortfolioMarginTrading": "портфельная маржа", "enableFixApiTrade": "торговля FIX",
}
LENDING_PROJECTS = ("aave", "morpho", "euler", "compound", "spark", "kamino", "marginfi", "solend", "save",
                    "radiant", "venus", "fluid", "silo", "dolomite", "benqi", "moonwell", "seamless", "juplend",
                    "drift", "zerolend", "justlend", "navi", "suilend", "scallop")


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def signed_query(secret: str, params: dict) -> str:
    """Binance HMAC-SHA256 signature over the query string."""
    q = urlencode(params)
    return q + "&signature=" + hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()


def key_problems(restrictions: dict) -> list[str]:
    """Rights a read-only key must not have (empty = the key is safe to use)."""
    return [RIGHT_NAMES[k] for k in DANGEROUS_RIGHTS if restrictions.get(k)]


def apr_from_hourly(h: float) -> float:
    return h * 24 * 365 * 100


class BorrowTracker:
    """Polls every source for the main coin; rows[venue] = latest values, history[venue] = 25 h."""

    def __init__(self, cfg, session: aiohttp.ClientSession | None, api_key: str = "", api_secret: str = "",
                 clock=None, store=None, demo: bool = False) -> None:
        self.cfg = cfg
        self.session = session
        self.api_key, self.api_secret = api_key.strip(), api_secret.strip()
        self.clock = clock
        self.store = store
        self.demo = demo
        self.coin = ""
        self.rows: dict[str, dict] = {}
        self.history: dict[str, deque] = {}
        self.status: dict[str, str] = {}
        self.key_state = "нет ключа" if not self.api_key else "проверяется"
        self.key_ok = False
        self._key_checked = 0.0
        self._pools: tuple[float, list] = (0.0, [])
        self._defi_at = 0.0
        self.rate_seed: list[tuple[float, float]] = []  # Binance daily rate history -> hourly, for the baseline
        self._seed_taken = True
        self._polling = ""

    def reset(self, coin: str) -> None:
        self.coin = coin
        self.rows, self.history, self.status = {}, {}, {}
        self._defi_at = 0.0
        self.rate_seed, self._seed_taken = [], True

    def take_seed(self) -> list[tuple[float, float]]:
        if self._seed_taken:
            return []
        self._seed_taken = True
        return self.rate_seed

    # ---- helpers ------------------------------------------------------------
    async def _get(self, url: str, params: dict | None = None, headers: dict | None = None,
                   timeout: aiohttp.ClientTimeout = TIMEOUT):
        async with self.session.get(url, params=params, headers=headers, timeout=timeout) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                msg = data.get("msg") if isinstance(data, dict) else None
                raise RuntimeError(f"HTTP {r.status}: {msg or str(data)[:120]}")
            return data

    async def _binance(self, path: str, params: dict | None = None):
        offset = (self.clock.offset_ms or 0) / 1000 if self.clock and self.clock.offset_ms is not None else 0.0
        p = {**(params or {}), "timestamp": int((time.time() + offset) * 1000), "recvWindow": 10000}
        url = f"{BINANCE}{path}?{signed_query(self.api_secret, p)}"
        return await self._get(url, headers={"X-MBX-APIKEY": self.api_key})

    def _put(self, venue: str, now: float, **row) -> None:
        if self._polling != self.coin:  # the coin changed while the request was in flight
            return
        self.rows[venue] = {"venue": venue, "ts": now, **row}
        avail = row.get("available") if row.get("available") is not None else row.get("available_usd")
        dq = self.history.setdefault(venue, deque())
        dq.append((now, avail, row.get("rate_h")))
        while dq and dq[0][0] < now - HISTORY_KEEP:
            dq.popleft()
        self.status[venue] = "ok"

    def change_pct(self, venue: str, now: float, sec: int) -> float | None:
        dq = self.history.get(venue)
        if not dq or dq[-1][1] is None:
            return None
        then = None
        for ts, a, _ in dq:
            if ts <= now - sec + 30 and a is not None:
                then = a
            elif ts > now - sec + 30:
                break
        if not then or dq[0][0] > now - sec + 60:
            return None  # not enough history yet
        return (dq[-1][1] / then - 1) * 100

    async def load_history(self, coin: str) -> None:
        """Availability of the last 25 h from the stored minute samples (survives restarts)."""
        if self.store is None:
            return
        try:
            for metric in await self.store.metrics(coin):
                if metric.startswith("borrow_avail:"):
                    venue = metric.split(":", 1)[1]
                    rows = await self.store.samples(coin, metric, time.time() - HISTORY_KEEP)
                    if coin == self.coin and rows:
                        dq = self.history.setdefault(venue, deque())
                        old = deque((float(ts), v, None) for ts, v in rows)
                        old.extend(dq)
                        self.history[venue] = old
        except Exception as e:  # noqa: BLE001
            log.info("borrow history not loaded: %s", e)

    # ---- sources -------------------------------------------------------------
    async def check_key(self) -> None:
        if not self.api_key or not self.api_secret:
            self.key_ok, self.key_state = False, "нет ключа"
            return
        r = await self._binance("/sapi/v1/account/apiRestrictions")
        bad = key_problems(r)
        if bad:
            self.key_ok = False
            self.key_state = ("ключ отклонён: у него есть права " + ", ".join(bad)
                              + ". Создайте ключ только на чтение")
            log.warning("Binance key refused: it has rights %s", bad)
        elif not r.get("enableReading", True):
            self.key_ok, self.key_state = False, "у ключа нет права чтения"
        else:
            self.key_ok = True
            self.key_state = "ключ только на чтение" + ("" if r.get("ipRestrict") else
                                                        " ⚠ без белого списка IP — включите его на Binance")

    async def poll_binance(self, coin: str, now: float) -> None:
        if time.time() - self._key_checked > 6 * 3600:
            self._key_checked = time.time()
            await self.check_key()
        if not self.key_ok:
            self.status["Binance"] = self.key_state
            return
        cross = (await self._binance("/sapi/v1/margin/available-inventory", {"type": "MARGIN"})) or {}
        iso = (await self._binance("/sapi/v1/margin/available-inventory", {"type": "ISOLATED"})) or {}
        avail = _f((cross.get("assets") or {}).get(coin))
        avail_iso = _f((iso.get("assets") or {}).get(coin))
        if avail is None and avail_iso is None:
            self.status["Binance"] = "монеты нет в марже Binance"
            return
        rate = None
        try:
            r = await self._binance("/sapi/v1/margin/next-hourly-interest-rate",
                                    {"assets": coin, "isIsolated": "FALSE"})
            rate = _f((r[0] if isinstance(r, list) and r else {}).get("nextHourlyInterestRate"))
        except Exception as e:  # noqa: BLE001
            log.info("Binance borrow rate: %s", e)
        if not self.rate_seed:
            try:
                hist = await self._binance("/sapi/v1/margin/interestRateHistory", {"asset": coin})
                self.rate_seed = [(int(h["timestamp"]) / 1000, float(h["dailyInterestRate"]) / 24)
                                  for h in hist or [] if h.get("dailyInterestRate") is not None]
                self._seed_taken = False
            except Exception as e:  # noqa: BLE001
                log.info("Binance borrow rate history: %s", e)
        self._put("Binance", now, kind="CEX", available=avail, available_iso=avail_iso, rate_h=rate,
                  rate_apr=apr_from_hourly(rate) if rate is not None else None,
                  updated=_f(cross.get("updateTime")), note="доступно к займу на бирже (кросс-маржа)")

    async def poll_okx(self, coin: str, now: float) -> None:
        q = await self._get(f"{OKX}/api/v5/public/interest-rate-loan-quota")
        basic = ((q.get("data") or [{}])[0] or {}).get("basic") or []
        row = next((b for b in basic if str(b.get("ccy")).upper() == coin), None)
        summary = None
        try:
            s = await self._get(f"{OKX}/api/v5/finance/savings/lending-rate-summary", {"ccy": coin})
            summary = (s.get("data") or [None])[0]
        except Exception as e:  # noqa: BLE001
            log.info("OKX lending summary: %s", e)
        if row is None and not summary:
            self.status["OKX"] = "монеты нет в займах OKX"
            return
        daily = _f(row.get("rate")) if row else None
        rate_h = daily / 24 if daily is not None else None
        lent = _f((summary or {}).get("avgAmt"))
        self._put("OKX", now, kind="CEX", available=lent, rate_h=rate_h,
                  rate_apr=apr_from_hourly(rate_h) if rate_h is not None else None,
                  quota=_f(row.get("quota")) if row else None,
                  market_rate_apr=(_f((summary or {}).get("estRate")) or 0) * 100 if summary else None,
                  note="доступно = средний объём в кредит за 24ч (рынок займов OKX); квота — на одного пользователя")

    async def poll_bybit(self, coin: str, now: float) -> None:
        d = await self._get(f"{BYBIT}/v5/spot-margin-trade/data", {"currency": coin})
        if d.get("retCode") not in (0, "0"):
            raise RuntimeError(d.get("retMsg") or "bad response")
        levels = (d.get("result") or {}).get("vipCoinList") or []
        base = next((lv for lv in levels if str(lv.get("vipLevel")).lower() in ("no vip", "")), levels[0] if levels else {})
        row = next((x for x in base.get("list") or [] if str(x.get("currency")).upper() == coin), None)
        if not row or not row.get("borrowable"):
            self.status["Bybit"] = "монету нельзя занять на Bybit"
            return
        rate_h = _f(row.get("hourlyBorrowRate"))
        self._put("Bybit", now, kind="CEX", available=None, rate_h=rate_h,
                  rate_apr=apr_from_hourly(rate_h) if rate_h is not None else None,
                  quota=_f(row.get("maxBorrowingAmount")),
                  note="Bybit публикует только ставку и лимит на пользователя; запас биржи закрыт")

    async def poll_defi(self, coin: str, now: float) -> None:
        if now - self._pools[0] > 3600:
            pools = await self._get(LLAMA_POOLS, timeout=BIG_TIMEOUT)
            self._pools = (now, [p for p in pools.get("data") or []
                                 if str(p.get("project", "")).lower().startswith(LENDING_PROJECTS)])
        mine = {p["pool"]: p for p in self._pools[1] if str(p.get("symbol", "")).upper() == coin}
        if not mine:
            self.status["DeFi"] = "монеты нет в протоколах займов"
            return
        lend = await self._get(LLAMA_LEND, timeout=BIG_TIMEOUT)
        found = []
        for x in lend if isinstance(lend, list) else []:
            p = mine.get(x.get("pool"))
            supply, borrow = _f(x.get("totalSupplyUsd")), _f(x.get("totalBorrowUsd"))
            if p and supply and borrow is not None:
                found.append((supply, p, x, borrow))
        for supply, p, x, borrow in sorted(found, key=lambda t: -t[0])[:5]:
            apy = _f(x.get("apyBaseBorrow"))
            venue = f"{p.get('project')} · {p.get('chain')}"
            self._put(venue, now, kind="DeFi", available=None, available_usd=supply - borrow,
                      rate_h=apy / 100 / 24 / 365 if apy is not None else None, rate_apr=apy,
                      utilization=borrow / supply if supply else None, supply_usd=supply, borrow_usd=borrow,
                      note="DefiLlama: свободная ликвидность пула, ставка займа и утилизация")
        if not found:
            self.status["DeFi"] = "нет данных о займах по пулам монеты"

    def _demo(self, coin: str, now: float) -> None:
        import random

        for venue, base in (("Binance", 2e6), ("OKX", 8e5), ("aave-v3 · Ethereum", None)):
            prev = self.rows.get(venue)
            if base:
                avail = (prev or {}).get("available") or base
                avail *= 1 + random.gauss(-0.002, 0.01)
                rate = ((prev or {}).get("rate_h") or 0.000004) * (1 + random.gauss(0, 0.03))
                self._put(venue, now, kind="CEX", available=avail, rate_h=rate, rate_apr=apr_from_hourly(rate),
                          note="демо")
            else:
                supply = 5e6 * (1 + random.gauss(0, 0.01))
                borrow = supply * min(0.95, max(0.2, 0.6 + random.gauss(0, 0.05)))
                self._put(venue, now, kind="DeFi", available_usd=supply - borrow, rate_h=0.00001,
                          rate_apr=8.76, utilization=borrow / supply, note="демо")

    # ---- loop ----------------------------------------------------------------
    async def run(self) -> None:
        while True:
            coin = self._polling = self.coin
            if coin and self.cfg.on("borrow"):
                now = time.time()
                if self.demo:
                    self._demo(coin, now)
                else:
                    jobs = {"Binance": self.poll_binance, "OKX": self.poll_okx, "Bybit": self.poll_bybit}
                    if self.cfg.get("borrow.defi") and now - self._defi_at >= float(self.cfg.get("borrow.defi_poll_sec")):
                        self._defi_at = now
                        jobs["DeFi"] = self.poll_defi
                    results = await asyncio.gather(*(f(coin, now) for f in jobs.values()), return_exceptions=True)
                    for name, res in zip(jobs, results):
                        if isinstance(res, Exception) and coin == self.coin:
                            self.status[name] = f"ошибка: {type(res).__name__}: {res}"[:160]
                            log.info("borrow %s: %s", name, res)
            # the next poll after the interval, or at once when the main coin changes
            until = time.time() + max(10.0, float(self.cfg.get("borrow.cex_poll_sec")))
            while time.time() < until and self.coin == coin:
                await asyncio.sleep(1)
