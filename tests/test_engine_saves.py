"""Save folders engines choose by themselves (Unreal, Godot, Ren'Py), and the
hours the itch app recorded."""
import json
import os
import sqlite3
from pathlib import Path

from openachievements.adapters import autodetect as ad
from openachievements.adapters import engine_saves as es
from openachievements.adapters import stores
from test_autodetect import catalogue, detector, make_game  # noqa: F401 - catalogue is a fixture
from test_stores import itch_db


def program(root: Path, rel: str, content: bytes = b"MZ") -> str:
    exe = root / rel
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(content)
    return os.path.normcase(str(exe))


# ---- Unreal -----------------------------------------------------------------------------

def test_unreal_saves_in_appdata_first_then_beside_the_game(tmp_path):
    exe = program(tmp_path / "G", "Proj/Binaries/Win64/Proj-Win64-Shipping.exe")
    beside = tmp_path / "G" / "Proj" / "Saved" / "SaveGames"
    beside.mkdir(parents=True)
    assert es.unreal_saves(exe, tmp_path / "nothing") == [str(beside)]
    local = tmp_path / "Local" / "Proj" / "Saved" / "SaveGames"
    local.mkdir(parents=True)
    assert es.unreal_saves(exe, tmp_path / "Local") == [str(local), str(beside)]
    assert es.unreal_saves(program(tmp_path / "G", "game.exe"), tmp_path) == []


# ---- Godot ------------------------------------------------------------------------------

def test_a_godot_game_finds_its_app_userdata_folder(tmp_path):
    root = tmp_path / "Godot" / "app_userdata"
    (root / "A Short Hike").mkdir(parents=True)
    (root / "Other Game").mkdir()
    exe = program(tmp_path / "itch", "a-short-hike/AShortHike.exe")
    Path(exe).with_suffix(".pck").write_bytes(b"GDPC")
    assert es.godot_saves(exe, ["A Short Hike"], root) == [str(root / "A Short Hike")]
    assert es.godot_saves(exe, [], root) == [str(root / "A Short Hike")]            # by the program's name
    embedded = program(tmp_path / "itch", "brotato/Brotato.exe", b"MZ....pck....\x00" * 4 + b"GDPC")
    (root / "Brotato").mkdir()
    assert es.godot_saves(embedded, ["Brotato"], root) == [str(root / "Brotato")]
    not_godot = program(tmp_path / "itch", "unity/Brotato.exe")
    assert es.godot_saves(not_godot, ["Brotato"], root) == []


# ---- Ren'Py -----------------------------------------------------------------------------

def renpy_game(root: Path, folder: str, exe: str, options: str | None = None) -> str:
    path = program(root, f"{folder}/{exe}")
    (root / folder / "renpy").mkdir()
    (root / folder / "game").mkdir()
    if options is not None:
        (root / folder / "game" / "options.rpy").write_text(options, encoding="utf-8")
    return path


def test_renpy_saves_by_the_games_own_save_directory(tmp_path):
    root = tmp_path / "RenPy"
    (root / "DDLC-1454445547").mkdir(parents=True)
    (root / "Unrelated-1234567890").mkdir()
    exe = renpy_game(tmp_path / "itch", "ddlc-win", "DDLC.exe",
                     'define config.name = _("Doki Doki")\ndefine config.save_directory = "DDLC-1454445547"\n')
    assert es.renpy_saves(exe, ["Doki Doki Literature Club!"], root) == [str(root / "DDLC-1454445547")]


def test_renpy_saves_by_name_when_options_are_compiled_only(tmp_path):
    root = tmp_path / "RenPy"
    (root / "KatawaShoujo-1287163027").mkdir(parents=True)
    exe = renpy_game(tmp_path / "itch", "Katawa Shoujo", "Katawa Shoujo.exe")
    (tmp_path / "itch" / "Katawa Shoujo" / "game" / "saves").mkdir()
    assert es.renpy_saves(exe, ["Katawa Shoujo"], root) == [
        str(root / "KatawaShoujo-1287163027"), str(tmp_path / "itch" / "Katawa Shoujo" / "game" / "saves")]
    assert es.renpy_saves(program(tmp_path / "x", "NotRenpy/game.exe"), ["Katawa Shoujo"], root) == []


def test_a_recognised_godot_game_has_its_save_folder_first(profile, catalogue, monkeypatch, tmp_path):
    exe = make_game(tmp_path / "D", "Games/Tiny Godot Thing/TinyGodot.exe")
    Path(exe).with_suffix(".pck").write_bytes(b"GDPC")
    userdata = tmp_path / "Roaming" / "Godot" / "app_userdata" / "Tiny Godot Thing"
    userdata.mkdir(parents=True)
    monkeypatch.setattr(es, "godot_root", lambda: tmp_path / "Roaming" / "Godot" / "app_userdata")
    monkeypatch.setattr(stores, "everything", lambda: [])
    found = detector(profile, catalogue, monkeypatch, tmp_path).poll({exe})          # .pck makes it a game
    assert found[0]["game_id"] == "local-tiny-godot-thing"
    assert found[0]["save_folders"][0] == str(userdata)


# ---- itch.io play time ------------------------------------------------------------------

def itch_with_time(tmp_path: Path, rows: list) -> Path:
    lib = tmp_path / "itch-apps"
    db = itch_db(tmp_path / "butler.db", caves=[(c, g, "loc1", f"f{g}", None) for c, g, *_ in rows],
                 games=[(g, t, "game") for _c, g, t, *_ in rows], locations=[("loc1", str(lib))])
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE caves ADD COLUMN local_seconds_run INTEGER")
    con.execute("ALTER TABLE caves ADD COLUMN local_last_run_at TEXT")
    con.execute("ALTER TABLE caves ADD COLUMN last_touched_at TEXT")
    for c, _g, _t, seconds, local, last, touched in rows:
        con.execute("UPDATE caves SET seconds_run = ?, local_seconds_run = ?, local_last_run_at = ?, "
                    "last_touched_at = ? WHERE id = ?", (seconds, local, last, touched, c))
    con.commit()
    con.close()
    return db


def test_itch_play_time_comes_from_the_itch_app(tmp_path):
    db = itch_with_time(tmp_path, [
        ("c1", 1234, "A Short Hike", 9000, 0, "2026-10-01T18:30:05.123456789+02:00", None),
        ("c2", 99, "Never Played", 0, 0, None, "2026-01-01T00:00:00Z"),
        ("c3", 77, "Only Local", 0, 600, None, "2026-09-30T10:00:00Z")])
    played = {p["install"].ref: (p["minutes"], p["last_played"]) for p in stores.itch_played(db)}
    assert played == {"1234": (150, "2026-10-01T16:30:05Z"), "77": (10, "2026-09-30T10:00:00Z")}


def test_itch_hours_join_the_library_once_and_never_go_down(profile, catalogue, monkeypatch, tmp_path):
    db = itch_with_time(tmp_path, [
        ("c1", 1234, "Odd Little Game", 9000, 0, "2026-10-01T16:30:05Z", None),
        ("c2", 55, "Hollow Knight", 3600, 0, "2026-10-02T10:00:00Z", None)])
    monkeypatch.setattr(stores, "itch_db", lambda: db)
    d = detector(profile, catalogue, monkeypatch, tmp_path)
    d._refresh()
    d.import_itch_playtime()
    lib = {g["game_id"]: g for g in profile.library()["games"]}
    assert lib["itch-1234"]["playtime_seconds"] == 150 * 60 and lib["itch-1234"]["platform"] == "PC (itch.io)"
    assert lib["itch-1234"]["last_played"] == "2026-10-01T16:30:05Z"
    assert lib["steam-367520"]["playtime_seconds"] == 60 * 60        # the catalogue title: the Steam game
    events = len(profile.log.known_ids())
    d.import_itch_playtime()                                           # nothing new: nothing recorded
    assert len(profile.log.known_ids()) == events
    profile.record("game.metadata_updated", {"playtime_minutes": 500}, game_id="itch-1234", adapter="itch")
    d.import_itch_playtime()
    assert profile.state()["games"]["itch-1234"]["playtime_minutes"] == 500
