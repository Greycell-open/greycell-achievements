"""RetroAchievements and Steam imports against recorded-shape fixtures, and
games from different sources shown together."""
import pytest

from openachievements import reducer
from openachievements.adapters import retroachievements as ra
from openachievements.adapters.base import AdapterError, utc_iso
from conftest import write_pack


def ra_fetcher(progress_by_game, calls=None):
    """Fake RA API. `progress_by_game[game_id]` is the game payload."""
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if "API_GetUserCompletionProgress" in url:
            results = [{"GameID": gid, "Title": g["Title"], "ConsoleName": g["ConsoleName"],
                        "MostRecentAwardedDate": g.get("_stamp", "2024-01-01T00:00:00+00:00")}
                       for gid, g in progress_by_game.items()]
            return {"Count": len(results), "Total": len(results), "Results": results}
        gid = int(url.split("g=")[1].split("&")[0])
        return progress_by_game[gid]
    return fetch


def sonic(earned_soft=True, earned_hard=False, stamp="2024-01-01T00:00:00+00:00"):
    ach = {"ID": 9, "Title": "That Was Easy", "Description": "Complete the first act in Green Hill Zone",
           "Points": 3, "BadgeName": "250336", "DisplayOrder": 1, "type": "progression"}
    if earned_soft:
        ach["DateEarned"] = "2016-03-12 17:47:29"
    if earned_hard:
        ach["DateEarnedHardcore"] = "2016-03-13 10:00:00"
    other = {"ID": 10, "Title": "Speed Run", "Description": "Finish fast", "Points": 10, "BadgeName": "1",
             "DisplayOrder": 2}
    return {"ID": 1, "Title": "Sonic the Hedgehog", "ConsoleName": "Mega Drive", "ImageIcon": "/Images/067895.png",
            "Achievements": {"9": ach, "10": other}, "_stamp": stamp}


def test_retroachievements_import_maps_games_packs_unlocks_and_keeps_ra_ids(profile):
    summary = ra.import_account(profile, "Zein", "key", fetch=ra_fetcher({1: sonic()}), pause=0)
    assert (summary.games_added, summary.packs_written, summary.unlocks_added) == (1, 1, 1)
    game = profile.library()["games"][0]
    assert game["game_id"] == "ra-1" and game["platform"] == "Mega Drive"
    assert game["external_ids"] == {"retroachievements": 1}
    ach = next(a for a in game["achievements"] if a["id"] == "a9")
    assert ach["unlocked"] and ach["points"] == 3 and ach["provenance"] == ["imported"]
    assert ach["unlocked_at"] == "2016-03-12T17:47:29.000Z"
    assert ach["icon"] == "https://media.retroachievements.org/Badge/250336.png"


def test_reimporting_adds_nothing(profile):
    fetch = ra_fetcher({1: sonic()})
    ra.import_account(profile, "Zein", "key", fetch=fetch, pause=0)
    before = len(profile.log.read().events)
    again = ra.import_account(profile, "Zein", "key", fetch=fetch, pause=0, only_changed=False)
    assert again.unlocks_added == 0 and again.packs_written == 0
    assert len(profile.log.read().events) == before + 1        # only the import_completed marker


def test_unchanged_games_are_not_fetched_again(profile):
    calls = []
    fetch = ra_fetcher({1: sonic()}, calls)
    ra.import_account(profile, "Zein", "key", fetch=fetch, pause=0)
    calls.clear()
    ra.import_account(profile, "Zein", "key", fetch=fetch, pause=0)
    assert not any("GetGameInfoAndUserProgress" in c for c in calls)


def test_hardcore_after_softcore_is_a_second_record_not_a_replacement(profile):
    ra.import_account(profile, "Zein", "key", fetch=ra_fetcher({1: sonic()}), pause=0)
    ra.import_account(profile, "Zein", "key", pause=0,
                      fetch=ra_fetcher({1: sonic(earned_hard=True, stamp="2024-02-01T00:00:00+00:00")}))
    records = profile.state()["unlocks"]["ra-1:a9"]["records"]
    assert sorted(r["mode"] for r in records.values()) == ["hardcore", "softcore"]


def test_changed_ra_definitions_update_the_pack(profile):
    ra.import_account(profile, "Zein", "key", fetch=ra_fetcher({1: sonic()}), pause=0)
    changed = sonic(stamp="2024-03-01T00:00:00+00:00")
    changed["Achievements"]["10"]["Points"] = 25
    summary = ra.import_account(profile, "Zein", "key", fetch=ra_fetcher({1: changed}), pause=0)
    assert summary.packs_written == 1
    assert profile.state()["packs"]["ra-1"]["achievements"]["a10"]["points"] == 25


def test_the_api_key_never_appears_in_errors():
    with pytest.raises(AdapterError) as exc:
        from openachievements.adapters.base import http_json
        http_json("http://127.0.0.1:9/API/x.php?y=SECRETKEY&u=me", retries=0)
    assert "SECRETKEY" not in str(exc.value)


def test_no_code_path_writes_to_a_platform():
    """Every adapter request is a GET to a read endpoint (acceptance step 24)."""
    import inspect
    for module in (ra,):
        source = inspect.getsource(module)
        assert "POST" not in source and "urlopen" not in source


def test_a_local_game_and_its_steam_copy_show_as_one_game(profile, tmp_path):
    from openachievements.adapters import steam_local
    from test_autodetect import fake_steam_install
    profile.install_pack(write_pack(tmp_path / "pack", pack_id="test-community", game_id="test-game"))
    profile.record_unlock("test-community:first-step", adapter="test")
    steam_local.link(profile, fake_steam_install(tmp_path / "Steam"))
    steam_local.SteamLocalWatcher(profile, catalog_dir=tmp_path / "nocat").poll()
    profile.link_games("steam-10", "test-game")
    games = profile.library()["games"]
    assert [g["game_id"] for g in games] == ["test-game"]
    both = games[0]
    assert both["total"] == 6 and both["unlocked"] == 2
    assert {a["source"] for a in both["achievements"]} == {"pack", "steam-local"}
    assert both["external_ids"] == {"steam": 10}
    profile.link_games("steam-10", None)
    assert len(profile.library()["games"]) == 2


def test_link_cycles_are_refused_and_survive_if_they_arrive_anyway(profile, tmp_path):
    from openachievements.profile import ProfileError
    profile.register_game("a-game", "A")
    profile.register_game("b-game", "B")
    profile.link_games("a-game", "b-game")
    with pytest.raises(ProfileError):
        profile.link_games("b-game", "a-game")
    # A cycle written by some other client still shows each game once.
    profile.record("game.metadata_updated", {"linked_to": "a-game"}, game_id="b-game")
    assert [g["game_id"] for g in profile.library()["games"]] == ["a-game"]


def test_timestamps_become_utc_iso():
    assert utc_iso("2016-03-12 17:47:29") == "2016-03-12T17:47:29.000Z"
    assert utc_iso(1518652800) == "2018-02-15T00:00:00.000Z"
    assert utc_iso("2024-04-23T21:28:49+02:00") == "2024-04-23T19:28:49.000Z"
    assert utc_iso(None) is None and utc_iso("garbage") is None
