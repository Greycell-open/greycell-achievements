"""Rarity PlayStation, Xbox and GOG already send with their syncs, and the
dashboard's rarest unlocks."""
from openachievements import rarity
from openachievements.adapters import gog, psn, xbox


def commit(profile, events):
    profile.commit(events)
    return profile.state()


def test_playstation_trophies_keep_their_earned_rate(profile):
    title = {"npServiceName": "trophy2", "npCommunicationId": "NPWR20001_00", "trophyTitleName": "Astro Test",
             "trophyTitlePlatform": "PS5", "lastUpdatedDateTime": "2026-09-01T10:00:00Z"}
    trophies = [{"trophyId": 0, "trophyType": "platinum", "trophyName": "All Done", "trophyDetail": "x"},
                {"trophyId": 1, "trophyType": "bronze", "trophyName": "First", "trophyDetail": "y"}]
    earned = [{"trophyId": 0, "earned": False, "trophyEarnedRate": "2.4"},
              {"trophyId": 1, "earned": True, "earnedDateTime": "2026-09-01T09:59:00Z", "trophyEarnedRate": "88.1"}]
    events = psn.plan_title(profile, profile.state(), set(), {"account_id": "7"}, title, trophies, earned)
    commit(profile, events)
    assert rarity.percent("psn-npwr20001_00", "All Done") == 2.4
    assert rarity.percent("psn-npwr20001_00", "First") == 88.1


def test_xbox_keeps_rarity_and_360_titles_simply_have_none(profile):
    title = {"titleId": "1234567", "name": "Halo Test", "titleHistory": {"lastTimePlayed": "2026-09-30T20:00:00Z"}}
    achievements = [
        {"id": "1", "name": "First Steps", "progressState": "Achieved", "rarity": {"currentCategory": "Common",
         "currentPercentage": 61.5}, "progression": {"timeUnlocked": "2026-09-30T19:55:00Z"},
         "rewards": [{"type": "Gamerscore", "value": "10"}]},
        {"id": "2", "name": "Secret End", "progressState": "NotStarted", "isSecret": True,
         "rarity": {"currentCategory": "Rare", "currentPercentage": 0.7}}]
    commit(profile, xbox.plan_title(profile, profile.state(), set(), {"xuid": "1"}, title, achievements))
    assert rarity.percent("xbox-1234567", "Secret End") == 0.7
    old = {"titleId": "1161890128", "name": "Old Test"}
    commit(profile, xbox.plan_title(profile, profile.state(), set(), {"xuid": "1"}, old,
                                    [{"id": 1, "name": "Old One", "gamerscore": 20}], unlocked_360=[]))
    assert rarity.percent("xbox-1161890128", "Old One") is None


def test_gog_keeps_each_achievements_rarity(profile):
    uid = "48628349957132247"
    item = {"game": {"id": "1457519353", "title": "Oxenfree", "achievementSupport": True},
            "stats": {uid: {"lastSession": "2026-09-30T20:00:00+00:00"}}}
    achievements = [{"achievement": {"id": "a1", "name": "Ghost", "visible": True, "rarity": 10.8},
                     "stats": {uid: {"isUnlocked": True, "unlockDate": "2026-09-30T19:00:00+00:00"}}},
                    {"achievement": {"id": "a2", "name": "Odd", "visible": True, "rarity": "not a number"},
                     "stats": {uid: {"isUnlocked": False}}}]
    commit(profile, gog.plan_game(profile, profile.state(), set(), {"user_id": uid}, item, achievements))
    assert rarity.percent("gog-1457519353", "Ghost") == 10.8
    assert rarity.percent("gog-1457519353", "Odd") is None                      # bad values are left out


def test_the_rarest_unlocks_come_first_from_every_platform(profile):
    title = {"npServiceName": "trophy2", "npCommunicationId": "NPWR20001_00", "trophyTitleName": "Astro Test",
             "trophyTitlePlatform": "PS5", "lastUpdatedDateTime": "2026-09-01T10:00:00Z"}
    trophies = [{"trophyId": 0, "trophyType": "gold", "trophyName": "Hard One"},
                {"trophyId": 1, "trophyType": "bronze", "trophyName": "Easy One"},
                {"trophyId": 2, "trophyType": "bronze", "trophyName": "Not Mine"}]
    earned = [{"trophyId": 0, "earned": True, "earnedDateTime": "2026-09-01T09:00:00Z", "trophyEarnedRate": "1.2"},
              {"trophyId": 1, "earned": True, "earnedDateTime": "2026-09-01T09:30:00Z", "trophyEarnedRate": "75.0"},
              {"trophyId": 2, "earned": False, "trophyEarnedRate": "0.1"}]
    commit(profile, psn.plan_title(profile, profile.state(), set(), {"account_id": "7"}, title, trophies, earned))
    xb = {"titleId": "1234567", "name": "Halo Test"}
    commit(profile, xbox.plan_title(profile, profile.state(), set(), {"xuid": "1"}, xb, [
        {"id": "1", "name": "Legend", "progressState": "Achieved", "rarity": {"currentPercentage": 0.4},
         "progression": {"timeUnlocked": "2026-09-30T19:55:00Z"}}]))
    top = rarity.rarest(profile)
    assert [(r["name"], r["percent"]) for r in top] == [("Legend", 0.4), ("Hard One", 1.2), ("Easy One", 75.0)]
    assert top[0]["title"] == "Halo Test"


def test_the_page_serves_the_rarest_shelf(profile):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    client = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    assert client.get("/v1/local/rarest").json() == {"rarest": []}
