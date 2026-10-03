"""The Linux app: Steam and Proton paths, the popup, Start at login, the app
menu, the browser window, sounds and the AppImage updating itself. Fakes and
scratch folders only: nothing is shown, played, started or downloaded."""
import hashlib
import io
import os
import sys
import time
from pathlib import Path

import pytest

from openachievements import linux_desktop, notify, popup_linux, self_update, steamfiles, toast, update
from openachievements.adapters import executable, savefile


# ---- Steam and Proton ---------------------------------------------------------------------

def _steam(root: Path, libraries=(), prefixes=()) -> Path:
    (root / "appcache" / "stats").mkdir(parents=True)
    (root / "steamapps").mkdir(parents=True, exist_ok=True)
    entries = "".join(f'\t"{i}"\n\t{{\n\t\t"path"\t\t"{lib}"\n\t}}\n' for i, lib in enumerate([root, *libraries]))
    (root / "steamapps" / "libraryfolders.vdf").write_text(f'"libraryfolders"\n{{\n{entries}}}\n')
    for lib, appid in prefixes:
        Path(lib, "steamapps", "compatdata", str(appid), *steamfiles.PROTON_USER).mkdir(parents=True)
    return root


def test_steam_is_found_where_linux_keeps_it_flatpak_and_snap_included(tmp_path):
    dirs = [d.relative_to(tmp_path).as_posix() for d in steamfiles.linux_steam_dirs(tmp_path)]
    assert dirs == [".steam/steam", ".steam/root", ".local/share/Steam",
                    ".var/app/com.valvesoftware.Steam/.local/share/Steam", "snap/steam/common/.local/share/Steam"]


def test_every_library_and_every_proton_prefix_is_found(tmp_path):
    card = tmp_path / "sdcard"
    root = _steam(tmp_path / "Steam", [card], [(tmp_path / "Steam", 10), (card, 20)])
    assert steamfiles.library_paths(root) == [root, card]
    users = steamfiles.proton_users(root)
    assert set(users) == {"10", "20"} and users["20"] == card.joinpath("steamapps", "compatdata", "20",
                                                                     *steamfiles.PROTON_USER)
    assert steamfiles.proton_users(root, 20) == {"20": users["20"]}
    assert steamfiles.proton_users(root, 30) == {}


def test_a_windows_game_played_through_proton_has_its_save_read_from_the_prefix(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    root = _steam(tmp_path / "Steam", prefixes=[(tmp_path / "Steam", 1636440)])
    monkeypatch.setattr(steamfiles, "steam_dir", lambda: root)
    save = {"id": "slots", "root": "LOCALAPPDATA", "path": "Townfall/Saved/SaveGames", "pattern": "*.sav",
            "format": "gvas"}
    user = steamfiles.proton_users(root, 1636440)["1636440"]
    inside = user / "AppData" / "Local" / "Townfall" / "Saved" / "SaveGames"
    native = savefile.root_folder("LOCALAPPDATA") / "Townfall" / "Saved" / "SaveGames"   # XDG data, as native games use
    assert savefile.save_folder(profile, "steam-1636440", save) == native        # not played yet: the usual place
    inside.mkdir(parents=True)
    assert savefile.save_folder(profile, "steam-1636440", save) == inside        # played through Proton
    native.mkdir(parents=True)
    assert savefile.save_folder(profile, "steam-1636440", save) == native        # a native save wins
    assert savefile.save_folder(profile, "my-game", save) == native              # only Steam games have a prefix


def test_proton_prefixes_are_searched_for_saves_too(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    root = _steam(tmp_path / "Steam", prefixes=[(tmp_path / "Steam", 10)])
    monkeypatch.setattr(steamfiles, "steam_dir", lambda: root)
    user = steamfiles.proton_users(root, 10)["10"]
    roots = savefile.search_roots()
    for part in (("AppData", "Local"), ("AppData", "LocalLow"), ("AppData", "Roaming"), ("Documents",),
                 ("Saved Games",), ("Documents", "My Games")):
        assert user.joinpath(*part) in roots
    assert len(roots) == len(set(roots))                                        # each folder once


def test_a_proton_game_counts_as_running_by_the_exe_wine_names():
    assert executable.wine_to_unix(r"Z:\home\deck\Games\Hades\x64\Hades.exe") == "/home/deck/Games/Hades/x64/Hades.exe"
    assert executable.wine_to_unix("/home/deck/Games/Hades/x64/Hades.exe") == "/home/deck/Games/Hades/x64/Hades.exe"
    assert executable.wine_to_unix(r"C:\windows\system32\steam.exe") is None     # wine's own, not a game
    assert executable.wine_to_unix("/usr/bin/bash") is None


# ---- the popup -----------------------------------------------------------------------------

def test_the_linux_popup_says_what_the_windows_one_says():
    plain = popup_linux.lines({"name": "From Scratch", "game": "Townfall", "points": 10})
    assert [t for t, *_ in plain] == ["Achievement unlocked \u00b7 10 pts", "From Scratch", "Townfall"]
    rare = popup_linux.lines({"name": "X", "game": "G", "rare": 0.4})
    assert rare[0][0] == "Rare achievement \u00b7 0.4% of players"
    plat = popup_linux.lines({"game": "Townfall", "platinum": True})
    assert [t for t, *_ in plat] == ["PLATINUM", "Townfall", "Every achievement unlocked"]


def test_the_linux_popup_is_cut_to_the_pill():
    from openachievements import card
    px = card.card_pixels()
    spans = popup_linux._spans(px)
    rows = {y: (x, w) for y, x, w in spans}
    assert rows[card.H // 2] == (0, card.W)                                     # the middle row is full width
    assert rows[0][0] > 20 and rows[0][1] < card.W - 40                         # the top is pulled in at the ends
    image = popup_linux.card_ppm(px)
    assert image.startswith(f"P6 {card.W} {card.H} 255\n".encode()) and len(image) == len(
        f"P6 {card.W} {card.H} 255\n") + card.W * card.H * 3


def test_the_linux_card_is_rendered_with_its_text_and_picture(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image
    from openachievements import card
    bases = {}
    blank = popup_linux.card_ppm(card.card_pixels("plain"))
    image, spans = popup_linux.render({"name": "From Scratch", "game": "Townfall", "points": 0}, bases)
    assert len(image) == len(blank) and image != blank and spans == popup_linux._spans(card.card_pixels("plain"))
    red = tmp_path / "red.jpg"
    Image.new("RGB", (64, 64), (230, 20, 20)).save(red)                         # a JPEG, as Steam serves them
    with_picture, _ = popup_linux.render({"name": "X", "game": "", "points": 0, "icon": str(red)}, bases)
    header = len(f"P6 {card.W} {card.H} 255\n")
    middle = header + (card.H // 2 * card.W + card.H // 2) * 3                  # the circle's centre
    r, g, b = with_picture[middle:middle + 3]
    assert r > 200 and g < 60 and b < 60
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a picture")
    assert popup_linux.render({"name": "X", "icon": str(broken)}, bases)        # the trophy, not a lost popup


def test_long_names_end_in_an_ellipsis_inside_the_card():
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = ImageFont.truetype(str(popup_linux.FONT_DIR / "DejaVuSans-Bold.ttf"), 17)
    short = popup_linux._ellipsize(draw, "Short", font, 200)
    long = popup_linux._ellipsize(draw, "An achievement with a very long name indeed", font, 200)
    assert short == "Short" and long.endswith("\u2026") and draw.textlength(long, font=font) <= 200


def test_with_no_display_the_desktop_notification_says_it(monkeypatch, _no_real_linux_desktop):
    monkeypatch.setitem(sys.modules, "tkinter", None)                          # no Tk: no window possible
    monkeypatch.setattr(popup_linux.shutil, "which", lambda name: "/usr/bin/" + name)
    played = []
    popup_linux.show([{"name": "From Scratch", "game": "Townfall", "points": 0}], True, played.append)
    assert played == ["unlock"]
    command = _no_real_linux_desktop[-1]
    assert command[:3] == ["/usr/bin/notify-send", "-a", "Greycell Achievements"]
    assert command[-2:] == ["Achievement unlocked", "From Scratch\nTownfall"]


def test_the_popup_runs_on_linux_only_with_a_desktop(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert notify.popups_here() is False                                        # the test fixture took the display away
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert notify.popups_here() is True
    monkeypatch.setattr(sys, "platform", "darwin")
    assert notify.popups_here() is False


def test_linux_sounds_go_through_the_system_player(monkeypatch, _no_real_linux_desktop):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(linux_desktop.shutil, "which", lambda name: "/usr/bin/paplay" if name == "paplay" else None)
    toast._play_chime(False, "echo")
    command = _no_real_linux_desktop[-1]
    assert command[0] == "/usr/bin/paplay" and command[1].endswith(".wav") and os.path.exists(command[1])


# ---- the desktop ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="Linux paths in desktop entries")
def test_start_at_login_is_an_autostart_entry_pointing_at_the_appimage(tmp_path, monkeypatch):
    image = tmp_path / "My Apps" / "GreycellAchievements.AppImage"
    image.parent.mkdir()
    image.write_bytes(b"")
    monkeypatch.setenv("APPIMAGE", str(image))
    assert linux_desktop.autostart_enabled() is False
    linux_desktop.set_autostart(True)
    text = linux_desktop.autostart_file().read_text()
    assert f'Exec="{image}" --background' in text and "Icon=greycell-achievements" in text
    assert linux_desktop.autostart_value() == f'"{image}" --background'
    linux_desktop.set_autostart(False)
    linux_desktop.set_autostart(False)                                         # off twice is fine
    assert linux_desktop.autostart_enabled() is False


def test_from_source_nothing_is_written_for_the_desktop(monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    with pytest.raises(OSError):
        linux_desktop.set_autostart(True)
    assert linux_desktop.install_menu_entry() is False


@pytest.mark.skipif(sys.platform == "win32", reason="Linux paths in desktop entries")
def test_the_app_menu_entry_follows_the_appimage(tmp_path, monkeypatch):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "G.AppImage").write_bytes(b"")
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "a" / "G.AppImage"))
    assert linux_desktop.install_menu_entry() is True
    assert linux_desktop.install_menu_entry() is False                         # unchanged: nothing rewritten
    assert linux_desktop.icon_file().exists()
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "b" / "G.AppImage"))
    assert linux_desktop.install_menu_entry() is True
    assert f"Exec={tmp_path / 'b' / 'G.AppImage'}\n" in linux_desktop.menu_entry_file().read_text()


def test_exec_lines_quote_what_the_desktop_would_misread():
    assert linux_desktop._quote("/opt/G.AppImage") == "/opt/G.AppImage"
    assert linux_desktop._quote('/home/me/My "Games"/$G.AppImage') == '"/home/me/My \\"Games\\"/\\$G.AppImage"'


def test_system_programs_get_the_library_path_from_before_the_app(monkeypatch):
    env = linux_desktop.system_env({"LD_LIBRARY_PATH": "/tmp/_MEI1", "LD_LIBRARY_PATH_ORIG": "/usr/local/lib",
                                    "_PYI_APPLICATION_HOME_DIR": "/tmp/_MEI1", "HOME": "/home/me"})
    assert env == {"LD_LIBRARY_PATH": "/usr/local/lib", "HOME": "/home/me"}
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert "LD_LIBRARY_PATH" not in linux_desktop.system_env({"LD_LIBRARY_PATH": "/tmp/_MEI1"})


def test_the_library_opens_as_an_app_window_else_in_the_default_browser(monkeypatch, _no_real_linux_desktop):
    found = {"chromium": "/usr/bin/chromium", "xdg-open": "/usr/bin/xdg-open"}
    monkeypatch.setattr(linux_desktop.shutil, "which", lambda name: found.get(name))
    linux_desktop.open_url("http://127.0.0.1:8788")
    assert _no_real_linux_desktop[-1][:2] == ["/usr/bin/chromium", "--app=http://127.0.0.1:8788"]
    del found["chromium"]
    linux_desktop.open_url("http://127.0.0.1:8788")
    assert _no_real_linux_desktop[-1] == ["/usr/bin/xdg-open", "http://127.0.0.1:8788"]


def test_the_page_offers_start_at_login_and_quit_where_there_is_no_tray(profile, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from openachievements import local_app
    image = tmp_path / "G.AppImage"
    image.write_bytes(b"")
    monkeypatch.setenv("APPIMAGE", str(image))
    quit = []
    monkeypatch.setattr(local_app, "APP_CONTROLS", {})
    page = TestClient(local_app.create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    assert page.get("/v1/mode").json()["app"] == {"quit": False, "autostart": None}      # Windows: the tray does it
    h = {"X-OA-Token": "t" * 32}
    assert page.post("/v1/local/app/quit", headers=h).status_code == 400
    local_app.APP_CONTROLS.update(quit=lambda: quit.append(1), autostart=linux_desktop)
    assert page.get("/v1/mode").json()["app"] == {"quit": True, "autostart": False}
    assert page.post("/v1/local/app/autostart", json={"on": True}, headers=h).json() == {"autostart": True}
    assert page.post("/v1/local/app/autostart", json={"on": False}, headers=h).json() == {"autostart": False}
    assert profile.config.load()["autostart"]["offered"] is True                # turned off stays off
    assert page.post("/v1/local/app/quit").status_code in (401, 403)          # the page's token is required
    assert page.post("/v1/local/app/quit", headers=h).json() == {"quitting": True}
    time.sleep(0.6)
    assert quit == [1]


# ---- the AppImage updating itself ------------------------------------------------------

BLOB = b"\x7fELF fake appimage " * 1000
LINUX = {"url": "https://greycell.app/downloads/greycell-achievements/GreycellAchievements-9.1.0-x86_64.AppImage",
         "sha256": hashlib.sha256(BLOB).hexdigest(), "size": len(BLOB)}
MANIFEST = {"version": "9.1.0", "setup": "https://greycell.app/downloads/greycell-achievements/S-9.1.0.exe",
            "sha256": "a" * 64, "size": 10, "notes": "Linux", "platforms": {"linux-x86_64": LINUX}}


def test_each_system_reads_its_own_build_from_the_manifest():
    assert update.platform_key("linux", "x86_64") == "linux-x86_64"
    assert update.platform_key("linux", "aarch64") == "linux-arm64"
    assert update.platform_key("win32", "AMD64") == "windows-x86_64"
    linux = update.latest_setup(update.MANIFEST, lambda u: MANIFEST, key="linux-x86_64")
    assert linux["setup"] == LINUX["url"] and linux["sha256"] == LINUX["sha256"] and linux["version"] == "9.1.0"
    windows = update.latest_setup(update.MANIFEST, lambda u: MANIFEST, key="windows-x86_64")
    assert windows["setup"].endswith("S-9.1.0.exe")                            # what every Windows version reads
    assert update.latest_setup(update.MANIFEST, lambda u: MANIFEST, key="linux-arm64") is None   # no build yet
    off_site = {**MANIFEST, "platforms": {"linux-x86_64": {**LINUX, "url": "https://evil.example/x.AppImage"}}}
    assert update.latest_setup(update.MANIFEST, lambda u: off_site, key="linux-x86_64") is None


def test_the_appimage_replaces_itself_then_starts_again_after_quitting(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    self_update.STATE.update(phase=None, error=None, version=None)
    image = tmp_path / "apps" / "GreycellAchievements.AppImage"
    image.parent.mkdir()
    image.write_bytes(b"old")
    monkeypatch.setenv("APPIMAGE", str(image))
    release = update.latest_setup(update.MANIFEST, lambda u: MANIFEST, key="linux-x86_64")
    started, quit = [], []
    ok = self_update.install(release, lambda: quit.append(1), lambda url: io.BytesIO(BLOB),
                             lambda cmd, **kw: started.append((cmd, kw)), tmp_path / "dl")
    assert ok and quit == [1] and image.read_bytes() == BLOB and os.access(image, os.X_OK)
    cmd, kw = started[0]
    assert cmd[:2] == ["/bin/sh", "-c"] and cmd[-1] == str(image) and cmd[-2] == str(os.getpid())
    assert kw["start_new_session"] and "APPIMAGE" not in kw["env"] and kw["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert not any((tmp_path / "dl").iterdir())                                 # the download is not kept
    assert not any(p.name.startswith(".") for p in image.parent.iterdir())     # nor anything half-placed


def test_a_tampered_appimage_never_replaces_the_app(tmp_path, monkeypatch):
    self_update.STATE.update(phase=None, error=None, version=None)
    image = tmp_path / "G.AppImage"
    image.write_bytes(b"old")
    monkeypatch.setenv("APPIMAGE", str(image))
    release = update.latest_setup(update.MANIFEST, lambda u: MANIFEST, key="linux-x86_64")
    started, quit = [], []
    assert not self_update.install(release, lambda: quit.append(1), lambda url: io.BytesIO(BLOB[:-1] + b"X"),
                                   lambda cmd, **kw: started.append(cmd), tmp_path / "dl")
    assert image.read_bytes() == b"old" and started == [] and quit == [] and self_update.STATE["phase"] == "error"


def test_only_the_appimage_updates_itself(tmp_path, monkeypatch):
    self_update.STATE.update(phase=None, error=None, version=None)
    monkeypatch.delenv("APPIMAGE", raising=False)
    release = update.latest_setup(update.MANIFEST, lambda u: MANIFEST, key="linux-x86_64")
    quit = []
    assert not self_update.install(release, lambda: quit.append(1), lambda url: io.BytesIO(BLOB),
                                   lambda cmd, **kw: None, tmp_path / "dl")
    assert quit == [] and "greycell.app/achievements" in self_update.STATE["error"]
