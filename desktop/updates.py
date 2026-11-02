"""GitHub release checks and verified installer downloads. No background polling."""
import hashlib
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from desktop.distribution import (FILES_NAME, MAX_JSON_SIZE, REPOSITORY, SETUP_NAME, MANIFEST_NAME,
                                  UpdateError, asset_url, validate_manifest, valid_hash, version_key)

RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_INSTALLER_SIZE = 1024 * 1024 * 1024


class Cancelled(UpdateError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    url: str
    sha256: str
    size: int
    file_plan: object = None

    @property
    def download_size(self):
        return self.file_plan.download_size if self.file_plan is not None else self.size


def parse_release(release: dict, manifest: dict) -> Release:
    if release.get("draft") or release.get("prerelease"):
        raise UpdateError("Выпуск ещё не опубликован")
    tag = release.get("tag_name", "")
    version = manifest.get("version", "")
    if version_key(version) != version_key(tag):
        raise UpdateError("Версия установщика не совпадает с выпуском")
    assets = {a.get("name"): a for a in release.get("assets", [])}
    setup = assets.get(SETUP_NAME)
    size = manifest.get("size")
    sha = manifest.get("sha256", "")
    if not setup or manifest.get("installer") != SETUP_NAME:
        raise UpdateError("В выпуске нет установщика Windows")
    if type(size) is not int or not 0 < size <= MAX_INSTALLER_SIZE or setup.get("size") != size:
        raise UpdateError("Неверный размер установщика")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        raise UpdateError("В выпуске отсутствует контрольная сумма SHA-256")
    digest = setup.get("digest")
    if digest and digest.lower() != f"sha256:{sha.lower()}":
        raise UpdateError("Контрольные суммы выпуска не совпадают")
    return Release(version, tag, asset_url(setup.get("browser_download_url", ""), tag, SETUP_NAME), sha.lower(), size)


def _request(url: str):
    # No account tokens are needed for this public repository.
    request = urllib.request.Request(url, headers={"User-Agent": "ManipulationRadar-Updater",
                                                 "Accept": "application/json" if "api.github.com" in url else "*/*"})
    response = urllib.request.urlopen(request, timeout=30)
    if urlsplit(response.geturl()).scheme != "https":
        response.close()
        raise UpdateError("Обновления загружаются только по HTTPS")
    return response


def read_json(url: str) -> dict:
    with _request(url) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise UpdateError("Ответ сервера обновлений слишком большой")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise UpdateError("Некорректный ответ сервера обновлений")
    return data


def check_release(current: str, root: Path | None = None, cancel=None) -> Release | None:
    try:
        release = read_json(RELEASES_URL)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("Установщик ещё не опубликован на GitHub") from exc
        if exc.code in (403, 429):
            raise UpdateError("Сервис обновлений временно ограничил запросы. Повторите позже") from exc
        raise
    if version_key(release.get("tag_name", "")) <= version_key(current):
        return None
    manifest = next((a for a in release.get("assets", []) if a.get("name") == MANIFEST_NAME), None)
    if not manifest:
        raise UpdateError("Выпуск ещё не готов: нет описания обновления")
    url = asset_url(manifest.get("browser_download_url", ""), release["tag_name"], MANIFEST_NAME)
    metadata = read_json(url)
    result = parse_release(release, metadata)
    description = metadata.get("file_manifest")
    if root is None or not isinstance(description, dict):
        return result
    try:
        from desktop.file_updates import plan_files
        asset = next(a for a in release.get("assets", []) if a.get("name") == FILES_NAME)
        size = description.get("size")
        if (description.get("asset") != FILES_NAME or type(size) is not int or not 0 < size <= MAX_JSON_SIZE
                or asset.get("size") != size or not valid_hash(description.get("sha256"))):
            return result
        digest = asset.get("digest")
        if digest and digest != "sha256:" + description["sha256"]:
            return result
        with _request(asset_url(asset["browser_download_url"], release["tag_name"], FILES_NAME)) as response:
            raw = response.read(size + 1)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != description["sha256"]:
            return result
        files = validate_manifest(json.loads(raw), result.version)
        plan = plan_files(root, current, files, cancel)
        # Prefer a complete installer over hundreds of files if it would actually download less.
        if plan is not None and plan.download_size >= result.size:
            plan = None
        return replace(result, file_plan=plan)
    except Cancelled:
        raise
    except (OSError, ValueError, StopIteration, KeyError, UpdateError):
        # Full installer remains a repair path, including migration from older update formats.
        return result


def prepare_update(release: Release, directory: Path, cancel: threading.Event, progress=lambda n,total:None,
                   argv=None, status=lambda message:None):
    from desktop.file_updates import PreparedUpdate, prepare_files
    if release.file_plan is not None:
        try:
            return prepare_files(release.file_plan, directory, cancel, progress, argv)
        except Cancelled:
            raise
        except Exception:
            if cancel.is_set():
                raise Cancelled()
            status("Малое обновление недоступно. Загружаем полный установщик для восстановления…")
    return PreparedUpdate("installer", download_installer(release, directory, cancel, progress))


def download_installer(release: Release, directory: Path, cancel: threading.Event, progress=lambda done, total: None) -> Path:
    """Stream to a unique temporary file, verify, then atomically expose the .exe."""
    version_key(release.version)
    asset_url(release.url, release.tag, SETUP_NAME)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"ManipulationRadar-Setup-{release.version}.exe"
    fd, tmp_name = tempfile.mkstemp(prefix="radar-", suffix=".part", dir=directory)
    temporary = Path(tmp_name)
    try:
        done = 0
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as out:
            if cancel.is_set():
                raise Cancelled()
            with _request(release.url) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) != release.size:
                    raise UpdateError("Размер загружаемого файла не совпадает с выпуском")
                while True:
                    if cancel.is_set():
                        raise Cancelled()
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > release.size or done > MAX_INSTALLER_SIZE:
                        raise UpdateError("Размер загружаемого файла превышает ожидаемый")
                    digest.update(chunk)
                    out.write(chunk)
                    progress(done, release.size)
        if cancel.is_set():
            raise Cancelled()
        if done != release.size or digest.hexdigest() != release.sha256:
            raise UpdateError("Установщик повреждён: размер или SHA-256 не совпадает. Повторите загрузку")
        os.replace(temporary, target)
        # Cache at most the new installer and one previous installer for a manual rollback.
        try:
            cached = sorted((p for p in directory.glob("ManipulationRadar-Setup-*.exe") if p != target),
                            key=lambda p: p.stat().st_mtime, reverse=True)
            for previous in cached[1:]:
                previous.unlink(missing_ok=True)
        except OSError:
            pass  # cache cleanup cannot invalidate a successfully verified update
        return target
    finally:
        temporary.unlink(missing_ok=True)


def installer_args(pid: int) -> list[str]:
    if pid <= 0:
        raise ValueError("Invalid process ID")
    return ["/SP-", "/SILENT", "/NORESTART", "/NOFORCECLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS",
            "/RADARUPDATE=1", f"/RADARPID={pid}", "/LOG"]
