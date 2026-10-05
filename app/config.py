"""Service settings (env) and the list of monitored venues."""
from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000
    default_coin: str = "BTC"
    db_path: str = str(ROOT / "data" / "radar.db")
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Seconds of per-second history kept per stream (grows with window + baseline).
    history_sec: int = 900
    log_level: str = "INFO"
    # If set, the dashboard/API/WebSocket require it: open http://host:8000/?token=...
    # once (a cookie remembers it) or send "Authorization: Bearer ...".
    auth_token: str = ""
    # Alerts older than this are deleted (0 = keep forever).
    alerts_keep_days: int = 30
    # IANA zone for times in Telegram messages, e.g. Europe/Moscow ("" = server local time).
    timezone: str = ""
    # Simulated exchanges instead of real ones: to try the app without network access.
    demo: bool = False


settings = Settings()


@dataclass(frozen=True)
class Source:
    kind: str  # "ccxt" | "custom"
    id: str  # ccxt exchange id or custom adapter name


@dataclass(frozen=True)
class Venue:
    name: str
    spot: Source | None
    perp: Source | None


def _c(i: str) -> Source:
    return Source("ccxt", i)


def _x(i: str) -> Source:
    return Source("custom", i)


# Top venues by spot + derivatives volume (CoinGecko / CoinMarketCap, 2026), Binance Alpha,
# perpetual DEXes and AMM DEXes. Those not in ccxt are implemented in app/collectors/.
VENUES: list[Venue] = [
    Venue("Binance", _c("binance"), _c("binanceusdm")),
    Venue("Binance Alpha", _x("binance_alpha"), None),  # early-stage tokens in the Binance app
    Venue("Bybit", _c("bybit"), _c("bybit")),
    Venue("OKX", _c("okx"), _c("okx")),
    Venue("Coinbase", _c("coinbaseexchange"), None),
    Venue("Bitget", _c("bitget"), _c("bitget")),
    Venue("Gate", _c("gate"), _c("gate")),
    Venue("KuCoin", _c("kucoin"), _c("kucoinfutures")),
    Venue("MEXC", _c("mexc"), _c("mexc")),
    Venue("HTX", _c("htx"), _c("htx")),
    Venue("Upbit", _c("upbit"), None),
    Venue("Kraken", _c("kraken"), _c("krakenfutures")),
    Venue("BingX", _c("bingx"), _c("bingx")),
    Venue("Crypto.com", _c("cryptocom"), _c("cryptocom")),
    Venue("Hyperliquid", _c("hyperliquid"), _c("hyperliquid")),
    Venue("WhiteBIT", _c("whitebit"), _c("whitebit")),
    Venue("LBank", _c("lbank"), _c("lbank")),
    Venue("Toobit", _c("toobit"), _c("toobit")),
    Venue("WEEX", _c("weex"), _c("weex")),
    Venue("CoinW", _x("coinw"), _x("coinw")),
    Venue("Ourbit", _x("ourbit"), _x("ourbit")),
    Venue("Bitunix", None, _x("bitunix")),
    Venue("BitMart", _x("bitmart"), _x("bitmart")),
    Venue("XT.com", _c("xt"), _c("xt")),
    Venue("Phemex", _c("phemex"), _c("phemex")),
    Venue("Bithumb", _c("bithumb"), None),
    Venue("Bitfinex", _c("bitfinex"), _c("bitfinex")),
    Venue("BloFin", None, _c("blofin")),
    Venue("CoinEx", _c("coinex"), _c("coinex")),
    Venue("Bitrue", _c("bitrue"), _c("bitrue")),
    Venue("Aster", _c("aster"), _c("aster")),
    # --- more of the top-100 (CoinGecko / CoinMarketCap, 2026): global and regional CEX ---
    Venue("Binance US", _c("binanceus"), None),
    Venue("Coinbase Intl", None, _c("coinbaseinternational")),
    Venue("Bitstamp", _c("bitstamp"), None),
    Venue("Gemini", _c("gemini"), _c("gemini")),
    Venue("Bitvavo", _c("bitvavo"), None),
    Venue("Poloniex", _c("poloniex"), _c("poloniex")),
    Venue("HashKey", _c("hashkey"), _c("hashkey")),
    Venue("WOO X", _c("woo"), _c("woo")),
    Venue("BitMEX", _c("bitmex"), _c("bitmex")),
    Venue("Deribit", None, _c("deribit")),
    Venue("Backpack", _c("backpack"), _c("backpack")),
    Venue("Bullish", _c("bullish"), _c("bullish")),
    Venue("Deepcoin", _c("deepcoin"), _c("deepcoin")),
    Venue("HitBTC", _c("hitbtc"), _c("hitbtc")),
    Venue("BTSE", _c("btse"), _c("btse")),
    Venue("BYDFi", None, _c("bydfi")),
    Venue("DigiFinex", _c("digifinex"), _c("digifinex")),
    Venue("BigONE", _c("bigone"), _c("bigone")),
    Venue("Delta", None, _c("delta")),
    Venue("CEX.IO", _c("cex"), None),
    Venue("P2B", _c("p2b"), None),
    Venue("LATOKEN", _c("latoken"), None),
    Venue("Blockchain.com", _c("blockchaincom"), None),
    Venue("Pionex", _x("pionex"), _x("pionex")),
    Venue("Zoomex", None, _x("zoomex")),
    Venue("Coinone", _c("coinone"), None),
    Venue("Coincheck", _c("coincheck"), None),
    Venue("bitFlyer", _c("bitflyer"), None),
    Venue("Bitbank", _c("bitbank"), None),
    Venue("BitoPro", _c("bitopro"), None),
    Venue("BtcTurk", _c("btcturk"), None),
    Venue("Indodax", _c("indodax"), None),
    Venue("Tokocrypto", _c("tokocrypto"), None),
    Venue("CoinDCX", _x("coindcx"), None),
    Venue("Bitso", _c("bitso"), None),
    Venue("Mercado Bitcoin", _c("mercado"), None),
    Venue("BTC Markets", _c("btcmarkets"), None),
    Venue("Independent Reserve", _c("independentreserve"), None),
    Venue("Luno", _c("luno"), None),
    # --- perpetual DEX (order books on-chain / app-chains) ---
    Venue("dYdX", None, _c("dydx")),
    Venue("Paradex", None, _c("paradex")),
    Venue("Lighter", None, _c("lighter")),
    Venue("ApeX", None, _c("apex")),
    Venue("Extended", None, _c("extended")),
    Venue("GRVT", None, _c("grvt")),
    Venue("Pacifica", None, _c("pacifica")),
    Venue("WOOFi Pro", None, _c("woofipro")),
    Venue("Derive", None, _c("derive")),
    Venue("Hibachi", None, _c("hibachi")),
    # --- spot DEX (AMM pools, any chain, via GeckoTerminal) ---
    Venue("Uniswap", _x("dex_uniswap"), None),
    Venue("PancakeSwap", _x("dex_pancakeswap"), None),
    Venue("Raydium", _x("dex_raydium"), None),
    Venue("Aerodrome", _x("dex_aerodrome"), None),
    Venue("Orca", _x("dex_orca"), None),
    Venue("Meteora", _x("dex_meteora"), None),
    Venue("PumpSwap", _x("dex_pumpswap"), None),
]

# Narrow what ccxt loads on startup: we never need options / dated futures.
CCXT_OPTIONS: dict[str, dict] = {
    "binance": {"fetchMarkets": {"types": ["spot"]}},
    "binanceusdm": {"fetchMarkets": {"types": ["linear"]}},
    "bybit": {"fetchMarkets": {"types": ["spot", "linear"]}},
    "okx": {"fetchMarkets": {"types": ["spot", "swap"]}},
    "gate": {"fetchMarkets": {"types": ["spot", "swap"]}},
    "htx": {"fetchMarkets": {"types": {"spot": True, "linear": True, "inverse": False}}},
    "kucoin": {"fetchMarkets": {"types": ["spot"]}},
    "hyperliquid": {"fetchMarkets": {"types": ["spot", "swap"]}},
    "bitrue": {"fetchMarkets": {"types": ["spot", "linear"]}},
}

# Venues whose main book is not USDT: pick these quotes first.
QUOTE_OVERRIDE: dict[str, list[str]] = {
    "upbit": ["KRW", "USDT"],
    "bithumb": ["KRW", "USDT"],
    "coinbaseexchange": ["USD", "USDC", "USDT"],
    "kraken": ["USD", "USDT", "USDC"],
    "cryptocom": ["USD", "USDT"],
    "bitfinex": ["USD", "UST"],
    "binanceus": ["USD", "USDT"],
    "bitstamp": ["USD", "USDT", "EUR"],
    "gemini": ["USD", "USDT"],
    "cex": ["USD", "USDT", "EUR"],
    "bitvavo": ["EUR"],
    "coinone": ["KRW"],
    "coincheck": ["JPY"],
    "bitflyer": ["JPY"],
    "bitbank": ["JPY"],
    "bitopro": ["TWD", "USDT"],
    "btcturk": ["TRY", "USDT"],
    "indodax": ["IDR"],
    "tokocrypto": ["USDT", "IDR"],
    "bitso": ["USD", "MXN"],
    "mercado": ["BRL"],
    "btcmarkets": ["AUD"],
    "independentreserve": ["AUD", "USD", "USDT"],
    "luno": ["ZAR", "EUR", "GBP"],
}

# ccxt normally reports derivative trade amounts in contracts; these exchanges'
# parse_trade already converts to base units, so contractSize must not be applied.
AMOUNT_IN_BASE: set[str] = {"xt", "bingx", "krakenfutures"}
# Same for order-book amounts (XT converts trades but not depth, so it is not here).
BOOK_AMOUNT_IN_BASE: set[str] = {"bingx", "krakenfutures"}
