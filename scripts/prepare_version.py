"""Start at VERSION and increment the latest published patch, independent of CI run numbers."""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from desktop.distribution import REPOSITORY, version_key

ROOT = Path(__file__).resolve().parent.parent


def choose_version(base: str, latest: dict | None, sha: str, tag: str | None = None) -> str:
    version_key(base)
    if tag is not None:
        version = tag.removeprefix("v")
        if version_key(version) < version_key(base):
            raise ValueError("Release tag is older than the current release line")
        return version
    if not latest or version_key(latest["tag_name"]) < version_key(base):
        return base
    current = latest["tag_name"].removeprefix("v")
    if latest.get("target_commitish") == sha:
        return current  # retries reuse their already published version
    major, minor, patch = map(int, current.split(".")[:3])
    patch += 1
    if patch > 65535:
        minor, patch = minor + 1, 0
    result = f"{major}.{minor}.{patch}"
    version_key(result)
    return result


def latest_release() -> dict | None:
    headers = {"User-Agent": "ManipulationRadar-Build", "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    request = urllib.request.Request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def main():
    base = (ROOT / "VERSION").read_text("utf-8").strip()
    tag = os.environ.get("GITHUB_REF_NAME") if os.environ.get("GITHUB_REF_TYPE") == "tag" else None
    sha = os.environ.get("GITHUB_SHA", "development")
    version = choose_version(base, None if tag else latest_release(), sha, tag)
    (ROOT / "build-info.json").write_text(json.dumps({"version": version, "sha": sha}) + "\n", encoding="utf-8")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"version={version}\ntag=v{version}\n")
    print(f"Windows version: {version}")


if __name__ == "__main__":
    main()
