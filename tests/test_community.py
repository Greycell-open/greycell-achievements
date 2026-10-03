"""Community stats: what leaves the app (anonymous counts only, fresh things
only, nothing when switched off) and what the service takes and shows (names
it looked up itself, never text a client sent)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from openachievements import community, community_server as cs, privacy

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def unlock(game_id, ach, minutes_ago, adapter="steam-local"):
    return {"event_type": "achievement.unlocked", "game_id": game_id, "achievement_id": f"{game_id}:{ach}",
            "occurred_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "source": {"adapter": adapter}}


def queued(profile):
    return json.loads((profile.config.dir / "community-queue.json").read_text())["items"]


@pytest.fixture
def steam_game(profile):
    """A Steam game's pack as Steam imports record it: with Steam's own names."""
    from openachievements import events as ev, packs
    profile.register_game("steam-10", "Game", platform="PC (Steam)", external_ids={"steam": 10})
    description = packs.validate_definitions(
        {"id": "steam-10", "name": "Game", "game_ids": ["steam-10"]},
        [{"id": "a", "name": "First"}, {"id": "b", "name": "Last"}])
    for item, api in zip(description["achievements"], ("ACH_FIRST", "ACH_LAST")):
        item["external_id"] = api
    profile.commit([ev.make_event("pack.installed", profile_id=profile.profile_id, device_id=profile.device_id,
                                  payload=payload) for payload in packs.split_for_events(description)])
    return profile


def test_a_fresh_unlock_is_counted_with_only_public_steam_facts(steam_game):
    assert community.from_round(steam_game, [unlock("steam-10", "a", 3)], now=NOW) == 1
    [item] = queued(steam_game)
    assert item == {"kind": "unlock", "platform": "steam", "at": "2026-10-03T11:57:00Z", "appid": 10, "api": "ACH_FIRST"}
    text = json.dumps(item)
    assert steam_game.profile_id not in text and str(steam_game.config.dir) not in text   # nobody in it


def test_old_unlocks_being_imported_are_not_counted(steam_game):
    assert community.from_round(steam_game, [unlock("steam-10", "a", 24 * 60)], now=NOW) == 0


def test_the_last_unlock_of_a_game_also_counts_a_platinum(steam_game):
    for key in ("steam-10:a", "steam-10:b"):
        steam_game.record_unlock(key, adapter="steam-local")
    community.from_round(steam_game, [unlock("steam-10", "b", 1)], now=NOW)
    assert [i["kind"] for i in queued(steam_game)] == ["unlock", "platinum"]


def test_an_unlock_read_from_a_save_is_never_named_even_in_a_steam_game(steam_game):
    community.from_round(steam_game, [unlock("steam-10", "a", 1, adapter="save-file")], now=NOW)
    [item] = queued(steam_game)
    assert item["platform"] == "pc" and "appid" not in item and "api" not in item


def test_a_save_that_unlocks_many_at_once_is_an_import_not_a_moment(steam_game, monkeypatch):
    from openachievements import notify
    events = [unlock("steam-10", a, 1, adapter="save-file") for a in ("a", "b", "a", "b")]
    assert notify.save_dumps(events) == {"steam-10"}
    assert community.from_round(steam_game, events, now=NOW) == 0
    shown = []
    n = notify.Notifier(steam_game, launch=lambda cards, sound: shown.append(cards))
    for e in events:
        n.add({**e, "occurred_at": datetime.now(timezone.utc).isoformat()})
    assert n.flush() == 0 and shown == []                                     # and no popup either
    few = [unlock("steam-10", "a", 1, adapter="save-file")] * notify.SAVE_DUMP
    assert notify.save_dumps(few) == set()                                     # a chapter's few still count


def test_local_games_are_counted_without_any_name(profile):
    community.note(profile.config, "unlock", "pc")
    assert queued(profile)[0] == {"kind": "unlock", "platform": "pc", "at": queued(profile)[0]["at"]}


def test_switched_off_nothing_is_queued_and_what_waited_is_dropped(steam_game):
    community.from_round(steam_game, [unlock("steam-10", "a", 1)], now=NOW)
    privacy.change(steam_game.config, stats=False)
    assert community.from_round(steam_game, [unlock("steam-10", "b", 1)], now=NOW) == 0
    sent = []
    assert community.send(steam_game.config, post=lambda u, b: sent.append(b) or 200, force=True) == "off"
    assert sent == [] and queued(steam_game) == []


def test_it_sends_at_most_once_an_hour_and_keeps_items_when_offline(steam_game):
    community.note(steam_game.config, "added", "steam", appid=10)
    def offline(url, body):
        raise OSError("offline")
    start = 1_800_000_000.0
    assert community.send(steam_game.config, now=start, post=offline) == "failed"
    assert len(queued(steam_game)) == 1
    sent = []
    assert community.send(steam_game.config, now=start + 600, post=lambda u, b: sent.append(b) or 200) == "later"
    assert community.send(steam_game.config, now=start + 3601, post=lambda u, b: sent.append(b) or 200) == "sent 1"
    assert json.loads(sent[0])["items"][0]["kind"] == "added" and queued(steam_game) == []


def test_a_new_install_is_counted_once(profile):
    assert community.first_run(profile.config) is True
    assert community.first_run(profile.config) is False
    assert [i["kind"] for i in queued(profile)] == ["install"]


def test_the_page_counts_shelves_the_player_chose(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    profile.register_game("steam-20", "G", platform="PC (Steam)")
    page = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    page.post("/v1/local/status", json={"game_id": "steam-20", "status": "backlog"}, headers=h)
    page.post("/v1/local/status", json={"game_id": "steam-20", "status": "playing"}, headers=h)
    assert [(i["kind"], i.get("appid")) for i in queued(profile)] == [("backlog", 20)]
    assert page.get("/v1/local/privacy").json()["notice"] is True
    page.post("/v1/local/privacy/notice", headers=h)
    assert page.get("/v1/local/privacy").json()["notice"] is False                 # said once


# ---- the service ----------------------------------------------------------------------------

def fake_steam(url):
    if "GetGameAchievements" in url:
        return {"response": {"achievements": [
            {"internal_name": "ACH_FIRST", "localized_name": "First Steps", "player_percent_unlocked": "80.5"},
            {"internal_name": "ACH_RARE", "localized_name": "Very Rare", "player_percent_unlocked": "0.4"}]}}
    return {"10": {"success": True, "data": {"name": "Real Game"}}}


@pytest.fixture
def service(tmp_path):
    from fastapi.testclient import TestClient
    cs._senders.clear()
    cs._snapshot.update(at=0.0, data=None)
    app = cs.create_app(str(tmp_path / "c.sqlite"), start_resolver=False)
    return TestClient(app), app.state.db


def post(client, items):
    return client.post("/api/achievements/events", json={"items": items})


def now_minute(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:00Z")


def test_the_service_keeps_only_well_formed_recent_items(service):
    client, db = service
    good = {"kind": "unlock", "platform": "steam", "at": now_minute(1), "appid": 10, "api": "ACH_FIRST"}
    bad = [{**good, "kind": "nickname"}, {**good, "platform": "<script>"}, {**good, "api": "<b>hi</b>"},
           {**good, "at": "2020-01-01T00:00:00Z"}, {**good, "appid": "10"}, {"kind": "unlock"}, "text"]
    assert post(client, [good] + bad).json() == {"stored": 1}
    assert post(client, [good] * 501).status_code == 400
    assert client.post("/api/achievements/events", content=b"x" * 70000).status_code == 413


def test_one_sender_is_limited_per_hour(service, monkeypatch):
    client, _ = service
    monkeypatch.setattr(cs, "PER_SENDER_HOUR", 3)
    item = {"kind": "added", "platform": "pc", "at": now_minute()}
    assert post(client, [item] * 3).status_code == 200
    assert post(client, [item]).status_code == 429


def test_the_snapshot_shows_only_names_steam_gave_this_service(service):
    client, db = service
    post(client, [{"kind": "unlock", "platform": "steam", "at": now_minute(2), "appid": 10, "api": "ACH_FIRST"},
                  {"kind": "unlock", "platform": "steam", "at": now_minute(1), "appid": 10, "api": "ACH_RARE"},
                  {"kind": "unlock", "platform": "steam", "at": now_minute(1), "appid": 10, "api": "FAKE_TEXT"},
                  {"kind": "unlock", "platform": "playstation", "at": now_minute(1)},
                  {"kind": "platinum", "platform": "steam", "at": now_minute(1), "appid": 10, "minutes": 600},
                  {"kind": "install", "platform": "linux", "at": now_minute(1)}])
    assert cs.pending(db, datetime.now().timestamp()) == [10]
    assert cs.resolve(db, 10, get=fake_steam)
    snap = client.get("/api/achievements/live").json()
    assert [u["name"] for u in snap["latest_unlocks"]] == ["Very Rare", "First Steps"]     # FAKE_TEXT never shown
    assert snap["latest_unlocks"][0]["game"] == "Real Game"
    assert snap["rarest_48h"][0]["name"] == "Very Rare" and snap["rarest_48h"][0]["pct"] == 0.4
    assert snap["latest_platinums"][0] == {"game": "Real Game", "appid": 10, "at": snap["latest_platinums"][0]["at"],
                                           "hours": 10.0}
    assert snap["totals"]["unlock"] == 4 and snap["totals"]["install"] == 1
    assert snap["platforms_24h"] == {"steam": 3, "playstation": 1}


def test_the_snapshot_is_rebuilt_once_a_day_not_on_every_visit(service, monkeypatch):
    client, db = service
    post(client, [{"kind": "added", "platform": "pc", "at": now_minute()}])
    first = client.get("/api/achievements/live").json()
    post(client, [{"kind": "added", "platform": "pc", "at": now_minute()}])
    assert client.get("/api/achievements/live").json() == first                 # the same until tomorrow
    monkeypatch.setattr(cs, "SNAPSHOT_EVERY", 0.0)
    assert client.get("/api/achievements/live").json()["totals"]["added"] == 2
