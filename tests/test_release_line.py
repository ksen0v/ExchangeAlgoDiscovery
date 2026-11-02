import pytest

from desktop.distribution import UpdateError
from scripts.prepare_version import choose_version
from scripts.retire_legacy_releases import legacy_candidates


def test_release_line_starts_at_one_and_increments_published_versions():
    assert choose_version("1.0.0", {"tag_name": "v0.1.40"}, "new") == "1.0.0"
    assert choose_version("1.0.0", {"tag_name": "v1.0.0", "target_commitish": "old"}, "new") == "1.0.1"
    assert choose_version("1.0.0", {"tag_name": "v1.0.1", "target_commitish": "same"}, "same") == "1.0.1"
    assert choose_version("1.1.0", {"tag_name": "v1.0.19"}, "new") == "1.1.0"
    assert choose_version("1.0.0", None, "new") == "1.0.0"


def test_explicit_tags_cannot_publish_old_line_or_invalid_paths():
    assert choose_version("1.0.0", None, "new", "v1.2.0") == "1.2.0"
    with pytest.raises(ValueError):
        choose_version("1.0.0", None, "new", "v0.1.99")
    with pytest.raises(UpdateError):
        choose_version("1.0.0", None, "new", "../1.0.0")


def test_only_pre_one_releases_are_selected_for_retirement():
    versions = [{"tag_name": tag} for tag in ("v0.1.40", "v0.2.0", "v1.0.0", "v1.0.1", "notes", None)]
    assert [r["tag_name"] for r in legacy_candidates(versions)] == ["v0.1.40", "v0.2.0"]
