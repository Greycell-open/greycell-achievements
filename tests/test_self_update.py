"""The Windows app updating itself from greycell.app, against fakes: nothing is downloaded or run."""
import hashlib
import io
import sys
from datetime import date

import pytest

from openachievements import self_update, update
from openachievements.profile import MachineConfig

BLOB = b"MZ fake installer " * 1000
GOOD = {"version": "9.1.0", "setup": "https://greycell.app/downloads/greycell-achievements/S-9.1.0.exe",
        "sha256": hashlib.sha256(BLOB).hexdigest(), "size": len(BLOB), "notes": "Faster sync"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    self_update.STATE.update(phase=None, error=None, version=None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(update, "platform_key", lambda *a: "windows-x86_64")   # these are the Windows app's updates


def opener(body=BLOB):
    return lambda url: io.BytesIO(body)


@pytest.mark.parametrize("bad", [
    {"sha256": "nope"}, {"setup": "http://greycell.app/x.exe"}, {"setup": "https://evil.example/x.exe"},
    {"size": 0}, {"version": "latest"}])
def test_a_manifest_that_cannot_be_trusted_offers_nothing(bad):
    assert update.latest_setup(update.MANIFEST, lambda url: {**GOOD, **bad}) is None
    assert update.latest_setup(update.MANIFEST, lambda url: GOOD)["setup"] == GOOD["setup"]


def test_only_a_file_matching_the_checksum_is_kept(tmp_path):
    release = update.latest_setup(update.MANIFEST, lambda url: GOOD)
    path = self_update.download(release, tmp_path, opener())
    assert path.read_bytes() == BLOB and path.name == "GreycellAchievementsSetup-9.1.0.exe"
    for body in (BLOB[:-1] + b"X", BLOB + b"more", BLOB[:100]):
        with pytest.raises(self_update.UpdateError):
            self_update.download(release, tmp_path / "other", opener(body))
        assert not any((tmp_path / "other").iterdir())                 # nothing half-written stays behind


def test_install_runs_the_checked_installer_silently_then_quits(tmp_path):
    release = update.latest_setup(update.MANIFEST, lambda url: GOOD)
    ran, quit = [], []
    assert self_update.install(release, lambda: quit.append(1), opener(), lambda cmd, **kw: ran.append(cmd), tmp_path)
    assert ran[0][1:] == ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"] and ran[0][0].endswith("9.1.0.exe")
    assert quit == [1]


def test_a_bad_download_never_runs_and_the_app_keeps_going(tmp_path):
    release = update.latest_setup(update.MANIFEST, lambda url: GOOD)
    ran, quit = [], []
    assert not self_update.install(release, lambda: quit.append(1), opener(b"tampered"),
                                   lambda cmd, **kw: ran.append(cmd), tmp_path)
    assert ran == [] and quit == [] and self_update.STATE["phase"] == "error"


def look(cfg, playing=False, answer=True):
    asked, started = [], []
    result = self_update.look_once(cfg, lambda: None, lambda: playing,
                                   ask=lambda t, x: asked.append(x) or answer, tell=lambda t, x: None,
                                   start=lambda r, q: started.append(r["version"]) or True)
    return result, asked, started


def test_the_popup_asks_once_a_day_and_never_during_a_game(tmp_path, monkeypatch):
    cfg = MachineConfig(tmp_path / "m")
    monkeypatch.setattr(update, "_get_json", lambda url: GOOD)
    monkeypatch.setattr(update, "check", lambda c, **kw: {"available": {**GOOD, "version": "9.1.0"},
                                                          "installable": True})
    assert look(cfg, playing=True)[0] == "playing"
    result, asked, started = look(cfg, answer=False)
    assert result == "declined" and "9.1.0 is available" in asked[0] and "Faster sync" in asked[0]
    assert look(cfg)[0] == "already"                                      # not again today
    config = cfg.load()
    config["update"]["asked"]["on"] = "2000-01-01"
    cfg.save(config)
    result, _, started = look(cfg)
    assert result == "installing" and started == ["9.1.0"]


def test_no_popup_without_an_installable_update(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "check", lambda c, **kw: {"available": None, "installable": False})
    assert look(MachineConfig(tmp_path / "m"))[0] == "none"


def test_the_dashboard_button_installs_and_the_pulse_carries_the_version(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements import __version__
    from openachievements.local_app import create_local_app
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    assert local.get("/v1/local/pulse").json()["version"] == __version__
    monkeypatch.setattr(update, "_get_json", lambda url: GOOD)
    monkeypatch.setattr(self_update, "QUIT", None)
    assert local.post("/v1/local/update/install", headers=h).status_code == 400   # not the Windows app
    started = []
    monkeypatch.setattr(self_update, "QUIT", lambda: None)
    monkeypatch.setattr(self_update, "install_in_background", lambda r, q: started.append(r["version"]) or True)
    assert local.post("/v1/local/update/install").status_code == 403
    assert local.post("/v1/local/update/install", headers=h).json()["started"] and started == ["9.1.0"]
    u = local.get("/v1/local/update").json()
    assert u["installable"] and u["available"]["setup"] == GOOD["setup"]


def test_the_installer_starts_with_a_clean_environment(tmp_path, monkeypatch):
    """Inherited one-file bootloader variables made the updated app load Python
    from the old app's deleted folder: "Failed to load Python DLL"."""
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\Temp\_MEI123")
    monkeypatch.setenv("_MEIPASS2", r"C:\Temp\_MEI123")
    monkeypatch.setenv("OPENACHIEVEMENTS_HOME", "kept")
    seen = {}
    self_update.launch(tmp_path / "S.exe", lambda cmd, **kw: seen.update(kw))
    env = seen["env"]
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1" and env["OPENACHIEVEMENTS_HOME"] == "kept"
    assert not any(k.upper().startswith("_PYI_") or k.upper() == "_MEIPASS2" for k in env)


def test_check_for_updates_in_about_asks_now(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    seen = []
    monkeypatch.setattr(update, "check", lambda c, **kw: seen.append(kw.get("force")) or
                        {"current": "1.0.0", "available": None, "installable": False})
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    assert local.get("/v1/local/update").json()["current"] == "1.0.0"
    local.get("/v1/local/update?force=true")
    assert seen == [False, True]


def test_settings_saved_during_an_update_check_survive_it(tmp_path, monkeypatch):
    """The check waits on the network; a RetroAchievements link saved meanwhile must not be lost."""
    cfg = MachineConfig(tmp_path / "m")

    def slow_get(url):
        cfg.edit(lambda c: c.setdefault("retroachievements", {}).__setitem__("p", {"username": "Zein"}))
        return GOOD
    update.check(cfg, get=slow_get, force=True)
    saved = cfg.load()
    assert saved["retroachievements"] == {"p": {"username": "Zein"}}
    assert saved["update"]["latest"]["version"] == "9.1.0"


def test_an_installer_that_cannot_start_is_an_error_and_can_be_retried(tmp_path):
    release = update.latest_setup(update.MANIFEST, lambda url: GOOD)
    quit = []

    def blocked(cmd, **kw):
        raise PermissionError("quarantined")
    assert not self_update.install(release, lambda: quit.append(1), opener(), blocked, tmp_path)
    assert self_update.STATE["phase"] == "error" and "could not start" in self_update.STATE["error"]
    assert quit == []                                                     # the app keeps running
    ran = []
    assert self_update.install(release, lambda: quit.append(1), opener(), lambda c, **k: ran.append(c), tmp_path)
    assert ran and quit == [1]


def test_older_downloaded_installers_are_cleared(tmp_path):
    (tmp_path / "GreycellAchievementsSetup-0.9.0.exe").write_bytes(b"old")
    release = update.latest_setup(update.MANIFEST, lambda url: GOOD)
    self_update.download(release, tmp_path, opener())
    assert [p.name for p in tmp_path.iterdir()] == ["GreycellAchievementsSetup-9.1.0.exe"]


def test_a_real_settings_writer_and_the_update_check_never_lose_each_other(tmp_path):
    """Round 2: every machine.json writer goes through the same lock. The sound
    setting changes while the update check waits on the network."""
    import threading
    from types import SimpleNamespace
    from openachievements import notify
    cfg = MachineConfig(tmp_path / "m")
    fake_profile = SimpleNamespace(config=cfg)
    started, release = threading.Event(), threading.Event()

    def slow_get(url):
        started.set()
        release.wait(5)
        return GOOD
    t = threading.Thread(target=update.check, args=(cfg,), kwargs={"get": slow_get, "force": True})
    t.start()
    started.wait(5)
    notify.change(fake_profile, unlock_sound="bwoop")               # a production writer, mid-check
    release.set()
    t.join(5)
    saved = cfg.load()
    assert saved["notify"]["unlock_sound"] == "bwoop" and saved["update"]["latest"]["version"] == "9.1.0"


def test_check_now_from_the_tray_says_what_it_found(tmp_path, monkeypatch):
    cfg = MachineConfig(tmp_path / "m")
    notes, asked, started = [], [], []
    run = lambda: self_update.check_now(cfg, lambda: None, lambda t, x: notes.append(t),
                                        ask=lambda t, x: asked.append(x) or True,
                                        start=lambda r, q: started.append(r["version"]) or True)

    def offline(url):
        raise OSError("no network")
    monkeypatch.setattr(update, "_get_json", offline)
    assert run() == "unreachable" and notes[-1] == "Could not check for updates"
    monkeypatch.setattr(update, "_get_json", lambda url: {**GOOD, "version": "0.0.1"})
    assert run() == "none" and notes[-1] == "Greycell Achievements is up to date"
    monkeypatch.setattr(update, "_get_json", lambda url: GOOD)
    assert run() == "installing" and started == ["9.1.0"] and asked     # asks even if asked today


def test_the_first_look_after_start_asks_greycell_now(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    cfg = MachineConfig(tmp_path / "m")
    cfg.save({"update": {"last_check": datetime.now(timezone.utc).isoformat(), "latest": None}})   # checked today
    calls = []
    monkeypatch.setattr(update, "_get_json", lambda url: calls.append(url) or GOOD)
    look = lambda force: self_update.look_once(cfg, lambda: None, lambda: False, ask=lambda t, x: False,
                                               tell=lambda t, x: None, force=force)
    assert look(False) == "none" and calls == []            # a later look trusts today's answer
    assert look(True) == "declined" and calls == [update.MANIFEST]


def test_the_pulse_carries_a_found_update_without_asking_the_network(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    monkeypatch.setattr(update, "_get_json", lambda url: pytest.fail("the pulse asked the network"))
    assert local.get("/v1/local/pulse").json()["update"] is None
    monkeypatch.setattr(update, "_get_json", lambda url: GOOD)
    update.check(profile.config, force=True)                # what the startup check does
    monkeypatch.setattr(update, "_get_json", lambda url: pytest.fail("the pulse asked the network"))
    assert local.get("/v1/local/pulse").json()["update"]["available"]["version"] == "9.1.0"


def test_no_popup_before_the_watcher_has_looked_for_games():
    assert self_update.game_may_be_running({"playing": [], "at": None})          # just started: unknown
    assert self_update.game_may_be_running({"playing": ["steam-1"], "at": 5.0})
    assert not self_update.game_may_be_running({"playing": [], "at": 5.0})


def test_check_now_says_when_an_update_is_already_on_its_way(tmp_path, monkeypatch):
    cfg = MachineConfig(tmp_path / "m")
    monkeypatch.setattr(update, "_get_json", lambda url: GOOD)
    self_update.STATE.update(phase="downloading")
    notes = []
    assert self_update.check_now(cfg, lambda: None, lambda t, x: notes.append(t)) == "already"
    assert notes == ["Update already on its way"]
