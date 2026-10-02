"""Save-file achievements: packs that read a game's own saves."""
import json

import pytest

from openachievements.adapters import savefile
from openachievements.cli import main
from openachievements.packs import PackError
from conftest import write_pack

SAVES = [{"id": "main", "root": "LOCALLOW", "path": "Studio/Game", "pattern": "slot*.json", "format": "json"}]
ACHIEVEMENTS = [
    {"id": "chapter-3", "name": "Halfway", "rules": [
        {"signal": "save", "save": "main", "field": "progress.chapter", "op": ">=", "value": 3}]},
    {"id": "true-ending", "name": "The Truth", "rules": [
        {"signal": "save", "save": "main", "field": "progress.chapter", "op": ">=", "value": 7},
        {"signal": "save", "save": "main", "field": "flags.ending", "op": "==", "value": "TRUE"}]},
    {"id": "boss", "name": "Down", "rules": [{"signal": "save", "save": "main", "contains": "BOSS_ALMA_DEFEATED"}]},
]


@pytest.fixture
def game(profile, tmp_path, monkeypatch):
    root = tmp_path / "LocalLow"
    monkeypatch.setattr(savefile, "root_folder", lambda name: root if name == "LOCALLOW" else None)
    profile.install_pack(write_pack(tmp_path / "pack", achievements=ACHIEVEMENTS, saves=SAVES))
    folder = root / "Studio" / "Game"
    folder.mkdir(parents=True)
    savefile.allow(profile, "test-pack", "main")
    return folder


def save(folder, name, data):
    (folder / name).write_text(json.dumps(data), encoding="utf-8")


def unlocked(profile):
    return {e["achievement_id"] for e in profile.log.read().events if e["event_type"] == "achievement.unlocked"}


def test_progress_in_a_save_unlocks_with_evidence_not_contents(profile, game):
    watcher = savefile.SaveWatcher(profile)
    save(game, "slot1.json", {"progress": {"chapter": 2}})
    assert watcher.poll() == []
    save(game, "slot1.json", {"progress": {"chapter": 3}, "log": "BOSS_ALMA_DEFEATED"})
    written = watcher.poll()
    assert {e["achievement_id"] for e in written} == {"test-pack:chapter-3", "test-pack:boss"}
    event = written[0]
    assert event["payload"]["provenance"] == "save-derived" and event["source"]["adapter"] == "save-file"
    assert event["payload"]["evidence"].startswith("main/slot1.json sha256:")
    assert "chapter" not in json.dumps(event["payload"])          # the save's contents never leave it
    assert watcher.poll() == []                                    # unchanged file: not even re-read


def test_all_rules_must_hold_in_one_slot(profile, game):
    save(game, "slot1.json", {"progress": {"chapter": 7}, "flags": {"ending": "bad"}})
    save(game, "slot2.json", {"progress": {"chapter": 1}, "flags": {"ending": "true"}})
    savefile.SaveWatcher(profile).poll()
    assert "test-pack:true-ending" not in unlocked(profile)       # split across slots is not an ending
    save(game, "slot2.json", {"progress": {"chapter": 7}, "flags": {"ending": "True"}})
    savefile.SaveWatcher(profile).poll()
    assert "test-pack:true-ending" in unlocked(profile)           # text compares ignore case


def test_an_unreadable_save_is_reported_and_skipped(profile, game):
    (game / "slot1.json").write_text("{not json", encoding="utf-8")
    save(game, "slot2.json", {"progress": {"chapter": 3}})
    watcher = savefile.SaveWatcher(profile)
    assert [e["achievement_id"] for e in watcher.poll()] == ["test-pack:chapter-3"]
    assert any("not JSON" in m for m in watcher.problems.values())


def test_another_folder_on_this_machine(profile, game, tmp_path):
    beside_game = tmp_path / "Games" / "Game" / "saves"
    beside_game.mkdir(parents=True)
    save(beside_game, "slot9.json", {"progress": {"chapter": 4}})
    savefile.locate(profile, "test-pack", "main", beside_game)
    assert "test-pack:chapter-3" in {e["achievement_id"] for e in savefile.SaveWatcher(profile).poll()}
    savefile.locate(profile, "test-pack", "main", None)
    assert savefile.save_folder(profile, "test-pack", SAVES[0]) == game
    assert savefile.granted_folder(profile, "test-pack", SAVES[0]) is None   # back to default: ask again


def test_ini_saves_and_the_check_report(profile, tmp_path, monkeypatch, capsys):
    root = tmp_path / "Docs"
    monkeypatch.setattr(savefile, "root_folder", lambda name: root)
    saves = [{"id": "cfg", "root": "DOCUMENTS", "path": "My Games/Old", "pattern": "*.ini", "format": "ini"}]
    achs = [{"id": "hard", "name": "Hard", "rules": [{"signal": "save", "save": "cfg", "field": "Game.Difficulty",
                                                      "op": "==", "value": "hard"}]},
            {"id": "rich", "name": "Rich", "rules": [{"signal": "save", "save": "cfg", "field": "gold",
                                                      "op": ">", "value": 1000}]}]
    profile.install_pack(write_pack(tmp_path / "pack", pack_id="old-game", game_id="old-game",
                                    achievements=achs, saves=saves))
    folder = root / "My Games" / "Old"
    folder.mkdir(parents=True)
    savefile.allow(profile, "old-game", "cfg")
    (folder / "save.ini").write_text("gold=1500\n[Game]\nDifficulty=Hard\n", encoding="utf-8")
    report = savefile.check(profile)
    assert report[0]["exists"] and set(report[0]["files"][0]["satisfies"]) == {"old-game:hard", "old-game:rich"}
    main(["--home", str(profile.folder.parent.parent), "--config", str(profile.config.dir), "save", "check"])
    assert "save.ini: " in capsys.readouterr().out


@pytest.mark.parametrize("saves, message", [
    ([{"id": "m", "root": "C:", "path": "x", "format": "json"}], "root must be"),
    ([{"id": "m", "root": "HOME", "path": ".ssh", "pattern": "id_*", "format": "text"}], "root must be"),
    ([{"id": "m", "root": "APPDATA", "path": "../../etc", "format": "json"}], "relative path"),
    ([{"id": "m", "root": "APPDATA", "path": "/etc", "format": "json"}], "relative path"),
    ([{"id": "m", "root": "LOCALAPPDATA", "path": "Game/.secrets", "format": "json"}], "hidden folders"),
    ([{"id": "m", "root": "APPDATA", "path": "a", "pattern": "../*", "format": "json"}], "file name pattern"),
    ([{"id": "m", "root": "APPDATA", "path": "a", "format": "exe"}], "format must be"),
])
def test_a_pack_cannot_point_saves_anywhere(profile, tmp_path, saves, message):
    with pytest.raises(PackError, match=message):
        profile.install_pack(write_pack(tmp_path / "pack", achievements=ACHIEVEMENTS[:1], saves=saves))


def test_save_rules_must_name_a_declared_save(profile, tmp_path):
    with pytest.raises(PackError, match="does not declare"):
        profile.install_pack(write_pack(tmp_path / "p1", saves=SAVES, achievements=[{"id": "a", "name": "A", "rules": [
            {"signal": "save", "save": "other", "field": "x", "op": "exists"}]}]))
    with pytest.raises(PackError, match="need a 'saves' list"):
        profile.install_pack(write_pack(tmp_path / "p2", achievements=ACHIEVEMENTS[:1]))
    with pytest.raises(PackError, match="op must be"):
        profile.install_pack(write_pack(tmp_path / "p3", saves=SAVES, achievements=[{"id": "a", "name": "A", "rules": [
            {"signal": "save", "save": "main", "field": "x", "op": "=~", "value": ".*"}]}]))


def test_a_save_unlock_has_one_stable_record_key_for_every_device(profile, game):
    save(game, "slot1.json", {"progress": {"chapter": 3}})
    first = savefile.SaveWatcher(profile).poll()[0]
    records = profile.state()["unlocks"]["test-pack:chapter-3"]["records"]
    assert list(records) == [f"save-file:{first['source']['external_event_id']}"]


def test_nothing_is_read_until_this_machine_allows_it(profile, tmp_path, monkeypatch):
    from openachievements import events as ev
    root = tmp_path / "LocalLow"
    monkeypatch.setattr(savefile, "root_folder", lambda name: root)
    folder = root / "Studio" / "Game"
    folder.mkdir(parents=True)
    save(folder, "slot1.json", {"progress": {"chapter": 9}})
    reads = []
    real_read = savefile.read_save
    monkeypatch.setattr(savefile, "read_save", lambda *a: reads.append(a) or real_read(*a))
    # The pack arrives through sync, from another device, never installed here.
    from openachievements.packs import validate_definitions
    payload = validate_definitions({"id": "test-pack", "game_ids": ["test-game"], "saves": SAVES}, ACHIEVEMENTS)
    profile.commit([ev.make_event("pack.installed", profile_id=profile.profile_id, device_id=ev.new_id(),
                                  payload=payload)])
    watcher = savefile.SaveWatcher(profile)
    assert watcher.poll() == [] and reads == []
    assert savefile.check(profile)[0]["allowed"] is False and reads == []
    assert savefile.allow(profile, "test-pack", "main") == folder.resolve()
    assert {e["achievement_id"] for e in watcher.poll()} >= {"test-pack:chapter-3"}
    assert reads


def test_a_moved_location_needs_a_new_grant(profile, game, tmp_path):
    moved = [dict(SAVES[0], path="Studio/Elsewhere")]
    (game.parent / "Elsewhere").mkdir()
    save(game.parent / "Elsewhere", "slot1.json", {"progress": {"chapter": 5}})
    profile.install_pack(write_pack(tmp_path / "pack2", achievements=ACHIEVEMENTS, saves=moved))
    assert savefile.SaveWatcher(profile).poll() == []
    savefile.allow(profile, "test-pack", "main")
    assert savefile.SaveWatcher(profile).poll()


def test_two_packs_reading_one_save_both_evaluate_it(profile, game, tmp_path):
    other = [{"id": "other", "name": "Other", "rules": [
        {"signal": "save", "save": "main", "field": "progress.chapter", "op": ">=", "value": 1}]}]
    profile.install_pack(write_pack(tmp_path / "second", pack_id="second-pack", achievements=other, saves=SAVES))
    savefile.allow(profile, "second-pack", "main")
    save(game, "slot1.json", {"progress": {"chapter": 3}})
    got = {e["achievement_id"] for e in savefile.SaveWatcher(profile).poll()}
    assert {"test-pack:chapter-3", "second-pack:other"} <= got


def test_one_broken_folder_does_not_starve_the_other_packs(profile, game, tmp_path, monkeypatch):
    other = [{"id": "other", "name": "Other", "rules": [
        {"signal": "save", "save": "main", "field": "progress.chapter", "op": ">=", "value": 1}]}]
    profile.install_pack(write_pack(tmp_path / "second", pack_id="zz-pack", achievements=other, saves=SAVES))
    savefile.allow(profile, "zz-pack", "main")
    save(game, "slot1.json", {"progress": {"chapter": 1}})
    real = savefile.save_files
    calls = {"n": 0}

    def flaky(folder, pattern):
        calls["n"] += 1
        if calls["n"] == 1:                     # the first pack's folder cannot be listed
            raise PermissionError(13, "Access is denied")
        return real(folder, pattern)
    monkeypatch.setattr(savefile, "save_files", flaky)
    watcher = savefile.SaveWatcher(profile)
    assert "zz-pack:other" in {e["achievement_id"] for e in watcher.poll()}
    assert any("cannot list" in m for m in watcher.new_problems())


def test_a_broken_save_is_reported_once_and_not_reread(profile, game, monkeypatch):
    (game / "slot1.json").write_text("{not json", encoding="utf-8")
    reads = []
    real_read = savefile.read_save
    monkeypatch.setattr(savefile, "read_save", lambda *a: reads.append(a) or real_read(*a))
    watcher = savefile.SaveWatcher(profile)
    for _ in range(3):
        watcher.poll()
    assert len(reads) == 1 and len(watcher.problems) == 1
    assert len(watcher.new_problems()) == 1 and watcher.new_problems() == []


def test_a_link_out_of_the_save_folder_is_not_followed(profile, game, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"progress": {"chapter": 9}}), encoding="utf-8")
    try:
        (game / "slot1.json").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("this system does not allow creating symlinks")
    assert savefile.save_files(game, "slot*.json") == []
    assert savefile.SaveWatcher(profile).poll() == []


def test_an_unlock_is_dated_when_the_game_wrote_the_save_so_old_saves_never_pop(profile, game):
    import os
    from openachievements import notify
    save(game, "slot1.json", {"progress": {"chapter": 3}})
    old = 1_700_000_000                                             # November 2023
    os.utime(game / "slot1.json", (old, old))
    written = savefile.SaveWatcher(profile).poll()
    assert written and written[0]["occurred_at"].startswith("2023-11-14")
    assert not notify.wanted(written[0], notify.DEFAULTS)
