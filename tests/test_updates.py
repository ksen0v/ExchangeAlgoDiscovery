"""Updater validation and cancellation, using in-memory downloads only (CI)."""
import hashlib
import io
import threading

import pytest

from desktop import updates as u


def release_data(content=b"installer"):
    url = f"https://github.com/{u.REPOSITORY}/releases/download/v0.1.20/{u.SETUP_NAME}"
    sha = hashlib.sha256(content).hexdigest()
    return ({"tag_name": "v0.1.20", "draft": False, "prerelease": False,
             "assets": [{"name": u.SETUP_NAME, "browser_download_url": url, "size": len(content),
                         "digest": f"sha256:{sha}"}]},
            {"version": "0.1.20", "installer": u.SETUP_NAME, "sha256": sha, "size": len(content)})


def test_versions_are_numeric_and_reject_paths():
    assert u.version_key("v0.1.10") > u.version_key("0.1.9")
    assert u.version_key("1.2.3") == u.version_key("1.2.3.0")
    for value in ("../a", "vlatest", "0.1.20-preview", "1.2.70000", None):
        with pytest.raises(u.UpdateError):
            u.version_key(value)


def test_only_this_repositories_release_assets_are_accepted():
    release, manifest = release_data()
    parsed = u.parse_release(release, manifest)
    assert parsed.version == "0.1.20" and parsed.size == len(b"installer")
    for url in (parsed.url.replace("https:", "http:"), parsed.url.replace("github.com", "github.com.evil.test"),
                parsed.url.replace(u.REPOSITORY, "other/project"), parsed.url + "?redirect=elsewhere"):
        with pytest.raises(u.UpdateError):
            u.asset_url(url, parsed.tag, u.SETUP_NAME)


@pytest.mark.parametrize("changes", [{"sha256": ""}, {"sha256": "a" * 64}, {"size": 1},
                                      {"version": "0.1.19"}, {"installer": "other.exe"}])
def test_bad_manifest_is_rejected(changes):
    release, manifest = release_data()
    manifest.update(changes)
    with pytest.raises(u.UpdateError):
        u.parse_release(release, manifest)


class Download(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.headers = {"Content-Length": str(len(content))}


def test_download_is_verified_before_exposing_exe(tmp_path, monkeypatch):
    content = b"verified installer bytes"
    parsed = u.parse_release(*release_data(content))
    monkeypatch.setattr(u, "_request", lambda url: Download(content))
    progress = []
    path = u.download_installer(parsed, tmp_path, threading.Event(), lambda n, total: progress.append((n, total)))
    assert path.read_bytes() == content
    assert progress[-1] == (len(content), len(content))
    assert not list(tmp_path.glob("*.part"))


def test_corrupt_download_and_cancellation_never_leave_an_executable(tmp_path, monkeypatch):
    parsed = u.parse_release(*release_data(b"right"))
    monkeypatch.setattr(u, "_request", lambda url: Download(b"wrong"))
    with pytest.raises(u.UpdateError):
        u.download_installer(parsed, tmp_path, threading.Event())
    assert not list(tmp_path.iterdir())
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(u.Cancelled):
        u.download_installer(parsed, tmp_path, cancel)
    assert not list(tmp_path.iterdir())


def test_existing_verified_exe_is_preserved_after_failed_retry(tmp_path, monkeypatch):
    content = b"right"
    parsed = u.parse_release(*release_data(content))
    monkeypatch.setattr(u, "_request", lambda url: Download(content))
    path = u.download_installer(parsed, tmp_path, threading.Event())
    monkeypatch.setattr(u, "_request", lambda url: Download(b"wrong"))
    with pytest.raises(u.UpdateError):
        u.download_installer(parsed, tmp_path, threading.Event())
    assert path.read_bytes() == content


def test_check_does_not_offer_a_downgrade_or_require_an_account(monkeypatch):
    release, manifest = release_data()
    release["assets"].append({"name": u.MANIFEST_NAME,
                              "browser_download_url": f"https://github.com/{u.REPOSITORY}/releases/download/v0.1.20/update.json"})
    monkeypatch.setattr(u, "read_json", lambda url: release if url == u.RELEASES_URL else manifest)
    assert u.check_release("0.1.19").version == "0.1.20"
    assert u.check_release("0.1.20") is None
    assert u.check_release("0.1.21") is None


def test_update_waits_for_old_pid_and_never_forces_a_reboot():
    args = u.installer_args(1234)
    assert "/RADARPID=1234" in args and "/RADARUPDATE=1" in args
    assert "/NORESTART" in args and "/NOFORCECLOSEAPPLICATIONS" in args
    with pytest.raises(ValueError):
        u.installer_args(0)
