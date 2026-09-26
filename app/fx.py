"""Quote currency -> USD conversion (stables are 1:1, KRW comes from Upbit)."""
import asyncio
import logging

import aiohttp

log = logging.getLogger(__name__)

_rates: dict[str, float] = {
    "USDT": 1.0,
    "USDC": 1.0,
    "USD": 1.0,
    "FDUSD": 1.0,
    "USD1": 1.0,
    "UST": 1.0,  # Bitfinex name for USDT
}

QUOTE_PREFERENCE = ["USDT", "USDC", "USD", "FDUSD", "USD1", "UST", "KRW"]


def usd_rate(quote: str | None) -> float | None:
    """USD value of one unit of `quote`, or None if unknown (trade is skipped)."""
    if not quote:
        return None
    return _rates.get(quote.upper())


async def _fetch_krw(session: aiohttp.ClientSession) -> float:
    url = "https://api.upbit.com/v1/ticker?markets=KRW-USDT"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
    return 1.0 / float(data[0]["trade_price"])


async def run_fx_updater() -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                _rates["KRW"] = await _fetch_krw(session)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep last known rate
                log.warning("KRW rate update failed: %s", e)
            await asyncio.sleep(60)
