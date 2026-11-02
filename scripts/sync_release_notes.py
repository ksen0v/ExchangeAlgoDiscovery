"""Publish documentation changes without building or publishing another app version."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from desktop.distribution import REPOSITORY, UpdateError, version_key

ROOT = Path(__file__).resolve().parent.parent
API = f"https://api.github.com/repos/{REPOSITORY}"


def request(path, data=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ManipulationRadar-Documentation",
               "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"]}
    raw = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + path, data=raw, headers=headers, method="PATCH" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    for path in sorted((ROOT / "docs" / "releases").glob("*.md")):
        version = path.stem
        try:
            version_key(version)
        except UpdateError:
            continue
        tag = f"v{version}"
        try:
            release = request("/releases/tags/" + urllib.parse.quote(tag, safe=""))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                print(f"Not published yet: {tag}")
                continue
            raise
        if release.get("draft"):
            print(f"Draft retained: {tag}")
            continue
        latest = request("/releases/latest")
        body = path.read_text("utf-8").replace("{version}", version)
        request(f"/releases/{int(release['id'])}", {"name": f"Manipulation Radar {version}", "body": body,
                "make_latest": "true" if release["id"] == latest["id"] else "false"})
        print(f"Updated release description: {tag}")


if __name__ == "__main__":
    main()
