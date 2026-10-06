"""Module settings (ТЗ, раздел 6) in one YAML file, re-read on change without a restart.

The file is created with the defaults and Russian comments on the first start. A
wrong value (a word instead of a number, ...) is not applied: the default stays and
the error is shown on the "Здоровье" screen. Saving from the dashboard rewrites the
file with the same comments.
"""
import copy
import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# (path, default, comment). Order = order in the file; a path ending with "." is a section.
SCHEMA: list[tuple[str, object, str]] = [
    ("collect_only", False, "только сбор данных: метрики, журнал и запись идут, алертов нет (для калибровки)"),
    ("modules.", None, "модули: true — включён, false — выключен"),
    ("modules.delta", True, "М1 — дельта спот против фьючерсов"),
    ("modules.open_interest", True, "М2 — открытый интерес по каждой бирже"),
    ("modules.regime", True, "М3 — режим рынка (бейдж 1м/5м/15м)"),
    ("modules.funding", True, "М4 — фандинг, премия mark/index, базис"),
    ("modules.liquidations", True, "М4 — ликвидации"),
    ("modules.orderbook", True, "М5 — стаканы: глубина, цена сдвига, пустоты, айсберги, исчезающие заявки"),
    ("modules.borrow", True, "М6 — займы для шортов (Binance — нужен ключ только на чтение в .env)"),
    ("modules.index", True, "М7 — составляющие индекса (mark/index) Binance, OKX, Bybit"),
    ("modules.journal", True, "М15 — журнал сигналов и что было дальше"),
    ("modules.recorder", True, "запись сырых данных на диск (сделки, стаканы, ОИ, фандинг, ликвидации)"),
    ("windows", ["1m", "5m", "15m", "1h"], "окна расчёта"),
    ("baseline_days", [7, 30], "базовые линии: за сколько дней"),
    ("alert_z", 3.0, "порог робастного z-score для сигналов"),
    ("min_baseline_samples", 60, "меньше замеров — норма не считается, сигналы по перцентилям молчат"),
    ("regime.", None, "М3 — классификатор режима"),
    ("regime.window", "5m", "основное окно бейджа (дополнительно всегда считаются 1м и 15м)"),
    ("regime.price_deadzone_pct", 0.15, "цена «на месте», если изменение меньше, %"),
    ("regime.price_deadzone_typical", 0.0,
     "доля от типичного хода монеты за окно (медиана |Δцены|); 0 — только фиксированная зона выше"),
    ("regime.oi_deadzone_pct", 0.3, "ОИ «на месте», если изменение меньше, % от ОИ"),
    ("regime.delta_deadzone_z", 1.0, "дельта «нулевая», если |z| меньше"),
    ("regime.liq_significant_z", 2.0, "ликвидации на окне значимы, если z больше"),
    ("delta.", None, "М1 — дельта"),
    ("delta.signal_window", "5m", "окно, на котором подаются сигналы М1"),
    ("delta.price_threshold_pct", 0.3, "«Рост на плечах»: цена выросла больше, %"),
    ("delta.absorb_price_pct", 0.1, "«Скрытый покупатель/продавец»: цена сдвинулась меньше, %"),
    ("delta.venue_weights", {}, "вес биржи в сумме (до М12 у всех 1), например {WEEX: 0.5}"),
    ("open_interest.", None, "М2 — открытый интерес"),
    ("open_interest.binance_poll_sec", 5, "как часто спрашивать ОИ у Binance, сек"),
    ("open_interest.poll_sec", 10, "у остальных бирж, сек"),
    ("open_interest.common_grid_sec", 5, "общая сетка, к которой приводятся ОИ всех бирж, сек"),
    ("open_interest.broad_min_venues", 3, "«Широкий набор»: ОИ растёт одновременно на стольких биржах"),
    ("funding.", None, "М4 — фандинг, базис, ликвидации"),
    ("funding.poll_sec", 30, "как часто спрашивать фандинг и mark/index, сек"),
    ("funding.farm_max_move_pct", 1.5, "«Фандинг-ферма»: цена «ползёт», если выросла меньше, % за 15 мин"),
    ("funding.big_liquidation_usd_depth", 0.05,
     "ликвидация «крупная» (отдельно в ленте), если больше этой доли depth_1% фьючерсов"),
    ("orderbook.", None, "М5 — стакан"),
    ("orderbook.snapshot_sec", 1, "разбирать стакан биржи не чаще, чем раз в N сек"),
    ("orderbook.bucket_pct", 0.1, "ширина корзины карты глубины, % (до ±5%)"),
    ("orderbook.depth_levels_pct", [0.5, 1, 2, 5], "для каких X% считать depth_X% и цену сдвига"),
    ("orderbook.gap_share_of_median", 0.2, "корзина «пустая», если в ней меньше этой доли медианы корзин"),
    ("orderbook.iceberg_exec_to_visible", 1.5, "айсберг: исполнено на цене больше, чем столько × видимого"),
    ("orderbook.iceberg_window_sec", 10, "айсберг: за сколько секунд считать исполненное"),
    ("orderbook.recovery_fast_sec", 2, "уровень «защищают», если съеденный лучший уровень вернулся за N сек"),
    ("orderbook.defended_min_events", 3, "«Защищаемый уровень»: столько айсбергов/восстановлений на одной цене"),
    ("orderbook.defended_window_min", 30, "за сколько минут их считать"),
    ("orderbook.spoof_size_pctl", 99, "исчезающая заявка: крупнее этого перцентиля уровней биржи"),
    ("orderbook.spoof_cancel_distance_pct", 0.25, "и снята без исполнения ближе этого расстояния до цены, %"),
    ("orderbook.spoof_alert_count", 3, "«Ложная стена»: столько исчезающих заявок на стороне за час (и вдвое больше другой стороны)"),
    ("borrow.", None, "М6 — займы для шортов"),
    ("borrow.cex_poll_sec", 60, "опрос бирж, сек"),
    ("borrow.defi_poll_sec", 180, "опрос DeFi (Aave, Morpho, Euler, Kamino… через DefiLlama), сек"),
    ("borrow.defi", True, "смотреть DeFi-протоколы займов"),
    ("borrow.inventory_drop_pct_4h", 50, "«Займ иссякает»: доступный объём упал больше чем на N% за 4ч"),
    ("borrow.rate_ratio_alert", 3, "или ставка выше медианы за 30 дней в N раз"),
    ("index.", None, "М7 — составляющие индекса"),
    ("index.constituents_refresh_min", 30, "как часто обновлять состав индекса, мин"),
    ("index.dev_alert_pct", 0.3, "«Индекс тянут»: площадка отклонилась от остальных больше, %"),
    ("index.dev_persist_sec", 10, "и держится дольше, сек"),
    ("index.protection_binance", "clamp:5", "защита индекса Binance: clamp:N — цена обрезается до ±N% от медианы, exclude:N — исключается, none"),
    ("index.protection_okx", "clamp:3", "OKX (по их правилам: ±3% от медианы)"),
    ("index.protection_bybit", "clamp:5", "Bybit"),
    ("dex.", None, "М8 — DEX (фаза 3)"),
    ("dex.price_poll_sec", 3, ""),
    ("dex.depth_poll_min", 3, ""),
    ("dex.leadlag_max_lag_sec", 30, ""),
    ("dex.leadlag_window_min", 15, ""),
    ("dex.arb_match_window_sec", 3, ""),
    ("dex.cheap_move_ratio", 3, ""),
    ("hyperliquid.", None, "М9 — Hyperliquid (фаза 3)"),
    ("hyperliquid.positions_poll_sec", 45, ""),
    ("hyperliquid.top_addresses_per_coin", 50, ""),
    ("hyperliquid.liq_bucket_pct", 0.5, ""),
    ("hyperliquid.target_distance_pct", 3, ""),
    ("algo.", None, "М10 — отпечаток алгоритма (фаза 4)"),
    ("algo.window_min", 10, ""),
    ("algo.step_sec", 30, ""),
    ("algo.interval_sec", 10, ""),
    ("algo.share_threshold", 0.55, ""),
    ("algo.sign_acf_lags", 50, ""),
    ("algo.score_alert", 70, ""),
    ("algo.min_duration_sec", 90, ""),
    ("leadership.", None, "М11 — кто печатает первым (фаза 4)"),
    ("leadership.move_pct", 0.5, ""),
    ("leadership.move_max_sec", 30, ""),
    ("leadership.xcorr_step_ms", 100, ""),
    ("leadership.xcorr_max_lag_sec", 5, ""),
    ("leadership.jitter_unreliable_ms", 50, ""),
    ("venue_quality.", None, "М12 — качество площадки (фаза 3)"),
    ("venue_quality.exclude_below", 0.3, ""),
    ("alerts.", None, "алерты модулей"),
    ("alerts.cooldown_min", 5, "пауза между алертами одного типа по монете, мин"),
    ("alerts.hysteresis_on", 70, "сигнал включается при силе от (0–100; 70 = порог достигнут)"),
    ("alerts.hysteresis_off", 50, "и выключается, когда сила упала ниже"),
    ("alerts.confirm_modules_min", 3, "М14 (фаза 5): алерт высшего уровня — при согласии стольких модулей"),
    ("alerts.telegram", True, "отправлять алерты модулей в Telegram (если он настроен)"),
    ("record.", None, "запись сырых данных (файлы .jsonl.gz по дням, читаются DuckDB и pandas)"),
    ("record.dir", "", "папка; пусто — data/raw рядом с базой"),
    ("record.trades", True, "все сделки всех бирж по всем отслеживаемым монетам"),
    ("record.books", True, "снимки стаканов основной монеты"),
    ("record.book_every_sec", 1, "снимок стакана биржи не чаще, чем раз в N сек"),
    ("record.book_levels", 50, "уровней на сторону"),
    ("record.keep_days_trades", 30, "сколько дней хранить сделки"),
    ("record.keep_days_books", 7, "стаканы"),
    ("record.keep_days_other", 90, "ОИ, фандинг, ликвидации"),
    ("record.max_gb", 20, "предел места на диске: при превышении удаляются самые старые дни"),
]

DEFAULTS: dict = {}
for _path, _default, _ in SCHEMA:
    if _path.endswith("."):
        continue
    _node = DEFAULTS
    *_parents, _leaf = _path.split(".")
    for _p in _parents:
        _node = _node.setdefault(_p, {})
    _node[_leaf] = _default


def parse_duration(v) -> int:
    """'30s' / '5m' / '1h' / 300 -> seconds."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        sec = int(v)
    else:
        s = str(v).strip().lower()
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(s[-1:], None)
        sec = int(float(s[:-1]) * mult) if mult else int(float(s))
    if sec <= 0:
        raise ValueError(f"окно должно быть больше нуля: {v}")
    return sec


def window_label(sec: int) -> str:
    if sec % 3600 == 0:
        return f"{sec // 3600}h"
    if sec % 60 == 0:
        return f"{sec // 60}m"
    return f"{sec}s"


def _get(d: dict, path: str):
    for p in path.split("."):
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def _set(d: dict, path: str, value) -> None:
    *parents, leaf = path.split(".")
    for p in parents:
        d = d.setdefault(p, {})
    d[leaf] = value


def _coerce(path: str, value, default):
    """Value of the same type as the default, or ValueError."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "yes", "no", "1", "0"):
            return value.lower() in ("true", "yes", "1")
        raise ValueError("нужно true или false")
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("нужно число")
        try:
            v = float(value)
        except ValueError:
            raise ValueError("нужно число") from None
        if v != v or v < 0:
            raise ValueError("нужно неотрицательное число")
        return int(v) if isinstance(default, int) and v == int(v) else v
    if isinstance(default, list):
        if not isinstance(value, list) or not value:
            raise ValueError("нужен список, например [1m, 5m]")
        if path == "windows":
            [parse_duration(x) for x in value]
        return value
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ValueError("нужен словарь, например {WEEX: 0.5}")
        return value
    if path.endswith("window"):
        parse_duration(value)
    return str(value) if value is not None else default


class ModulesConfig:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = copy.deepcopy(DEFAULTS)
        self.errors: list[str] = []
        self._mtime = 0.0

    # ---- access ----------------------------------------------------------
    def get(self, path: str):
        v = _get(self.data, path)
        return v if v is not None else _get(DEFAULTS, path)

    def on(self, module: str) -> bool:
        return bool(self.get(f"modules.{module}"))

    @property
    def collect_only(self) -> bool:
        return bool(self.get("collect_only"))

    def windows(self) -> list[int]:
        try:
            out = sorted({parse_duration(w) for w in self.get("windows")})
        except (TypeError, ValueError):
            out = [60, 300, 900, 3600]
        return out

    def seconds(self, path: str) -> int:
        try:
            return parse_duration(self.get(path))
        except (TypeError, ValueError):
            return parse_duration(_get(DEFAULTS, path))

    # ---- file ------------------------------------------------------------
    def load(self) -> None:
        """Read the file (create it with the defaults if missing)."""
        if not self.path.exists():
            try:
                self.save()
            except OSError as e:
                self.errors = [f"не удалось создать {self.path}: {e}"]
                return
        try:
            self._mtime = self.path.stat().st_mtime
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            self.errors = [f"файл не прочитан, действуют прежние настройки: {e}"]
            log.warning("modules config: %s", self.errors[0])
            return
        raw = raw if isinstance(raw, dict) else {}
        self.data, self.errors = self.validate(raw)
        for e in self.errors:
            log.warning("modules config: %s", e)
        missing = [p for p, _, _ in SCHEMA if not p.endswith(".") and _get(raw, p) is None]
        if missing and not self.errors:  # a newer version added settings: write them into the file
            try:
                self.save()
                log.info("modules config: added %d new settings to %s", len(missing), self.path)
            except OSError as e:
                log.warning("modules config: could not add new settings: %s", e)

    @staticmethod
    def validate(raw: dict) -> tuple[dict, list[str]]:
        data, errors = copy.deepcopy(DEFAULTS), []
        for path, default, _ in SCHEMA:
            if path.endswith("."):
                continue
            value = _get(raw, path)
            if value is None:
                continue
            try:
                _set(data, path, _coerce(path, value, default))
            except (TypeError, ValueError) as e:
                errors.append(f"{path}: {e} (оставлено {default!r})")
        return data, errors

    def reload_if_changed(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        self.load()
        log.info("modules config reloaded from %s", self.path)
        return True

    def update(self, changes: dict) -> None:
        """Apply {"modules.delta": false, ...} and save; ValueError (nothing changed) on a bad value."""
        data = copy.deepcopy(self.data)
        known = {p: d for p, d, _ in SCHEMA if not p.endswith(".")}
        for path, value in changes.items():
            if path not in known:
                raise ValueError(f"неизвестная настройка {path}")
            try:
                _set(data, path, _coerce(path, value, known[path]))
            except ValueError as e:
                raise ValueError(f"{path}: {e}") from None
        self.data = data
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(self.render(), encoding="utf-8")
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    def render(self) -> str:
        lines = [
            "# Manipulation Radar — настройки модулей анализа (ТЗ, раздел 6).",
            "# Файл перечитывается сам через пару секунд после сохранения, перезапуск не нужен.",
            "# Окна: 30s, 1m, 5m, 15m, 1h. Ошибочное значение не применяется — его видно на экране «Здоровье».",
            "",
        ]
        prev_depth = 0
        for path, default, comment in SCHEMA:
            depth = path.count(".") - (1 if path.endswith(".") else 0)
            pad = "  " * depth
            if depth < prev_depth and not path.endswith("."):
                lines.append("")
            prev_depth = depth + (1 if path.endswith(".") else 0)
            if path.endswith("."):
                lines.append("")
                if comment:
                    lines.append(f"{pad}# {comment}")
                lines.append(f"{pad}{path.rstrip('.').split('.')[-1]}:")
                continue
            value = self.get(path)
            dumped = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=1000).strip()
            if dumped.endswith("\n..."):
                dumped = dumped[:-4].strip()
            dumped = dumped.removesuffix("...").strip()
            line = f"{pad}{path.split('.')[-1]}: {dumped}"
            lines.append(f"{line}  # {comment}" if comment else line)
        return "\n".join(lines) + "\n"

    def public(self) -> dict:
        return {"path": str(self.path), "errors": self.errors, "data": self.data}
