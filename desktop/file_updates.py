"""Compare installed files and stage only changed payloads, keeping the GUI responsive."""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from desktop.distribution import (CURRENT_NAME, FILES_NAME, LAUNCHER_NAME, UpdateError, atomic_json,
                                  file_hash, file_matches, no_link, read_current, read_json_file,
                                  safe_file, validate_manifest, version_key)


@dataclass(frozen=True)
class FilePlan:
    root: Path
    base: dict
    base_manifest_sha: str
    target: dict
    changed: dict

    @property
    def download_size(self):
        return sum(entry["source"]["size"] for entry in {e["sha256"]:e for e in self.changed.values()}.values())


@dataclass(frozen=True)
class PreparedUpdate:
    mode: str  # installer | files
    path: Path
    launcher: Path | None = None


def clean_update_cache(cache: Path) -> None:
    """Only our completed update caches; never user settings or an in-progress stage."""
    if not cache.is_dir():
        return
    no_link(cache)
    for path in cache.iterdir():
        try:
            no_link(path)
            if path.is_file() and re.fullmatch(r"ManipulationRadar-Setup-\d+\.\d+\.\d+(?:\.\d+)?\.exe", path.name):
                path.unlink()  # Windows keeps an installer that is still executing locked.
            elif path.is_dir() and re.fullmatch(r"files-[0-9a-f]{32}", path.name):
                result = safe_file(path, "result.json")
                if (not result.is_file() or time.time() - result.stat().st_mtime < 3600
                        or read_json_file(safe_file(path,"owned.json")) != {"schema":1,"type":"radar-file-update"}):
                    continue
                if path.resolve().parent != cache.resolve():
                    continue
                for child in path.rglob("*"):
                    no_link(child)
                shutil.rmtree(path)
        except (OSError, ValueError, UpdateError):
            continue


def plan_files(root: Path, current: str, target: dict, cancel=None) -> FilePlan | None:
    validate_manifest(target)
    if version_key(target["version"]) <= version_key(current):
        raise UpdateError("Обновление должно быть новее установленной версии")
    pointer, active = read_current(root)
    if pointer["version"] != current:
        raise UpdateError("Установленная версия изменилась. Повторите проверку обновлений")
    previous = validate_manifest(read_json_file(safe_file(active, FILES_NAME)), current)
    launcher = safe_file(root, LAUNCHER_NAME)
    # The copied apply helper must itself be the original installed launcher.
    if not file_matches(launcher, previous["launcher"]):
        return None
    changed = {}
    for name, entry in target["files"].items():
        if cancel is not None and cancel.is_set():
            from desktop.updates import Cancelled
            raise Cancelled()
        if not file_matches(safe_file(active, name), entry):
            changed[name] = entry
    if not file_matches(launcher, target["launcher"]):
        changed["$launcher"] = target["launcher"]
    if any(entry.get("source") is None for entry in changed.values()):
        return None  # old baseline file missing/corrupt: use the complete installer to repair it
    return FilePlan(root.resolve(), pointer, file_hash(active / FILES_NAME), target, changed)


def _download(url, size, sha, path, cancel, progress):
    # Import lazily: the standalone launcher never needs HTTP or Qt.
    from desktop.updates import Cancelled, _request

    digest = hashlib.sha256()
    done = 0
    with _request(url) as response, path.open("xb") as output:
        length = response.headers.get("Content-Length")
        if length is not None and int(length) != size:
            raise UpdateError("Размер файла обновления не совпадает")
        while True:
            if cancel.is_set():
                raise Cancelled()
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            done += len(chunk)
            if done > size:
                raise UpdateError("Слишком большой файл обновления")
            digest.update(chunk); output.write(chunk); progress(len(chunk))
    if done != size or digest.hexdigest() != sha:
        raise UpdateError("SHA-256 или размер скачанного файла не совпадает")


def extract_payload(archive: Path, output: Path, entry: dict, cancel: threading.Event) -> None:
    from desktop.updates import Cancelled

    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if len(members) != 1 or members[0].filename != "payload" or members[0].file_size != entry["size"]:
            raise UpdateError("Некорректное содержимое пакета обновления")
        digest = hashlib.sha256()
        done = 0
        with package.open(members[0]) as source, output.open("xb") as target:
            while True:
                if cancel.is_set():
                    raise Cancelled()
                chunk = source.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > entry["size"]:
                    raise UpdateError("Файл обновления превышает ожидаемый размер")
                digest.update(chunk); target.write(chunk)
        if done != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise UpdateError("Распакованный файл обновления повреждён")


def prepare_files(plan: FilePlan, cache: Path, cancel: threading.Event, progress=lambda done,total:None,
                  argv: list[str] | None = None) -> PreparedUpdate:
    from desktop.updates import Cancelled

    cache.mkdir(parents=True, exist_ok=True)
    no_link(cache)
    stage = cache / ("files-" + uuid.uuid4().hex)
    stage.mkdir()
    atomic_json(stage / "owned.json", {"schema":1, "type":"radar-file-update"})
    (stage / "payload").mkdir()
    try:
        total = plan.download_size
        done = 0

        def advanced(size):
            nonlocal done
            done += size
            progress(done, total)

        for entry in {e["sha256"]:e for e in plan.changed.values()}.values():
            if cancel.is_set():
                raise Cancelled()
            source = entry["source"]
            archive = stage / (entry["sha256"] + ".zip")
            _download(source["url"], source["size"], source["sha256"], archive, cancel, advanced)
            extract_payload(archive, stage / "payload" / entry["sha256"], entry, cancel)
            archive.unlink()
        if cancel.is_set():
            raise Cancelled()
        # No install files are changed before this verified staging directory is complete.
        pointer, active = read_current(plan.root)
        if pointer != plan.base or file_hash(active / FILES_NAME) != plan.base_manifest_sha:
            raise UpdateError("Установленная версия изменилась во время загрузки")
        old = validate_manifest(read_json_file(active / FILES_NAME))
        helper = stage / "apply.exe"
        shutil.copyfile(safe_file(plan.root, LAUNCHER_NAME), helper)
        if not file_matches(helper, old["launcher"]):
            raise UpdateError("Программа установки обновления повреждена")
        request = {"schema":1, "root":str(plan.root), "base":plan.base,
                   "base_manifest_sha":plan.base_manifest_sha, "target":plan.target,
                   "argv":list(argv or []), "ready":str(stage / "ready.json")}
        atomic_json(stage / "plan.json", request)
        progress(total, total)
        return PreparedUpdate("files", stage / "plan.json", helper)
    except BaseException:
        # This directory was exclusively created above, inside the checked update cache.
        if stage.resolve().parent == cache.resolve() and not stage.is_symlink():
            shutil.rmtree(stage, ignore_errors=True)
        raise
