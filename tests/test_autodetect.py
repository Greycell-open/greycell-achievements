"""Recognising running games, and following Steam on this computer."""
import json
import os
import struct
from pathlib import Path

import pytest

from openachievements import steamfiles as sf
from openachievements.adapters import autodetect as ad
from openachievements.adapters import savefile, steam_local
from openachievements.catalog import steam as cat
from test_catalog import fake_steam, page, row

SYSTEM = ad._SYSTEM


# ---- recognising games --------------------------------------------------------------

@pytest.fixture
def catalogue(tmp_path):
    games = {367520: (1, page("Hollow Knight", [row("Keen", "Hit", "aa", "80.0")])),
             1091500: (1, page("Cyberpunk 2077", [row("The Fool", "Become", "bb", "88.0")])),
             1070990: (1, page("Flux", [row("F", "f", "cc", "1.0")]))}
    cat.crawl([(a, None) for a in games], tmp_path / "catalog", fake_steam(games))
    return tmp_path / "catalog"


def make_game(root: Path, rel: str, markers=()) -> str:
    exe = root / rel
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    for m in markers:
        target = exe.parent / m
        (target.mkdir() if m.endswith("_Data") or m == "MonoBleedingEdge" else target.write_bytes(b"x"))
    return os.path.normcase(str(exe))       # how the process list reports it: lowercase on Windows only


def detector(profile, catalogue, monkeypatch, tmp_path, keep_appdata_rule=False):
    # pytest's own folders live under AppData\Local\Temp; games never do.
    monkeypatch.setattr(ad, "_SYSTEM", SYSTEM if keep_appdata_rule else tuple(s for s in SYSTEM if "appdata" not in s))
    monkeypatch.setattr(sf, "steam_dir", lambda: None)
    monkeypatch.setattr(savefile, "root_folder", lambda name: tmp_path / "roots" / name)
    return ad.AutoDetector(profile, catalog_dir=catalogue)


def test_a_unity_game_in_a_repack_folder_is_recognised(profile, catalogue, monkeypatch, tmp_path):
    exe = make_game(tmp_path / "D", "Games/Hollow Knight [FitGirl Repack]/hollow_knight.exe",
                    ["UnityPlayer.dll", "hollow_knight_Data"])
    (tmp_path / "roots" / "LOCALLOW" / "Team Cherry" / "Hollow Knight").mkdir(parents=True)
    found = detector(profile, catalogue, monkeypatch, tmp_path).poll({exe})
    assert found[0]["game_id"] == "steam-367520" and found[0]["title"] == "Hollow Knight"
    game = profile.state()["games"]["steam-367520"]
    assert game["status"] == "playing" and len(game["installations"]) == 1
    assert found[0]["save_folders"][0].endswith("Hollow Knight")
    assert ad.save_candidates(profile, "steam-367520") == found[0]["save_folders"]


def test_an_unreal_game_is_recognised_from_its_binaries_folder(profile, catalogue, monkeypatch, tmp_path):
    exe = make_game(tmp_path / "D", "Cyberpunk 2077 GOTY Edition v2.1/bin/x64/Binaries/Cyberpunk2077.exe")
    found = detector(profile, catalogue, monkeypatch, tmp_path).poll({exe})
    assert found and found[0]["game_id"] == "steam-1091500"


def test_ordinary_programs_are_never_games(profile, catalogue, monkeypatch, tmp_path):
    # Named exactly like a catalogued game, but no engine files: an app, not a game.
    flux = make_game(tmp_path / "D", "Programs/Flux/flux.exe", ["resources.pak", "chrome_100_percent.pak", "lib"])
    appdata = make_game(tmp_path / "Users/z/AppData/Local", "Flux/flux.exe", ["UnityPlayer.dll"])
    assert detector(profile, catalogue, monkeypatch, tmp_path).poll({flux, "flux.exe"}) == []
    assert detector(profile, catalogue, monkeypatch, tmp_path, keep_appdata_rule=True).poll({appdata}) == []
    assert profile.state()["games"] == {}


def test_a_name_that_matches_two_games_is_not_guessed(profile, monkeypatch, tmp_path):
    games = {1: (1, page("Doom", [row("A", "a", "aa", "1.0")])), 2: (1, page("DOOM", [row("B", "b", "bb", "1.0")]))}
    cat.crawl([(a, None) for a in games], tmp_path / "c", fake_steam(games))
    exe = make_game(tmp_path / "D", "Games/Doom/doom.exe", ["steam_api64.dll"])
    assert detector(profile, tmp_path / "c", monkeypatch, tmp_path).poll({exe}) == []


def test_a_steam_game_is_identified_by_its_install_folder(profile, catalogue, monkeypatch, tmp_path):
    steam = tmp_path / "Steam"
    (steam / "steamapps").mkdir(parents=True)
    (steam / "steamapps" / "appmanifest_367520.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"367520"\n\t"installdir"\t\t"Hollow Knight"\n}\n', encoding="utf-8")
    exe = make_game(steam, "steamapps/common/Hollow Knight/hollow_knight.exe", ["UnityPlayer.dll"])
    d = detector(profile, catalogue, monkeypatch, tmp_path)
    monkeypatch.setattr(sf, "steam_dir", lambda: steam)
    assert d.poll({exe})[0]["game_id"] == "steam-367520"


def test_a_status_the_player_chose_is_kept(profile, catalogue, monkeypatch, tmp_path):
    cat.add_to_profile(profile, cat.CatalogIndex(catalogue), 367520, status="completed")
    exe = make_game(tmp_path / "D", "Games/Hollow Knight/hollow_knight.exe", ["UnityPlayer.dll"])
    detector(profile, catalogue, monkeypatch, tmp_path).poll({exe})
    assert profile.state()["games"]["steam-367520"]["status"] == "completed"


def test_recognition_can_be_turned_off(profile, catalogue, monkeypatch, tmp_path):
    ad.set_enabled(profile, False)
    exe = make_game(tmp_path / "D", "Games/Hollow Knight/hollow_knight.exe", ["UnityPlayer.dll"])
    assert detector(profile, catalogue, monkeypatch, tmp_path).poll({exe}) == []


# ---- following Steam on this computer ---------------------------------------------------

def kv(d: dict) -> bytes:
    out = b""
    for k, v in d.items():
        key = k.encode() + b"\0"
        if isinstance(v, dict):
            out += b"\x00" + key + kv(v) + b"\x08"
        elif isinstance(v, str):
            out += b"\x01" + key + v.encode() + b"\0"
        else:
            out += b"\x02" + key + struct.pack("<i", v)
    return out


def fake_steam_install(root: Path, account: int = 42, unlocked_bits=(0,), times=None):
    stats = root / "appcache" / "stats"
    stats.mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "loginusers.vdf").write_text(
        f'"users"\n{{\n\t"{sf.STEAMID64_BASE + account}"\n\t{{\n\t\t"MostRecent"\t\t"1"\n\t}}\n}}\n', encoding="utf-8")
    bits = {str(i): {"name": f"ACH_{i}", "display": {"name": {"english": n}, "desc": {"english": d},
                                                    "hidden": h, "icon": f"icon{i}.jpg"}}
            for i, (n, d, h) in enumerate([("First", "Do it", "0"), ("Secret", "The real ending", "1"),
                                           ("Third", "Again", "0")])}
    (stats / "UserGameStatsSchema_10.bin").write_bytes(
        kv({"10": {"gamename": "Test &amp; Game", "stats": {"1": {"type": "ACHIEVEMENTS", "bits": bits}}}}) + b"\x08")
    data = sum(1 << b for b in unlocked_bits)
    times = times or {str(b): 1600000000 + b for b in unlocked_bits}
    (stats / f"UserGameStats_{account}_10.bin").write_bytes(
        kv({"cache": {"crc": 0, "1": {"data": data, "AchievementTimes": times}}}) + b"\x08")
    return root


def test_steam_files_parse_and_link_follows_the_signed_in_account(profile, tmp_path, monkeypatch):
    root = fake_steam_install(tmp_path / "Steam")
    schema = sf.read_schema(root / "appcache" / "stats", 10)
    assert schema["title"] == "Test & Game" and [a["hidden"] for a in schema["achievements"]] == [False, True, False]
    info = steam_local.link(profile, root)
    assert info == {"steam_id": str(sf.STEAMID64_BASE + 42), "games": 1}
    assert profile.state()["accounts"]["steam"]["account"] == info["steam_id"]


def test_unlocks_arrive_once_with_steams_own_time_and_real_hidden_flags(profile, tmp_path):
    root = fake_steam_install(tmp_path / "Steam")
    steam_local.link(profile, root)
    watcher = steam_local.SteamLocalWatcher(profile, catalog_dir=tmp_path / "nocat")
    first = watcher.poll()
    assert [e["achievement_id"] for e in first] == ["steam-10:first"]
    assert first[0]["occurred_at"] == "2020-09-13T12:26:40.000Z"
    pack = profile.state()["packs"]["steam-10"]
    assert pack["source"] == "steam-local"
    assert pack["achievements"]["secret"]["hidden"] is True
    assert pack["achievements"]["secret"]["description"] == "The real ending"
    assert watcher.poll() == []                                    # unchanged file: nothing
    assert "status" not in profile.state()["games"]["steam-10"]     # history does not mean playing now
    fake_steam_install(root, unlocked_bits=(0, 2))                  # the game records a new unlock
    later = watcher.poll()
    assert [e["achievement_id"] for e in later] == ["steam-10:third"]
    assert profile.state()["games"]["steam-10"]["status"] == "playing"


def test_the_web_import_and_the_local_files_never_double_count(profile, tmp_path):
    root = fake_steam_install(tmp_path / "Steam")
    steam_local.link(profile, root)
    steam_local.SteamLocalWatcher(profile, catalog_dir=tmp_path / "nocat").poll()
    ext = next(e for e in profile.log.read().events if e["event_type"] == "achievement.unlocked")
    assert ext["source"]["external_event_id"] == f"{sf.STEAMID64_BASE + 42}:10:ACH_0"


def test_a_broken_steam_file_is_reported_not_fatal(profile, tmp_path):
    root = fake_steam_install(tmp_path / "Steam")
    (root / "appcache" / "stats" / "UserGameStatsSchema_10.bin").write_bytes(b"\x00garbage")
    steam_local.link(profile, root)
    watcher = steam_local.SteamLocalWatcher(profile, catalog_dir=tmp_path / "nocat")
    assert watcher.poll() == [] and "Steam app 10" in watcher.new_problems()[0]


def test_nothing_is_read_before_link(profile, tmp_path, monkeypatch):
    fake_steam_install(tmp_path / "Steam")
    monkeypatch.setattr(sf, "read_schema", lambda *a: pytest.fail("read before link"))
    assert steam_local.SteamLocalWatcher(profile).poll() == []


def test_a_game_that_could_not_be_imported_is_tried_again(profile, tmp_path, monkeypatch):
    from openachievements.fsutil import LockTimeout
    root = fake_steam_install(tmp_path / "Steam")
    steam_local.link(profile, root)
    real = profile.commit
    calls = {"n": 0}

    def busy_once(events):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LockTimeout("could not lock (held by another watcher)")
        return real(events)
    monkeypatch.setattr(profile, "commit", busy_once)
    watcher = steam_local.SteamLocalWatcher(profile, catalog_dir=tmp_path / "nocat")
    assert watcher.poll() == [] and "will retry" in watcher.new_problems()[0]
    assert [e["achievement_id"] for e in watcher.poll()] == ["steam-10:first"]   # same file, second try
    assert watcher.problems == {}


def test_an_achievement_named_like_a_number_does_not_lose_the_whole_game(tmp_path):
    """Rusty's Retirement has an achievement named 666, which Steam's binary
    file stores as the integer 666. Reading it crashed, and the game's 71
    unlocks never came in (2026-10-02)."""
    stats = tmp_path / "appcache" / "stats"
    stats.mkdir(parents=True)
    bits = {"0": {"name": "SIX", "display": {"name": {"english": 666}, "desc": {"english": "Roll it"}, "hidden": "0"}},
            "1": {"name": "OK", "display": {"name": {"english": "Plain"}, "desc": {"english": 7}, "hidden": "0"}}}
    (stats / "UserGameStatsSchema_99.bin").write_bytes(
        kv({"99": {"gamename": "Numbers", "stats": {"1": {"type": "ACHIEVEMENTS", "bits": bits}}}}) + b"\x08")
    schema = sf.read_schema(stats, 99)
    assert [(a["name"], a["description"]) for a in schema["achievements"]] == [("666", "Roll it"), ("Plain", "7")]
