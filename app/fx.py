"""Quote currency -> USD conversion.

Stablecoins are 1:1. KRW comes from Upbit's USDT/KRW (it carries the Korean premium,
which is what Korean prices are compared with). Other fiat quotes of local venues
(JPY, EUR, TRY, BRL, ...) come from public FX rates, refreshed hourly.
"""
import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)

_rates: dict[str, float] = {
    "USDT": 1.0,
    "USDC": 1.0,
    "USD": 1.0,
    "FDUSD": 1.0,
    "USD1": 1.0,
    "UST": 1.0,  # Bitfinex name for USDT
    "DUSD": 1.0,
}

QUOTE_PREFERENCE = ["USDT", "USDC", "USD", "FDUSD", "USD1", "UST", "KRW"]
# Fiat quotes used by local venues (see QUOTE_OVERRIDE in app/config.py).
FIAT = ("EUR", "GBP", "JPY", "TRY", "BRL", "IDR", "AUD", "MXN", "ZAR", "TWD", "INR", "THB", "CAD", "PLN")
FX_URLS = (
    "https://open.er-api.com/v6/latest/USD",  # {"rates": {"EUR": 0.92, ...}} = units per 1 USD
    "https://api.frankfurter.app/latest?from=USD",  # ECB, same shape
)
FIAT_EVERY = 3600.0


def usd_rate(quote: str | None) -> float | None:
    """USD value of one unit of `quote`, or None if unknown (trade is skipped)."""
    if not quote:
        return None
    return _rates.get(quote.upper())


def apply_fiat(per_usd: dict) -> int:
    """{"EUR": 0.92, ...} (units per 1 USD) -> USD per unit for the fiat we use."""
    n = 0
    for code in FIAT:
        v = per_usd.get(code)
        if isinstance(v, int | float) and v > 0:
            _rates[code] = 1.0 / v
            n += 1
    return n


async def _fetch_krw(session: aiohttp.ClientSession) -> float:
    url = "https://api.upbit.com/v1/ticker?markets=KRW-USDT"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
    return 1.0 / float(data[0]["trade_price"])


async def _fetch_fiat(session: aiohttp.ClientSession) -> int:
    for url in FX_URLS:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json(content_type=None)
            n = apply_fiat(data.get("rates") or {})
            if n:
                return n
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - try the next source
            log.warning("FX rates from %s failed: %s", url, e)
    return 0


async def run_fx_updater() -> None:
    fiat_at = 0.0
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                _rates["KRW"] = await _fetch_krw(session)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep last known rate
                log.warning("KRW rate update failed: %s", e)
            if time.time() - fiat_at > FIAT_EVERY and await _fetch_fiat(session):
                fiat_at = time.time()
            await asyncio.sleep(60)
