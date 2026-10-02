"""Unreal Engine (GVAS) saves, Townfall's shipped rules, and hidden achievements' text."""
import json
import struct

import pytest

from openachievements import gvas, reducer
from openachievements.adapters import savefile, saverules
from openachievements.catalog import steam as cat
from test_catalog import fake_steam, page, row


# ---- writing small saves the way Unreal does -------------------------------------------

def fstr(text):
    data = text.encode("utf-8") + b"\0"
    return struct.pack("<i", len(data)) + data


def header(ue5, extra_byte=False):
    out = b"GVAS" + struct.pack("<iii", 3, 522, ue5) + struct.pack("<HHHI", 5, 6, 1, 0) + fstr("++UE5+Release-5.6")
    out += struct.pack("<ii", 3, 1) + b"\x11" * 16 + struct.pack("<i", 7)
    return out + fstr("/Script/Game.SaveData") + (b"\0" if extra_byte else b"")


def new_type(name, *params):
    return fstr(name) + struct.pack("<i", len(params)) + b"".join(params)


def new_prop(name, type_bytes, value, flags=0):
    return fstr(name) + type_bytes + struct.pack("<i", len(value)) + bytes([flags]) + value


def int_map(entries):
    body = struct.pack("<ii", 0, len(entries)) + b"".join(fstr(k) + struct.pack("<i", v) for k, v in entries.items())
    return new_prop("SavedIntMap", new_type("MapProperty", new_type("NameProperty"), new_type("IntProperty")), body)


def float_map(entries):
    body = struct.pack("<ii", 0, len(entries)) + b"".join(fstr(k) + struct.pack("<f", v) for k, v in entries.items())
    return new_prop("SavedFloatMap", new_type("MapProperty", new_type("NameProperty"), new_type("FloatProperty")), body)


def townfall_profile(crafted=1, record=-1.0):
    """ProfileData.sav as Townfall (UE 5.6) writes it, reduced to what matters."""
    return (header(1017, extra_byte=True)
            + new_prop("SaveDateTimeTicks", new_type("Int64Property"), struct.pack("<q", 639264125053260000))
            + new_prop("bSeenIntro", new_type("BoolProperty"), b"", flags=0x10)
            + new_prop("Blob", new_type("MysteryProperty"), b"\xff" * 9)          # not something rules read
            + int_map({"Total_NumItemsCrafted": crafted, "Total_EndingsAchievedBitmask": 0})
            + float_map({"TimePlayed_TotalRecord": record, "NC.Character.GyroMult": 1.0})
            + fstr("None"))


def old_prop(name, kind, value, extra=b""):
    return fstr(name) + fstr(kind) + struct.pack("<q", len(value)) + extra + b"\0" + value


def old_layout_save():
    """Before UE 5.4: type as a string, int64 size, type-specific extras."""
    names = struct.pack("<i", 2) + fstr("Alpha") + fstr("Beta")
    return (header(1009)
            + old_prop("Level", "IntProperty", struct.pack("<i", 7))
            + fstr("Hard") + fstr("BoolProperty") + struct.pack("<q", 0) + b"\x01" + b"\0"
            + old_prop("Name", "StrProperty", fstr("Zein"))
            + old_prop("Bosses", "ArrayProperty", names, extra=fstr("StrProperty"))
            + fstr("None"))


# ---- the reader ---------------------------------------------------------------------------

def test_a_ue5_save_reads_into_plain_values():
    data = gvas.parse(townfall_profile(crafted=21, record=13000.5))
    assert data["SavedIntMap"]["Total_NumItemsCrafted"] == 21
    assert data["SavedFloatMap"]["TimePlayed_TotalRecord"] == 13000.5
    assert data["bSeenIntro"] is True and "Blob" not in data


def test_an_older_layout_save_reads_too():
    data = gvas.parse(old_layout_save())
    assert data == {"Level": 7, "Hard": True, "Name": "Zein", "Bosses": ["Alpha", "Beta"]}


@pytest.mark.parametrize("damage", [b"", b"GVAS", b"NOPE" + b"\0" * 40])
def test_a_broken_save_is_an_error_not_a_crash(damage):
    with pytest.raises(gvas.GvasError):
        gvas.parse(damage)


def test_a_truncated_save_never_reads_past_its_end():
    blob = townfall_profile()
    with pytest.raises(gvas.GvasError):
        gvas.parse(blob[:len(blob) - 30])


def test_fields_reach_keys_that_contain_dots():
    data = gvas.parse(townfall_profile())
    assert savefile.lookup(data, "SavedFloatMap.NC.Character.GyroMult") == 1.0
    assert savefile.lookup(data, "SavedFloatMap.NC.Character") is savefile._MISSING


# ---- Townfall, end to end -----------------------------------------------------------------

TOWNFALL = [row("Welcome To St. Amelia", "", "a1", "88.4"), row("From Scratch", "Create 20 new items.", "a2", "33.0"),
            row("Fleeting Visit", "", "a3", "4.1")]


@pytest.fixture
def townfall(profile, tmp_path, monkeypatch):
    local = tmp_path / "Local"
    monkeypatch.setattr(savefile, "root_folder", lambda name: local if name == "LOCALAPPDATA" else None)
    games = {1636440: (3, page("SILENT HILL: Townfall", TOWNFALL))}
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 1636440, fetch=fake_steam(games))
    folder = local / "Townfall" / "Saved" / "SaveGames"
    folder.mkdir(parents=True)
    return folder


def unlocked(profile):
    return {e["achievement_id"]: e for e in profile.log.read().events if e["event_type"] == "achievement.unlocked"}


def test_townfall_unlocks_from_its_own_save_and_nothing_else(profile, townfall):
    (townfall / "ProfileData.sav").write_bytes(townfall_profile(crafted=3))
    assert saverules.apply(profile) == ["steam-1636440"]
    pack = profile.state()["packs"]["steam-1636440"]
    assert pack["saves"][0]["format"] == "gvas" and pack["source"] == "steam-catalog"
    watcher = savefile.SaveWatcher(profile)
    assert watcher.poll() == []                                        # 3 crafted, no finished run
    (townfall / "ProfileData.sav").write_bytes(townfall_profile(crafted=20, record=-1.0))
    written = watcher.poll()
    assert [e["achievement_id"] for e in written] == ["steam-1636440:from-scratch"]
    assert written[0]["payload"]["provenance"] == "save-derived"
    (townfall / "ProfileData.sav").write_bytes(townfall_profile(crafted=20, record=13500.0))
    assert [e["achievement_id"] for e in watcher.poll()] == ["steam-1636440:fleeting-visit"]
    assert "steam-1636440:welcome-to-st-amelia" not in unlocked(profile)  # never guessed


def test_shipped_rules_are_applied_once_and_again_after_an_update_drops_them(profile, townfall, tmp_path):
    assert saverules.apply(profile) == ["steam-1636440"]
    assert saverules.apply(profile) == []
    games = {1636440: (3, page("SILENT HILL: Townfall", TOWNFALL))}
    entry = cat.fetch_game(1636440, fake_steam(games), known_title="SILENT HILL: Townfall")
    cat.install_entry(profile, entry, kind="pack.updated")             # a refresh without rules
    assert not profile.state()["packs"]["steam-1636440"]["achievements"]["from-scratch"].get("rules")
    assert saverules.apply(profile) == ["steam-1636440"]


def test_the_save_folder_is_allowed_once_and_a_revoke_sticks(profile, townfall):
    saverules.apply(profile)
    save = profile.state()["packs"]["steam-1636440"]["saves"][0]
    assert savefile.granted_folder(profile, "steam-1636440", save) == townfall.resolve()
    savefile.revoke(profile, "steam-1636440", "profile")
    saverules.apply(profile)
    assert savefile.granted_folder(profile, "steam-1636440", save) is None


def test_every_shipped_rules_file_is_a_valid_pack_addition():
    from openachievements.packs import validate_definitions
    for game_id, rules in saverules.bundled().items():
        assert game_id.startswith("steam-") and rules["saves"] and rules["achievements"]
        achievements = []
        for n, (name, item) in enumerate(rules["achievements"].items()):
            assert item["why"] and item["rules"], name
            achievements.append({"id": f"a{n}", "name": name, "rules": item["rules"]})
        validate_definitions({"id": game_id, "game_ids": [game_id], "saves": rules["saves"]}, achievements)


def old_map_of_structs():
    """UE 4.27 (Deadlink): a map of names to {AsFloat, AsString} structs."""
    entry = (old_prop("AsFloat", "FloatProperty", struct.pack("<f", 1.0))
             + old_prop("AsString", "StrProperty", fstr("")) + fstr("None"))
    body = struct.pack("<ii", 0, 1) + fstr("game.tutorial.finished") + entry
    return (header(0).replace(struct.pack("<iii", 3, 522, 0), struct.pack("<ii", 2, 522))
            + old_prop("GameplayDatabase", "MapProperty", body, extra=fstr("StrProperty") + fstr("StructProperty"))
            + fstr("None"))


def test_an_older_layout_map_of_structs_reads_its_values():
    data = gvas.parse(old_map_of_structs())
    assert data["GameplayDatabase"]["game.tutorial.finished"] == {"AsFloat": 1.0, "AsString": ""}


# ---- hidden achievements ----------------------------------------------------------------

SCHEMA = [{"internal_name": "ACH_1", "localized_name": "Welcome To St. Amelia",
           "localized_desc": "Escape the Otherworld and return to the town square.", "icon": "a1.jpg",
           "hidden": True, "player_percent_unlocked": "88.4"},
          {"internal_name": "ACH_24", "localized_name": "From Scratch", "localized_desc": "Create 20 new items.",
           "icon": "a2.jpg", "hidden": False, "player_percent_unlocked": "33.0"}]


def test_steams_schema_gives_hidden_achievements_their_real_text():
    entry = cat.fetch_game(1636440, fake_steam({}, schemas={1636440: SCHEMA}), known_title="Townfall")
    first = entry["achievements"][0]
    assert first["hidden"] is True and first["description"].startswith("Escape the Otherworld")
    assert first["icon"].endswith("/1636440/a1.jpg")


def test_a_public_page_list_is_refreshed_with_hidden_text_keeping_its_ids(profile, tmp_path):
    games = {1636440: (3, page("SILENT HILL: Townfall", TOWNFALL))}
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 1636440, fetch=fake_steam(games))
    before = profile.state()["packs"]["steam-1636440"]["achievements"]
    assert before["welcome-to-st-amelia"]["description"] == ""
    fetcher = cat.PackFetcher(profile, fetch=fake_steam(games, schemas={1636440: SCHEMA}))
    assert fetcher.fetch_one(1636440) is True
    after = profile.state()["packs"]["steam-1636440"]["achievements"]
    assert after["welcome-to-st-amelia"]["description"].startswith("Escape the Otherworld")
    assert "welcome-to-st-amelia" in after and "from-scratch" in after
    assert fetcher.fetch_one(1636440) is False                        # nothing left to fill in


def test_the_owner_can_read_hidden_text_and_the_public_cannot(profile, tmp_path):
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 1636440,
                       fetch=fake_steam({}, schemas={1636440: SCHEMA}))
    ach = next(a for g in profile.library()["games"] for a in g["achievements"] if a["id"] == "welcome-to-st-amelia")
    assert ach["name"] == "Hidden achievement" and ach["description"] == ""
    assert ach["secret"]["name"] == "Welcome To St. Amelia"
    public = reducer.without_secrets(profile.library()["games"])
    assert all(a["secret"] is None for g in public for a in g["achievements"])
    assert "Otherworld" not in json.dumps(public)

