"""Small standalone Windows launcher / transactional file updater. No Qt dependencies."""
import argparse
import contextlib
import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from desktop.distribution import (BUILD_INFO, CORE_NAME, CURRENT_NAME, FILES_NAME, LAUNCHER_NAME,
                                  SCHEMA, UpdateError, atomic_json, directory_name, file_hash,
                                  file_matches, no_link, read_current, read_json_file, safe_file,
                                  validate_manifest, version_key)

log = logging.getLogger("updater")


def wait_for_pid(pid: int, timeout: float = 60) -> None:
    if pid <= 0:
        raise UpdateError("Некорректный процесс приложения")
    if sys.platform != "win32":
        return  # Windows handles are exercised by the packaged integration check in CI.
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() in (87, 1168):
            return  # the app already exited
        raise UpdateError("Не удалось дождаться завершения приложения")
    try:
        if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
            raise UpdateError("Приложение ещё работает. Закройте его и повторите обновление")
    finally:
        kernel.CloseHandle(handle)


@contextlib.contextmanager
def update_lock(root):
    if sys.platform != "win32":
        yield
        return
    from ctypes import wintypes
    import hashlib
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = "Local\\ManipulationRadarUpdate-" + hashlib.sha256(str(root).casefold().encode()).hexdigest()
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise UpdateError("Не удалось заблокировать обновление")
    owned = False
    try:
        owned = kernel.WaitForSingleObject(handle, 0) in (0, 0x80)
        if not owned:
            raise UpdateError("Другое обновление уже устанавливается")
        yield
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def start_core(root: Path, pointer: dict, argv: list[str], ready: Path | None = None):
    name = directory_name(pointer["directory"])
    core = safe_file(root, "versions/" + name)
    executable = safe_file(core, CORE_NAME)
    info = read_json_file(safe_file(core, BUILD_INFO))
    if info.get("version") != pointer["version"] or not executable.is_file():
        raise UpdateError("Файлы приложения не соответствуют номеру версии")
    env = dict(os.environ, RADAR_INSTALL_ROOT=str(root))
    env.pop("RADAR_UPDATE_READY", None)
    if ready:
        env["RADAR_UPDATE_READY"] = str(ready)
    return subprocess.Popen([str(executable), *argv], cwd=core, env=env)


def wait_ready(child, ready: Path, version: str, timeout: float = 60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.is_file():
            message = read_json_file(ready)
            if message.get("pid") == child.pid and message.get("version") == version:
                return
        if child.poll() is not None:
            raise UpdateError("Новая версия завершилась при запуске")
        time.sleep(.2)
    raise UpdateError("Новая версия не подтвердила запуск")


def _stop_our_child(child):
    """Only the child created for this update, never an arbitrary user process."""
    if child is None or child.poll() is not None:
        return
    if sys.platform == "win32":
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]

        def close_window(hwnd, _param):
            process_id = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value == child.pid:
                user.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True
        user.EnumWindows(callback_type(close_window), 0)
        try:
            child.wait(5)
            return
        except subprocess.TimeoutExpired:
            pass
    child.terminate()
    child.wait(10)


def _remove_created(root: Path, created: Path) -> None:
    if created.parent.resolve() != (root / "versions").resolve():
        raise UpdateError("Отказ удаления вне папки версий")
    directory_name(created.name)
    no_link(created)
    shutil.rmtree(created)


def clean_versions(root: Path, keep: set[str]):
    """Keep current and rollback versions. Unknown files/directories are never removed."""
    versions = safe_file(root, "versions")
    for folder in versions.iterdir():
        if folder.name in keep:
            continue
        try:
            directory_name(folder.name)
            no_link(folder)
            manifest = validate_manifest(read_json_file(safe_file(folder, FILES_NAME)))
            if folder.name.split("-", 1)[0] != manifest["version"]:
                continue
            actual = set()
            for path in folder.rglob("*"):
                no_link(path)
                if path.is_file():
                    actual.add(path.relative_to(folder).as_posix())
            if actual - (set(manifest["files"]) | {FILES_NAME}):
                continue
            _remove_created(root, folder)
        except (OSError, ValueError, UpdateError):
            log.info("kept unrecognised or in-use folder %s", folder)


def apply_plan(plan_path: Path, root: Path, pid: int) -> dict:
    root = root.resolve()
    stage = plan_path.resolve().parent
    no_link(stage)
    if read_json_file(stage / "owned.json") != {"schema":1, "type":"radar-file-update"}:
        raise UpdateError("Неизвестная папка подготовки обновления")
    plan = read_json_file(plan_path)
    if plan.get("schema") != SCHEMA or plan.get("root") != str(root):
        raise UpdateError("Обновление предназначено для другой установки")
    target = validate_manifest(plan.get("target"))
    argv = plan.get("argv", [])
    if not isinstance(argv, list) or len(argv) > 20 or any(not isinstance(a,str) or len(a)>500 for a in argv):
        raise UpdateError("Некорректные параметры перезапуска")
    ready = stage / "ready.json"
    ready.unlink(missing_ok=True)
    with update_lock(root):
        old, active = read_current(root)
        if old != plan.get("base") or file_hash(safe_file(active, FILES_NAME)) != plan.get("base_manifest_sha"):
            raise UpdateError("Установленная версия уже изменилась")
        if version_key(target["version"]) <= version_key(old["version"]):
            raise UpdateError("Понижение версии не допускается")
        previous = validate_manifest(read_json_file(active / FILES_NAME), old["version"])
        current_launcher = safe_file(root, LAUNCHER_NAME)
        if not file_matches(current_launcher, previous["launcher"]):
            raise UpdateError("Программа запуска изменилась во время обновления")
        wait_for_pid(pid)
        versions = safe_file(root, "versions")
        name = target["version"] + "-" + uuid.uuid4().hex[:12]
        created = safe_file(versions, directory_name(name))
        created.mkdir()
        switched = False
        launcher_changed = False
        child = None
        try:
            for relative, entry in target["files"].items():
                destination = safe_file(created, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                payload = safe_file(stage, "payload/" + entry["sha256"])
                if payload.is_file():
                    if not file_matches(payload, entry):
                        raise UpdateError("Подготовленный файл повреждён")
                    shutil.copyfile(payload, destination)
                else:
                    source = safe_file(active, relative)
                    if not file_matches(source, entry):
                        raise UpdateError("Исходный файл изменился. Требуется повторная загрузка")
                    try:
                        os.link(source, destination)  # unchanged Qt/Python binaries occupy no extra space
                    except OSError:
                        shutil.copyfile(source, destination)  # filesystems without hardlinks
            info = read_json_file(safe_file(created, BUILD_INFO))
            if info.get("version") != target["version"]:
                raise UpdateError("Номер подготовленной версии не совпадает")
            atomic_json(created / FILES_NAME, target)
            if not file_matches(current_launcher, target["launcher"]):
                payload = safe_file(stage, "payload/" + target["launcher"]["sha256"])
                if not file_matches(payload, target["launcher"]):
                    raise UpdateError("Обновление программы запуска повреждено")
                shutil.copyfile(current_launcher, stage / "launcher.previous.exe")
                replacement = safe_file(root, ".launcher-" + uuid.uuid4().hex + ".tmp")
                try:
                    shutil.copyfile(payload, replacement)
                    os.replace(replacement, current_launcher)
                    launcher_changed = True
                finally:
                    replacement.unlink(missing_ok=True)
            pointer = {"schema":SCHEMA, "version":target["version"], "directory":name,
                       "previous":{"version":old["version"], "directory":old["directory"]}}
            atomic_json(safe_file(root, CURRENT_NAME), pointer)
            switched = True
            child = start_core(root, pointer, argv, ready)
            wait_ready(child, ready, target["version"])
            result = {"ok":True, "version":target["version"], "pid":child.pid, "previous":old["version"]}
            atomic_json(stage / "result.json", result)
            try:
                clean_versions(root, {name, old["directory"]})
            except (OSError, UpdateError):
                log.info("old-version cleanup deferred", exc_info=True)
            return result
        except BaseException as error:
            log.exception("file update failed; restoring %s", old["version"])
            _stop_our_child(child)
            if switched:
                atomic_json(safe_file(root, CURRENT_NAME), old)
            if launcher_changed:
                replacement = safe_file(root, ".launcher-rollback-" + uuid.uuid4().hex + ".tmp")
                try:
                    shutil.copyfile(stage / "launcher.previous.exe", replacement)
                    os.replace(replacement, current_launcher)
                finally:
                    replacement.unlink(missing_ok=True)
            _remove_created(root, created)
            try:
                start_core(root, old, argv)
            except Exception:
                log.exception("could not restart previous version")
            atomic_json(stage / "result.json", {"ok":False, "restored":old["version"], "error":str(error)})
            raise


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments[:1] == ["--apply-plan"]:
            parser = argparse.ArgumentParser()
            parser.add_argument("--apply-plan", type=Path, required=True)
            parser.add_argument("--root", type=Path, required=True)
            parser.add_argument("--pid", type=int, required=True)
            parser.add_argument("--quiet", action="store_true")
            args = parser.parse_args(arguments)
            logging.basicConfig(filename=args.apply_plan.parent / "apply.log", level=logging.INFO, encoding="utf-8",
                                format="%(asctime)s %(levelname)s %(message)s")
            apply_plan(args.apply_plan, args.root, args.pid)
            return 0
        root = Path(sys.executable).resolve().parent
        pointer, _ = read_current(root)
        child = start_core(root, pointer, arguments)
        return child.wait() if "--selftest" in arguments else 0
    except Exception as error:
        log.exception("launcher failed")
        if sys.platform == "win32" and "--quiet" not in arguments:
            ctypes.windll.user32.MessageBoxW(None, f"Не удалось запустить или обновить Manipulation Radar.\n{error}\n"
                                           "При ошибке обновления сохранена предыдущая версия.", "Manipulation Radar", 0x10)
        return 1


if __name__ == "__main__":
    if sys.stdout is None: sys.stdout = open(os.devnull, "w")
    if sys.stderr is None: sys.stderr = open(os.devnull, "w")
    sys.exit(main())
