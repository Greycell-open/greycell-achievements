"""Save rules found by name (ruleseek.py), and the Mini Airways rules that ship.

Mini Airways writes Ach_<Steam id> = true in its own save for every
achievement it awards; a DRM-free copy recorded nothing until a rule read it.
"""
import json

import pytest

from openachievements import reducer, ruleseek
from openachievements.adapters import saverules, savefile

APPID = 2289650
NAMES = {"Map_LondonPass": "Beat London Map", "GM_ContinuesTO5+": "Outbound Priority I",
         "GM_ContinuesTO10+": "Outbound Priority II", "AP_Time900+": "Patience Test",
         "Map_BarraLand": "Landing on Water", "Map_ParisPass": "Beat Paris Map"}


def es3(values: dict) -> bytes:
    return json.dumps({k: {"__type": "bool" if isinstance(v, bool) else "string", "value": v}
                       for k, v in values.items()}).encode()


def install_list(profile, names=NAMES, with_ids=True):
    schema = [{"internal_name": api, "localized_name": name, "localized_desc": "", "icon": "", "hidden": False}
              for api, name in names.items()]
    from openachievements.catalog import steam as cat

    def fetch(url):
        return 200, json.dumps({"response": {"achievements": schema}})
    entry = cat.fetch_game(APPID, fetch, known_title="Mini Airways - ATC simulator")
    if not with_ids:
        for a in entry["achievements"]:
            a.pop("external_id", None)
    cat.install_entry(profile, entry)
    return fetch


@pytest.fixture
def save(tmp_path, monkeypatch):
    low = tmp_path / "LocalLow"
    folder = low / "ContextCrossDivision" / "MiniAirways"
    folder.mkdir(parents=True)
    monkeypatch.setattr(savefile, "root_folder", lambda name: low if name == "LOCALLOW" else None)
    path = folder / "SaveFile.es3"
    path.write_bytes(es3({"TutorialDone": True, "Ach_Map_LondonPass": True, "Ach_Map_LondonPass_Date": "2026-10-07",
                          "Ach_AP_Time900+": True, "Ach_AP_Time900+_Date": "2026-10-07", "BestAircraftNumberLondon": 65}))
    return folder


def test_fields_named_after_achievements_are_found():
    values = {"Ach_Map_LondonPass": True, "Ach_Map_LondonPass_Date": "2026-10-07", "TutorialDone": True,
              "stats.ACH_AP_Time900+": 1, "Ach_Map_ParisPass": False}
    found = ruleseek.matches(values, NAMES)
    assert found["Beat London Map"][0] == "Ach_Map_LondonPass"
    assert found["Patience Test"][0] == "stats.ACH_AP_Time900+"
    assert "Beat Paris Map" not in found                      # false is not an unlock
    assert len(found) == 2


def test_two_fields_with_one_pattern_give_every_achievement_a_rule():
    values = {"Ach_Map_LondonPass": True, "Ach_AP_Time900+": True}
    rules = ruleseek.rules_for(values, NAMES)
    assert rules["Beat Paris Map"] == {"field": "Ach_Map_ParisPass", "op": "==", "value": "true"}
    assert set(rules) == set(NAMES.values())


def test_one_field_alone_is_not_generalised():
    rules = ruleseek.rules_for({"Ach_Map_LondonPass": True}, NAMES)
    assert set(rules) == {"Beat London Map"}


def test_ids_that_collide_without_punctuation_are_never_used():
    names = {"GM_Day10": "Ten days", "GM_Day10+": "Ten days plus"}
    assert ruleseek.matches({"Ach_GM_Day10": True}, names) == {}


def test_playing_a_game_writes_rules_and_unlocks_what_its_save_says(profile, save, monkeypatch):
    monkeypatch.setattr(saverules, "bundled", lambda: {})
    install_list(profile, with_ids=False)                       # an older list without Steam's ids
    monkeypatch.setattr(ruleseek, "_NAMES", {f"steam-{APPID}": NAMES})
    with profile.config.editing() as config:
        config.setdefault("save_candidates", {})[f"{profile.profile_id}:steam-{APPID}"] = [str(save)]
    found = ruleseek.seek_playing(profile, [f"steam-{APPID}"], {}, now=0)
    assert found == [{"game_id": f"steam-{APPID}", "file": "SaveFile.es3", "rules": 6, "seen": 2}]
    savefile.SaveWatcher(profile).poll()
    state = profile.state()
    unlocked = {k for k in state["unlocks"] if reducer.is_unlocked(state, k)}
    assert unlocked == {f"steam-{APPID}:beat-london-map", f"steam-{APPID}:patience-test"}
    # The next achievement the game awards unlocks with the rule written before it.
    (save / "SaveFile.es3").write_bytes(es3({"Ach_Map_LondonPass": True, "Ach_AP_Time900+": True,
                                             "Ach_Map_ParisPass": True}))
    savefile.SaveWatcher(profile).poll()
    assert reducer.is_unlocked(profile.state(), f"steam-{APPID}:beat-paris-map")
    # Looking again soon after does nothing; later, nothing new to add.
    assert ruleseek.seek_playing(profile, [f"steam-{APPID}"], {f"steam-{APPID}": 0}, now=60) == []
    assert ruleseek.seek_playing(profile, [f"steam-{APPID}"], {}, now=500) == []


def test_a_rule_already_there_is_never_replaced(profile, save, monkeypatch):
    install_list(profile)
    shipped = {f"steam-{APPID}": {"game_id": f"steam-{APPID}", "saves": [], "achievements": {
        "Beat London Map": {"rules": [{"signal": "save", "save": "x", "field": "Other", "op": "==", "value": "1"}]}}}}
    monkeypatch.setattr(saverules, "bundled", lambda: shipped)
    found = ruleseek.seek(profile, f"steam-{APPID}", [str(save)])
    from openachievements.savelearn import rules_dir
    written = json.loads((rules_dir(profile) / f"steam-{APPID}.json").read_text(encoding="utf-8"))
    assert "Beat London Map" not in written["achievements"] and found["rules"] == 5


def test_the_shipped_mini_airways_rules_match_the_real_save_layout(profile, save):
    install_list(profile)
    rules = saverules.bundled()[f"steam-{APPID}"]
    assert rules["saves"][0] == {"id": "save", "root": "LOCALLOW", "path": "ContextCrossDivision/MiniAirways",
                                 "pattern": "SaveFile.es3", "format": "es3"}
    assert saverules.apply(profile) == [f"steam-{APPID}"]
    savefile.SaveWatcher(profile).poll()
    state = profile.state()
    assert reducer.is_unlocked(state, f"steam-{APPID}:beat-london-map")
    assert reducer.is_unlocked(state, f"steam-{APPID}:patience-test")
    assert not reducer.is_unlocked(state, f"steam-{APPID}:beat-paris-map")
