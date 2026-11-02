"""Prepare reader-facing release text; publication uses the matching version's notes when present."""
from pathlib import Path
import sys

from desktop.distribution import version_key

ROOT = Path(__file__).resolve().parent.parent


def main():
    version = sys.argv[1]
    version_key(version)
    notes = ROOT / "docs" / "releases" / f"{version}.md"
    template = notes if notes.is_file() else ROOT / "docs" / "RELEASE_TEMPLATE.md"
    text = template.read_text("utf-8").replace("{version}", version)
    target = ROOT / "dist" / "release-notes.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
