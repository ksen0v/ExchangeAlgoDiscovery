"""Finding the market for a coin on an exchange (handles 1000PEPE / kPEPE style bases)."""
from app.fx import QUOTE_PREFERENCE


def base_multiplier(base: str, coin: str) -> float | None:
    """How many coins one unit of `base` represents, or None if it is another asset.

    PEPE -> 1, 1000PEPE -> 1000, kPEPE -> 1000, 1MBABYDOGE -> 1e6.
    """
    b, c = base.upper(), coin.upper()
    if b == c:
        return 1.0
    if not b.endswith(c):
        return None
    prefix = b[: -len(c)]
    if prefix.isdigit():
        return float(prefix)
    if prefix == "K":
        return 1000.0
    if prefix.endswith("M") and prefix[:-1].isdigit():
        return float(prefix[:-1]) * 1_000_000
    return None


def _quote_rank(quote: str | None, prefer: list[str]) -> int:
    q = (quote or "").upper()
    order = prefer + [x for x in QUOTE_PREFERENCE if x not in prefer]
    return order.index(q) if q in order else len(order)


def pick_ccxt_market(
    markets: dict, coin: str, kind: str, prefer: list[str] | None = None
) -> tuple[dict, float] | None:
    """Best ccxt market for `coin`: kind is "spot" or "perp" (linear preferred)."""
    prefer = prefer or []
    limit = len(set(QUOTE_PREFERENCE) | set(prefer))
    best: tuple[tuple, dict, float] | None = None
    for m in markets.values():
        if m.get("active") is False:
            continue
        if kind == "spot" and not m.get("spot"):
            continue
        if kind == "perp" and not m.get("swap"):
            continue
        mult = base_multiplier(str(m.get("base") or ""), coin)
        if mult is None:
            continue
        qr = _quote_rank(m.get("quote"), prefer)
        if qr >= limit:
            continue
        # perps: prefer settlement in the quote currency (BTC/USDT:USDT over BTC/USDC:USDT)
        mixed = 0 if kind == "spot" or (m.get("settle") or m.get("quote")) == m.get("quote") else 1
        rank = (qr, mixed, 0 if m.get("linear", True) else 1, 0 if mult == 1 else 1)
        if best is None or rank < best[0]:
            best = (rank, m, mult)
    return (best[1], best[2]) if best else None
