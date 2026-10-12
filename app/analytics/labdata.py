"""Laboratory data: one row of features per minute and what the price did next.

The same code builds rows for training (history) and for the live forecast (the last
minute), so the model sees the same inputs in both (ТЗ М15: one code base for live and
history).

Inputs per minute (all from the main coin, stored in analytics.db `samples`):
  - metrics recorded since phase 0 (normalised delta, funding, basis, liquidations, book cost to move)
  - "lab:*" metrics recorded since the laboratory (price, OI, signals, DEX, Hyperliquid, ...)
  - the price may be rebuilt from the raw trade files (data/raw) for minutes recorded before
Derived here: past returns, realised volatility, volume / book / liquidations against their
own 24-hour level, time of day. Absolute dollar amounts never enter the model (they differ
between coins and over time) - only their normalised forms.
"""
import gzip
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import orjson

WINDOWS = ("1m", "5m", "15m", "1h")
# recorded every minute since phase 0 / 2 under these names
STORED = ([f"nd_{k}_{w}" for k in ("spot", "perp", "all") for w in WINDOWS]
          + ["vol_spot_5m", "vol_perp_5m", "f8_med", "basis_x", "d_basis_5m", "d_prem_5m", "liq_5m",
             "ctm2_up", "ctm2_down"])
# recorded as "lab:<name>" since the laboratory
LAB = ["px", "oi_pct_5m", "oi_pct_15m", "oi_pct_1h", "oi_up_venues", "oi_z_max", "f8_spread", "premium",
       "liq_long_5m_n", "liq_short_5m_n", "spoof_bid", "spoof_ask", "defended",
       "icebergs_30m", "borrow_ratio_max", "borrow_chg4h_min", "index_dev_max", "dex_prem", "dex_lead",
       "dex_cheap5", "hl_whale_net15_n", "hl_cluster_long_n", "hl_cluster_short_n", "hl_twap_net",
       "regime_P", "regime_O", "regime_Dp", "regime_Ds", "sig_up", "sig_down"]
# absolute amounts: only their normalised forms are features
NOT_FEATURES = {"px", "vol_spot_5m", "vol_perp_5m", "liq_5m", "ctm2_up", "ctm2_down"}

NAMES_RU = {
    **{f"nd_{k}_{w}": f"Дельта {name} за {w.replace('m', 'м').replace('h', 'ч')} (норм.)"
       for k, name in (("spot", "спота"), ("perp", "фьючерсов"), ("all", "всех рынков")) for w in WINDOWS},
    "f8_med": "Фандинг (медиана, к 8ч)", "basis_x": "Базис перп/спот", "d_basis_5m": "Изменение базиса за 5м",
    "d_prem_5m": "Изменение премии mark/index за 5м", "oi_pct_5m": "ОИ всех бирж за 5м, %",
    "oi_pct_15m": "ОИ всех бирж за 15м, %", "oi_pct_1h": "ОИ всех бирж за 1ч, %",
    "oi_up_venues": "Бирж с ростом ОИ за 5м", "oi_z_max": "Сильнейший всплеск ОИ на одной бирже (z)",
    "f8_spread": "Разброс фандинга между биржами", "premium": "Премия mark/index",
    "liq_long_5m_n": "Ликвидации лонгов за 5м (к глубине)", "liq_short_5m_n": "Ликвидации шортов за 5м (к глубине)",
    "spoof_bid": "Исчезающие заявки на покупку за час", "spoof_ask": "Исчезающие заявки на продажу за час",
    "defended": "Защищаемых уровней", "icebergs_30m": "Айсбергов за 30 мин",
    "borrow_ratio_max": "Ставка займа к норме", "borrow_chg4h_min": "Изменение доступного к займу за 4ч",
    "index_dev_max": "Отклонение площадки в индексе", "dex_prem": "Цена DEX к биржам, %",
    "dex_lead": "Кто ведёт: сдвиг DEX, с", "dex_cheap5": "Насколько DEX дешевле двигать (лог)",
    "hl_whale_net15_n": "Киты Hyperliquid: набор за 15м (к глубине)",
    "hl_cluster_long_n": "Кластер ликвидаций лонгов рядом (досягаемость)",
    "hl_cluster_short_n": "Кластер ликвидаций шортов рядом (досягаемость)", "hl_twap_net": "TWAP китов: покупки − продажи",
    "regime_P": "Режим 5м: цена", "regime_O": "Режим 5м: ОИ", "regime_Dp": "Режим 5м: дельта фьючерсов",
    "regime_Ds": "Режим 5м: дельта спота", "sig_up": "Активных сигналов вверх", "sig_down": "Активных сигналов вниз",
    "ret_1": "Цена за 1м, %", "ret_5": "Цена за 5м, %", "ret_15": "Цена за 15м, %", "ret_60": "Цена за 1ч, %",
    "rv_15": "Волатильность за 15м", "rv_60": "Волатильность за 1ч",
    "vol_x": "Объём за 5м к своему суточному уровню", "spot_share": "Доля спота в объёме",
    "ctm_imb": "Перекос стакана: вверх против вниз (лог)", "ctm_x": "Глубина стакана к своему уровню",
    "liq_x": "Ликвидации за 5м к своему уровню", "hour_sin": "Время суток (sin)", "hour_cos": "Время суток (cos)",
    "weekday": "День недели",
}
DERIVED = ["ret_1", "ret_5", "ret_15", "ret_60", "rv_15", "rv_60", "vol_x", "spot_share", "ctm_imb", "ctm_x",
           "liq_x", "hour_sin", "hour_cos", "weekday"]
FEATURES = [n for n in STORED + LAB if n not in NOT_FEATURES] + DERIVED


def metric_name(name: str) -> str:
    return name if name in STORED else f"lab:{name}"


async def load_series(store, coin: str, since: float) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {}
    for name in STORED + LAB:
        rows = await store.samples(coin, metric_name(name), since)
        if rows:
            out[name] = {int(ts): float(v) for ts, v in rows}
    return out


def px_from_raw(raw_dir: Path, coin: str, since: float, until: float, skip=frozenset(),
                days: set[str] | None = None) -> dict[int, float]:
    """Price at each minute boundary rebuilt from the recorded trade files (minutes before the lab).

    The live price is recorded at the first second of a minute, so here too: the median USD price
    of the last trades before the boundary (last 15 s, else the whole previous minute; >= 3 trades,
    all venues). Boundaries in `skip` (already recorded) are not rebuilt; only the `days` given
    ("YYYY-MM-DD") are read, if given.
    """
    out: dict[int, float] = {}
    day = datetime.fromtimestamp(since - 60, timezone.utc).date()
    last = datetime.fromtimestamp(until, timezone.utc).date()
    while day <= last:
        path = raw_dir / day.isoformat() / f"{coin}.trades.jsonl.gz"
        if path.exists() and (days is None or day.isoformat() in days):
            buckets: defaultdict[int, list[tuple[float, float]]] = defaultdict(list)
            try:
                with gzip.open(path, "rb") as f:
                    for line in f:
                        try:
                            r = orjson.loads(line)
                            ts = r["ts_exchange"] / 1000
                            qty, usd = float(r["qty"]), float(r["notional_usd"])
                        except (ValueError, KeyError, TypeError):
                            continue
                        if qty <= 0 or usd <= 0:
                            continue
                        m = (int(ts) // 60 + 1) * 60  # the boundary after the trade
                        if m not in skip and since <= m <= until:
                            buckets[m].append((ts, usd / qty))
            except (OSError, EOFError):  # a file still being written may end mid-member
                pass
            for m, rows in buckets.items():
                near = [p for t, p in rows if t >= m - 15]
                prices = sorted(near if len(near) >= 3 else [p for _, p in rows])
                if len(prices) >= 3:
                    out[m] = prices[len(prices) // 2]
        day += timedelta(days=1)
    return out


def _col(series: dict[str, dict[int, float]], name: str, ts: np.ndarray) -> np.ndarray:
    d = series.get(name) or {}
    return np.array([d.get(int(t), np.nan) for t in ts], dtype=float)


def _lag(x: np.ndarray, ts: np.ndarray, k: int) -> np.ndarray:
    """x at t - k minutes (NaN where that minute is missing)."""
    pos = {int(t): i for i, t in enumerate(ts)}
    out = np.full(len(x), np.nan)
    for i, t in enumerate(ts):
        j = pos.get(int(t) - 60 * k)
        if j is not None:
            out[i] = x[j]
    return out


def _trailing_mean(x: np.ndarray, n: int) -> np.ndarray:
    """Mean of the previous n values (NaN ignored)."""
    v = np.where(np.isnan(x), 0.0, x)
    c = np.where(np.isnan(x), 0.0, 1.0)
    cs, cc = np.concatenate([[0.0], np.cumsum(v)]), np.concatenate([[0.0], np.cumsum(c)])
    idx = np.arange(len(x))
    lo = np.maximum(0, idx - n)
    s, k = cs[idx] - cs[lo], cc[idx] - cc[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(k >= max(5, n // 20), s / np.maximum(k, 1), np.nan)


def _trailing_std(x: np.ndarray, n: int) -> np.ndarray:
    m = _trailing_mean(x, n)
    m2 = _trailing_mean(x * x, n)
    with np.errstate(invalid="ignore"):
        return np.sqrt(np.maximum(m2 - m * m, 0.0))


def build(series: dict[str, dict[int, float]], ts: np.ndarray) -> np.ndarray:
    """Feature matrix (rows = minutes `ts`, sorted; columns = FEATURES)."""
    ts = np.asarray(ts, dtype=np.int64)
    cols = {n: _col(series, n, ts) for n in STORED + LAB}
    px = cols["px"]
    with np.errstate(invalid="ignore", divide="ignore"):
        for k in (1, 5, 15, 60):
            cols[f"ret_{k}"] = (px / _lag(px, ts, k) - 1) * 100
        r1 = cols["ret_1"]
        cols["rv_15"] = _trailing_std(r1, 15)
        cols["rv_60"] = _trailing_std(r1, 60)
        vol = cols["vol_spot_5m"] + cols["vol_perp_5m"]
        lv = np.log1p(vol)
        cols["vol_x"] = lv - _trailing_mean(lv, 1440)
        cols["spot_share"] = cols["vol_spot_5m"] / vol
        up, dn = cols["ctm2_up"], cols["ctm2_down"]
        cols["ctm_imb"] = np.log(up / dn)
        lb = np.log1p(up + dn)
        cols["ctm_x"] = lb - _trailing_mean(lb, 1440)
        ll = np.log1p(cols["liq_5m"])
        cols["liq_x"] = ll - _trailing_mean(ll, 1440)
    hours = (ts % 86400) / 3600.0
    cols["hour_sin"] = np.sin(2 * math.pi * hours / 24)
    cols["hour_cos"] = np.cos(2 * math.pi * hours / 24)
    cols["weekday"] = ((ts // 86400) + 3) % 7.0  # 1970-01-01 was a Thursday: 0 = Monday
    X = np.column_stack([cols[n] for n in FEATURES])
    X[~np.isfinite(X)] = np.nan
    return X


def targets(px: dict[int, float], ts: np.ndarray, horizon_min: int, move_pct: float):
    """Did the price go `move_pct` % up / down within the next `horizon_min` minutes (highs and lows)?

    Returns (y_up, y_down, valid, fwd_ret) - valid where >= 80 % of the future minutes are known.
    """
    n = len(ts)
    y_up, y_dn = np.zeros(n), np.zeros(n)
    valid = np.zeros(n, dtype=bool)
    fwd = np.full(n, np.nan)
    for i, t in enumerate(ts):
        p0 = px.get(int(t))
        if not p0:
            continue
        fut = [px.get(int(t) + 60 * k) for k in range(1, horizon_min + 1)]
        known = [p for p in fut if p]
        if len(known) < 0.8 * horizon_min:
            continue
        valid[i] = True
        y_up[i] = (max(known) / p0 - 1) * 100 >= move_pct
        y_dn[i] = (min(known) / p0 - 1) * 100 <= -move_pct
        if fut[-1]:
            fwd[i] = (fut[-1] / p0 - 1) * 100
    return y_up, y_dn, valid, fwd
