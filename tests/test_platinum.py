"""Platinum (every achievement in a game) and the test game that shows it off."""
from datetime import datetime, timedelta, timezone

import pytest

from openachievements import notify, testgame
from openachievements.adapters import savefile


@pytest.fixture
def docs(tmp_path, monkeypatch):
    """The test game's Documents folder, in scratch space, never the real one."""
    folder = tmp_path / "Documents"
    monkeypatch.setattr(savefile, "root_folder", lambda root: folder)
    return folder


def unlocks(profile):
    lib = {g["game_id"]: g for g in profile.library()["games"]}
    return lib[testgame.GAME_ID]["unlocked"]


def test_each_play_finishes_a_level_and_the_save_watcher_unlocks_it(profile, docs):
    watcher = savefile.SaveWatcher(profile)
    for level in range(1, 6):
        assert testgame.play(profile) == level
        events = watcher.poll()
        assert [e["achievement_id"] for e in events] == [f"{testgame.PACK_ID}:{testgame.ACHIEVEMENTS[level - 1][0]}"]
        assert events[0]["payload"]["provenance"] == "save-derived"           # no unlock by hand, even here
    assert testgame.play(profile) == 5                                           # it ends at five
    assert unlocks(profile) == 5


def test_reset_takes_unlocks_back_and_the_game_can_be_played_again(profile, docs):
    watcher = savefile.SaveWatcher(profile)
    for _ in range(3):
        testgame.play(profile)
        watcher.poll()
    assert testgame.reset(profile) == 3
    assert unlocks(profile) == 0 and not (docs / testgame.TITLE / "progress.json").exists()
    testgame.play(profile)
    assert [e["achievement_id"] for e in watcher.poll()] == [f"{testgame.PACK_ID}:first-steps"]
    testgame.remove(profile)
    assert profile.state()["packs"][testgame.PACK_ID].get("removed")


def fake_launch(shown):
    return lambda cards, sound: shown.append(cards)


def test_the_unlock_that_completes_a_game_brings_a_platinum_card(profile, docs):
    watcher = savefile.SaveWatcher(profile)
    shown = []
    popup = notify.Notifier(profile, launch=fake_launch(shown))
    for level in range(1, 6):
        testgame.play(profile)
        for e in watcher.poll():
            popup.add(e)
        popup.flush()
    names = [[c["name"] for c in cards] for cards in shown]
    assert names[:4] == [["First Steps"], ["Getting Warm"], ["Halfway There"], ["Almost There"]]
    assert names[4] == ["The End", "Platinum"]
    assert shown[4][1] == {"name": "Platinum", "game": testgame.TITLE, "points": 0, "platinum": True}


def test_old_progress_completing_a_game_is_no_moment(profile, tmp_path):
    from conftest import write_pack
    profile.install_pack(write_pack(tmp_path / "pack"))
    events = [profile.record_unlock(f"test-pack:{a}", adapter="test") for a in ("first-step", "collector", "secret")]
    now = datetime.now(timezone.utc) + timedelta(hours=3)                     # the unlocks are hours old by now
    assert notify.platinum_cards(profile, events, notify.DEFAULTS, now) == []
    fresh = notify.platinum_cards(profile, events, notify.DEFAULTS, datetime.now(timezone.utc))
    assert [c["name"] for c in fresh] == ["Platinum"]
    assert notify.platinum_cards(profile, events, {**notify.DEFAULTS, "enabled": False},
                                 datetime.now(timezone.utc)) == []          # popups off means off


def test_the_sounds_dialog_reads_changes_and_previews_choices(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements import toast
    from openachievements.local_app import create_local_app
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    s = local.get("/v1/local/notify").json()
    assert (s["unlock_sound"], s["platinum_sound"]) == ("echo", "burst")
    assert [c["id"] for c in s["choices"]["platinum"]] == ["burst", "flurry", "parade", "rocket", "ki-completion", "roar", "warp-step",
                                                             "beam-roar", "custom"]
    assert local.post("/v1/local/notify", json={"platinum_sound": "flurry"}).status_code == 403      # token needed
    assert local.post("/v1/local/notify", headers=h, json={"platinum_sound": "flurry", "steam": True}).json()["platinum_sound"] == "flurry"
    assert local.post("/v1/local/notify", headers=h, json={"unlock_sound": "airhorn"}).status_code == 400
    played = []
    monkeypatch.setattr(toast, "_play_chime", lambda platinum=False, name=None: played.append((platinum, name)))
    assert local.post("/v1/local/notify/preview", headers=h, json={"kind": "platinum", "name": "rocket"}).status_code == 200
    assert played == [(True, "rocket")]
    assert local.post("/v1/local/notify/preview", headers=h, json={"kind": "unlock", "name": "nope"}).status_code == 400


def test_a_players_own_wav_can_be_the_sound_and_bad_files_are_refused(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements import notify, toast
    from openachievements.local_app import create_local_app
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    assert local.post("/v1/local/notify/custom/platinum", headers=h, content=b"ID3 not a wav").status_code == 400
    wav = toast.chime_wav("unlock", "pop-duo")
    r = local.post("/v1/local/notify/custom/platinum", headers=h, content=wav).json()
    assert r["platinum_sound"] == "custom" and notify.custom_file(profile, "platinum").read_bytes() == wav
    assert local.get("/v1/local/notify").json()["custom"] == {"unlock": False, "platinum": True, "rare": False}
    shown = []
    n = notify.Notifier(profile, launch=lambda cards, sound: shown.append(sound))
    sent = n._launch                                                  # what the popup process would be told
    choice = {"on": True, "unlock": "pop", "platinum": "custom",
              "platinum_file": str(notify.custom_file(profile, "platinum"))}
    played = []
    monkeypatch.setattr(toast, "_play_chime", lambda platinum=False, name=None, file=None: played.append((name, file)))
    assert local.post("/v1/local/notify/preview", headers=h, json={"kind": "platinum", "name": "custom"}).status_code == 200
    assert played == [("custom", choice["platinum_file"])]
