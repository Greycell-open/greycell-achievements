"""Rare achievements: Steam's unlock percentages, cached on this computer, and the rare popup and sound."""
import json
from datetime import datetime, timezone

import pytest

from openachievements import notify, rarity, toast

SCHEMA = {"response": {"achievements": [
    {"internal_name": "A", "localized_name": "Welcome To St. Amelia", "player_percent_unlocked": "87.9"},
    {"internal_name": "B", "localized_name": "Fleeting  Visit", "player_percent_unlocked": "0.6"},
    {"internal_name": "C", "localized_name": "Eraser", "player_percent_unlocked": "4.2"}]}}


class Steam:
    def __init__(self, answer=(200, json.dumps(SCHEMA)), fail=False):
        self.answer, self.fail, self.calls = answer, fail, []

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("offline")
        return self.answer


def test_percentages_are_kept_and_looked_up_by_name(tmp_path):
    steam = Steam()
    assert rarity.fetch("1636440", steam, tmp_path)
    assert rarity.percent("steam-1636440", "Welcome to St. Amelia", tmp_path) == 87.9     # case and spacing ignored
    assert rarity.percent("steam-1636440", "Fleeting Visit", tmp_path) == 0.6
    assert rarity.percent("steam-1636440", "Not An Achievement", tmp_path) is None
    assert rarity.percent("ra-1", "Eraser", tmp_path) is None                            # Steam games only


def test_a_failed_request_is_not_cached_but_a_real_answer_is(tmp_path):
    assert rarity.fetch("9", Steam(fail=True), tmp_path) is None
    assert not rarity.fresh("9", tmp_path)                       # asked again next time
    assert rarity.fetch("9", Steam(answer=(200, json.dumps({"response": {}}))), tmp_path) is None
    assert rarity.fresh("9", tmp_path)                           # Steam said: no achievements. Remembered a day.
    assert not rarity.fresh("9", tmp_path, now=10 ** 12)


def test_an_unlock_in_a_new_game_fetches_its_rarity_once(tmp_path):
    steam = Steam()
    assert rarity.for_unlock("steam-1636440", "Eraser", steam, tmp_path) == 4.2
    assert rarity.for_unlock("steam-1636440", "Fleeting Visit", steam, tmp_path) == 0.6
    assert len(steam.calls) == 1


def test_the_crawler_takes_recent_games_first_and_paces_itself(profile, tmp_path):
    from openachievements.packs import validate_definitions, split_for_events
    from openachievements import events as ev
    for gid, when in (("steam-10", "2026-01-01T00:00:00.000Z"), ("steam-20", "2026-09-01T00:00:00.000Z")):
        profile.register_game(gid, gid, platform="PC (Steam)")
        profile.record("game.metadata_updated", {"last_played": when}, game_id=gid, adapter="steam")
        pack = validate_definitions({"id": gid, "name": gid, "game_ids": [gid], "version": "1"},
                                    [{"id": "a", "name": "A", "description": ""}])
        profile.commit([ev.make_event("pack.installed", profile_id=profile.profile_id, device_id=profile.device_id,
                                      payload=p, adapter="steam") for p in split_for_events(pack)])
    t = [0.0]
    steam = Steam()
    crawler = rarity.RarityCrawler(profile, http=steam, folder=tmp_path, clock=lambda: t[0])
    assert crawler.poll() == "20"                                # most recently played first
    assert crawler.poll() is None                                # not again within PACE
    t[0] += rarity.PACE
    assert crawler.poll() == "10"
    t[0] += rarity.PACE
    assert crawler.poll() is None and len(steam.calls) == 2      # both fresh now: nothing to ask


def unlock(profile, game_id="steam-1636440", name="Fleeting Visit"):
    from openachievements import events as ev
    return {"event_type": "achievement.unlocked", "achievement_id": f"{game_id}:x", "game_id": game_id,
            "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "source": {"adapter": "save-file"}}


def test_a_rare_unlock_says_so_and_plays_the_rare_sound(profile, monkeypatch):
    prefs = {**notify.DEFAULTS}
    rare = notify.card(profile, unlock(profile), prefs, rarity_of=lambda gid, name: 0.6)
    assert rare["rare"] == 0.6
    common = notify.card(profile, unlock(profile), prefs, rarity_of=lambda gid, name: 4.2)
    assert "rare" not in common                                  # 4.2% is not under the default 1%
    assert notify.card(profile, unlock(profile), {**prefs, "rare_below": 5.0}, rarity_of=lambda g, n: 4.2)["rare"] == 4.2
    played = []
    import sys
    import types
    choice = {"unlock": "echo", "rare": "rare-marimba", "platinum": "burst"}

    def overlay_show(cards, sound, play):                      # stands in for the native popup: plays each card
        for c in cards:
            play("platinum" if c.get("platinum") else "rare" if "rare" in c else "unlock")
    import openachievements
    fake = types.SimpleNamespace(show=overlay_show)            # never a real window on the screen
    monkeypatch.setitem(sys.modules, "openachievements.overlay", fake)
    monkeypatch.setattr(openachievements, "overlay", fake, raising=False)
    monkeypatch.setattr(toast.sys, "platform", "win32")
    monkeypatch.setattr(toast, "_play_chime",
                        lambda platinum=False, name=None, file=None, kind=None: played.append((platinum, name, kind)))
    toast.show([rare, common, {"name": "Platinum", "game": "G", "points": 0, "platinum": True}], True, choice)
    assert played == [(False, "rare-marimba", "rare"), (False, "echo", None), (True, "burst", None)]


def test_rare_settings_are_checked(profile):
    assert notify.change(profile, rare_below="5")["rare_below"] == 5.0
    assert notify.change(profile, rare_sound="rare-retro")["rare_sound"] == "rare-retro"
    with pytest.raises(ValueError):
        notify.change(profile, rare_below=3)
    with pytest.raises(ValueError):
        notify.change(profile, rare_sound="echo")                 # an unlock sound is not a rare sound


def test_the_rare_sound_can_be_previewed(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    played = []
    monkeypatch.setattr(toast, "_play_chime", lambda platinum=False, name=None, file=None, kind=None: played.append((name, kind)))
    assert local.post("/v1/local/notify/preview", headers=h, json={"kind": "rare", "name": "rare-echo"}).status_code == 200
    assert played == [("rare-echo", "rare")]
    s = local.get("/v1/local/notify").json()
    assert [c["id"] for c in s["choices"]["rare"]][:3] == ["rare-echo", "rare-marimba", "rare-retro"]
    assert s["rare_below"] == 1.0 and s["rare_choices"] == [1.0, 5.0, 10.0]


def test_retroachievements_rarity_comes_with_the_sync(tmp_path, monkeypatch):
    from openachievements.adapters import retroachievements as ra
    monkeypatch.setattr(rarity, "cache_dir", lambda: tmp_path)
    ra._keep_rarity("ra-1", {"NumDistinctPlayers": 2000, "Achievements": {
        "9": {"Title": "That Was Easy", "NumAwarded": 1500}, "10": {"Title": "Speed Run", "NumAwarded": 12}}})
    assert rarity.percent("ra-1", "Speed Run") == 0.6 and rarity.percent("ra-1", "That Was Easy") == 75.0
    ra._keep_rarity("ra-2", {"Achievements": {"1": {"Title": "X", "NumAwarded": 1}}})      # no player count: nothing
    assert rarity.percent("ra-2", "X") is None


def test_rare_settings_travel_from_the_page_to_the_popup(profile, monkeypatch):
    """Round 1 review: the page's POST, the preview and the popup's payload all carry the rare choice."""
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    s = local.post("/v1/local/notify", headers=h, json={"rare_sound": "rare-retro"}).json()
    s = local.post("/v1/local/notify", headers=h, json={"rare_below": "5"}).json()
    assert (s["rare_sound"], s["rare_below"]) == ("rare-retro", 5.0)
    assert local.post("/v1/local/notify", headers=h, json={"rare_below": 3}).status_code == 400
    sent = []

    class Proc:
        class stdin:
            write = staticmethod(lambda b: sent.append(json.loads(b)))
            close = staticmethod(lambda: None)
    monkeypatch.setattr(notify.subprocess, "Popen", lambda cmd, **kw: Proc())
    prefs = notify.settings(profile.config.load())
    notify._launch([{"name": "x", "game": "g", "points": 0, "rare": 0.5}],
                   {"on": True, "unlock": prefs["unlock_sound"], "platinum": prefs["platinum_sound"],
                    "rare": prefs["rare_sound"], "rare_file": "C:/x.wav"})
    assert sent[0]["sounds"]["rare"] == "rare-retro" and sent[0]["sounds"]["rare_file"] == "C:/x.wav"


def test_steam_percentages_never_enter_history(profile):
    """Rarity changes over time: it lives in the rarity cache, never in a pack event."""
    from openachievements.catalog import steam as cat
    entry = {"pack": {"id": "steam-10", "name": "G (Steam)", "version": "1", "game_ids": ["steam-10"],
                      "games": [{"id": "steam-10", "title": "G", "platform": "PC (Steam)", "external_ids": {"steam": 10}}],
                      "source": "steam-catalog"},
             "achievements": [{"id": "a", "name": "A", "description": "", "points": 0, "rarity": "0.7% of players"}]}
    cat.install_entry(profile, entry)
    text = "".join(p.read_text(encoding="utf-8") for p in profile.folder.rglob("*.jsonl"))
    assert "steam-10" in text and "% of players" not in text


def test_a_refusal_is_not_cached_and_the_crawler_moves_on(profile, tmp_path):
    from openachievements.packs import validate_definitions, split_for_events
    from openachievements import events as ev
    assert rarity.fetch("5", Steam(answer=(403, "")), tmp_path) is None
    assert not rarity.fresh("5", tmp_path)                                  # a refusal is asked again later
    for gid, when in (("steam-5", "2026-09-01T00:00:00.000Z"), ("steam-6", "2026-01-01T00:00:00.000Z")):
        profile.register_game(gid, gid, platform="PC (Steam)")
        profile.record("game.metadata_updated", {"last_played": when}, game_id=gid, adapter="steam")
        pack = validate_definitions({"id": gid, "name": gid, "game_ids": [gid], "version": "1"},
                                    [{"id": "a", "name": "A", "description": ""}])
        profile.commit([ev.make_event("pack.installed", profile_id=profile.profile_id, device_id=profile.device_id,
                                      payload=p, adapter="steam") for p in split_for_events(pack)])
    t = [0.0]

    def steam(url):
        return (403, "") if "appid=5" in url else (200, json.dumps(SCHEMA))
    crawler = rarity.RarityCrawler(profile, http=steam, folder=tmp_path, clock=lambda: t[0])
    assert crawler.poll() == "5"                       # refused
    t[0] += rarity.PACE
    assert crawler.poll() == "6"                       # not stuck on the refused game
    assert rarity.fresh("6", tmp_path)


def test_all_pictures_of_a_popup_share_one_short_wait():
    import time as clock
    cards = [{"name": str(i)} for i in range(10)]

    def slow(url):
        clock.sleep(1.0)
        return None
    started = clock.monotonic()
    notify.add_pictures(cards, [f"https://x/{i}.jpg" for i in range(10)], wait=0.3, cached=slow)
    assert clock.monotonic() - started < 0.8 and not any("icon" in c for c in cards)
    fast = [{"name": "a"}, {"name": "b"}]
    notify.add_pictures(fast, ["https://x/a.jpg", None], cached=lambda url: "C:/a.img")
    assert fast == [{"name": "a", "icon": "C:/a.img"}, {"name": "b"}]
