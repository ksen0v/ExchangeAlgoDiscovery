"""М3: market regime from four directions (ТЗ, М3).

P  - price direction (+1 / 0 / -1) with a dead zone,
O  - total open interest in coins (dead zone in % of OI); None = no OI data,
Dp - sign of the normalised perp delta (|z| below the dead zone = 0),
Ds - the same for spot,
L  - significant liquidations in the window.
"""
from collections import deque
from dataclasses import dataclass


def sign(x: float | None, deadzone: float) -> int | None:
    if x is None:
        return None
    if abs(x) < deadzone:
        return 0
    return 1 if x > 0 else -1


@dataclass(frozen=True)
class Regime:
    code: str
    label: str
    tone: str  # bull | bear | flat | warn | none (badge colour)


UNDEFINED = Regime("undefined", "Неопределённо", "none")


def classify(p: int | None, o: int | None, dp: int | None, ds: int | None, liq: bool) -> Regime:
    """The table of ТЗ М3; everything else is "Неопределённо"."""
    if p is None or o is None:
        return UNDEFINED
    if p > 0 and o > 0 and dp == 1:
        if ds == 1:
            return Regime("longs_open_spot", "Открываются лонги · подтверждено спотом", "bull")
        return Regime("longs_open_fragile", "Открываются лонги · только фьючерсы, хрупко", "warn")
    if p > 0 and o < 0:
        if liq:
            return Regime("short_squeeze_liq", "Закрываются шорты (сквиз) · ликвидации шортов", "warn")
        return Regime("short_squeeze", "Закрываются шорты (сквиз)", "warn")
    if p < 0 and o > 0 and dp == -1:
        if ds == -1:
            return Regime("shorts_open_spot", "Открываются шорты · подтверждено спотом", "bear")
        return Regime("shorts_open", "Открываются шорты", "bear")
    if p < 0 and o < 0:
        if liq:
            return Regime("longs_close_liq", "Закрываются лонги · ликвидации лонгов", "bear")
        return Regime("longs_close", "Закрываются лонги", "bear")
    if p == 0 and o > 0:
        side = {1: "агрессор — покупатели", -1: "агрессор — продавцы"}.get(dp, "агрессор не виден")
        return Regime(f"accumulation_{'buy' if dp == 1 else 'sell' if dp == -1 else 'flat'}",
                      f"Набор позиций в боковике · {side}", "flat")
    if p == 0 and o < 0:
        return Regime("exit", "Выход из позиций", "flat")
    if p > 0 and o == 0 and ds == 1:
        return Regime("spot_rally", "Спотовый рост без плеча", "bull")
    return UNDEFINED


def direction_of(code: str) -> int:
    """+1 bullish, -1 bearish, 0 neutral (to show when windows disagree)."""
    if code.startswith(("longs_open", "short_squeeze", "spot_rally")):
        return 1
    if code.startswith(("shorts_open", "longs_close")):
        return -1
    return 0


class RegimeTracker:
    """Current regime per window, time in it and the history of changes."""

    def __init__(self) -> None:
        self.current: dict[str, tuple[Regime, float]] = {}  # window label -> (regime, since)
        self.history: deque[dict] = deque(maxlen=200)

    def reset(self) -> None:
        self.current = {}
        self.history.clear()

    def update(self, window: str, regime: Regime, now: float) -> None:
        prev = self.current.get(window)
        if prev and prev[0].code == regime.code:
            return
        self.current[window] = (regime, now)
        if prev:
            self.history.append({"ts": now, "window": window, "from": prev[0].label, "to": regime.label,
                                 "tone": regime.tone})

    def since(self, window: str) -> float | None:
        cur = self.current.get(window)
        return cur[1] if cur else None

    def divergence(self, short: str, long: str) -> str | None:
        """Text when the short and the long window point in opposite directions."""
        a, b = self.current.get(short), self.current.get(long)
        if not a or not b:
            return None
        da, db = direction_of(a[0].code), direction_of(b[0].code)
        if da and db and da != db:
            return f"Окна расходятся: {short} — «{a[0].label}», {long} — «{b[0].label}»"
        return None
