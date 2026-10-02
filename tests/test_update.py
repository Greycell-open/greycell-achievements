"""Updates from GitHub releases: when to look, what counts as newer, how it installs."""
from datetime import datetime, timedelta, timezone

from openachievements import update
from openachievements.profile import MachineConfig

RELEASE = {"tag_name": "v9.1.0", "html_url": "https://github.com/o/r/releases/tag/v9.1.0", "body": "notes",
           "draft": False, "prerelease": False}


def test_versions_compare_as_numbers():
    assert update.newer("0.10.0", "0.9.9") and update.newer("v0.2", "0.1.0")
    assert not update.newer("0.1.0", "0.1.0") and not update.newer("nightly", "0.1.0")


def test_releases_come_from_the_public_repository_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OA_UPDATE_REPO", raising=False)
    assert update.settings({})["repo"] == "greycell-open/greycell-achievements"


def test_nothing_is_checked_without_a_source(tmp_path, monkeypatch):
    monkeypatch.delenv("OA_UPDATE_REPO", raising=False)
    monkeypatch.setattr(update, "DEFAULT_REPO", None)
    calls = []
    status = update.check(MachineConfig(tmp_path / "m"), get=lambda u: calls.append(u) or RELEASE)
    assert status["available"] is None and calls == []


def test_a_newer_release_is_found_and_github_is_asked_at_most_daily(tmp_path):
    cfg = MachineConfig(tmp_path / "m")
    cfg.save({"update": {"repo": "o/r"}})
    calls = []
    get = lambda u: calls.append(u) or RELEASE
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    first = update.check(cfg, now=now, get=get)
    assert first["available"]["version"] == "9.1.0"
    assert first["available"]["url"] == "https://github.com/o/r/archive/refs/tags/v9.1.0.zip"
    update.check(cfg, now=now + timedelta(hours=5), get=get)
    assert len(calls) == 1                                       # remembered, not asked again
    update.check(cfg, now=now + timedelta(days=1, minutes=1), get=get)
    assert len(calls) == 2


def test_offline_or_turned_off_is_quiet(tmp_path):
    cfg = MachineConfig(tmp_path / "m")
    cfg.save({"update": {"repo": "o/r"}})

    def offline(_url):
        raise OSError("no network")
    assert update.check(cfg, get=offline)["available"] is None
    cfg.save({"update": {"repo": "o/r", "check": False}})
    assert update.check(cfg, get=lambda u: RELEASE)["available"] is None


def test_drafts_prereleases_and_bad_repos_are_ignored(tmp_path):
    assert update.latest("o/r", get=lambda u: {**RELEASE, "prerelease": True}) is None
    cfg = MachineConfig(tmp_path / "m")
    cfg.save({"update": {"repo": "not a repo; rm -rf"}})
    assert update.settings(cfg.load())["repo"] is None


def test_install_uses_this_environments_pip_with_the_release_archive():
    seen = []
    assert update.install({"url": "https://github.com/o/r/archive/refs/tags/v0.2.0.zip"},
                          run=lambda cmd: seen.append(cmd) or 0) == 0
    assert seen[0][1:4] == ["-m", "pip", "install"]
    assert seen[0][-1] == "open-achievements[server] @ https://github.com/o/r/archive/refs/tags/v0.2.0.zip"
