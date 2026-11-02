"""Read version metadata outside the executable, keeping UI-only updates small."""
import json
import sys
from pathlib import Path

from desktop.distribution import UpdateError, version_key

_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
try:
    _info = json.loads((_root / "build-info.json").read_text("utf-8"))
    VERSION = _info["version"]
    version_key(VERSION)
    BUILD_SHA = str(_info.get("sha", "development"))
except (OSError, ValueError, KeyError, TypeError, UpdateError):
    if getattr(sys, "frozen", False):
        raise RuntimeError("Номер установленной версии повреждён. Повторите установку")
    VERSION, BUILD_SHA = "0.1.0", "development"
