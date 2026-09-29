"""Desktop preferences: one JSON file in the user's app-data folder."""
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "ManipulationRadar"


def data_dir() -> Path:
    """%APPDATA%\\ManipulationRadar on Windows; RADAR_HOME overrides (tests, portable use)."""
    if os.environ.get("RADAR_HOME"):
        d = Path(os.environ["RADAR_HOME"])
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class OverlayPrefs:
    keys: list[str] = field(default_factory=list)  # "KuCoin:spot"...; empty = every venue
    min_usd: float = 1000.0
    side: str = "all"  # all | buy | sell
    only_repeats: bool = False  # show only prints of repeating-size series (algorithms)
    highlight_repeats: bool = True
    show_walls: bool = True  # large orders near the price (put / re-placed / pulled / eaten)
    wall_min_usd: float = 0.0  # 0 = the server threshold (detector settings)
    font_size: int = 11
    opacity: float = 0.75  # of the background; text always stays solid
    show_time: bool = True
    show_price: bool = True
    show_qty: bool = False
    big_mult: float = 5.0  # prints >= min_usd * big_mult are bold
    max_rows: int = 300


@dataclass
class Prefs:
    mode: str = "local"  # local: exchanges are polled from this PC | remote: connect to a server
    remote_url: str = ""
    remote_token: str = ""
    local_port: int = 8765
    demo: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""
    notify_alerts: bool = True
    overlay_visible: bool = True
    overlay_locked: bool = False
    overlay_geometry: str = ""
    main_geometry: str = ""
    hotkey_overlay: str = "ctrl+alt+t"
    hotkey_lock: str = "ctrl+alt+l"
    overlay: OverlayPrefs = field(default_factory=OverlayPrefs)

    @classmethod
    def load(cls, path: Path) -> "Prefs":
        p = cls()
        try:
            raw = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return p
        except (OSError, ValueError) as e:
            log.warning("preferences unreadable, using defaults: %s", e)
            return p
        _fill(p, raw)
        _fill(p.overlay, raw.get("overlay") or {})
        return p

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, path)


def _fill(obj, raw: dict) -> None:
    """Copy values of matching type only, so a hand-edited file cannot break the app."""
    for f in fields(obj):
        if f.name not in raw or f.name == "overlay":
            continue
        cur, v = getattr(obj, f.name), raw[f.name]
        if isinstance(cur, bool):
            ok = isinstance(v, bool)
        elif isinstance(cur, (int, float)):
            ok = isinstance(v, (int, float)) and not isinstance(v, bool)
            v = type(cur)(v) if ok else v
        else:
            ok = isinstance(v, type(cur))
        if ok:
            setattr(obj, f.name, v)
