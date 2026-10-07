"""Achievements a Steam emulator recorded: the file formats, matching Steam's
internal names to the library, and unlocks that pop up as they happen."""
import json
import time
from datetime import datetime, timezone

import pytest

from openachievements import notify, rarity, reducer
from openachievements.adapters import emulator
from openachievements.catalog import steam as cat
from test_catalog import fake_steam, page, row

INI = """[FOY_COMPLETE]
Achieved=1
CurProgress=0
MaxProgress=0
UnlockTime=1791315515

[KIT_COMPLETE]
Achieved=0
CurProgress=0
MaxProgress=0
UnlockTime=0

[SteamAchievements]
00000=FOY_COMPLETE
Count=1
"""


def test_ini_emulators_read_only_what_is_achieved():
    assert emulator.parse(INI.encode(), "ini") == {"FOY_COMPLETE": 1791315515.0}


def test_onlinefix_ini_and_utf16_files():
    text = "[ACH_A]\nachieved=true\ntimestamp=1700000000\n[ACH_B]\nachieved=false\n"
    assert emulator.parse(text.encode("utf-16"), "ini") == {"ACH_A": 1700000000.0}


def test_goldberg_json_in_both_shapes():
    doc = {"ACH_A": {"earned": True, "earned_time": 1700000000}, "ACH_B": {"earned": False, "earned_time": 0},
           "ACH_C": {"earned": True, "earned_time": 0}}
    assert emulator.parse(json.dumps(doc).encode(), "json") == {"ACH_A": 1700000000.0, "ACH_C": None}
    listed = [{"name": "ACH_A", "achieved": 1, "UnlockTime": 1700000001}, {"name": "ACH_B", "achieved": 0}]
    assert emulator.parse(json.dumps(listed).encode(), "json") == {"ACH_A": 1700000001.0}


@pytest.mark.parametrize("data,fmt", [(b"{not json", "json"), (b"[[[", "ini")])
def test_a_broken_file_is_an_error_not_a_crash(data, fmt):
    with pytest.raises(ValueError):
        emulator.parse(data, fmt)


# ---- end to end ---------------------------------------------------------------------------

ROOM = [row("Foyer Complete", "Completed the Foyer", "a1", "70.0"), row("Kitchen Complete", "Completed the Kitchen",
                                                                        "a2", "50.0")]
SCHEMA = [{"internal_name": "FOY_COMPLETE", "localized_name": "Foyer Complete", "localized_desc": "Completed the Foyer",
           "icon": "a1.jpg", "player_percent_unlocked": "70.0", "hidden": False},
          {"internal_name": "KIT_COMPLETE", "localized_name": "Kitchen Complete",
           "localized_desc": "Completed the Kitchen", "icon": "a2.jpg", "player_percent_unlocked": "50.0",
           "hidden": False}]


@pytest.fixture
def public(tmp_path, monkeypatch):
    """A Windows-shaped Public Documents and Roaming folder, and nothing else."""
    roots = {"PUBLIC_DOCUMENTS": tmp_path / "Public" / "Documents", "APPDATA": tmp_path / "Roaming"}
    monkeypatch.setattr(emulator, "_roots", lambda: [roots])
    return roots


def ini_file(public, appid, text):
    folder = public["PUBLIC_DOCUMENTS"] / "Steam" / "CODEX" / str(appid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "achievements.ini").write_text(text)
    return folder / "achievements.ini"


def unlocks(profile):
    return [e for e in profile.log.read().events if e["event_type"] == "achievement.unlocked"]


def test_an_emulator_unlock_is_recorded_with_its_own_time(profile, tmp_path, public):
    games = {1361320: (2, page("The Room 4: Old Sins", ROOM))}
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 1361320,
                       fetch=fake_steam(games, schemas={1361320: SCHEMA}))          # pack carries Steam's names
    ini_file(public, 1361320, INI)
    watcher = emulator.EmulatorWatcher(profile, catalog_dir=tmp_path / "c")
    written = watcher.poll()
    assert [e["achievement_id"] for e in written] == ["steam-1361320:foyer-complete"]
    e = written[0]
    assert e["source"]["adapter"] == "steam-emulator" and e["payload"]["mode"] == "CODEX"
    assert e["occurred_at"].startswith("2026-10-06T")
    lib = next(g for g in profile.library()["games"] if g["game_id"] == "steam-1361320")
    foyer = next(a for a in lib["achievements"] if a["id"] == "foyer-complete")
    assert foyer["provenance"] == ["emulator"]
    assert watcher.poll() == []                                     # unchanged file: not read again
    ini_file(public, 1361320, INI.replace("Achieved=0", "Achieved=1").replace("UnlockTime=0", "UnlockTime=1791316000"))
    assert [e["achievement_id"] for e in watcher.poll()] == ["steam-1361320:kitchen-complete"]
    assert len(unlocks(profile)) == 2


def test_names_come_from_the_rarity_cache_when_the_pack_has_none(profile, tmp_path, public, monkeypatch):
    games = {1361320: (2, page("The Room 4: Old Sins", ROOM))}
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 1361320, fetch=fake_steam(games))
    assert not any(a.get("external_id") for a in profile.state()["packs"]["steam-1361320"]["achievements"].values())
    ini_file(public, 1361320, INI)
    asked = []

    def steam(url):
        asked.append(url)
        return 200, json.dumps({"response": {"achievements": SCHEMA}})
    monkeypatch.setattr(rarity, "_default_fetch", steam)
    watcher = emulator.EmulatorWatcher(profile, catalog_dir=tmp_path / "c")
    assert [e["achievement_id"] for e in watcher.poll()] == ["steam-1361320:foyer-complete"]
    assert len(asked) == 1 and rarity.names("1361320")["FOY_COMPLETE"] == "Foyer Complete"


def test_an_unlock_waits_for_names_then_is_recorded(profile, tmp_path, public, monkeypatch):
    games = {1361320: (2, page("The Room 4: Old Sins", ROOM))}
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 1361320, fetch=fake_steam(games))
    ini_file(public, 1361320, INI)
    from openachievements import privacy
    privacy.change(profile.config, rarity=False)                    # the player said: do not ask Steam
    watcher = emulator.EmulatorWatcher(profile, catalog_dir=tmp_path / "c")
    assert watcher.poll() == []
    assert any("FOY_COMPLETE" in p for p in watcher.new_problems())
    data = {"by_id": {}, "by_name": {}, "names": {"FOY_COMPLETE": "Foyer Complete"}, "fetched": time.time()}
    (rarity.cache_dir() / "steam-1361320.json").write_text(json.dumps(data))
    assert [e["achievement_id"] for e in watcher.poll()] == ["steam-1361320:foyer-complete"]   # same file, read again


def test_a_game_only_the_emulator_knows_joins_the_library(profile, tmp_path, public):
    games = {1361320: (2, page("The Room 4: Old Sins", ROOM))}
    cat.crawl([(1361320, None)], tmp_path / "c", fake_steam(games, schemas={1361320: SCHEMA}))
    ini_file(public, 1361320, INI)
    written = emulator.EmulatorWatcher(profile, catalog_dir=tmp_path / "c").poll()
    assert [e["achievement_id"] for e in written] == ["steam-1361320:foyer-complete"]
    game = profile.state()["games"]["steam-1361320"]
    assert game["title"] == "The Room 4: Old Sins" and game["status"] == "playing"


def test_goldberg_gse_and_empress_folders_are_read(profile, tmp_path, public):
    games = {10: (2, page("Ten", ROOM))}
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 10, fetch=fake_steam(games, schemas={10: SCHEMA}))
    now = int(time.time())
    for rel in ("Goldberg SteamEmu Saves/10/achievements.json", "GSE Saves/10/achievements.json",
                "EMPRESS/10/remote/10/achievements.json"):
        path = public["APPDATA"].joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"FOY_COMPLETE": {"earned": True, "earned_time": now}}))
    found = {(name, appid) for name, appid, _p, _f in emulator.achievement_files()}
    assert found == {("Goldberg", 10), ("GSE", 10), ("EMPRESS", 10)}
    written = emulator.EmulatorWatcher(profile, catalog_dir=tmp_path / "c").poll()
    assert [e["achievement_id"] for e in written] == ["steam-10:foyer-complete"]       # once, not three times


def test_a_fresh_emulator_unlock_pops_up_and_an_old_one_does_not(profile, tmp_path, public):
    games = {10: (2, page("Ten", ROOM))}
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 10, fetch=fake_steam(games, schemas={10: SCHEMA}))
    fresh = INI.replace("Achieved=0", "Achieved=1").replace("UnlockTime=0", f"UnlockTime={int(time.time()) - 5}")
    ini_file(public, 10, fresh)                                       # Foyer yesterday, Kitchen just now
    written = emulator.EmulatorWatcher(profile, catalog_dir=tmp_path / "c").poll()
    now = datetime.now(timezone.utc)
    assert [(e["achievement_id"], notify.wanted(e, dict(notify.DEFAULTS), now)) for e in written] == [
        ("steam-10:foyer-complete", False), ("steam-10:kitchen-complete", True)]


def test_switched_off_reads_nothing(profile, tmp_path, public):
    games = {10: (2, page("Ten", ROOM))}
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 10, fetch=fake_steam(games, schemas={10: SCHEMA}))
    ini_file(public, 10, INI)
    emulator.set_enabled(profile, False)
    assert emulator.EmulatorWatcher(profile).poll() == []
    assert not reducer.is_unlocked(profile.state(), "steam-10:foyer-complete")


def test_a_library_game_without_a_list_gets_it_from_the_catalogue(profile, tmp_path, public):
    """Hell Clock: in the library before the crawl reached it, never played
    through Steam, so no list ever came and its emulator unlocks waited."""
    profile.register_game("steam-1782460", "Hell Clock", platform="PC", external_ids={"steam": 1782460})
    games = {1782460: (2, page("Hell Clock", ROOM))}
    cat.crawl([(1782460, None)], tmp_path / "c", fake_steam(games, schemas={1782460: SCHEMA}))
    ini_file(public, 1782460, INI)
    watcher = emulator.EmulatorWatcher(profile, catalog_dir=tmp_path / "c")
    assert watcher.poll() == []                                       # no list yet: the unlock waits
    cat.PackFetcher(profile, fetch=fake_steam({})).want(cat.CatalogIndex(tmp_path / "c"))
    assert profile.state()["packs"]["steam-1782460"]["achievements"]
    assert [e["achievement_id"] for e in watcher.poll()] == ["steam-1782460:foyer-complete"]
