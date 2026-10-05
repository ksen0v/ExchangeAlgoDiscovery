from functools import partial

import aiohttp

from app.collectors.base import Stream, TradesCallback
from app.collectors.binance_alpha import BinanceAlphaStream
from app.collectors.bitmart import BitmartStream
from app.collectors.bitunix import BitunixStream
from app.collectors.ccxt_stream import CcxtPool, CcxtStream
from app.collectors.coinw import CoinwStream
from app.collectors.demo import DemoStream
from app.collectors.dex import DEX_IDS, DexStream
from app.collectors.ourbit import OurbitStream
from app.collectors.rest_venues import CoinDcxStream, NoPublicApi, PionexStream, ZoomexStream
from app.config import Source, settings

CUSTOM = {
    "binance_alpha": BinanceAlphaStream,
    "bitmart": BitmartStream,
    "coinw": CoinwStream,
    "bitunix": BitunixStream,
    "ourbit": OurbitStream,
    "pionex": PionexStream,
    "coindcx": CoinDcxStream,
    "zoomex": ZoomexStream,
    "noapi": NoPublicApi,
    **{f"dex_{d}": partial(DexStream, dex=d) for d in DEX_IDS},
}


def make_stream(
    venue: str,
    kind: str,
    source: Source,
    coin: str,
    on_trades: TradesCallback,
    pool: CcxtPool,
    session: aiohttp.ClientSession,
) -> Stream:
    if settings.demo:
        return DemoStream(venue, kind, coin, on_trades)
    if source.kind == "ccxt":
        return CcxtStream(venue, kind, coin, on_trades, pool, source.id)
    return CUSTOM[source.id](venue, kind, coin, on_trades, session)


__all__ = ["CcxtPool", "Stream", "make_stream"]
