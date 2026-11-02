"""Give each successful CI build a monotonically increasing Windows version."""
import os
from pathlib import Path

import json
from desktop.distribution import version_key

ROOT = Path(__file__).resolve().parent.parent
tag = os.environ.get("GITHUB_REF_NAME", "")
if os.environ.get("GITHUB_REF_TYPE") == "tag":
    version_key(tag)
    version = tag.removeprefix("v")
else:
    version = f"0.1.{int(os.environ['GITHUB_RUN_NUMBER'])}"
version_key(version)
sha = os.environ.get("GITHUB_SHA", "development")
(ROOT / "build-info.json").write_text(json.dumps({"version":version, "sha":sha}) + "\n", encoding="utf-8")
with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
    output.write(f"version={version}\ntag=v{version}\n")
print(f"Windows version: {version}")
