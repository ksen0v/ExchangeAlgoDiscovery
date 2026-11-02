"""User-requested release reset. Run only after the verified 1.0.0 release is public."""
import hashlib
import json
import os
import urllib.request
from pathlib import Path

from desktop.distribution import FILES_NAME, REPOSITORY, SETUP_NAME, MANIFEST_NAME, UpdateError, validate_manifest, version_key

API = f"https://api.github.com/repos/{REPOSITORY}"
ROOT = Path(__file__).resolve().parent.parent


def request(path, method="GET", data=None, content_type="application/json"):
    # No credential discovery: this is the repository-scoped Actions token supplied by the workflow.
    token = os.environ["GITHUB_TOKEN"]
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "Content-Type": content_type, "User-Agent": "ManipulationRadar-Release"}
    url = path if path.startswith("https://uploads.github.com/") else API + path
    with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers, data=data), timeout=60) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def legacy_candidates(releases):
    result = []
    for release in releases:
        try:
            old = version_key(release["tag_name"]) < version_key("1.0.0")
        except (KeyError, ValueError, UpdateError):
            continue
        if old:
            result.append(release)
    return result


def main():
    current = request("/releases/latest")
    if current["tag_name"] != "v1.0.0" or current.get("target_commitish") != os.environ["GITHUB_SHA"]:
        raise RuntimeError("Refusing release cleanup outside the verified 1.0.0 publication")
    assets = {a["name"]: a for a in current["assets"]}
    if not {SETUP_NAME, MANIFEST_NAME, FILES_NAME}.issubset(assets):
        raise RuntimeError("The new release is incomplete; all old releases are retained")
    local = (ROOT / "dist" / FILES_NAME).read_bytes()
    if assets[FILES_NAME].get("digest") != "sha256:" + hashlib.sha256(local).hexdigest():
        raise RuntimeError("Published file manifest differs from the verified build")
    manifest = validate_manifest(json.loads(local), "1.0.0")
    for entry in [*manifest["files"].values(), manifest["launcher"]]:
        if entry["source"] and "/releases/download/v0." in entry["source"]["url"]:
            raise RuntimeError("The new updater still depends on an old release")
    # All required new payloads have already been uploaded by the preceding release step.
    required = {e["source"]["url"].rsplit("/", 1)[-1] for e in [*manifest["files"].values(), manifest["launcher"]]
                if e["source"] and "/releases/download/v1.0.0/" in e["source"]["url"]}
    if not required.issubset(assets):
        raise RuntimeError("The new release has missing update payloads")
    releases = []
    for page in range(1, 1000):
        batch = request(f"/releases?per_page=100&page={page}")
        releases.extend(batch)
        if len(batch) < 100:
            break
    legacy = legacy_candidates(releases)
    # Preserve release descriptions, checksums, commit references and the asset index before removal.
    archive = {"baseline": "v1.0.0", "releases": legacy}
    output = ROOT / "dist" / "legacy-release-index.json"
    output.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    if "legacy-release-index.json" not in assets:
        upload = current["upload_url"].split("{", 1)[0] + "?name=legacy-release-index.json"
        if not upload.startswith(f"https://uploads.github.com/repos/{REPOSITORY}/releases/"):
            raise RuntimeError("Unexpected release upload destination")
        request(upload, "POST", output.read_bytes())
    for release in legacy:
        request(f"/releases/{int(release['id'])}", "DELETE")
        print(f"Retired old release {release['tag_name']}")
    print(f"Release list now starts at 1.0.0; retired {len(legacy)} old releases. Git tags and commits are retained.")


if __name__ == "__main__":
    main()
