"""An on-demand updater; blocking network and hashing stay off the GUI thread."""
import sys
import threading

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from desktop.prefs import data_dir
from pathlib import Path
from desktop.distribution import installed_root
from desktop.updates import Cancelled, Release, check_release, prepare_update
from desktop.version import VERSION


class UpdateJob(QObject):
    checked = Signal(object)
    downloaded = Signal(object)
    failed = Signal(str)
    progress = Signal(object, object)
    status = Signal(str)

    def __init__(self):
        super().__init__()  # no Qt parent: the Python worker retains this object until it exits
        self.cancel = threading.Event()

    def start(self, release: Release | None = None):
        def run():
            try:
                if release is None:
                    root = installed_root(Path(sys.executable)) if getattr(sys, "frozen", False) else None
                    if root:
                        self.status.emit("Проверяем выпуск и сравниваем файлы приложения…")
                    result = check_release(VERSION, root, self.cancel)
                    if not self.cancel.is_set():
                        self.checked.emit(result)
                else:
                    prepared = prepare_update(release, data_dir() / "updates", self.cancel,
                                              lambda n, total: self.progress.emit(n, total),
                                              argv=sys.argv[1:], status=self.status.emit)
                    if not self.cancel.is_set():
                        self.downloaded.emit(prepared)
            except Cancelled:
                pass
            except Exception as exc:
                if not self.cancel.is_set():
                    self.failed.emit(f"Не удалось обновить приложение: {exc}")
        threading.Thread(target=run, name="radar-update", daemon=True).start()


class UpdateDialog(QDialog):
    install_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Обновление Manipulation Radar")
        self.setMinimumWidth(460)
        self.release = None
        self.job = None
        self.busy = False
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Установленная версия: {VERSION}"))
        self.message = QLabel("Нажмите «Проверить», чтобы найти новую версию.")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.check = QPushButton("Проверить")
        self.check.clicked.connect(self.check_now)
        buttons.addWidget(self.check)
        self.update = QPushButton("Обновить и перезапустить")
        self.update.setEnabled(False)
        self.update.clicked.connect(self.download)
        buttons.addWidget(self.update)
        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def _job(self):
        if self.job:
            self.job.cancel.set()
        job = UpdateJob()
        job.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        job.status.connect(self._status, Qt.ConnectionType.QueuedConnection)
        self.job = job
        return job

    def check_now(self):
        self.busy = True
        self.check.setEnabled(False)
        self.update.setEnabled(False)
        self.progress.hide()
        self.message.setText("Проверяем опубликованные версии на GitHub…")
        job = self._job()
        job.checked.connect(self._checked, Qt.ConnectionType.QueuedConnection)
        job.start()

    def _checked(self, release):
        if self.job is None or self.job.cancel.is_set():
            return
        self.busy = False
        self.check.setEnabled(True)
        self.release = release
        frozen = sys.platform == "win32" and bool(getattr(sys, "frozen", False))
        self.update.setEnabled(release is not None and frozen)
        if release is None:
            self.message.setText("У вас последняя опубликованная версия.")
        else:
            amount = release.download_size
            size = f"{amount / 1024:.0f} КБ" if amount < 1024 * 1024 else f"{amount / 1024 / 1024:.1f} МБ"
            kind = f"изменённых файлов: {len(release.file_plan.changed)}" if release.file_plan is not None else "полный установщик"
            self.message.setText(f"Доступна версия {release.version}: {size}, {kind}.\n"
                                 "Настройки и база сохранятся. Приложение перезапустится после установки."
                                 + ("\nУстановка обновлений доступна в Windows-сборке .exe." if not frozen else ""))

    def download(self):
        if self.busy or not self.release:
            return
        self.busy = True
        self.check.setEnabled(False)
        self.update.setEnabled(False)
        self.close_button.setText("Отменить")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.show()
        self.message.setText("Скачиваем изменённые файлы… Приложение продолжает работать."
                             if self.release.file_plan is not None else "Скачиваем установщик… Приложение продолжает работать.")
        job = self._job()
        job.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        job.downloaded.connect(self._ready, Qt.ConnectionType.QueuedConnection)
        job.start(self.release)

    def _progress(self, done, total):
        if self.job is None or self.job.cancel.is_set():
            return
        self.progress.setValue(int(done * 100 / total) if total else 100)

    def _status(self, message):
        if self.job is not None and not self.job.cancel.is_set():
            self.message.setText(message)

    def _ready(self, path):
        if self.job is None or self.job.cancel.is_set() or not self.isVisible():
            return  # a completion queued before Cancel must never install afterwards
        self.busy = False
        self.install_requested.emit(path)

    def _failed(self, message):
        if self.job is None or self.job.cancel.is_set():
            return
        self.busy = False
        self.message.setText(message)
        self.check.setEnabled(True)
        self.update.setEnabled(False)
        self.close_button.setText("Закрыть")
        self.progress.hide()

    def reject(self):
        if self.job:
            self.job.cancel.set()
        super().reject()

    def closeEvent(self, event):
        if self.job:
            self.job.cancel.set()
        super().closeEvent(event)
