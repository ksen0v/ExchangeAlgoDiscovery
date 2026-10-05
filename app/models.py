from dataclasses import dataclass


@dataclass(slots=True)
class Trade:
    """One aggressive print, normalised to USD and to the price of 1 coin."""

    ts: float  # exchange timestamp, seconds
    price: float  # USD price of ONE coin (quote FX and 1000x-contracts removed)
    amount: float  # coins
    usd: float  # notional in USD
    side: str  # "buy" | "sell" | "?" (taker side)
    fills: int = 1  # how many exchange fills were merged into this print
    inferred: bool = False  # side computed by the tick rule, not given by the venue
    ts_local: float = 0.0  # when we received it, seconds
    tid: str = ""  # venue trade id, if any


def aggregate_fills(trades: list[Trade]) -> list[Trade]:
    """Merge fills of one taker order (same millisecond, same side) into one print.

    A single market order sweeping the book is reported as many fills; for the
    tape and for algo-pattern detection we want the order, not its fills.
    """
    out: list[Trade] = []
    for t in sorted(trades, key=lambda x: x.ts):
        last = out[-1] if out else None
        if last and last.side == t.side and abs(last.ts - t.ts) < 0.0005:
            usd = last.usd + t.usd
            amount = last.amount + t.amount
            last.price = usd / amount if amount else t.price
            last.usd, last.amount = usd, amount
            last.fills += t.fills
            last.inferred = last.inferred or t.inferred
            last.ts_local = max(last.ts_local, t.ts_local)
        else:
            out.append(Trade(t.ts, t.price, t.amount, t.usd, t.side, t.fills, t.inferred, t.ts_local, t.tid))
    return out
