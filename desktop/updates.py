"""GitHub release checks and verified installer downloads. No background polling."""
import hashlib
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPOSITORY = "ksen0v/ExchangeAlgoDiscovery"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
SETUP_NAME = "ManipulationRadar-Setup.exe"
MANIFEST_NAME = "update.json"
MAX_INSTALLER_SIZE = 1024 * 1024 * 1024


class UpdateError(Exception):
    pass


class Cancelled(UpdateError):
    pass


def version_key(value: str) -> tuple[int, ...]:
    if not isinstance(value, str):
        raise UpdateError("Неверный номер версии в выпуске")
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        raise UpdateError("Неверный номер версии в выпуске")
    parts = tuple(int(x or 0) for x in match.groups())
    if any(p > 65535 for p in parts):
        raise UpdateError("Номер версии вне допустимого диапазона Windows")
    return parts


def asset_url(value: str, tag: str, name: str) -> str:
    url = urlsplit(value)
    expected = f"/{REPOSITORY}/releases/download/{tag}/{name}"
    if (url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment
            or unquote(url.path) != expected):
        raise UpdateError("Файл обновления должен находиться в выпуске этого проекта на GitHub")
    return value


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    url: str
    sha256: str
    size: int


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


def check_release(current: str) -> Release | None:
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
    return parse_release(release, read_json(url))


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
