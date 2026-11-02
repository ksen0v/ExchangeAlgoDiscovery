"""Transactional file updates. These checks run on GitHub, not on the user's PC."""
import copy
import hashlib
import io
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

from desktop import distribution as d, file_updates as f, launcher, updates


class Download(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.headers = {"Content-Length":str(len(content))}


def package(content):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("payload", (2020,1,1,0,0,0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, content)
    return result.getvalue()


def entry(content, source=False):
    value = {"size":len(content), "sha256":hashlib.sha256(content).hexdigest(), "source":None}
    if source:
        compressed = package(content)
        value["source"] = {"size":len(compressed), "sha256":hashlib.sha256(compressed).hexdigest(),
                           "url":f"https://github.com/{d.REPOSITORY}/releases/download/v0.1.11/part-{value['sha256']}.zip"}
    return value


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "installed"
    core = root / "versions" / "0.1.10"
    core.mkdir(parents=True)
    contents = {d.CORE_NAME:b"original app", d.BUILD_INFO:b'{"version":"0.1.10"}',
                "_internal/static/chart.js":b"original chart", "_internal/runtime.dll":b"unchanged runtime",
                "_internal/obsolete.txt":b"old package file"}
    for name, content in contents.items():
        path = core / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
    helper = b"standalone helper"
    (root / d.LAUNCHER_NAME).write_bytes(helper)
    old = {"schema":1, "version":"0.1.10", "files":{n:entry(c) for n,c in contents.items()}, "launcher":entry(helper)}
    d.atomic_json(core / d.FILES_NAME, old)
    d.atomic_json(root / d.CURRENT_NAME, {"schema":1, "version":"0.1.10", "directory":"0.1.10"})
    target_contents = {**contents, d.BUILD_INFO:b'{"version":"0.1.11"}', "_internal/static/chart.js":b"new chart"}
    target_contents.pop("_internal/obsolete.txt")
    target = {"schema":1, "version":"0.1.11", "files":{n:entry(c, n in (d.BUILD_INFO,"_internal/static/chart.js")) for n,c in target_contents.items()}, "launcher":entry(helper)}
    blobs = {e["source"]["url"]:package(target_contents[n]) for n,e in target["files"].items() if e["source"]}
    monkeypatch.setattr(updates, "_request", lambda url: Download(blobs[url]))
    monkeypatch.setattr(launcher, "wait_for_pid", lambda pid:None)
    return root, core, target, blobs


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/app.exe", "a\\b", "a//b", "a/../b", "a:stream", "CON.txt", "a/NUL", "a.", "a "])
def test_package_paths_cannot_escape_or_alias_windows_files(path):
    with pytest.raises(d.UpdateError): d.safe_relative(path)


def test_manifest_rejects_case_collisions_and_foreign_payloads(installation):
    _, _, target, _ = installation
    bad = copy.deepcopy(target)
    bad["files"][d.CORE_NAME.lower()] = bad["files"][d.CORE_NAME]
    with pytest.raises(d.UpdateError): d.validate_manifest(bad)
    bad = copy.deepcopy(target)
    bad["files"][d.BUILD_INFO]["source"]["url"] = "https://evil.example/payload.zip"
    with pytest.raises(d.UpdateError): d.validate_manifest(bad)
    bad = copy.deepcopy(target)
    bad["files"]["_internal"] = entry(b"file conflicts with directory")
    with pytest.raises(d.UpdateError): d.validate_manifest(bad)


def test_only_changed_files_are_downloaded_and_existing_files_are_untouched(installation, tmp_path):
    root, core, target, blobs = installation
    plan = f.plan_files(root, "0.1.10", target)
    assert set(plan.changed) == {d.BUILD_INFO, "_internal/static/chart.js"}
    assert plan.download_size == sum(len(data) for data in blobs.values())
    prepared = f.prepare_files(plan, tmp_path / "updates", threading.Event())
    assert prepared.mode == "files" and prepared.launcher.is_file()
    assert len(list((prepared.path.parent / "payload").iterdir())) == 2
    assert d.read_current(root)[0]["version"] == "0.1.10"
    assert (core / "_internal/static/chart.js").read_bytes() == b"original chart"


def test_missing_baseline_files_require_full_repair(installation):
    root, core, target, _ = installation
    (core / "_internal/runtime.dll").write_bytes(b"corrupted runtime")
    assert f.plan_files(root, "0.1.10", target) is None


def test_cancelled_or_corrupt_download_leaves_installation_untouched(installation, tmp_path, monkeypatch):
    root, core, target, _ = installation
    plan = f.plan_files(root, "0.1.10", target)
    cancelled = threading.Event(); cancelled.set()
    with pytest.raises(updates.Cancelled): f.prepare_files(plan, tmp_path / "cancelled", cancelled)
    assert not list((tmp_path / "cancelled").iterdir())
    monkeypatch.setattr(updates, "_request", lambda url:Download(b"wrong bytes"))
    with pytest.raises(d.UpdateError): f.prepare_files(plan, tmp_path / "corrupt", threading.Event())
    assert not list((tmp_path / "corrupt").iterdir())
    assert d.read_current(root)[0]["version"] == "0.1.10"


def test_zip_cannot_write_extra_paths(tmp_path):
    malicious = tmp_path / "bad.zip"
    with zipfile.ZipFile(malicious, "w") as archive: archive.writestr("../escaped.txt", b"bad")
    with pytest.raises(d.UpdateError): f.extract_payload(malicious, tmp_path / "output", entry(b"bad"), threading.Event())
    assert not (tmp_path.parent / "escaped.txt").exists()


class Child:
    pid = 424242
    def poll(self): return 0


def test_apply_switches_complete_version_and_preserves_rollback(installation, tmp_path, monkeypatch):
    root, old_core, target, _ = installation
    prepared = f.prepare_files(f.plan_files(root,"0.1.10",target), tmp_path / "updates", threading.Event())
    launches = []
    def start(root, pointer, argv, ready=None):
        launches.append(pointer)
        if ready: d.atomic_json(ready, {"pid":Child.pid,"version":pointer["version"]})
        return Child()
    monkeypatch.setattr(launcher, "start_core", start)
    result = launcher.apply_plan(prepared.path, root, 123)
    current, new_core = d.read_current(root)
    assert result["ok"] and current["version"] == "0.1.11"
    assert (new_core / "_internal/static/chart.js").read_bytes() == b"new chart"
    assert not (new_core / "_internal/obsolete.txt").exists()
    assert (old_core / "_internal/obsolete.txt").exists()
    assert current["previous"]["directory"] == old_core.name
    assert len(launches) == 1
    assert d.file_hash(old_core / "_internal/runtime.dll") == d.file_hash(new_core / "_internal/runtime.dll")


def test_failed_start_rolls_back_and_restarts_previous_version(installation, tmp_path, monkeypatch):
    root, core, target, _ = installation
    prepared = f.prepare_files(f.plan_files(root,"0.1.10",target), tmp_path / "updates", threading.Event())
    launches = []
    monkeypatch.setattr(launcher, "start_core", lambda root,pointer,argv,ready=None: launches.append(pointer["version"]) or Child())
    monkeypatch.setattr(launcher, "wait_ready", lambda *a: (_ for _ in ()).throw(d.UpdateError("startup failed")))
    with pytest.raises(d.UpdateError): launcher.apply_plan(prepared.path, root, 123)
    assert d.read_current(root)[0]["version"] == "0.1.10"
    assert launches == ["0.1.11", "0.1.10"]
    assert list((root / "versions").iterdir()) == [core]
    assert not d.read_json_file(prepared.path.parent / "result.json")["ok"]


def test_stale_plan_cannot_replace_another_update(installation, tmp_path):
    root, core, target, _ = installation
    prepared = f.prepare_files(f.plan_files(root,"0.1.10",target), tmp_path / "updates", threading.Event())
    d.atomic_json(root / d.CURRENT_NAME, {"schema":1,"version":"0.1.10","directory":"0.1.10","changed":True})
    with pytest.raises(d.UpdateError): launcher.apply_plan(prepared.path, root, 123)
    assert (core / "_internal/static/chart.js").read_bytes() == b"original chart"


def test_file_download_failure_uses_the_full_installer(installation, tmp_path, monkeypatch):
    root, _, target, _ = installation
    release = updates.Release("0.1.11", "v0.1.11", "unused", "a"*64, 1000, f.plan_files(root,"0.1.10",target))
    monkeypatch.setattr(updates, "_request", lambda url: (_ for _ in ()).throw(OSError("asset unavailable")))
    monkeypatch.setattr(updates, "download_installer", lambda *a: tmp_path / "full.exe")
    messages = []
    result = updates.prepare_update(release, tmp_path / "cache", threading.Event(), status=messages.append)
    assert result.mode == "installer" and messages
