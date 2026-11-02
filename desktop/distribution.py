"""Shared, stdlib-only distribution format for the app, launcher and CI packaging."""
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

REPOSITORY = "ksen0v/ExchangeAlgoDiscovery"
SETUP_NAME = "ManipulationRadar-Setup.exe"
MANIFEST_NAME = "update.json"
FILES_NAME = "files.json"
CURRENT_NAME = "current.json"
LAUNCHER_NAME = "ManipulationRadar.exe"
CORE_NAME = "RadarApp.exe"
BUILD_INFO = "_internal/build-info.json"
MAX_FILE_SIZE = 1024 * 1024 * 1024
MAX_TOTAL_SIZE = 3 * 1024 * 1024 * 1024
MAX_JSON_SIZE = 8 * 1024 * 1024
MAX_FILES = 5000
SCHEMA = 1


class UpdateError(Exception):
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
    if not isinstance(value, str):
        raise UpdateError("Некорректный адрес обновления")
    url = urlsplit(value)
    if (url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment
            or unquote(url.path) != f"/{REPOSITORY}/releases/download/{tag}/{name}"):
        raise UpdateError("Файлы обновления должны находиться в выпусках этого проекта на GitHub")
    return value


def valid_hash(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json_file(path: Path) -> dict:
    with path.open("rb") as source:
        raw = source.read(MAX_JSON_SIZE + 1)
    if len(raw) > MAX_JSON_SIZE:
        raise UpdateError("Описание обновления слишком большое")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise UpdateError("Некорректное описание обновления")
    return value


def atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".radar-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
            json.dump(value, target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 220 or "\\" in value:
        raise UpdateError("Некорректный путь файла обновления")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise UpdateError("Некорректный путь файла обновления")
    for part in value.split("/"):
        stem = part.split(".", 1)[0].upper()
        if (part in ("", ".", "..") or part.endswith((" ", "."))
                or any(ord(c) < 32 or c in ':<>"|?*' for c in part)
                or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)):
            raise UpdateError("Недопустимый путь файла обновления")
    return value


def no_link(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise UpdateError("Обновление не работает через ссылки и перенаправления папок")


def safe_file(root: Path, relative: str) -> Path:
    relative = safe_relative(relative)
    no_link(root)
    path = root
    for part in relative.split("/"):
        path = path / part
        no_link(path)
    # Also check the canonical boundary before any copy/link/delete/replace operation.
    if not path.resolve().is_relative_to(root.resolve()):
        raise UpdateError("Файл выходит за пределы папки приложения")
    return path


def file_matches(path: Path, entry: dict) -> bool:
    try:
        no_link(path)
        return path.is_file() and path.stat().st_size == entry["size"] and file_hash(path) == entry["sha256"]
    except OSError:
        return False


def validate_entry(entry: dict) -> None:
    if (not isinstance(entry, dict) or not valid_hash(entry.get("sha256"))
            or type(entry.get("size")) is not int or not 0 <= entry["size"] <= MAX_FILE_SIZE):
        raise UpdateError("Некорректный размер или SHA-256 файла обновления")
    source = entry.get("source")
    if source is None:
        return  # baseline files ship in the full installer; missing ones require repair
    if (not isinstance(source, dict) or not valid_hash(source.get("sha256"))
            or type(source.get("size")) is not int or not 0 < source["size"] <= MAX_FILE_SIZE):
        raise UpdateError("Некорректный пакет файла обновления")
    url = source.get("url", "")
    parts = urlsplit(url).path.split("/") if isinstance(url, str) else []
    if len(parts) != 7:
        raise UpdateError("Неверный адрес пакета обновления")
    tag = parts[-2]
    version_key(tag)
    asset_url(url, tag, f"part-{entry['sha256']}.zip")


def validate_manifest(value: dict, version: str | None = None) -> dict:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise UpdateError("Формат обновления требует полного установщика")
    version_key(value.get("version"))
    if version is not None and version_key(value["version"]) != version_key(version):
        raise UpdateError("Версии описания файлов и выпуска не совпадают")
    files = value.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise UpdateError("Некорректный список файлов обновления")
    paths = set()
    for name, entry in files.items():
        safe_relative(name)
        key = name.casefold()
        if key in paths or key == FILES_NAME:
            raise UpdateError("Повторяющиеся имена файлов обновления")
        paths.add(key)
        validate_entry(entry)
    for name in paths:
        parents = PurePosixPath(name).parents
        if any(str(parent) in paths for parent in parents):
            raise UpdateError("Конфликт файлов и папок обновления")
    if sum(e["size"] for e in files.values()) > MAX_TOTAL_SIZE:
        raise UpdateError("Размер обновления превышает допустимый")
    if CORE_NAME not in files or BUILD_INFO not in files:
        raise UpdateError("В обновлении нет приложения или номера версии")
    validate_entry(value.get("launcher"))
    return value


def directory_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9a-f]{12})?", value):
        raise UpdateError("Некорректная папка версии")
    version_key(value.split("-", 1)[0])
    return value


def read_current(root: Path) -> tuple[dict, Path]:
    no_link(root)
    pointer = read_json_file(safe_file(root, CURRENT_NAME))
    if pointer.get("schema") != SCHEMA:
        raise UpdateError("Неизвестный формат установленного приложения")
    name = directory_name(pointer.get("directory"))
    version_key(pointer.get("version"))
    if name.split("-", 1)[0] != pointer["version"]:
        raise UpdateError("Папка и версия приложения не совпадают")
    core = safe_file(root, "versions/" + name)
    if not core.is_dir():
        raise UpdateError("Папка установленной версии не найдена")
    return pointer, core


def installed_root(executable: Path) -> Path | None:
    core = executable.resolve().parent
    if executable.name.casefold() != CORE_NAME.casefold() or core.parent.name != "versions":
        return None
    root = core.parent.parent
    try:
        _, active = read_current(root)
        if active.resolve() == core:
            return root
    except (OSError, ValueError, UpdateError):
        pass
    return None
