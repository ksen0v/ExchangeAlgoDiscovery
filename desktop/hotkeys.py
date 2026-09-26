"""System-wide hotkeys (Windows RegisterHotKey), work while the terminal has focus.

Elsewhere this is a no-op: the tray menu and the main window toolbar do the same.
"""
import ctypes
import logging
import sys
import threading

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
NAMED_KEYS = {
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "insert": 0x2D, "delete": 0x2E, "pause": 0x13, "tab": 0x09, "`": 0xC0, "~": 0xC0,
}


def parse(combo: str) -> tuple[int, int] | None:
    """"ctrl+alt+t" -> (modifiers, virtual key); None if malformed or without modifiers."""
    parts = [p for p in combo.lower().replace(" ", "").split("+") if p]
    if len(parts) < 2 or any(p not in MODIFIERS for p in parts[:-1]):
        return None
    key = parts[-1]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    else:
        return None
    mods = 0
    for p in parts[:-1]:
        mods |= MODIFIERS[p]
    return mods, vk


class Hotkeys(QObject):
    triggered = Signal(str)  # name given in start()
    failed = Signal(str)  # human-readable reason

    def __init__(self) -> None:
        super().__init__()
        self._thread: threading.Thread | None = None
        self._tid = 0

    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    def start(self, bindings: dict[str, str]) -> None:
        """bindings: name -> "ctrl+alt+t"."""
        if not self.available:
            return
        self.stop()
        started = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(bindings, started), name="hotkeys", daemon=True)
        self._thread.start()
        started.wait(2)

    def stop(self) -> None:
        if self._thread and self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(2)
        self._thread, self._tid = None, 0

    def _run(self, bindings: dict[str, str], started: threading.Event) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        names: dict[int, str] = {}
        for i, (name, combo) in enumerate(bindings.items(), start=1):
            parsed = parse(combo)
            if not parsed:
                self.failed.emit(f"Не понял сочетание «{combo}»")
                continue
            if user32.RegisterHotKey(None, i, parsed[0] | MOD_NOREPEAT, parsed[1]):
                names[i] = name
            else:
                self.failed.emit(f"Сочетание «{combo}» уже занято другой программой")
        started.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in names:
                    self.triggered.emit(names[msg.wParam])
        finally:
            for i in names:
                user32.UnregisterHotKey(None, i)
