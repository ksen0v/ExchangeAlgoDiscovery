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


# Top-30 venues by spot + derivatives volume (CoinGecko / CoinMarketCap, 2026) + Binance Alpha.
# Five of them are not in ccxt and are implemented by hand in app/collectors/.
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
}

# ccxt normally reports derivative trade amounts in contracts; these exchanges'
# parse_trade already converts to base units, so contractSize must not be applied.
AMOUNT_IN_BASE: set[str] = {"xt", "bingx", "krakenfutures"}
# Same for order-book amounts (XT converts trades but not depth, so it is not here).
BOOK_AMOUNT_IN_BASE: set[str] = {"bingx", "krakenfutures"}
