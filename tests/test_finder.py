"""Games recognised without achievements get them from Steam (finder.py).

The case that started it: Mini Airways was recognised by its folder as a local
game, because Steam calls it "Mini Airways - ATC simulator", so it never got
its 65 achievements. Every Steam here is a fake.
"""
import json
from datetime import datetime, timedelta, timezone

from openachievements import finder as fd
from openachievements.adapters import autodetect as ad
from openachievements.catalog import steam as cat

APPID = 2289650
TITLE = "Mini Airways - ATC simulator"
SCHEMA = [{"internal_name": "ACH_FIRST", "localized_name": "First Landing", "localized_desc": "Land a plane",
           "icon": "a.jpg", "hidden": False},
          {"internal_name": "ACH_SECRET", "localized_name": "Night Shift", "localized_desc": "Work at night",
           "icon": "b.jpg", "hidden": True}]
NOW = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)


def fake_steam(results, schemas=None, calls=None, status=200):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if "storesearch" in url:
            return status, json.dumps({"total": len(results), "items": results})
        if "GetGameAchievements" in url:
            appid = int(url.split("appid=")[1].split("&")[0])
            rows = (schemas or {}).get(appid)
            return 200, json.dumps({"response": {"achievements": rows} if rows else {}})
        if "GetGlobalAchievementPercentagesForApp" in url:
            return 403, "{}"
        return 404, ""
    return fetch


def local_game(profile, title="Mini Airways", status="playing"):
    game_id = ad.local_game_id(title)
    profile.register_game(game_id, title, platform="PC", external_ids={})
    if status:
        profile.set_status(game_id, status)
    return game_id


def finder(profile, fetch, index=None, now=NOW):
    return fd.AchievementFinder(profile, fetch=fetch, index_loader=lambda: index, now=lambda: now)


def test_title_parts_give_the_name_before_and_after_a_subtitle():
    assert ad.title_parts(TITLE) == ["mini airways", "atc simulator"]
    assert ad.title_parts("Silent Hill: Townfall") == ["silent hill", "townfall"]
    assert ad.title_parts("Portal 2") == []


def test_the_matcher_knows_a_title_by_the_name_before_its_subtitle():
    index = ad._Titles({APPID: TITLE, 287980: "Mini Metro"})
    assert ad.TitleMatcher(index).match(["Mini Airways"]) == ("steam", APPID)
    # Shared by two titles, the short name is no evidence of either.
    index = ad._Titles({APPID: TITLE, 999: "Mini Airways - Soundtrack"})
    assert ad.TitleMatcher(index).match(["Mini Airways"]) is None


def test_a_local_game_is_found_on_steam_and_linked_with_its_achievements(profile):
    game_id = local_game(profile)
    calls = []
    results = [{"type": "app", "name": TITLE, "id": APPID}, {"type": "app", "name": "Mini Metro", "id": 287980}]
    found = finder(profile, fake_steam(results, {APPID: SCHEMA}, calls)).find_one(game_id)
    assert found == {"result": "linked", "steam_game": f"steam-{APPID}", "appid": APPID}
    state = profile.state()
    pack = state["packs"][f"steam-{APPID}"]
    assert len(pack["achievements"]) == 2
    assert state["games"][game_id]["linked_to"] == f"steam-{APPID}"
    assert state["games"][f"steam-{APPID}"]["status"] == "playing"
    assert any("storesearch" in c and "Mini%20Airways" in c for c in calls)
    assert fd.AchievementFinder(profile).waiting() == []      # linked: nothing left to look up


def test_the_catalogue_on_this_computer_is_asked_before_steam(profile):
    game_id = local_game(profile)
    entry = cat.fetch_game(APPID, fake_steam([], {APPID: SCHEMA}), known_title=TITLE)

    class Index:
        entries = {APPID: (0, 0, 2, TITLE), 287980: (0, 0, 9, "Mini Metro")}

        def get(self, appid):
            return entry if appid == APPID else None

    calls = []
    found = finder(profile, fake_steam([], calls=calls), index=Index()).find_one(game_id)
    assert found["result"] == "linked" and calls == []          # no request at all
    assert profile.state()["games"][game_id]["linked_to"] == f"steam-{APPID}"


def test_an_unclear_store_answer_links_nothing(profile):
    game_id = local_game(profile, "Airways")
    results = [{"type": "app", "name": "Airways - Part One", "id": 1}, {"type": "app", "name": "Airways: Two", "id": 2}]
    found = finder(profile, fake_steam(results)).find_one(game_id)
    assert found["result"] == "not-on-steam"
    assert not profile.state()["games"][game_id].get("linked_to")
    assert set(profile.state()["games"]) == {game_id}


def test_a_games_dlc_and_demo_beside_it_do_not_hide_it(profile):
    # What Steam's store search really answers for "Mini Airways" (2026-10-07).
    game_id = local_game(profile)
    results = [{"type": "app", "name": TITLE, "id": APPID}, {"type": "app", "name": "Mini Airways Demo", "id": 2554700},
               {"type": "app", "name": "Mini Airways - Tides of Barra", "id": 4676210}]
    found = finder(profile, fake_steam(results, {APPID: SCHEMA})).find_one(game_id)
    assert found["result"] == "linked" and found["appid"] == APPID


def test_two_games_with_lists_sharing_a_name_link_nothing(profile):
    game_id = local_game(profile)
    results = [{"type": "app", "name": TITLE, "id": APPID}, {"type": "app", "name": "Mini Airways - Remix", "id": 7}]
    found = finder(profile, fake_steam(results, {APPID: SCHEMA, 7: SCHEMA})).find_one(game_id)
    assert found["result"] == "not-on-steam"


def test_a_similar_but_different_title_is_not_taken(profile):
    game_id = local_game(profile, "Mini Airways")
    found = finder(profile, fake_steam([{"type": "app", "name": "Mini Airways Tycoon", "id": 5}])).find_one(game_id)
    assert found["result"] == "not-on-steam"


def test_a_steam_game_without_achievements_stays_local(profile):
    game_id = local_game(profile)
    found = finder(profile, fake_steam([{"type": "app", "name": TITLE, "id": APPID}])).find_one(game_id)
    assert found == {"result": "no-achievements", "appid": APPID}
    assert f"steam-{APPID}" not in profile.state()["games"]
    assert not profile.state()["games"][game_id].get("linked_to")


def test_each_game_is_looked_up_at_most_once_a_day(profile):
    game_id = local_game(profile)
    first = finder(profile, fake_steam([], status=503))
    assert first.find_one(game_id)["result"] == "unreachable"
    assert not first.due(game_id)
    assert not finder(profile, None, now=NOW + timedelta(hours=23)).due(game_id)
    assert finder(profile, None, now=NOW + timedelta(days=1)).due(game_id)
    finder(profile, fake_steam([])).find_one(game_id)            # not on Steam: a week
    assert not finder(profile, None, now=NOW + timedelta(days=6)).due(game_id)
    assert finder(profile, None, now=NOW + timedelta(days=7)).due(game_id)


def test_most_recently_played_games_are_looked_up_first(profile):
    old = local_game(profile, "Old Game", status=None)
    new = local_game(profile, "New Game", status=None)
    profile.record("game.metadata_updated", {"last_played": "2026-01-01T00:00:00Z"}, game_id=old, adapter="save-file")
    profile.record("game.metadata_updated", {"last_played": "2026-10-01T00:00:00Z"}, game_id=new, adapter="save-file")
    assert fd.AchievementFinder(profile).waiting() == [new, old]


def test_the_dashboard_button_finds_a_local_games_achievements(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    game_id = local_game(profile)
    fake = fake_steam([{"type": "app", "name": TITLE, "id": APPID}], {APPID: SCHEMA})
    monkeypatch.setattr(cat, "PacedFetcher", lambda **_kw: fake)
    monkeypatch.setattr(fd, "_default_index", lambda: None)
    client = TestClient(create_local_app(profile, token="t" * 32))
    r = client.post(f"/v1/local/games/{game_id}/fetch-achievements", headers={"X-OA-Token": "t" * 32})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "game_id": f"steam-{APPID}"}
    assert profile.state()["games"][game_id]["linked_to"] == f"steam-{APPID}"


# ---- games being played get their list first -------------------------------------------

class Recorder:
    def __init__(self, result=True):
        self.asked, self.result = [], result

    def prioritise(self, ref):
        self.asked.append(ref)
        return self.result


def test_a_running_steam_game_without_a_list_is_asked_for_first(profile):
    profile.register_game(f"steam-{APPID}", TITLE, platform="PC", external_ids={"steam": APPID})
    lists, find, asked = Recorder(), Recorder(), {}
    assert fd.ask_for_playing(profile, [f"steam-{APPID}"], lists, find, asked, now=1000) == [f"steam-{APPID}"]
    assert lists.asked == [APPID] and find.asked == []
    # Still running a minute later: not asked again until ASK_EVERY has passed.
    assert fd.ask_for_playing(profile, [f"steam-{APPID}"], lists, find, asked, now=1060) == []
    assert fd.ask_for_playing(profile, [f"steam-{APPID}"], lists, find, asked, now=1000 + fd.ASK_EVERY) != []


def test_a_running_game_with_a_list_is_left_alone(profile):
    game_id = local_game(profile)
    finder(profile, fake_steam([{"type": "app", "name": TITLE, "id": APPID}], {APPID: SCHEMA})).find_one(game_id)
    lists, find = Recorder(), Recorder()
    assert fd.ask_for_playing(profile, [game_id, f"steam-{APPID}"], lists, find, {}, now=0) == []
    assert lists.asked == find.asked == []


def test_a_running_local_game_goes_to_the_finder_first(profile):
    game_id = local_game(profile)
    lists, find = Recorder(), Recorder()
    assert fd.ask_for_playing(profile, [game_id], lists, find, {}, now=0) == [game_id]
    assert find.asked == [game_id] and lists.asked == []


def test_a_local_game_linked_into_a_steam_game_without_a_list_asks_for_the_steam_list(profile):
    game_id = local_game(profile)
    profile.register_game(f"steam-{APPID}", TITLE, platform="PC", external_ids={"steam": APPID})
    profile.link_games(game_id, f"steam-{APPID}")
    lists, find = Recorder(), Recorder()
    fd.ask_for_playing(profile, [game_id], lists, find, {}, now=0)
    assert lists.asked == [APPID] and find.asked == []


def test_the_finder_jumps_the_queue_but_not_more_than_hourly(profile):
    game_id = local_game(profile)
    f = finder(profile, fake_steam([], status=503))
    f.find_one(game_id)                                       # looked up just now: unreachable
    assert not f.prioritise(game_id)
    later = finder(profile, fake_steam([{"type": "app", "name": TITLE, "id": APPID}], {APPID: SCHEMA}),
                   now=NOW + timedelta(minutes=61))
    assert later.prioritise(game_id)
    later._thread.join(5)
    assert profile.state()["games"][game_id]["linked_to"] == f"steam-{APPID}"


def test_the_list_fetcher_retries_a_prioritised_game_it_already_tried(profile):
    profile.register_game(f"steam-{APPID}", TITLE, platform="PC", external_ids={"steam": APPID})
    answers = {"schema": None}

    def fetch(url):
        if "GetGameAchievements" in url:
            rows = answers["schema"]
            return 200, json.dumps({"response": {"achievements": rows} if rows else {}})
        return 403, "{}"

    lists = cat.PackFetcher(profile, fetch=fetch)
    lists.want(None)
    lists._thread.join(5)
    assert f"steam-{APPID}" not in profile.state()["packs"]      # Steam had nothing then
    answers["schema"] = SCHEMA
    lists.want(None)                                               # a normal look does not try it again
    assert not (lists._thread and lists._thread.is_alive())
    lists.prioritise(APPID)                                        # being played: asked again, first
    lists._thread.join(5)
    assert len(profile.state()["packs"][f"steam-{APPID}"]["achievements"]) == 2
