"""Overlay settings (venues, filters, look) and connection settings dialogs."""
import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import VENUES
from desktop.prefs import OverlayPrefs, Prefs

STATUS = {"na": "нет пары", "noapi": "нет API", "error": "ошибка", "connecting": "подключение", "init": "ожидание"}


class OverlaySettingsDialog(QDialog):
    """Edits a copy of the overlay prefs; `changed` fires on every edit for a live preview."""

    changed = Signal(object)

    def __init__(self, prefs: OverlayPrefs, snapshot: dict, parent: QWidget | None = None,
                 coin: str = "", walls: bool = True):
        """walls=False: an extra coin's overlay (its tape has no order-book analysis)."""
        super().__init__(parent, Qt.WindowType.WindowStaysOnTopHint)
        self.prefs = copy.deepcopy(prefs)
        status = {s["key"]: s.get("status", "") for s in snapshot.get("streams") or []}
        coin = coin or snapshot.get("coin") or ""
        self.setWindowTitle(f"Оверлей {coin}: биржи и фильтры".replace("  ", " "))

        root = QVBoxLayout(self)

        # --- venues ---------------------------------------------------------
        venues_box = QGroupBox(f"Биржи {coin}".strip())
        vb = QVBoxLayout(venues_box)
        quick = QHBoxLayout()
        for label, fn in (
            ("Все споты", lambda: self._select(lambda k: k.endswith(":spot"))),
            ("Все фьючерсы", lambda: self._select(lambda k: k.endswith(":perp"))),
            ("Торгуются сейчас", lambda: self._select(lambda k: status.get(k) in ("live", "polling"))),
            ("Снять все", lambda: self._select(lambda k: False)),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            quick.addWidget(b)
        quick.addStretch(1)
        vb.addLayout(quick)
        hint = QLabel("Ничего не выбрано — показываются все биржи.")
        hint.setStyleSheet("color:#7d8793;")
        vb.addWidget(hint)

        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setHorizontalSpacing(14)
        grid.addWidget(QLabel("<b>Биржа</b>"), 0, 0)
        grid.addWidget(QLabel("<b>Спот</b>"), 0, 1)
        grid.addWidget(QLabel("<b>Фьючерс</b>"), 0, 2)
        self.boxes: dict[str, QCheckBox] = {}
        selected = set(self.prefs.keys)
        for row, v in enumerate(VENUES, start=1):
            grid.addWidget(QLabel(v.name), row, 0)
            for col, kind in ((1, "spot"), (2, "perp")):
                if getattr(v, kind) is None:
                    continue
                key = f"{v.name}:{kind}"
                cb = QCheckBox(STATUS.get(status.get(key, ""), ""))
                cb.setChecked(key in selected)
                if status.get(key) == "na":
                    cb.setStyleSheet("color:#7d8793;")
                    cb.setToolTip(f"{coin} не торгуется на {v.name} ({kind})")
                cb.toggled.connect(self._emit)
                grid.addWidget(cb, row, col)
                self.boxes[key] = cb
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_host)
        scroll.setMinimumHeight(260)
        vb.addWidget(scroll)
        root.addWidget(venues_box, 1)

        # --- filters --------------------------------------------------------
        filt = QGroupBox("Фильтр сделок")
        ff = QFormLayout(filt)
        self.min_usd = QDoubleSpinBox(prefix="$ ", decimals=0, maximum=100_000_000, singleStep=500)
        self.min_usd.setValue(self.prefs.min_usd)
        self.side = QComboBox()
        for code, label in (("all", "все"), ("buy", "только покупки"), ("sell", "только продажи")):
            self.side.addItem(label, code)
        self.side.setCurrentIndex(max(0, self.side.findData(self.prefs.side)))
        self.only_repeats = QCheckBox("только серии одинаковых ордеров (алгоритмы)")
        self.only_repeats.setChecked(self.prefs.only_repeats)
        self.highlight = QCheckBox("подсвечивать серии цветом и счётчиком ×N")
        self.highlight.setChecked(self.prefs.highlight_repeats)
        ff.addRow("Сделки от", self.min_usd)
        ff.addRow("Сторона", self.side)
        self.show_walls = QCheckBox("показывать плиты: крупные заявки у цены (поставили / переставили / сняли)")
        self.show_walls.setChecked(self.prefs.show_walls)
        self.wall_min = QDoubleSpinBox(prefix="$ ", decimals=0, maximum=1_000_000_000, singleStep=10_000)
        self.wall_min.setValue(self.prefs.wall_min_usd)
        self.wall_min.setSpecialValueText("как в настройках детектора")
        ff.addRow("", self.only_repeats)
        ff.addRow("", self.highlight)
        if walls:
            ff.addRow("", self.show_walls)
            ff.addRow("Плиты от", self.wall_min)
        else:
            self.show_walls.hide()
            self.wall_min.hide()
        root.addWidget(filt)

        # --- look -----------------------------------------------------------
        look = QGroupBox("Вид")
        lf = QFormLayout(look)
        self.font_size = QSpinBox(minimum=7, maximum=28, value=self.prefs.font_size)
        self.opacity = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=100, value=int(self.prefs.opacity * 100))
        self.big_mult = QDoubleSpinBox(minimum=1, maximum=100, decimals=1, singleStep=1, suffix=" × порог")
        self.big_mult.setValue(self.prefs.big_mult)
        self.max_rows = QSpinBox(minimum=50, maximum=3000, singleStep=50, value=self.prefs.max_rows)
        cols = QHBoxLayout()
        self.show_time = QCheckBox("время")
        self.show_price = QCheckBox("цена")
        self.show_qty = QCheckBox("кол-во монет")
        for cb, val in ((self.show_time, self.prefs.show_time), (self.show_price, self.prefs.show_price),
                        (self.show_qty, self.prefs.show_qty)):
            cb.setChecked(val)
            cols.addWidget(cb)
        cols.addStretch(1)
        lf.addRow("Размер шрифта", self.font_size)
        lf.addRow("Непрозрачность фона", self.opacity)
        lf.addRow("Колонки", cols)
        lf.addRow("Жирным от", self.big_mult)
        lf.addRow("Строк в памяти", self.max_rows)
        root.addWidget(look)

        for w in (self.min_usd, self.big_mult, self.wall_min):
            w.valueChanged.connect(self._emit)
        for w in (self.font_size, self.max_rows, self.opacity):
            w.valueChanged.connect(self._emit)
        self.side.currentIndexChanged.connect(self._emit)
        for cb in (self.only_repeats, self.highlight, self.show_walls, self.show_time, self.show_price, self.show_qty):
            cb.toggled.connect(self._emit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.resize(520, 720)

    def _select(self, pred) -> None:
        for key, cb in self.boxes.items():
            cb.blockSignals(True)
            cb.setChecked(pred(key))
            cb.blockSignals(False)
        self._emit()

    def _emit(self, *_args) -> None:
        p = self.prefs
        p.keys = [k for k, cb in self.boxes.items() if cb.isChecked()]
        p.min_usd = self.min_usd.value()
        p.side = self.side.currentData()
        p.only_repeats = self.only_repeats.isChecked()
        p.highlight_repeats = self.highlight.isChecked()
        p.show_walls = self.show_walls.isChecked()
        p.wall_min_usd = self.wall_min.value()
        p.font_size = self.font_size.value()
        p.opacity = self.opacity.value() / 100
        p.show_time = self.show_time.isChecked()
        p.show_price = self.show_price.isChecked()
        p.show_qty = self.show_qty.isChecked()
        p.big_mult = self.big_mult.value()
        p.max_rows = self.max_rows.value()
        self.changed.emit(copy.deepcopy(p))


class ConnectionDialog(QDialog):
    """Where the data comes from. Returns an edited copy; the caller decides about a restart."""

    def __init__(self, prefs: Prefs, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Подключение")
        self.prefs = copy.deepcopy(prefs)
        root = QVBoxLayout(self)

        self.local = QRadioButton("Встроенный сервер — биржи опрашиваются с этого компьютера")
        self.remote = QRadioButton("Удалённый сервер — Manipulation Radar на VPS (Docker)")
        group = QButtonGroup(self)
        group.addButton(self.local)
        group.addButton(self.remote)
        (self.remote if prefs.mode == "remote" else self.local).setChecked(True)

        root.addWidget(self.local)
        local_box = QGroupBox()
        lf = QFormLayout(local_box)
        self.port = QSpinBox(minimum=1024, maximum=65535, value=prefs.local_port)
        self.demo = QCheckBox("демо-режим: сгенерированные сделки вместо настоящих бирж")
        self.demo.setChecked(prefs.demo)
        self.tg_token = QLineEdit(prefs.telegram_token, placeholderText="токен от @BotFather (необязательно)")
        self.tg_chat = QLineEdit(prefs.telegram_chat_id, placeholderText="chat id")
        lf.addRow("Порт", self.port)
        lf.addRow("", self.demo)
        lf.addRow("Telegram токен", self.tg_token)
        lf.addRow("Telegram chat id", self.tg_chat)
        root.addWidget(local_box)

        root.addWidget(self.remote)
        remote_box = QGroupBox()
        rf = QFormLayout(remote_box)
        self.url = QLineEdit(prefs.remote_url, placeholderText="http://1.2.3.4:8000")
        self.token = QLineEdit(prefs.remote_token, placeholderText="AUTH_TOKEN сервера (если задан)")
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        rf.addRow("Адрес", self.url)
        rf.addRow("Токен", self.token)
        note = QLabel("Удобно, если часть бирж недоступна из вашей сети: сервер на зарубежном VPS "
                      "собирает сделки, а это приложение только показывает их.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#7d8793;")
        rf.addRow(note)
        root.addWidget(remote_box)

        other = QGroupBox("Прочее")
        of = QFormLayout(other)
        self.hk_overlay = QLineEdit(prefs.hotkey_overlay)
        self.hk_lock = QLineEdit(prefs.hotkey_lock)
        self.notify = QCheckBox("уведомления Windows об алертах")
        self.notify.setChecked(prefs.notify_alerts)
        of.addRow("Показать/скрыть оверлей", self.hk_overlay)
        of.addRow("Сквозные клики вкл/выкл", self.hk_lock)
        of.addRow("", self.notify)
        root.addWidget(other)

        def sync() -> None:
            local_box.setEnabled(self.local.isChecked())
            remote_box.setEnabled(self.remote.isChecked())

        self.local.toggled.connect(sync)
        sync()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.resize(560, 0)

    def _accept(self) -> None:
        p = self.prefs
        p.mode = "remote" if self.remote.isChecked() else "local"
        url = self.url.text().strip().rstrip("/")
        if url and "://" not in url:
            url = "http://" + url
        p.remote_url = url
        p.remote_token = self.token.text().strip()
        p.local_port = self.port.value()
        p.demo = self.demo.isChecked()
        p.telegram_token = self.tg_token.text().strip()
        p.telegram_chat_id = self.tg_chat.text().strip()
        p.hotkey_overlay = self.hk_overlay.text().strip()
        p.hotkey_lock = self.hk_lock.text().strip()
        p.notify_alerts = self.notify.isChecked()
        if p.mode == "remote" and not p.remote_url:
            self.url.setFocus()
            self.url.setStyleSheet("border:1px solid #f6465d;")
            return
        self.accept()

    def needs_restart(self, old: Prefs) -> bool:
        fields = ("mode", "remote_url", "remote_token", "local_port", "demo", "telegram_token", "telegram_chat_id")
        return any(getattr(old, f) != getattr(self.prefs, f) for f in fields)
