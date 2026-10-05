"""Signal state: hysteresis and cooldown (ТЗ 3.7).

Every signal reports a strength: 1.0 = its threshold is just reached. It is shown
on a 0-100 scale where the threshold is `hysteresis_on` (70): the signal turns on
at strength >= 1 and turns off only below hysteresis_off / hysteresis_on (50/70),
so it does not flicker around the threshold. An alert is raised when a signal
turns on, at most once per `cooldown_min` for the same coin, signal and venue.
"""
from dataclasses import dataclass, field


@dataclass
class SignalSpec:
    type: str
    module: str  # "М1", "М2", ...
    title: str
    direction: int = 0  # +1 the move it points to is up, -1 down, 0 no direction


SPECS: dict[str, SignalSpec] = {s.type: s for s in [
    SignalSpec("lever_rally", "М1", "Рост на плечах", 0),
    SignalSpec("demand_rally", "М1", "Рост, подтверждённый спросом", 1),
    SignalSpec("hidden_buyer", "М1", "Скрытый покупатель", 1),
    SignalSpec("hidden_seller", "М1", "Скрытый продавец", -1),
    SignalSpec("oi_initiator", "М2", "Инициатор на бирже", 0),
    SignalSpec("oi_broad", "М2", "Широкий набор", 0),
    SignalSpec("crowd_long", "М4", "Толпа в лонгах", -1),
    SignalSpec("funding_farm", "М4", "Фандинг-ферма", 0),
    SignalSpec("liq_cascade", "М4", "Каскад", 0),
    SignalSpec("perp_leads", "М4", "Перп ведёт", 0),
    SignalSpec("tape_anomaly", "Лента", "Аномалия в ленте", 0),
]}


@dataclass
class _State:
    on: bool = False
    last_alert: float = float("-inf")
    since: float = 0.0
    payload: dict = field(default_factory=dict)


class SignalBoard:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], _State] = {}

    def reset(self) -> None:
        self.states = {}

    @staticmethod
    def score(strength: float, on_level: float) -> float:
        return max(0.0, min(100.0, strength * on_level))

    def update(self, type_: str, key: str, strength: float | None, payload: dict, now: float,
               on_level: float, off_level: float, cooldown_sec: float) -> bool:
        """Feed the current strength; True when an alert should be raised now."""
        st = self.states.get((type_, key))
        if st is None:
            st = self.states[(type_, key)] = _State()
        s = 0.0 if strength is None else strength
        if st.on:
            if s * on_level < off_level:
                st.on = False
            else:
                st.payload = payload
            return False
        if s < 1.0:
            return False
        st.on, st.since, st.payload = True, now, payload
        if now - st.last_alert < cooldown_sec:
            return False
        st.last_alert = now
        return True

    def active(self) -> list[dict]:
        out = []
        for (type_, key), st in self.states.items():
            if st.on:
                spec = SPECS[type_]
                out.append({"type": type_, "key": key, "module": spec.module, "title": st.payload.get("title")
                            or spec.title, "since": st.since, **{k: v for k, v in st.payload.items() if k != "title"}})
        return sorted(out, key=lambda x: -x["since"])
