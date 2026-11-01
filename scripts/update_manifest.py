"""Manifest uploaded beside the installer, used to verify its download."""
import hashlib
import json
from pathlib import Path

from desktop.updates import SETUP_NAME
from desktop.version import VERSION

root = Path(__file__).resolve().parent.parent
installer = root / "dist" / SETUP_NAME
digest = hashlib.sha256()
with installer.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
manifest = {"version": VERSION, "installer": SETUP_NAME, "sha256": digest.hexdigest(),
            "size": installer.stat().st_size}
(installer.parent / "update.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(json.dumps(manifest))
