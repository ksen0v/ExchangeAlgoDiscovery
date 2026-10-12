"""Laboratory: our own model, trained on the recorded history (LightGBM, gradient boosted trees).

Question it answers, separately for up and down: "will the price move `move_pct` % within the
next `horizon_min` minutes?" (by the minute highs / lows), from the minute snapshot of all
modules (labdata.FEATURES).

Honesty first:
  - walk-forward check: the history is cut into consecutive periods, the model learns on the
    past and is graded only on the next period it has never seen; between them a gap of
    `horizon_min` minutes so that no answer leaks into the training part;
  - verdict "ok" only if it beats chance on these unseen periods (AUC >= 0.55, the top 10 %
    of its forecasts hit >= 1.3 times more often than usual, >= 30 moves in the check);
    only then the lab raises signals;
  - relationships are measured on plain data (deciles of each feature) with a correction for
    neighbouring minutes being alike, and checked again on the last quarter of the period.

The final model is trained on all the data with the number of trees found by the check. The
live forecast explains itself: the features that pushed this minute's probability up / down.
"""
import asyncio
import logging
import math
import os
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import orjson

from app.analytics import labdata as D

log = logging.getLogger(__name__)

PARAMS = {
    "objective": "binary", "metric": "auc", "learning_rate": 0.05, "num_leaves": 15, "min_data_in_leaf": 50,
    "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
    "verbose": -1, "seed": 7, "deterministic": True,
}
MAX_ROUNDS = 500
EARLY_STOP = 50
FOLDS = 4
TOP = 0.10  # "the most confident forecasts"
LEVELS = (0.10, 0.05, 0.02)  # shares of the most confident minutes, checked separately
MIN_ROWS = 2000
RECENT_MIN = 1500  # minutes kept in memory for the live features (1440 for the daily levels)
VERSION = 1
TARGET_RU = {"up": "рост", "down": "падение"}


def lgb_module():
    """LightGBM, imported on first use (it pulls numpy / scipy: slow to load)."""
    try:
        import lightgbm
        return lightgbm
    except Exception as e:  # noqa: BLE001 - missing library or its OpenMP runtime
        log.warning("LightGBM unavailable: %s", e)
        return None


def selfcheck() -> str:
    """"ok" if LightGBM loads and trains (desktop self test of the built app)."""
    lgb = lgb_module()
    if lgb is None:
        return "LightGBM не загрузился"
    try:
        rng = np.random.default_rng(1)
        X = rng.normal(size=(600, 3))
        y = (X[:, 0] + rng.normal(scale=0.5, size=600) > 0).astype(float)
        model = _fit(lgb, X, y, {**PARAMS, "num_threads": 1, "min_data_in_leaf": 20}, rounds=10)
        p = model.predict(X[:5], pred_contrib=True)
        return "ok" if p.shape == (5, 4) else f"неожиданный ответ {p.shape}"
    except Exception as e:  # noqa: BLE001
        return f"ошибка: {e}"


# ---- metrics ---------------------------------------------------------------
def _rank(x: np.ndarray) -> np.ndarray:
    """Ranks 1..n, ties get their average rank."""
    order = np.argsort(x, kind="mergesort")
    xs = x[order]
    ends = np.concatenate([np.flatnonzero(np.diff(xs) != 0) + 1, [len(x)]])
    starts = np.concatenate([[0], ends[:-1]])
    r = np.empty(len(x))
    r[order] = np.repeat((starts + ends + 1) / 2.0, ends - starts)
    return r


def auc(y: np.ndarray, p: np.ndarray) -> float | None:
    """Area under the ROC curve: chance that a minute with a move is ranked above one without."""
    y = np.asarray(y, dtype=bool)
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return None
    r = _rank(np.asarray(p, dtype=float))
    return float((r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def top_hit(y: np.ndarray, p: np.ndarray, frac: float = TOP) -> tuple[float | None, float]:
    """(share of moves among the `frac` most confident forecasts, the threshold of that group)."""
    if not len(y):
        return None, float("nan")
    k = max(1, int(len(y) * frac))
    idx = np.argsort(-p, kind="mergesort")[:k]
    return float(np.mean(y[idx])), float(p[idx[-1]])


def episodes(ts: np.ndarray, flag: np.ndarray, gap_sec: float) -> list[int]:
    """Indexes of the first minute of each run of flagged minutes (a run ends after a pause > gap)."""
    out, last = [], None
    for i in np.flatnonzero(flag):
        if last is None or ts[i] - last > gap_sec:
            out.append(int(i))
        last = ts[i]
    return out


def calibration(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    """Forecasts split into equal groups: average forecast against how often the move happened."""
    if len(p) < bins * 20:
        return []
    order = np.argsort(p, kind="mergesort")
    out = []
    for part in np.array_split(order, bins):
        out.append({"p": float(np.mean(p[part])), "rate": float(np.mean(y[part])), "n": int(len(part)),
                    "lo": float(p[part[0]]), "hi": float(p[part[-1]])})
    return out


# ---- training --------------------------------------------------------------
def _fit(lgb, X, y, params, rounds=MAX_ROUNDS, Xv=None, yv=None):
    ds = lgb.Dataset(X, y, free_raw_data=True)
    if Xv is None:
        return lgb.train(params, ds, num_boost_round=rounds)
    dv = lgb.Dataset(Xv, yv, reference=ds)
    return lgb.train(params, ds, num_boost_round=rounds, valid_sets=[dv],
                     callbacks=[lgb.early_stopping(EARLY_STOP, verbose=False)])


def walk_forward(lgb, X, y, ts, horizon_min: int, params: dict, folds: int = FOLDS) -> dict:
    """Out-of-time check. Rows are sorted by time; returns the forecasts for the unseen periods."""
    n = len(y)
    edges = np.linspace(0, n, folds + 2).astype(int)
    gap = horizon_min * 60
    oos = np.full(n, np.nan)
    out = {"folds": [], "best": []}
    for k in range(1, folds + 1):
        a, b = edges[k], edges[k + 1]
        if b <= a:
            continue
        tr = np.flatnonzero(ts < ts[a] - gap)  # the gap: no future answer inside the training part
        if len(tr) < MIN_ROWS // 2 or y[tr].sum() < 10:
            continue
        cut = ts[tr[int(len(tr) * 0.8)]]  # last 20 % of the past: when to stop adding trees
        fit = tr[ts[tr] < cut - gap]
        val = tr[ts[tr] >= cut]
        if len(fit) < MIN_ROWS // 4 or y[fit].sum() < 5 or len(val) < 50 or y[val].sum() < 5 or y[val].all():
            continue
        model = _fit(lgb, X[fit], y[fit], params, Xv=X[val], yv=y[val])
        best = model.best_iteration or model.current_iteration()
        p = model.predict(X[a:b], num_iteration=best)
        oos[a:b] = p
        out["best"].append(int(best))
        out["folds"].append({"from": float(ts[a]), "to": float(ts[b - 1]), "rows": int(b - a),
                             "pos": int(y[a:b].sum()), "auc": auc(y[a:b], p), "trees": int(best)})
    out["oos"] = oos
    return out


def levels(y: np.ndarray, p: np.ndarray, ts: np.ndarray, fwd: np.ndarray, base: float, gap_sec: float) -> list[dict]:
    """For the 10 / 5 / 2 % most confident minutes of the check: how often the move happened.

    Neighbouring minutes give nearly the same forecast, so they are also grouped into episodes
    (a pause longer than the horizon starts a new one) and the first minute of each is graded:
    that is what a trader acting on the first alert would have got.
    """
    out = []
    for frac in LEVELS:
        hit, thr = top_hit(y, p, frac)
        if hit is None or not np.isfinite(thr):
            continue
        ep = episodes(ts, p >= thr, gap_sec)
        fr = fwd[ep] if ep else np.array([])
        out.append({"frac": frac, "thr": thr, "hit": hit, "lift": hit / base if base > 0 else None,
                    "episodes": len(ep), "episode_hit": float(np.mean(y[ep])) if ep else None,
                    "episode_fwd": float(np.nanmean(fr)) if len(fr) and np.isfinite(fr).any() else None})
    return out


def pick_level(lv: list[dict], base: float) -> dict | None:
    """The signal threshold. A signal fires on the first minute of an episode, so among the levels
    that work (>= 10 episodes, lift >= 1.5, first minutes >= 1.3x the usual rate) the one whose first
    minutes hit most often."""
    good = [x for x in lv if x["episodes"] >= 10 and (x["lift"] or 0) >= 1.5
            and x["episode_hit"] is not None and x["episode_hit"] >= 1.3 * base]
    return max(good, key=lambda x: x["episode_hit"]) if good else None


def judge(t: dict, move_pct: float, horizon_min: int) -> tuple[str, str]:
    word = "рост" if t["target"] == "up" else "падение"
    move = f"{word} на {move_pct:g}% за {horizon_min} мин"
    if t["test_pos"] < 30:
        return "few", (f"Мало случаев «{move}» на проверке ({t['test_pos']}): выводы ненадёжны. "
                       f"Нужна более долгая запись или меньший порог движения.")
    a = t.get("auc")
    lvl = t.get("signal")
    if a is not None and lvl and (a >= 0.55 or (a >= 0.53 and lvl["lift"] >= 2)):
        return "ok", (f"Закономерность есть: в {lvl['frac'] * 100:.0f}% самых уверенных минут {move} случался в "
                      f"{lvl['hit'] * 100:.0f}% случаев против обычных {t['test_base'] * 100:.0f}% "
                      f"(в {lvl['lift']:.1f} раза чаще) — на периодах, которых модель не видела.")
    lift = max([x["lift"] or 0 for x in t.get("levels") or []], default=0)
    if a is not None and a >= 0.52:
        return "weak", (f"Слабая закономерность (AUC {a:.2f}, лучшие прогнозы в {lift:.1f} раза чаще обычного, "
                        f"но это не держится стабильно): сигналы модели не включаются.")
    return "none", f"Закономерностей для «{move}», которые держатся на новых данных, не найдено."


def importance(model, names: list[str], top: int = 15) -> list[dict]:
    gain = model.feature_importance("gain")
    total = float(gain.sum()) or 1.0
    order = np.argsort(-gain)[:top]
    return [{"name": names[i], "ru": D.NAMES_RU.get(names[i], names[i]), "share": float(gain[i] / total)}
            for i in order if gain[i] > 0]


def relations(X, y_up, y_dn, fwd, ts, names: list[str], horizon_min: int, top: int = 12) -> dict:
    """Plain-data relationships: for each feature the decile (or the extreme 2 % at either end)
    where moves happen most often.

    z is the usual two-proportion test (this decile against the other minutes), divided by
    sqrt(horizon): neighbouring minutes share the same future, so there are about n / horizon
    independent cases, not n. "Stable" = the decile also beats the usual rate in the first 3/4
    and in the last 1/4 of the period separately.
    """
    n = len(ts)
    late = np.arange(n) >= int(n * 0.75)
    min_n = max(30, n // 100)
    edges = np.concatenate([[0.0, 0.02], np.linspace(0.1, 0.9, 9), [0.98, 1.0]])
    found = {"up": [], "down": []}
    for j, name in enumerate(names):
        x = X[:, j]
        ok = ~np.isnan(x)
        if ok.sum() < 10 * min_n:
            continue
        qs = np.unique(np.quantile(x[ok], edges))
        if len(qs) < 3:
            continue
        b = np.clip(np.searchsorted(qs, np.where(ok, x, qs[0]), side="right") - 1, 0, len(qs) - 2)
        for target, y in (("up", y_up), ("down", y_dn)):
            base = float(y[ok].mean())
            if base <= 0 or base >= 1:
                continue
            best = None
            for k in range(len(qs) - 1):
                m = ok & (b == k)
                nk = int(m.sum())
                rest = int(ok.sum()) - nk
                if nk < min_n or rest < min_n:
                    continue
                r, r0 = float(y[m].mean()), float(y[ok & ~m].mean())
                se = math.sqrt(base * (1 - base) * (1 / nk + 1 / rest))
                z = (r - r0) / se / math.sqrt(max(1, horizon_min)) if se > 0 else 0.0
                if best is None or z > best["z"]:
                    best = {"k": k, "m": m, "n": nk, "rate": r, "z": z}
            if best is None or best["z"] < 2 or best["rate"] < 1.3 * base:
                continue
            m = best["m"]
            stab = []
            for part in (~late, late):
                pb = y[ok & part].mean() if (ok & part).any() else 0
                pm = y[m & part]
                stab.append(float(pm.mean() / pb) if len(pm) >= 10 and pb > 0 else None)
            k = best["k"]
            fr = fwd[m]
            found[target].append({
                "name": name, "ru": D.NAMES_RU.get(name, name),
                "lo": float(qs[k]), "hi": float(qs[k + 1]),
                "q_lo": float(np.mean(x[ok] < qs[k])), "q_hi": float(np.mean(x[ok] < qs[k]) + best["n"] / ok.sum()),
                "n": best["n"], "rate": best["rate"], "base": base,
                "lift": best["rate"] / base, "z": round(best["z"], 1),
                "fwd": float(np.nanmean(fr)) if np.isfinite(fr).any() else None,
                "early": stab[0], "late": stab[1],
                "stable": all(s is not None and s >= 1.15 for s in stab),
            })
    for target in found:
        found[target].sort(key=lambda r: (-r["stable"], -r["z"]))
        found[target] = found[target][:top]
    return found


def choose_features(X: np.ndarray, span_days: float) -> list[int]:
    """Columns worth learning from: filled in >= 5 % of the minutes; the time of day only with 2+ weeks
    of history and the weekday with 4+ (with less they just tell the days apart)."""
    filled = np.mean(~np.isnan(X), axis=0)
    out = []
    for j, name in enumerate(D.FEATURES):
        if filled[j] < 0.05:
            continue
        if (name == "weekday" and span_days < 28) or (name.startswith("hour_") and span_days < 14):
            continue
        out.append(j)
    return out


def train_sync(X: np.ndarray, ts: np.ndarray, y_up, y_dn, fwd, horizon_min: int, move_pct: float,
               threads: int, progress=None) -> tuple[dict, dict]:
    """Everything heavy, in a worker thread: returns (report, {"up": booster, "down": booster})."""
    lgb = lgb_module()
    if lgb is None:
        raise RuntimeError("библиотека LightGBM не установлена (pip install lightgbm)")
    span_days = float(ts[-1] - ts[0]) / 86400 if len(ts) else 0.0
    cols = choose_features(X, span_days)
    names = [D.FEATURES[j] for j in cols]
    Xs = np.ascontiguousarray(X[:, cols])
    params = {**PARAMS, "num_threads": max(1, int(threads))}
    report = {"targets": {}, "features": names}
    models = {}
    for step, (target, y) in enumerate((("up", y_up), ("down", y_dn))):
        if progress:
            progress(f"проверка на будущих периодах: {TARGET_RU[target]}", 0.15 + 0.35 * step)
        wf = walk_forward(lgb, Xs, y, ts, horizon_min, params)
        oos = wf["oos"]
        seen = ~np.isnan(oos)
        yt, pt = y[seen], oos[seen]
        t = {"target": target, "base": float(y.mean()), "positives": int(y.sum()), "test_rows": int(seen.sum()),
             "test_pos": int(yt.sum()), "test_base": float(yt.mean()) if len(yt) else 0.0,
             "auc": auc(yt, pt) if len(yt) else None, "folds": wf["folds"]}
        t["levels"] = levels(yt, pt, ts[seen], fwd[seen], t["test_base"], horizon_min * 60) if len(yt) else []
        t["signal"] = pick_level(t["levels"], t["test_base"])
        t["thr"] = t["signal"]["thr"] if t["signal"] else None
        t["calib"] = calibration(yt, pt)
        t["verdict"], t["verdict_text"] = judge(t, move_pct, horizon_min)
        aucs = [f["auc"] for f in wf["folds"] if f["auc"] is not None]
        if t["verdict"] == "ok" and aucs and min(aucs) < 0.5:
            t["verdict_text"] += " Но на одном из периодов модель не сработала — доверяйте осторожно."
        if progress:
            progress(f"итоговая модель: {TARGET_RU[target]}", 0.35 + 0.35 * step)
        rounds = int(np.median(wf["best"]) * 1.1) if wf["best"] else 100
        t["trees"] = max(20, rounds)
        model = _fit(lgb, Xs, y, params, rounds=t["trees"])
        t["importance"] = importance(model, names)
        models[target] = model
        report["targets"][target] = t
    if progress:
        progress("взаимосвязи в данных", 0.85)
    report["relations"] = relations(Xs, y_up, y_dn, fwd, ts, names, horizon_min)
    return report, models


# ---- orchestration ---------------------------------------------------------
class Lab:
    """Recording of the minute rows for the live forecast, training in the background, saved models."""

    def __init__(self, cfg, store, recorder=None, lab_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.recorder = recorder
        self.dir = Path(lab_dir) if lab_dir else None
        self.coin = ""
        self.recent: defaultdict[str, dict[int, float]] = defaultdict(dict)
        self.models: dict = {}
        self.meta: dict | None = None
        self.live: dict | None = None
        self.status: dict = {"state": "idle", "text": "", "progress": 0.0, "error": ""}
        self.task: asyncio.Task | None = None
        self._coverage: tuple[float, dict] | None = None
        self._tried_at = 0.0

    # ---- lifecycle -------------------------------------------------------
    def reset(self, coin: str) -> None:
        self.coin = coin
        self.recent = defaultdict(dict)
        self.live = None
        self.models, self.meta = {}, None
        self._coverage = None
        self._tried_at = 0.0
        self._load_model(coin)
        if self.store is not None:
            try:
                asyncio.get_running_loop().create_task(self._load_recent(coin))
            except RuntimeError:  # no loop (tests)
                pass

    def _paths(self, coin: str) -> dict[str, Path] | None:
        if self.dir is None:
            return None
        return {"meta": self.dir / f"{coin}.json", "up": self.dir / f"{coin}.up.txt", "down": self.dir / f"{coin}.down.txt"}

    def _load_model(self, coin: str) -> None:
        paths = self._paths(coin)
        if not paths or not paths["meta"].exists():
            return
        try:
            meta = orjson.loads(paths["meta"].read_bytes())
        except (OSError, ValueError) as e:
            log.warning("lab model of %s not read: %s", coin, e)
            return
        self.meta = meta
        if meta.get("version") != VERSION or not set(meta.get("features") or []) <= set(D.FEATURES):
            self.meta["stale"] = "модель от прежней версии программы — обучите заново"
            return
        lgb = lgb_module()
        if lgb is None:
            return
        try:
            self.models = {t: lgb.Booster(model_file=str(paths[t])) for t in ("up", "down")}
        except Exception as e:  # noqa: BLE001
            log.warning("lab model of %s not loaded: %s", coin, e)
            self.models = {}

    async def _load_recent(self, coin: str) -> None:
        """The last day of minute rows from the database: the live forecast works right after a restart."""
        try:
            series = await D.load_series(self.store, coin, time.time() - RECENT_MIN * 60)
        except Exception as e:  # noqa: BLE001
            log.info("lab: recent rows not loaded: %s", e)
            return
        if coin != self.coin:
            return
        for name, d in series.items():
            d.update(self.recent.get(name, {}))  # rows recorded meanwhile win
            self.recent[name] = d

    # ---- live ------------------------------------------------------------
    def on_minute(self, ts: float, values: dict) -> None:
        """Minute row (the same values as stored) -> memory; then the live forecast."""
        m = int(ts // 60 * 60)
        for name, v in values.items():
            if v is not None and math.isfinite(v):
                self.recent[name][m] = float(v)
        lo = m - RECENT_MIN * 60
        if m % 600 == 0:
            for d in self.recent.values():
                for k in [k for k in d if k < lo]:
                    del d[k]
        self.live = self.forecast(m) if self.models and self.meta else None

    def forecast(self, m: int) -> dict | None:
        names = self.meta.get("features") or []
        ts = np.array(sorted({k for d in self.recent.values() for k in d if k >= m - RECENT_MIN * 60}), dtype=np.int64)
        if not len(ts) or ts[-1] != m:
            return None
        X = D.build(self.recent, ts)
        idx = [D.FEATURES.index(n) for n in names]
        x = X[-1:, idx]
        out = {"ts": m, "horizon_min": self.meta["horizon_min"], "move_pct": self.meta["move_pct"], "targets": {}}
        for target, model in self.models.items():
            t = self.meta["targets"][target]
            p = float(model.predict(x)[0])
            contrib = model.predict(x, pred_contrib=True)[0][:-1]
            order = np.argsort(-np.abs(contrib))[:5]
            reasons = [{"name": names[i], "ru": D.NAMES_RU.get(names[i], names[i]),
                        "value": None if np.isnan(x[0, i]) else float(x[0, i]), "effect": float(contrib[i])}
                       for i in order if abs(contrib[i]) > 1e-6]
            calib = t.get("calib") or []
            row = next((r for r in calib if p <= r["hi"]), calib[-1] if calib else None)
            hist = row["rate"] if row else None  # how often it happened at such forecasts in the check
            out["targets"][target] = {"p": p, "base": t["base"], "thr": t.get("thr"), "verdict": t["verdict"],
                                      "frac": (t.get("signal") or {}).get("frac"),
                                      "signal_hit": (t.get("signal") or {}).get("episode_hit"),
                                      "hist_rate": hist, "reasons": reasons,
                                      "strength": (p / t["thr"]) if t.get("thr") else None}
        return out

    # ---- data ------------------------------------------------------------
    def raw_dir(self) -> Path | None:
        return self.recorder.dir if self.recorder is not None else None

    async def coverage(self, coin: str) -> dict:
        """How much history there is to learn from (cached for a minute)."""
        now = time.time()
        if self._coverage and now - self._coverage[0] < 60 and self._coverage[1].get("coin") == coin:
            return self._coverage[1]
        out = {"coin": coin, "lab_days": 0.0, "metric_days": 0.0, "raw_days": 0, "coins": []}
        if self.store is not None:
            for key, metric in (("lab_days", "lab:px"), ("metric_days", "nd_all_5m")):
                n = await self.store.count(coin, metric)
                out[key] = round(n / 1440, 2)
            out["coins"] = [{"coin": c, "days": round(n / 1440, 2)} for c, n in await self.store.coins_with("nd_all_5m")]
        raw = self.raw_dir()
        if raw is not None and raw.exists():
            out["raw_days"] = sum(1 for d in raw.iterdir() if (d / f"{coin}.trades.jsonl.gz").exists())
        self._coverage = (now, out)
        return out

    async def _rows(self, coin: str, since: float, until: float, horizon_min: int, move_pct: float):
        series = await D.load_series(self.store, coin, since)
        px = series.setdefault("px", {})
        raw = self.raw_dir()
        if raw is not None and raw.exists():
            minutes = {m for d in series.values() for m in d}
            # only the days with many minutes without a price (before the lab, or the recorder was off)
            gaps = defaultdict(int)
            for m in minutes - px.keys():
                gaps[time.strftime("%Y-%m-%d", time.gmtime(m - 60))] += 1
            days = {d for d, n in gaps.items() if n > 60}
            rebuilt = await asyncio.to_thread(D.px_from_raw, raw, coin, since, until, frozenset(px), days) if days else {}
            rebuilt = {m: v for m, v in rebuilt.items() if m in minutes or m - 60 in minutes or m + 60 in minutes}
            if rebuilt:
                px.update(rebuilt)
                await self.store.add_samples([(coin, "lab:px", m, v) for m, v in rebuilt.items()])
        ts = np.array(sorted({m for d in series.values() for m in d if since <= m <= until}), dtype=np.int64)
        if not len(ts):
            return None
        X = await asyncio.to_thread(D.build, series, ts)
        y_up, y_dn, valid, fwd = await asyncio.to_thread(D.targets, px, ts, horizon_min, move_pct)
        return X[valid], ts[valid], y_up[valid], y_dn[valid], fwd[valid]

    # ---- training --------------------------------------------------------
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, horizon_min: int | None = None, move_pct: float | None = None, scope: str | None = None,
              days: float | None = None, auto: bool = False) -> None:
        if self.busy():
            raise RuntimeError("обучение уже идёт")
        pick = lambda v, path: v if v is not None else self.cfg.get(path)  # noqa: E731
        horizon = int(pick(horizon_min, "lab.horizon_min"))
        move = float(pick(move_pct, "lab.move_pct"))
        days = float(pick(days, "lab.train_days"))
        if not 1 <= horizon <= 1440 or not 0.05 <= move <= 50 or days < 1:
            raise ValueError("горизонт 1–1440 мин, движение 0.05–50%, история от 1 дня")
        scope = scope if scope in ("coin", "all") else str(self.cfg.get("lab.scope"))
        self._tried_at = time.time()
        self.status = {"state": "running", "text": "в очереди", "progress": 0.0, "error": "", "started": time.time(),
                       "auto": auto}
        self.task = asyncio.get_running_loop().create_task(self._train(self.coin, horizon, move, scope, days, auto))

    def _progress(self, text: str, frac: float) -> None:
        self.status.update(text=text, progress=round(frac, 2))

    async def _train(self, coin: str, horizon: int, move: float, scope: str, days: float, auto: bool) -> None:
        started = time.time()
        self.status.update(text="загрузка истории", progress=0.02)
        try:
            if self.store is None:
                raise RuntimeError("нет базы данных")
            until = started - 60
            since = until - days * 86400
            coins = [coin]
            if scope == "all":
                coins = [c for c, n in await self.store.coins_with("nd_all_5m") if n >= 1440] or [coin]
            parts = []
            for i, c in enumerate(coins):
                self._progress(f"загрузка истории {c}", 0.02 + 0.1 * i / len(coins))
                rows = await self._rows(c, since, until, horizon, move)
                if rows is not None and len(rows[1]):
                    parts.append(rows)
            need = float(self.cfg.get("lab.min_days"))
            if not parts:
                raise ValueError(f"мало истории: записанных минут пока нет, нужно от {need:g} дн. "
                                 f"Программа копит её сама, пока работает")
            X = np.concatenate([p[0] for p in parts])
            ts = np.concatenate([p[1] for p in parts])
            order = np.argsort(ts, kind="mergesort")
            X, ts = X[order], ts[order]
            y_up, y_dn, fwd = (np.concatenate([p[k] for p in parts])[order] for k in (2, 3, 4))
            span = float(ts[-1] - ts[0]) / 86400
            if span < need or len(ts) < MIN_ROWS:
                raise ValueError(f"мало истории: {span:.1f} дн. ({len(ts)} минут с ценой и показателями), "
                                 f"нужно от {need:g} дн. Программа копит её сама, пока работает")
            for target, y in (("up", y_up), ("down", y_dn)):
                if y.sum() < 30:
                    raise ValueError(f"за {span:.1f} дн. {TARGET_RU[target]} на {move:g}% за {horizon} мин "
                                     f"случался всего {int(y.sum())} раз — уменьшите порог движения "
                                     f"или подождите больше истории")
            report, models = await asyncio.to_thread(train_sync, X, ts, y_up, y_dn, fwd, horizon, move,
                                                     int(self.cfg.get("lab.threads")), self._progress)
            meta = {"version": VERSION, "coin": coin, "coins": coins, "scope": scope, "trained_at": time.time(),
                    "took_sec": round(time.time() - started, 1), "horizon_min": horizon, "move_pct": move,
                    "period": [float(ts[0]), float(ts[-1])], "days": round(span, 2), "rows": int(len(ts)),
                    "auto": auto, **report}
            self._save(coin, meta, models)
            if coin == self.coin:
                self.meta, self.models = meta, models
            self.status = {"state": "done", "text": "готово", "progress": 1.0, "error": "", "finished": time.time()}
            log.info("lab: %s trained in %.0f s (%d rows, %.1f days)", coin, time.time() - started, len(ts), span)
        except asyncio.CancelledError:
            raise
        except (ValueError, RuntimeError) as e:
            self.status = {"state": "error", "text": "", "progress": 0.0, "error": str(e), "finished": time.time()}
        except Exception as e:  # noqa: BLE001
            log.exception("lab training failed")
            self.status = {"state": "error", "text": "", "progress": 0.0, "error": f"ошибка: {e}", "finished": time.time()}

    def _save(self, coin: str, meta: dict, models: dict) -> None:
        paths = self._paths(coin)
        if not paths:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        for target, model in models.items():
            tmp = paths[target].with_suffix(".tmp")
            model.save_model(str(tmp))
            os.replace(tmp, paths[target])
        tmp = paths["meta"].with_suffix(".tmp")
        tmp.write_bytes(orjson.dumps(meta, option=orjson.OPT_SERIALIZE_NUMPY))
        os.replace(tmp, paths["meta"])

    async def run(self) -> None:
        """Retrains on its own every lab.auto_retrain_hours (once there is enough history)."""
        while True:
            await asyncio.sleep(600)
            try:
                hours = float(self.cfg.get("lab.auto_retrain_hours"))
                if not self.cfg.on("lab") or hours <= 0 or not self.coin or self.busy():
                    continue
                now = time.time()
                age = now - (self.meta or {}).get("trained_at", 0)
                if age < hours * 3600 or now - self._tried_at < hours * 3600 / 4:
                    continue
                cov = await self.coverage(self.coin)
                if max(cov["lab_days"], cov["metric_days"]) < float(self.cfg.get("lab.min_days")):
                    continue
                m = self.meta or {}
                self.start(m.get("horizon_min"), m.get("move_pct"), m.get("scope"), auto=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("lab auto retrain failed")

    # ---- public ----------------------------------------------------------
    def summary(self) -> dict | None:
        """The model's report for the dashboard (without the heavy parts)."""
        if not self.meta:
            return None
        return self.meta

    def health(self) -> dict:
        m = self.meta or {}
        return {"state": self.status.get("state"), "error": self.status.get("error"),
                "model": bool(self.models), "trained_at": m.get("trained_at"),
                "verdict": {t: v.get("verdict") for t, v in (m.get("targets") or {}).items()}}
