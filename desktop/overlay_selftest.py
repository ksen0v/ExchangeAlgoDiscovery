"""Packaged Windows CI: check the actual compositor and real mouse routing."""
import ctypes
import statistics
import sys

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtWidgets import QMenu, QPushButton

from desktop.prefs import data_dir


def verify_controls(ctl, complete):
    if sys.platform != "win32":
        complete({"supported": False})
        return
    overlay = next(iter(ctl.module_overlays.values()), None)
    if overlay is None or overlay.web is None:
        complete({"ready": False})
        return
    original = {**overlay.appearance(), "locked": overlay.record["locked"]}
    others = [ov for ov in ctl.module_overlays.values() if ov is not overlay]
    hidden = [widget for widget in ctl.app.topLevelWidgets() if widget.isVisible()
              and widget not in (overlay, overlay.locked_header, ctl.main)]
    for widget in hidden:
        widget.hide()
    target = QPushButton(None)
    target.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    target.setStyleSheet("background:#ff00ff;border:0;border-radius:0")
    target.setGeometry(overlay.geometry())
    clicks = []
    target.clicked.connect(lambda: clicks.append(True))
    target.show()
    overlay.raise_()
    overlay.set_appearance(False, .25, persist=False)
    overlay.set_locked(True, persist=False)
    result = {"independent": not any(ov.record["locked"] for ov in others),
              "automatic_transparency": overlay.record["transparent"]}
    user32 = ctypes.windll.user32

    def click(point):
        user32.SetCursorPos(point.x(), point.y())
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)

    def sample():
        point = overlay.mapToGlobal(QPoint(overlay.width() - 55, overlay.height() - 35))
        image = ctl.app.primaryScreen().grabWindow(0, point.x(), point.y(), 16, 16).toImage()
        pixels = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())]
        return [statistics.median(getattr(pixel, name)() for pixel in pixels) for name in ("red", "green", "blue")]

    def guarded(step):
        def run():
            try:
                step()
            except Exception as error:
                result["error"] = f"{type(error).__name__}: {error}"
                finish()
        return run

    def finish():
        popup = ctl.app.activePopupWidget()
        if popup:
            popup.close()
        target.hide()
        target.deleteLater()
        overlay.set_appearance(original["transparent"], original["background_opacity"], persist=False)
        overlay.set_locked(original["locked"], persist=False)
        for widget in hidden:
            widget.show()
        QTimer.singleShot(300, lambda: complete(result))

    def transparent_check():
        color = sample()
        result["transparent_rgb"] = color
        result["transparent_pixels"] = color[0] > 150 and color[2] > 150 and color[1] < 160
        header = overlay.locked_header
        result["controls_separate"] = bool(header and header.isVisible() and header.isWindow()
                                           and not header.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        ctl.app.primaryScreen().grabWindow(0).save(str(data_dir() / "selftest-overlay-transparent-desktop.png"))
        click(overlay.mapToGlobal(QPoint(overlay.width() // 2, overlay.height() // 2)))
        QTimer.singleShot(200, guarded(menu_check))

    def menu_check():
        result["body_passes"] = bool(clicks)
        button = overlay.locked_header.menu_button
        click(button.mapToGlobal(button.rect().center()))
        QTimer.singleShot(200, guarded(unlock))

    def unlock():
        popup = ctl.app.activePopupWidget()
        result["menu_accessible"] = isinstance(popup, QMenu) and any("Настройки оверлея" in action.text() for action in popup.actions())
        if popup:
            popup.close()
        QTimer.singleShot(150, guarded(click_unlock))

    def click_unlock():
        button = overlay.locked_header.lock_button
        click(button.mapToGlobal(button.rect().center()))
        QTimer.singleShot(200, guarded(opaque_check))

    def opaque_check():
        result["unlock_by_header"] = not overlay.record["locked"] and not overlay.locked_header.isVisible()
        overlay.set_appearance(False, .25, persist=False)
        QTimer.singleShot(500, guarded(opaque_sample))

    def opaque_sample():
        color = sample()
        result["opaque_rgb"] = color
        result["opaque_pixels"] = abs(color[0] - color[1]) < 70 and abs(color[2] - color[1]) < 70
        ctl.app.primaryScreen().grabWindow(0).save(str(data_dir() / "selftest-overlay-opaque-desktop.png"))
        result["before_opaque_clicks"] = len(clicks)
        click(overlay.mapToGlobal(QPoint(overlay.width() // 2, overlay.height() // 2)))
        QTimer.singleShot(200, guarded(blocked_check))

    def blocked_check():
        result["opaque_blocks"] = len(clicks) == result["before_opaque_clicks"]
        finish()

    QTimer.singleShot(700, guarded(transparent_check))
