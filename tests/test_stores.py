"""Games other stores installed, named from the stores' own records, and the
save folders those stores keep."""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from openachievements import savekeeper
from openachievements import steamfiles as sf
from openachievements.adapters import autodetect as ad
from openachievements.adapters import engine_saves, savefile, stores
from openachievements.finder import AchievementFinder
from test_autodetect import catalogue, detector, make_game  # noqa: F401 - catalogue is a fixture


def store_detector(profile, catalogue, monkeypatch, tmp_path, installs=()):
    monkeypatch.setattr(stores, "everything", lambda: list(installs))
    return detector(profile, catalogue, monkeypatch, tmp_path)


def epic_item(folder: Path, name: str, title: str, install: Path, **extra) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.item").write_text(json.dumps(
        {"AppName": name, "DisplayName": title, "InstallLocation": str(install), **extra}), encoding="utf-8")


# ---- each store's records ------------------------------------------------------------------

def test_epic_manifests_name_games_and_skip_addons_plugins_and_half_installs(tmp_path):
    m = tmp_path / "Manifests"
    epic_item(m, "Fortnite", "Fortnite\u2122", tmp_path / "Fortnite", AppCategories=["public", "games"])
    epic_item(m, "dlc1", "Some DLC", tmp_path / "Fortnite", AppCategories=["addons"])
    epic_item(m, "launchable", "A Launchable Addon", tmp_path / "L", AppCategories=["addons", "addons/launchable"])
    epic_item(m, "plug", "A Plugin", tmp_path / "P", AppCategories=["plugins", "plugins/engine"])
    epic_item(m, "ue", "Engine thing", tmp_path / "U", CompatibleApps=["UE_5.3"])
    epic_item(m, "UE_5.4", "Unreal Engine 5.4", tmp_path / "E", AppCategories=["engines"])
    epic_item(m, "half", "Half", tmp_path / "H", bIsIncompleteInstall=True)
    (m / "broken.item").write_text("{not json")
    heroic = tmp_path / "installed.json"
    heroic.write_text(json.dumps({"Sugar": {"app_name": "Sugar", "title": "Hades", "install_path": "/games/Hades"},
                                  "Dlc": {"title": "x", "install_path": "/games/x", "is_dlc": True}}))
    found = {(i.ref, i.title) for i in stores.epic_installs(m, heroic)}
    assert found == {("Fortnite", "Fortnite"), ("launchable", "A Launchable Addon"), ("Sugar", "Hades")}


def test_amazon_installs_come_from_its_database_read_only(tmp_path):
    db = tmp_path / "GameInstallInfo.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE DbSet (Id TEXT, InstallDirectory TEXT, ProductTitle TEXT, Installed INTEGER)")
    con.executemany("INSERT INTO DbSet VALUES (?, ?, ?, ?)", [
        ("amzn1.adg.product.abc", str(tmp_path / "Games" / "Tomb"), "Tomb Raider\u00ae", 1),
        ("amzn1.adg.product.old", str(tmp_path / "Games" / "Gone"), "Uninstalled", 0)])
    con.commit()
    con.close()
    before = db.read_bytes()
    found = stores.amazon_installs(db)
    assert [(i.ref, i.title) for i in found] == [("amzn1.adg.product.abc", "Tomb Raider")]
    assert db.read_bytes() == before
    assert stores.amazon_installs(tmp_path / "missing.sqlite") == []
    (tmp_path / "junk.sqlite").write_bytes(b"not a database")
    assert stores.amazon_installs(tmp_path / "junk.sqlite") == []


def itch_db(path: Path, caves: list, games: list, locations: list) -> Path:
    """butler.db reduced to the columns read, named as butler's models give them."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE games (id INTEGER PRIMARY KEY, title TEXT, classification TEXT)")
    con.execute("CREATE TABLE install_locations (id TEXT PRIMARY KEY, path TEXT)")
    con.execute("CREATE TABLE caves (id TEXT PRIMARY KEY, game_id INTEGER, install_location_id TEXT, "
                "install_folder_name TEXT, custom_install_folder TEXT, seconds_run INTEGER)")
    con.executemany("INSERT INTO games VALUES (?, ?, ?)", games)
    con.executemany("INSERT INTO install_locations VALUES (?, ?)", locations)
    con.executemany("INSERT INTO caves VALUES (?, ?, ?, ?, ?, 0)", caves)
    con.commit()
    con.close()
    return path


def test_itch_installs_come_from_the_itch_apps_database_read_only(tmp_path):
    lib = tmp_path / "itch-apps"
    db = itch_db(tmp_path / "butler.db",
                 caves=[("c1", 1234, "loc1", "a-short-hike", None),
                        ("c2", 99, "loc1", "pixel-tool", None),
                        ("c3", 77, None, None, str(tmp_path / "Elsewhere" / "Celeste Classic")),
                        ("c4", 55, "loc1", "ost", None),
                        ("c5", 1234, "loc1", "a-short-hike", None)],          # the same install twice
                 games=[(1234, "A Short Hike", "game"), (99, "Pixel Tool", "tool"),
                        (77, "Celeste Classic", None), (55, "Hike OST", "soundtrack")],
                 locations=[("loc1", str(lib))])
    before = db.read_bytes()
    found = [(i.ref, i.title, i.folder) for i in stores.itch_installs(db)]
    assert found == [("1234", "A Short Hike", str(lib / "a-short-hike")),
                     ("77", "Celeste Classic", str(tmp_path / "Elsewhere" / "Celeste Classic"))]
    assert db.read_bytes() == before
    assert stores.itch_installs(tmp_path / "none.db") == []
    (tmp_path / "junk.db").write_bytes(b"not sqlite")
    assert stores.itch_installs(tmp_path / "junk.db") == []


def test_an_itch_game_is_recognised_without_engine_files(profile, catalogue, monkeypatch, tmp_path):
    lib = tmp_path / "itch-apps"
    exe = make_game(lib, "a-short-hike/AShortHike/AShortHike.exe")             # one folder down, no engine files
    db = itch_db(tmp_path / "butler.db", caves=[("c1", 1234, "loc1", "a-short-hike", None)],
                 games=[(1234, "A Short Hike", "game")], locations=[("loc1", str(lib))])
    found = store_detector(profile, catalogue, monkeypatch, tmp_path, stores.itch_installs(db)).poll({exe})
    assert found[0]["game_id"] == "itch-1234"
    game = profile.state()["games"]["itch-1234"]
    assert game["title"] == "A Short Hike" and game["platform"] == "PC (itch.io)"
    assert game["external_ids"] == {"itch": 1234} and len(game["installations"]) == 1
    assert AchievementFinder(profile).waiting() == ["itch-1234"]                # Steam sells it: looked up there


def test_battlenet_and_ea_games_come_from_their_uninstall_entries(tmp_path):
    bf = tmp_path / "EA Games" / "Battlefield 1"
    (bf / "__Installer").mkdir(parents=True)
    (bf / "__Installer" / "installerdata.xml").write_text(
        "<DiPManifest><contentIDs><contentID>1026023</contentID><contentID>9</contentID></contentIDs></DiPManifest>")
    entries = [
        {"DisplayName": "Diablo IV", "Publisher": "Blizzard Entertainment", "InstallLocation": "D:\\Diablo IV",
         "UninstallString": '"C:\\ProgramData\\Battle.net\\Agent\\Blizzard Uninstaller.exe" --lang=enUS --uid=fenris'},
        {"DisplayName": "Battle.net", "Publisher": "Blizzard Entertainment", "InstallLocation": "C:\\Battle.net",
         "UninstallString": '"C:\\Battle.net\\Battle.net Uninstaller.exe" --lang=enUS'},
        {"DisplayName": "Battlefield\u2122 1", "Publisher": "Electronic Arts", "InstallLocation": str(bf)},
        {"DisplayName": "Unravel", "Publisher": "Electronic Arts, Inc.", "InstallLocation": "E:\\Unravel"},
        {"DisplayName": "EA app", "Publisher": "Electronic Arts", "InstallLocation": "C:\\EA Desktop"},
        {"DisplayName": "Notepad++", "Publisher": "Notepad++ Team", "InstallLocation": "C:\\npp"},
        {"DisplayName": "No folder", "Publisher": "Electronic Arts"}]
    found = [(i.store, i.ref, i.title) for i in stores.registry_installs(entries)]
    # Unravel's folder has no EA installer record (EA's anti-cheat looks just like that): not a game.
    assert found == [("battlenet", "fenris", "Diablo IV"), ("ea", "1026023", "Battlefield 1")]
    assert stores.game_id(stores.Install("ea", "1026023", "Battlefield 1", "x")) == "ea-1026023"


def test_xbox_config_names_the_game_by_its_title_id(tmp_path):
    content = tmp_path / "XboxGames" / "Hi-Fi RUSH" / "Content"
    content.mkdir(parents=True)
    (content / "MicrosoftGame.config").write_text(
        '<?xml version="1.0"?><Game configVersion="1"><Identity Name="BethesdaSoftworks.HiFiRush" '
        'Publisher="CN=x" Version="1.0.0.0"/><TitleId>6a4f2e01</TitleId>'
        '<ShellVisuals DefaultDisplayName="ms-resource:AppName"/></Game>')
    inst = stores.xbox_config(content)
    assert (inst.ref, inst.title, inst.package) == ("6A4F2E01", "Hi-Fi RUSH", "BethesdaSoftworks.HiFiRush")
    assert stores.game_id(inst) == f"xbox-{0x6A4F2E01}"
    (content / "MicrosoftGame.config").write_text(
        '<Game><Identity Name="Dev.Template"/><TitleId>FFFFFFFF</TitleId></Game>')     # the template's placeholder
    assert stores.xbox_config(content) is None


# ---- recognising a running game from them --------------------------------------------------

def test_an_epic_game_needs_no_engine_files_and_takes_the_catalogue_list(profile, catalogue, monkeypatch, tmp_path):
    exe = make_game(tmp_path / "D", "Epic Games/HollowKnightEpic/hk.exe")          # no engine files at all
    inst = stores.Install("epic", "Kestrel", "Hollow Knight", str(tmp_path / "D" / "Epic Games" / "HollowKnightEpic"))
    found = store_detector(profile, catalogue, monkeypatch, tmp_path, [inst]).poll({exe})
    assert found[0]["game_id"] == "steam-367520"                                    # the exact catalogue title


def test_an_epic_game_no_catalogue_names_keeps_its_store_id(profile, catalogue, monkeypatch, tmp_path):
    exe = make_game(tmp_path / "D", "Epic Games/Odd/Binaries/Win64/Odd-Win64-Shipping.exe")
    saves = tmp_path / "roots" / "LOCALAPPDATA" / "Odd" / "Saved" / "SaveGames"
    saves.mkdir(parents=True)
    inst = stores.Install("epic", "a1b2C3", "Odd Little Game", str(tmp_path / "D" / "Epic Games" / "Odd"))
    found = store_detector(profile, catalogue, monkeypatch, tmp_path, [inst]).poll({exe})
    assert found[0]["game_id"] == "epic-a1b2c3"
    game = profile.state()["games"]["epic-a1b2c3"]
    assert game["title"] == "Odd Little Game" and game["platform"] == "PC (Epic)"
    assert game["external_ids"] == {"epic": "a1b2C3"}
    assert found[0]["save_folders"][0] == str(saves)                                # the Unreal project's own
    assert AchievementFinder(profile).waiting() == ["epic-a1b2c3"]                 # looked up on Steam


def test_a_ubisoft_game_keeps_its_own_id_and_its_connect_saves(profile, catalogue, monkeypatch, tmp_path):
    folder = tmp_path / "Ubisoft" / "games" / "Hollow Knight"
    exe = make_game(folder.parent, "Hollow Knight/hk.exe")
    connect = tmp_path / "Ubisoft Game Launcher"
    kept = connect / "savegames" / "4d2f-user" / "635"
    kept.mkdir(parents=True)
    (kept / "1.save").write_bytes(b"\x01")
    from openachievements.adapters import ubisoft
    monkeypatch.setattr(ubisoft, "connect_dir", lambda: connect)
    inst = stores.Install("ubisoft", "635", "Hollow Knight", str(folder))
    found = store_detector(profile, catalogue, monkeypatch, tmp_path, [inst]).poll({exe})
    assert found[0]["game_id"] == "ubisoft-635"                     # not the Steam title: LumaPlay uses this id
    assert profile.state()["games"]["ubisoft-635"]["external_ids"] == {"ubisoft": 635}
    assert ad.store_saves(profile, "ubisoft-635") == [str(kept)]
    assert ad.save_candidates(profile, "ubisoft-635")[0] == str(kept)
    assert kept in savekeeper.save_folders(profile)["ubisoft-635"]


def test_an_xbox_app_game_is_recognised_from_its_config_and_its_wgs_is_kept(profile, catalogue, monkeypatch, tmp_path):
    content = tmp_path / "XboxGames" / "Hi-Fi RUSH" / "Content"
    exe = make_game(content.parent, "Content/Hi-Fi-RUSH.exe")
    (content / "MicrosoftGame.config").write_text(
        '<Game><Identity Name="BethesdaSoftworks.HiFiRush"/><TitleId>6A4F2E01</TitleId>'
        '<ShellVisuals DefaultDisplayName="Hi-Fi RUSH"/></Game>')
    wgs = tmp_path / "Local" / "Packages" / "BethesdaSoftworks.HiFiRush_3275kfvn8vcwc" / "SystemAppData" / "wgs"
    wgs.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    found = store_detector(profile, catalogue, monkeypatch, tmp_path).poll({exe})
    gid = f"xbox-{0x6A4F2E01}"
    assert found[0]["game_id"] == gid and profile.state()["games"][gid]["platform"] == "PC (Xbox)"
    assert ad.store_saves(profile, gid) == [str(wgs)]


def test_store_records_do_not_make_the_launchers_themselves_games(profile, catalogue, monkeypatch, tmp_path):
    launcher = make_game(tmp_path / "C", "Epic Games/Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe")
    inst = stores.Install("epic", "Fortnite", "Fortnite", str(tmp_path / "C" / "Epic Games" / "Fortnite"))
    d = store_detector(profile, catalogue, monkeypatch, tmp_path, [inst])
    found = d.poll({launcher})
    assert all(not f["game_id"].startswith("epic-") for f in found)


# ---- saves stores keep, found without the game running ---------------------------------------

def test_ubisoft_and_steam_cloud_saves_are_found_by_discovery(profile, catalogue, monkeypatch, tmp_path):
    from openachievements.adapters import ubisoft
    connect = tmp_path / "Ubisoft Game Launcher"
    for app in ("635", "720"):
        folder = connect / "savegames" / "user" / app
        folder.mkdir(parents=True)
        (folder / "1.save").write_bytes(b"\x01")
    (connect / "savegames" / "user" / "999").mkdir()                       # empty: nothing to keep
    monkeypatch.setattr(ubisoft, "connect_dir", lambda: connect)
    monkeypatch.setattr(stores, "ubisoft_folders", lambda: {"635": "C:\\Ubisoft\\games\\Far Cry 3"})
    steam = tmp_path / "Steam"
    remote = steam / "userdata" / "12345" / "367520" / "remote"
    remote.mkdir(parents=True)
    (remote / "user1.dat").write_bytes(b"x")
    (steam / "userdata" / "12345" / "1091500" / "remote").mkdir(parents=True)   # no files
    d = store_detector(profile, catalogue, monkeypatch, tmp_path)
    from openachievements.catalog import steam as cat
    cat.add_to_profile(profile, cat.CatalogIndex(catalogue), 367520)
    cat.add_to_profile(profile, cat.CatalogIndex(catalogue), 1091500)
    monkeypatch.setattr(sf, "steam_dir", lambda: steam)
    d.discover_saves()
    games = profile.state()["games"]
    assert games["ubisoft-635"]["title"] == "Far Cry 3"
    assert "ubisoft-720" not in games                                      # not installed and not in the library
    assert ad.store_saves(profile, "ubisoft-635") == [str(connect / "savegames" / "user" / "635")]
    assert ad.store_saves(profile, "steam-367520") == [str(remote)]
    assert ad.store_saves(profile, "steam-1091500") == []
    before = profile.config.load()
    d.discover_saves()                                                     # nothing new: nothing written
    assert profile.config.load() == before


# ---- the keeper copies them, and never puts them back by itself ------------------------------

@pytest.mark.parametrize("folder,synced", [
    ("C:/Program Files (x86)/Ubisoft/Ubisoft Game Launcher/savegames/u/635", True),
    ("C:\\Users\\z\\AppData\\Local\\Packages\\Game_abc\\SystemAppData\\wgs", True),
    ("C:\\Program Files (x86)\\Steam\\userdata\\1\\367520\\remote", True),
    ("C:\\Users\\z\\AppData\\LocalLow\\Team Cherry\\Hollow Knight", False),
    ("C:\\Users\\z\\Documents\\My Games\\remote", False)])
def test_store_synced_folders(folder, synced):
    assert stores.store_synced(folder) is synced


def test_a_store_synced_save_is_kept_but_never_put_back_on_its_own(profile, monkeypatch, tmp_path):
    from openachievements.adapters import executable
    remote = tmp_path / "Steam" / "userdata" / "1" / "367520" / "remote"
    remote.mkdir(parents=True)
    save = remote / "user1.dat"
    save.write_bytes(b"progress")
    old = time.time() - 60
    os.utime(save, (old, old))
    profile.register_game("steam-367520", "Hollow Knight")
    ad.note_store_saves(profile, "steam-367520", [str(remote)])
    watcher = savekeeper.KeeperWatcher(profile)
    assert any("kept steam-367520" in m for m in watcher.poll(set(), force=True))
    for path in (watcher._keeper().root / "steam-367520" / "snapshots").glob("*.json"):
        snap = json.loads(path.read_text())
        snap["taken_at"] = "2020-01-03T00:00:00Z"
        path.write_text(json.dumps(snap))
    exe = tmp_path / "Steam" / "steamapps" / "common" / "Hollow Knight" / "hk.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    executable.register(profile, "steam-367520", exe)
    save.unlink()                                                          # reinstalled, cloud not synced yet
    assert not any("put" in m for m in watcher.poll(set(), force=True))
    assert not save.exists()                                               # Steam Cloud brings it back, not us
    keeper = watcher._keeper()
    keeper.restore("steam-367520", keeper.snapshots("steam-367520")[0]["id"])     # the player still can
    assert save.read_bytes() == b"progress"
