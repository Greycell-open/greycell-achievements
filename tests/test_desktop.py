"""GreycellAchievements.exe: the double-click path, the popup inside the exe,
and updates that send a frozen app to the download page."""
import sys

import pytest

from openachievements import desktop, notify, update
from openachievements.profile import MachineConfig


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Profiles and machine settings in a scratch folder, never the real ones."""
    monkeypatch.setenv("OPENACHIEVEMENTS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENACHIEVEMENTS_CONFIG", str(tmp_path / "config"))
    calls = {"cli": [], "browser": []}
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: calls["browser"].append(url))
    monkeypatch.setattr(desktop, "_edge", lambda: None)          # never a real browser window in tests
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)
    return tmp_path, calls


def test_first_run_names_the_profile_after_the_windows_user(sandbox, monkeypatch):
    tmp_path, _ = sandbox
    monkeypatch.setattr(desktop.getpass, "getuser", lambda: "ada")
    assert desktop._first_profile() is True
    assert desktop._first_profile() is False                  # only ever once
    from openachievements.profile import Profile
    assert Profile.open().state()["profile"]["name"] == "ada"


def test_a_double_click_serves_and_opens_the_library_and_background_does_not(sandbox, monkeypatch):
    _, calls = sandbox
    served = []
    monkeypatch.setattr(desktop, "_running", lambda: False)
    monkeypatch.setattr(desktop, "_log_to_file", lambda: None)
    monkeypatch.setattr(desktop, "serve_in_tray", lambda background: served.append(background) or 0)
    assert desktop.main([]) == 0 and desktop.main(["--background"]) == 0
    assert served == [False, True]


def test_a_second_launch_only_opens_the_browser(sandbox, monkeypatch):
    _, calls = sandbox
    from openachievements import cli
    monkeypatch.setattr(desktop, "_running", lambda: True)
    monkeypatch.setattr(cli, "main", lambda argv: calls["cli"].append(argv) or 0)
    assert desktop.main([]) == 0
    assert calls["cli"] == [] and calls["browser"] == ["http://127.0.0.1:8788"]
    assert desktop.main(["--background"]) == 0                # Start with Windows, already running: nothing
    assert calls["browser"] == ["http://127.0.0.1:8788"]


class FakeRegistry:
    HKEY_CURRENT_USER, REG_SZ = "HKCU", 1

    def __init__(self):
        self.values = {}

    class _Key:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def OpenKey(self, root, path): return self._Key()
    def CreateKey(self, root, path): return self._Key()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise OSError("missing")
        return self.values[name], 1

    def SetValueEx(self, key, name, reserved, kind, value): self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise OSError("missing")
        del self.values[name]


def test_start_with_windows_is_one_value_for_this_user_only(monkeypatch):
    from openachievements import tray
    reg = FakeRegistry()
    monkeypatch.setattr(sys, "executable", r"C:\Apps\GreycellAchievements.exe")
    assert tray.autostart_enabled(reg) is False
    tray.set_autostart(True, reg)
    assert reg.values == {"GreycellAchievements": '"C:\\Apps\\GreycellAchievements.exe" --background'}
    assert tray.autostart_enabled(reg) is True
    tray.set_autostart(False, reg)
    tray.set_autostart(False, reg)                            # turning it off twice is fine
    assert reg.values == {} and tray.autostart_enabled(reg) is False


def test_commands_pass_through_and_toast_mode_runs_the_popup(sandbox, monkeypatch):
    _, calls = sandbox
    from openachievements import cli, toast
    monkeypatch.setattr(cli, "main", lambda argv: calls["cli"].append(argv) or 0)
    shown = []
    monkeypatch.setattr(toast, "main", lambda: shown.append(True))
    desktop.main(["save", "check"])
    desktop.main(["--toast"])
    assert calls["cli"] == [["save", "check"]] and shown == [True]


def test_inside_the_exe_the_popup_is_the_exe_itself(monkeypatch):
    started = []

    class FakeProc:
        stdin = type("S", (), {"write": lambda self, b: None, "close": lambda self: None})()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(notify.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or FakeProc())
    notify._launch([{"name": "x", "game": "y", "points": 0}], False)
    assert started == [[sys.executable, "--toast"]]


def test_a_frozen_app_reads_the_greycell_manifest_and_a_source_copy_github(tmp_path, monkeypatch):
    cfg = MachineConfig(tmp_path / "m")
    cfg.save({"update": {"repo": "o/r"}})
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    manifest = {"version": "9.1.0", "setup": "https://greycell.app/downloads/greycell-achievements/S-9.1.0.exe",
                "sha256": "a" * 64, "size": 1000, "notes": "new"}
    asked = []
    status = update.check(cfg, get=lambda url: asked.append(url) or manifest, force=True)
    assert asked == [update.MANIFEST]
    assert status["available"]["version"] == "9.1.0" and status["installable"]
    assert status["download"] == "https://greycell.app/achievements/"
    monkeypatch.setattr(sys, "frozen", False)
    release = {"tag_name": "v9.1.0", "html_url": "https://github.com/o/r/releases/tag/v9.1.0", "body": ""}
    status = update.check(cfg, get=lambda url: release, force=True)
    assert status["download"] is None and not status["installable"]


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows process list")
def test_listing_running_programs_never_starts_a_program(monkeypatch):
    """Owner, 2026-10-01: no terminal windows. From the windowed app, any child
    console program flashes a window, so the watcher must start none."""
    import subprocess
    from openachievements.adapters import executable

    def refuse(*a, **k):
        raise AssertionError("the watcher started a program")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    found = executable.running_executables()
    assert sys.executable.lower() in found                   # this very test process is in the list


def test_no_child_process_is_started_without_hiding_its_window():
    """Every place the app starts a program asks Windows for no window."""
    import pathlib
    import re
    src = pathlib.Path(desktop.__file__).resolve().parent
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for call in re.finditer(r"subprocess\.(?:run|Popen|call|check_output)\(", text):
            window = text[call.start(): call.start() + 400]
            nix_only = "(\"ps\"," in window                  # the macOS branch
            assert nix_only or "creationflags" in window or "CREATE_NO_WINDOW" in text[max(0, call.start() - 300):call.start()], \
                f"{path.name}: {window[:80]!r} may open a console window"


def test_start_with_windows_is_on_by_default_once_and_follows_the_exe(sandbox, monkeypatch):
    from openachievements import tray
    from openachievements.profile import Profile
    reg = FakeRegistry()
    monkeypatch.setattr(tray, "__import__", None, raising=False)
    real_set, real_enabled, real_value = tray.set_autostart, tray.autostart_enabled, tray.autostart_value
    monkeypatch.setattr(tray, "set_autostart", lambda on, r=None: real_set(on, reg))
    monkeypatch.setattr(tray, "autostart_enabled", lambda r=None: real_enabled(reg))
    monkeypatch.setattr(tray, "autostart_value", lambda r=None: real_value(reg))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\A\GreycellAchievements.exe")
    desktop._first_profile()
    p = Profile.open()
    desktop._autostart_by_default(p)
    assert reg.values["GreycellAchievements"] == '"C:\\A\\GreycellAchievements.exe" --background'
    real_set(False, reg)                                     # the player unticks it
    desktop._autostart_by_default(p)
    assert reg.values == {}                                  # and it stays off
    real_set(True, reg)
    monkeypatch.setattr(sys, "executable", r"D:\Games\GreycellAchievements.exe")
    desktop._autostart_by_default(p)                         # moved: the entry follows
    assert reg.values["GreycellAchievements"] == '"D:\\Games\\GreycellAchievements.exe" --background'


def test_the_popup_card_has_soft_transparent_corners_and_a_solid_middle():
    from openachievements import overlay
    px = overlay.card_pixels()
    at = lambda x, y: px[y * overlay.W + x]
    assert at(0, 0)[3] == 0                                  # the corner outside the rounding is clear
    assert at(overlay.W // 2, overlay.H // 2)[3] == 1         # the middle is solid
    edge = [at(x, 0)[3] for x in range(overlay.W)]
    assert any(0 < a < 1 for a in edge)                      # anti-aliased, not jagged


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows popup")
def test_on_windows_the_popup_never_goes_through_tk(monkeypatch):
    """Tk activates its windows; the Windows popup must be the native one."""
    from openachievements import overlay, toast
    used = []
    monkeypatch.setattr(overlay, "show", lambda cards, sound=True, play=None: used.append(cards))
    monkeypatch.setitem(sys.modules, "tkinter", None)        # importing Tk would now fail
    toast.show([{"name": "x", "game": "", "points": 0}], sound=False)
    assert used == [[{"name": "x", "game": "", "points": 0}]]


def test_the_dashboard_opens_as_its_own_app_window(monkeypatch):
    monkeypatch.delenv("GREYCELL_ACHIEVEMENTS_NO_BROWSER", raising=False)
    started = []
    monkeypatch.setattr(desktop, "_edge", lambda: r"C:\Edge\msedge.exe")
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    desktop._open_browser()
    cmd, kw = started[0]
    assert cmd[0].endswith("msedge.exe") and cmd[1] == "--app=http://127.0.0.1:8788"
    assert "creationflags" in kw


def test_the_pulse_changes_when_something_is_recorded_and_lists_what_runs(profile, tmp_path):
    from fastapi.testclient import TestClient
    from openachievements import watching
    from openachievements.local_app import create_local_app
    from conftest import write_pack
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    first = local.get("/v1/local/pulse", headers=h).json()
    assert first["playing"] == [] and first["watching"] is False
    profile.install_pack(write_pack(tmp_path / "pack"))
    profile.record_unlock("test-pack:first-step", adapter="test")
    later = local.get("/v1/local/pulse", headers=h).json()
    assert later["stamp"] != first["stamp"]
    assert local.get("/v1/local/pulse", headers=h).json()["stamp"] == later["stamp"]   # steady when nothing happens
    old = watching.LIVE
    try:
        watching.LIVE = {"playing": ["test-game"], "at": 1.0}
        now = local.get("/v1/local/pulse", headers=h).json()
        assert now["watching"] is True and now["playing"] == [{"game_id": "test-game", "title": "Test Game"}]
    finally:
        watching.LIVE = old
