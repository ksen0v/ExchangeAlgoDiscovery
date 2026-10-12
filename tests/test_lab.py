"""Laboratory: our own model on the recorded history (labdata / labmodel)."""
import asyncio
import gzip
import time

import numpy as np
import orjson
import pytest

from app.analytics import labdata as D
from app.analytics.config import ModulesConfig
from app.analytics.engine import Analytics
from app.analytics.labmodel import Lab, auc, episodes, relations, selfcheck, train_sync
from app.analytics.store import AnalyticsStore

T0 = 1_760_000_000 // 86400 * 86400  # midnight UTC


@pytest.fixture
def cfg(tmp_path):
    c = ModulesConfig(tmp_path / "radar.yaml")
    c.load()
    return c


def synth(days=8, seed=1, strength=1.0):
    """Random walk; 1 % of minutes a spike of the perp delta, after which the price drifts up for 15 min."""
    rng = np.random.default_rng(seed)
    n = days * 1440
    ts = T0 + 60 * np.arange(n)
    nd = rng.normal(0, 0.5, n)
    spikes = rng.random(n) < 0.01
    nd[spikes] = rng.uniform(1.5, 3, spikes.sum())
    drift = np.zeros(n)
    for i in np.flatnonzero(spikes):
        drift[i + 1:i + 16] += 0.0012 * strength
    px = 100 * np.exp(np.cumsum(rng.normal(0, 0.0015, n) + drift - drift.mean()))
    col = lambda v: dict(zip(ts.tolist(), np.asarray(v, dtype=float).tolist()))  # noqa: E731
    series = {"px": col(px), "nd_perp_5m": col(nd), "nd_all_5m": col(rng.normal(0, 0.5, n)),
              "f8_med": col(rng.normal(0.01, 0.005, n)), "vol_spot_5m": col(rng.lognormal(10, 1, n)),
              "vol_perp_5m": col(rng.lognormal(11, 1, n))}
    return series, ts


# ---- data --------------------------------------------------------------------------
def test_targets_use_future_highs_and_lows():
    ts = T0 + 60 * np.arange(6)
    px = {int(t): p for t, p in zip(ts, [100, 100.5, 101.2, 99.6, 100, 100])}
    up, down, valid, fwd = D.targets(px, ts, 3, 1.0)
    assert valid.tolist() == [True, True, True, False, False, False]  # the last 3 minutes have no future
    assert up.tolist()[:3] == [1, 0, 0]  # 100 -> 101.2 (+1.2 %) within 3 min
    assert down.tolist()[:3] == [0, 0, 1]  # 101.2 -> 100 is -1.19 %
    assert fwd[0] == pytest.approx(-0.4)  # 100 -> 99.6 after exactly 3 min


def test_build_derived_features():
    ts = T0 + 60 * np.arange(70)
    px = {int(t): 100 * (1.001 ** i) for i, t in enumerate(ts)}
    X = D.build({"px": px, "vol_spot_5m": {int(ts[-1]): 300.0}, "vol_perp_5m": {int(ts[-1]): 700.0},
                 "ctm2_up": {int(ts[-1]): 2e5}, "ctm2_down": {int(ts[-1]): 1e5}}, ts)
    assert X.shape == (70, len(D.FEATURES))
    f = {n: X[-1, i] for i, n in enumerate(D.FEATURES)}
    assert f["ret_5"] == pytest.approx((1.001 ** 5 - 1) * 100)
    assert f["ret_60"] == pytest.approx((1.001 ** 60 - 1) * 100)
    assert np.isnan(X[0, D.FEATURES.index("ret_1")])  # no minute before the first one
    assert f["spot_share"] == pytest.approx(0.3)
    assert f["ctm_imb"] == pytest.approx(np.log(2))
    assert f["rv_15"] == pytest.approx(0, abs=1e-9)  # constant +0.1 % a minute: no volatility
    assert set(D.FEATURES).isdisjoint(D.NOT_FEATURES)  # absolute amounts never enter the model


def test_px_from_raw_takes_the_last_trades_before_the_minute(tmp_path):
    day = time.strftime("%Y-%m-%d", time.gmtime(T0))
    (tmp_path / day).mkdir()
    rows = [(T0 + 5, 90.0), (T0 + 50, 100.0), (T0 + 55, 101.0), (T0 + 58, 102.0),  # -> boundary T0 + 60
            (T0 + 61, 50.0), (T0 + 62, 51.0), (T0 + 63, 52.0)]  # -> T0 + 120 (whole minute, none in its last 15 s)
    with gzip.open(tmp_path / day / "XYZ.trades.jsonl.gz", "wb") as f:
        for ts, p in rows:
            f.write(orjson.dumps({"ts_exchange": int(ts * 1000), "price": p * 1300, "qty": 2, "notional_usd": 2 * p}) + b"\n")
    px = D.px_from_raw(tmp_path, "XYZ", T0, T0 + 600)
    assert px == {T0 + 60: 101.0, T0 + 120: 51.0}  # USD price (notional / qty), not the KRW quote
    assert D.px_from_raw(tmp_path, "XYZ", T0, T0 + 600, skip={T0 + 60}) == {T0 + 120: 51.0}


# ---- model ---------------------------------------------------------------------------
def test_auc_and_episodes():
    assert auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.3, 0.4])) == 1.0
    assert auc(np.array([0, 1, 0, 1]), np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    assert auc(np.array([1, 1]), np.array([0.1, 0.2])) is None
    ts = np.array([0, 60, 120, 600, 660, 2000])
    assert episodes(ts, np.array([1, 1, 0, 1, 1, 1], dtype=bool), 300) == [0, 3, 5]


def _train(series, ts):
    X = D.build(series, ts)
    up, dn, valid, fwd = D.targets(series["px"], ts, 15, 1.0)
    return train_sync(X[valid], ts[valid], up[valid], dn[valid], fwd[valid], 15, 1.0, 2)


def test_model_finds_a_planted_pattern_and_only_where_it_is():
    report, models = _train(*synth())
    up, down = report["targets"]["up"], report["targets"]["down"]
    assert up["verdict"] == "ok" and up["auc"] > 0.55 and up["signal"]["lift"] > 1.5
    assert down["verdict"] in ("none", "weak")  # the pattern only pushes the price up
    assert {i["name"] for i in up["importance"][:3]} & {"nd_perp_5m", "ret_5", "ret_15"}
    rel = {r["name"]: r for r in report["relations"]["up"]}
    assert "nd_perp_5m" in rel and rel["nd_perp_5m"]["q_lo"] >= 0.9 and rel["nd_perp_5m"]["stable"]
    assert "hour_sin" not in report["features"]  # 8 days: the time of day would only tell the days apart
    assert set(models) == {"up", "down"}


def test_model_finds_nothing_in_noise():
    report, _ = _train(*synth(seed=3, strength=0.0))
    assert {t["verdict"] for t in report["targets"].values()} <= {"none", "weak"}
    assert all(t["signal"] is None or t["verdict"] != "ok" for t in report["targets"].values())
    assert report["relations"] == {"up": [], "down": []}


def test_relations_need_enough_minutes():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, 1))
    y = (X[:, 0] > 1).astype(float)
    assert relations(X, y, y, np.zeros(200), np.arange(200) * 60, ["x"], 15) == {"up": [], "down": []}


def test_selfcheck():
    assert selfcheck() == "ok"


# ---- laboratory end to end --------------------------------------------------------------
def test_lab_trains_saves_and_forecasts(cfg, tmp_path):
    series, ts = synth(days=6)
    now = float(ts[-1]) + 120

    async def run():
        store = AnalyticsStore(str(tmp_path / "a.db"))
        await store.open()
        rows = [("XYZ", D.metric_name(name), int(t), v) for name, d in series.items() for t, v in d.items()]
        await store.add_samples(rows)
        lab = Lab(cfg, store, None, tmp_path / "lab")
        lab.coin = "XYZ"
        cov = await lab.coverage("XYZ")
        lab.start(horizon_min=15, move_pct=1.0, scope="coin", days=30)
        with pytest.raises(RuntimeError):
            lab.start()  # one training at a time
        await lab.task
        await store.close()
        return lab, cov

    orig = time.time
    time.time = lambda: now  # the synthetic history ends "now"
    try:
        lab, cov = asyncio.run(run())
    finally:
        time.time = orig
    assert cov["lab_days"] == pytest.approx(6, abs=0.01) and cov["metric_days"] == pytest.approx(6, abs=0.01)
    assert lab.status["state"] == "done", lab.status
    assert lab.meta["targets"]["up"]["verdict"] == "ok" and lab.meta["horizon_min"] == 15
    assert (tmp_path / "lab" / "XYZ.json").exists() and (tmp_path / "lab" / "XYZ.up.txt").exists()

    # a fresh laboratory loads the saved model; the live forecast explains itself
    lab2 = Lab(cfg, None, None, tmp_path / "lab")
    lab2.reset("XYZ")
    assert set(lab2.models) == {"up", "down"} and lab2.meta["coin"] == "XYZ"
    last = [int(t) for t in ts[-200:]]
    for name, d in series.items():
        lab2.recent[name] = {t: d[t] for t in last}
    m = last[-1] + 60
    spike = {name: d[last[-1]] for name, d in series.items()}
    spike["nd_perp_5m"] = 2.5
    lab2.on_minute(m, spike)
    live = lab2.live
    assert live and live["ts"] == m and set(live["targets"]) == {"up", "down"}
    up = live["targets"]["up"]
    assert 0 < up["p"] < 1 and up["reasons"] and up["reasons"][0]["ru"]
    assert any(r["name"] == "nd_perp_5m" and r["effect"] > 0 for r in up["reasons"])


def test_lab_says_when_history_is_short(cfg, tmp_path):
    async def run():
        store = AnalyticsStore(str(tmp_path / "a.db"))
        await store.open()
        now = time.time()
        await store.add_samples([("XYZ", "lab:px", int(now // 60 * 60 - 60 * i), 1.0) for i in range(100)])
        lab = Lab(cfg, store, None, tmp_path / "lab")
        lab.coin = "XYZ"
        lab.start()
        await lab.task
        await store.close()
        return lab

    lab = asyncio.run(run())
    assert lab.status["state"] == "error" and "мало истории" in lab.status["error"]
    assert lab.meta is None and not (tmp_path / "lab" / "XYZ.json").exists()


def test_prune_keeps_the_laboratory_history(tmp_path):
    async def run():
        store = AnalyticsStore(str(tmp_path / "a.db"))
        await store.open()
        old, older = 1_000, 500
        await store.add_samples([("X", "doi5:Binance", old, 1.0), ("X", "nd_all_5m", old, 1.0), ("X", "lab:px", old, 1.0),
                                 ("X", "nd_all_5m", older, 1.0), ("X", "lab:px", older, 1.0)])
        await store.prune_samples(2_000, ("nd_all_5m",), 800)
        left = {(m, t) for m in ("doi5:Binance", "nd_all_5m", "lab:px") for t, _ in await store.samples("X", m, 0)}
        await store.close()
        return left

    assert asyncio.run(run()) == {("nd_all_5m", 1_000), ("lab:px", 1_000)}


def test_engine_records_the_lab_row_every_minute(cfg):
    a = Analytics(cfg, None, None)
    a.reset("XYZ")
    a.baselines.coin = "XYZ"
    now = 3_000_000.0
    a.tick(now, 1.23, {})
    a.tick(now + 60, 1.25, {})
    stored = {m for _, m, _, _ in a.baselines.pending}
    assert {"lab:px", "lab:sig_up", "lab:sig_down"} <= stored
    assert a.lab.recent["px"][int((now + 60) // 60 * 60)] == 1.25
    assert a.snapshot["lab"] is None and a.snapshot["modules"]["lab"] is True  # no model yet: no forecast
    assert [s for s in a._sig_lab(now) if s[2] is not None] == []  # and no lab signals


def test_coins_with_a_metric(tmp_path):
    async def run():
        store = AnalyticsStore(str(tmp_path / "a.db"))
        await store.open()
        await store.add_samples([("AAA", "nd_all_5m", t, 1.0) for t in range(3)] + [("BBB", "nd_all_5m", 0, 1.0),
                                 ("CCC", "lab:px", 0, 1.0)])
        out = await store.coins_with("nd_all_5m")
        await store.close()
        return out

    assert asyncio.run(run()) == [("AAA", 3), ("BBB", 1)]


def test_lab_rebuilds_the_price_from_raw_trades(cfg, tmp_path):
    """Minutes recorded before the laboratory have metrics but no price: it comes from data/raw and is stored."""
    day = time.strftime("%Y-%m-%d", time.gmtime(T0))
    (tmp_path / "raw" / day).mkdir(parents=True)
    with gzip.open(tmp_path / "raw" / day / "XYZ.trades.jsonl.gz", "wb") as f:
        for i in range(120):
            for k in range(3):
                ts = T0 + 60 * i + 50 + k
                f.write(orjson.dumps({"ts_exchange": ts * 1000, "qty": 1, "notional_usd": 100 + i}) + b"\n")

    class Rec:
        dir = tmp_path / "raw"

    async def run():
        store = AnalyticsStore(str(tmp_path / "a.db"))
        await store.open()
        await store.add_samples([("XYZ", "nd_all_5m", T0 + 60 * i, 0.1) for i in range(1, 121)])
        lab = Lab(cfg, store, Rec(), None)
        rows = await lab._rows("XYZ", T0, T0 + 7200, 5, 1.0)
        stored = await store.samples("XYZ", "lab:px", 0)
        await store.close()
        return rows, stored

    rows, stored = asyncio.run(run())
    assert len(stored) == 120 and stored[0] == (T0 + 60, 100.0)  # trades of minute 0 -> price at its end
    X, ts, up, down, fwd = rows
    assert len(ts) > 100 and up.sum() > 0  # +1 $ a minute on 100 $: >= 1 % within 5 minutes
