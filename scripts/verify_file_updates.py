"""Windows CI: a real packaged launcher updates a running, installed demo app."""
import copy
import io
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from desktop import distribution as d, updates
from desktop.file_updates import plan_files, prepare_files
from desktop.launcher import wait_for_pid
from desktop.version import VERSION
from scripts.file_release import build_manifest


def main():
    workspace = Path(__file__).resolve().parent.parent
    dist = workspace / "dist"
    installed = workspace / "file-update-test"
    data = workspace / "file-update-selftest"
    data.mkdir()
    cache = data / "updates"
    cache.mkdir()
    # A real earlier version with only a different chart file and external build metadata.
    parts = VERSION.split(".")
    # Also works for 1.0.0: borrow from the first nonzero version component.
    for index in range(len(parts) - 1, -1, -1):
        if int(parts[index]) > 0:
            parts[index] = str(int(parts[index]) - 1)
            break
        parts[index] = "0"
    baseline = ".".join(parts)
    core = installed / "versions" / baseline
    core.parent.mkdir(parents=True)
    def link_or_copy(source, target):
        try: os.link(source, target)
        except OSError: shutil.copy2(source, target)
    shutil.copytree(dist / "RadarApp", core, copy_function=link_or_copy)
    shutil.copy2(dist / d.LAUNCHER_NAME, installed / d.LAUNCHER_NAME)
    info = d.read_json_file(core / d.BUILD_INFO)
    d.atomic_json(core / d.BUILD_INFO, {**info,"version":baseline})  # replace, never mutate hardlinks
    chart = core / "_internal/static/chart.js"
    changed = chart.with_suffix(".previous")
    changed.write_bytes(chart.read_bytes() + b"\n// previous release\n")
    os.replace(changed, chart)
    previous = build_manifest(core, installed / d.LAUNCHER_NAME, baseline, workspace / "unused-parts")
    d.atomic_json(core / d.FILES_NAME, previous)
    d.atomic_json(installed / d.CURRENT_NAME, {"schema":1,"version":baseline,"directory":baseline})
    packages = workspace / "file-update-parts"
    target = build_manifest(dist / "RadarApp", dist / d.LAUNCHER_NAME, VERSION, packages, previous)
    prefs = data / "desktop.json"
    env_file = data / ".env"
    prefs.write_text('{"overlay":{"min_usd":1234}}', encoding="utf-8")
    env_file.write_text("# Existing configuration\n", encoding="utf-8")
    before = {p.name:d.file_hash(p) for p in (prefs,env_file)}
    plan = plan_files(installed, baseline, target)
    assert set(plan.changed) == {d.BUILD_INFO,"_internal/static/chart.js"}
    requested = []
    class Download(io.BytesIO):
        def __init__(self, content):
            super().__init__(content); self.headers={"Content-Length":str(len(content))}
    def download(url):
        name = url.rsplit("/",1)[1]
        requested.append(name)
        return Download((packages / name).read_bytes())
    original = updates._request
    updates._request = download
    try:
        prepared = prepare_files(plan, cache, threading.Event(), argv=["--demo","--selftest","8"])
    finally:
        updates._request = original
    env = dict(os.environ, RADAR_HOME=str(data))
    old = subprocess.Popen([str(core / d.CORE_NAME),"--demo","--selftest","8"], env=env)
    try:
        result = subprocess.run([str(prepared.launcher),"--apply-plan",str(prepared.path),"--root",str(installed),
                                 "--pid",str(old.pid),"--quiet"], env=env, timeout=150)
        assert result.returncode == 0, (prepared.path.parent / "apply.log").read_text("utf-8", errors="replace")
        applied = d.read_json_file(prepared.path.parent / "result.json")
        assert applied["ok"] and applied["version"] == VERSION
        wait_for_pid(applied["pid"], 90)
        smoke = d.read_json_file(data / "selftest.json")
        assert smoke["ok"] and smoke["version"] == VERSION
        pointer, active = d.read_current(installed)
        assert pointer["previous"]["version"] == baseline
        assert before == {p.name:d.file_hash(p) for p in (prefs,env_file)}
        runtime = "_internal/PySide6/Qt6WebEngineCore.dll"
        hardlinked = (core / runtime).stat().st_ino == (active / runtime).stat().st_ino
        report = {"ok":True,"from":baseline,"to":VERSION,"downloaded_files":len(requested),
                  "download_bytes":plan.download_size,"changed_files":list(plan.changed),
                  "runtime_hardlinked":hardlinked,"user_data_preserved":True}
        d.atomic_json(data / "file-update-report.json", report)
        print(json.dumps(report))
    finally:
        if old.poll() is None:
            old.terminate(); old.wait(10)


if __name__ == "__main__":
    main()
