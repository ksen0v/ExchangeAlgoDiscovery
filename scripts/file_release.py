"""Assemble the first-install bundle and content-addressed changed-file release assets."""
import json
import logging
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

from desktop.distribution import (BUILD_INFO, CORE_NAME, CURRENT_NAME, FILES_NAME, LAUNCHER_NAME,
                                  MAX_JSON_SIZE, REPOSITORY, SCHEMA, atomic_json, asset_url, file_hash,
                                  validate_manifest, version_key)
from desktop.version import VERSION

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
MAX_NEW_PARTS = 900  # GitHub permits 1000 assets per release; reserve metadata/installers.


def entry_for(path: Path) -> dict:
    return {"size":path.stat().st_size, "sha256":file_hash(path), "source":None}


def make_part(path: Path, entry: dict, parts: Path, version: str) -> dict:
    parts.mkdir(parents=True, exist_ok=True)
    archive = parts / f"part-{entry['sha256']}.zip"
    if not archive.exists():
        info = zipfile.ZipInfo("payload", (2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
            with package.open(info, "w", force_zip64=True) as target, path.open("rb") as source:
                shutil.copyfileobj(source, target, 1024 * 1024)
    return {**entry, "source":{"size":archive.stat().st_size, "sha256":file_hash(archive),
            "url":f"https://github.com/{REPOSITORY}/releases/download/v{version}/{archive.name}"}}


def build_manifest(core: Path, launcher: Path, version: str, parts: Path, previous: dict | None = None) -> dict:
    files = {p.relative_to(core).as_posix():entry_for(p) for p in sorted(core.rglob("*"))
             if p.is_file() and p.name != FILES_NAME}
    result = {"schema":SCHEMA, "version":version, "files":files, "launcher":entry_for(launcher)}
    if previous is not None:
        validate_manifest(previous)
        old_entries = {**previous["files"], "$launcher":previous["launcher"]}
        new_entries = {**files, "$launcher":result["launcher"]}
        changed = {key for key, value in new_entries.items()
                   if old_entries.get(key, {}).get("sha256") != value["sha256"]}
        # Large format/runtime migrations can still fall back to the full installer.
        publish = len({new_entries[key]["sha256"] for key in changed}) <= MAX_NEW_PARTS
        for key, entry in new_entries.items():
            before = old_entries.get(key)
            if before and before["sha256"] == entry["sha256"]:
                entry["source"] = before.get("source")
                # The 1.0 line remains independent when the user-requested old releases are removed.
                if (version_key(version) >= version_key("1.0.0") and entry["source"]
                        and "/releases/download/v0." in entry["source"]["url"]):
                    entry["source"] = None
            elif publish:
                source = launcher if key == "$launcher" else core / key
                entry.update(make_part(source, entry, parts, version))
    validate_manifest(result, version)
    return result


def _json_request(url, api=False):
    headers = {"User-Agent":"ManipulationRadar-Build", "Accept":"application/json"}
    token = os.environ.get("GITHUB_TOKEN")
    if api and token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        raw = response.read(MAX_JSON_SIZE + 1)
    if len(raw) > MAX_JSON_SIZE:
        raise ValueError("Release metadata too large")
    return raw


def previous_manifest():
    try:
        release = json.loads(_json_request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest", api=True))
        asset = next(a for a in release["assets"] if a["name"] == FILES_NAME)
        raw = _json_request(asset_url(asset["browser_download_url"], release["tag_name"], FILES_NAME))
        if asset.get("digest") and asset["digest"] != "sha256:" + __import__("hashlib").sha256(raw).hexdigest():
            raise ValueError("Previous manifest digest mismatch")
        return validate_manifest(json.loads(raw))
    except Exception as exc:
        # First release / migration from an installer-only release: publish an initial baseline.
        print(f"No compatible previous file release ({type(exc).__name__}); full installer is the baseline")
        return None


def size_report(manifest: dict, previous: dict | None, parts: Path):
    files = manifest["files"]
    total = sum(e["size"] for e in files.values()) + manifest["launcher"]["size"]
    largest = sorted(({"path":p, "size":e["size"]} for p,e in files.items()), key=lambda x:-x["size"])[:25]
    groups = {"Qt":0, "Python code and runtime":0, "Other":0}
    for name, entry in files.items():
        group = "Qt" if "/PySide6/" in name else "Python code and runtime" if name.endswith((".pyc", ".pyz", ".zip")) or "python3" in name.lower() else "Other"
        groups[group] += entry["size"]
    old_files = previous["files"] if previous else {}
    changed = [p for p,e in files.items() if old_files.get(p, {}).get("sha256") != e["sha256"]]
    payloads = list(parts.glob("*.zip"))
    return {"version":manifest["version"], "previous":previous["version"] if previous else None,
            "file_count":len(files), "installed_bytes":total, "groups":groups, "largest":largest,
            "changed_files":changed, "new_payload_count":len(payloads),
            "new_payload_bytes":sum(p.stat().st_size for p in payloads)}


def main():
    previous = previous_manifest()
    core = DIST / "RadarApp"
    launcher = DIST / LAUNCHER_NAME
    parts = DIST / "update-parts"
    # Preserve the exact interpreter/dependency bits; version and Git SHA are external metadata.
    manifest = build_manifest(core, launcher, VERSION, parts, previous)
    output = DIST / "ManipulationRadar"
    if output.exists():
        raise RuntimeError("Assembled distribution directory already exists")
    active = output / "versions" / VERSION
    active.parent.mkdir(parents=True)
    shutil.copytree(core, active)
    shutil.copyfile(launcher, output / LAUNCHER_NAME)
    atomic_json(active / FILES_NAME, manifest)
    atomic_json(DIST / FILES_NAME, manifest)
    atomic_json(output / CURRENT_NAME, {"schema":SCHEMA, "version":VERSION, "directory":VERSION})
    report = size_report(manifest, previous, parts)
    atomic_json(DIST / "size-report.json", report)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("parts<<RADAR_PARTS\n")
            for path in sorted(parts.glob("*.zip")):
                stream.write(path.as_posix() + "\n")
            stream.write("RADAR_PARTS\n")
    print(json.dumps({k:v for k,v in report.items() if k not in ("largest", "changed_files")}))


if __name__ == "__main__":
    main()
