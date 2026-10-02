"""The local core: a profile folder is the durable copy of everything."""
import json
import shutil

import pytest

from openachievements import events as ev
from openachievements import reducer
from openachievements.packs import PackError
from openachievements.profile import MachineConfig, Profile, ProfileError
from conftest import write_pack


def test_create_profile_records_creation_and_device(profile):
    kinds = [e["event_type"] for e in profile.log.read().events]
    assert kinds == ["profile.created", "device.registered"]
    assert profile.library()["profile"]["name"] == "Zein"


def test_install_pack_registers_its_game_and_lists_achievements(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    game = profile.library()["games"][0]
    assert game["game_id"] == "test-game" and game["title"] == "Test Game"
    assert game["total"] == 3 and game["unlocked"] == 0


def test_an_unlock_is_recorded_once_with_its_adapters_provenance(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    assert profile.record_unlock("test-pack:first-step", adapter="test") is not None
    assert profile.record_unlock("test-pack:first-step", adapter="test") is None
    ach = next(a for a in profile.library()["games"][0]["achievements"] if a["id"] == "first-step")
    assert ach["unlocked"] and ach["provenance"] == ["imported"]


def test_nothing_unlocks_by_hand_from_here_or_from_any_other_device(profile, tmp_path):
    """Owner decision 2026-10-01: no ticking achievements by hand."""
    from openachievements import events as ev
    profile.install_pack(write_pack(tmp_path / "pack"))
    with pytest.raises(ProfileError):
        profile.record_unlock("test-pack:first-step", adapter="manual")
    with pytest.raises(ProfileError):
        profile.record_unlock("test-pack:first-step", adapter="test", provenance="manual")
    with pytest.raises(ProfileError):
        profile.record_progress("test-pack:collector", 3, adapter="manual")
    # What an older client or another device would send through sync:
    other = "00000000-0000-4000-8000-000000000002"
    made = lambda kind, payload: ev.make_event(kind, profile_id=profile.profile_id, device_id=other,
                                              payload=payload, achievement_id=f"test-pack:{payload.pop('_a')}")
    profile.commit([made("achievement.unlocked", {"provenance": "manual", "_a": "first-step"}),
                    made("progress.incremented", {"amount": 3, "_a": "collector"})])
    lib = profile.library()
    assert lib["totals"]["unlocked"] == 0
    assert all(a["progress"] is None for a in lib["games"][0]["achievements"])
    assert profile.reconcile_progress() == []


def test_progress_unlocks_at_target(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    profile.record_progress("test-pack:collector", adapter="test")
    profile.record_progress("test-pack:collector", adapter="test")
    assert not reducer.is_unlocked(profile.state(), "test-pack:collector")
    written = profile.record_progress("test-pack:collector", adapter="test")
    assert [e["event_type"] for e in written] == ["progress.incremented", "achievement.unlocked"]
    ach = next(a for a in profile.library()["games"][0]["achievements"] if a["id"] == "collector")
    assert ach["unlocked"] and ach["progress"] == {"value": 3, "target": 3}


def test_hidden_achievement_stays_hidden_until_unlocked(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    shown = next(a for a in profile.library()["games"][0]["achievements"] if a["id"] == "secret")
    assert shown["name"] == "Hidden achievement" and shown["description"] == ""
    profile.record_unlock("test-pack:secret", adapter="test")
    shown = next(a for a in profile.library()["games"][0]["achievements"] if a["id"] == "secret")
    assert shown["name"] == "Secret"


def test_revocation_is_a_new_event_and_the_unlock_stays_in_history(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    profile.record_unlock("test-pack:first-step", adapter="test")
    profile.revoke("test-pack:first-step", "misclick")
    assert not reducer.is_unlocked(profile.state(), "test-pack:first-step")
    kinds = [e["event_type"] for e in profile.log.read().events]
    assert "achievement.unlocked" in kinds and "achievement.revoked" in kinds


def test_deleting_the_index_loses_nothing(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    profile.record_unlock("test-pack:first-step", adapter="test")
    before = profile.library()
    profile.index.path.unlink()
    reopened = Profile(profile.folder, profile.config)
    assert reopened.library() == before


def test_rebuild_quarantines_a_truncated_line_and_keeps_the_rest(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    profile.record_unlock("test-pack:first-step", adapter="test")
    log_file = profile.log.files()[-1]
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write('{"event_id": "half a line')
    problems = profile.index.rebuild()
    assert len(problems) == 1 and "invalid_json" in problems[0]
    assert reducer.is_unlocked(profile.state(), "test-pack:first-step")
    assert (profile.folder / "events" / "quarantine" / "quarantined.jsonl").exists()


def test_a_tampered_event_is_rejected_on_read(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    profile.record_unlock("test-pack:first-step", adapter="test")
    log_file = profile.log.files()[-1]
    lines = log_file.read_text(encoding="utf-8").splitlines()
    unlock = json.loads(lines[-1])
    unlock["payload"]["provenance"] = "adapter-verified"
    lines[-1] = json.dumps(unlock)
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    problems = profile.index.rebuild()
    assert any("bad_hash" in p for p in problems)
    assert not reducer.is_unlocked(profile.state(), "test-pack:first-step")


def test_the_same_folder_on_a_second_machine_gets_its_own_device(profile, tmp_path):
    other = Profile(profile.folder, MachineConfig(tmp_path / "machine-2"))
    assert other.device_id != profile.device_id
    assert len(other.state()["devices"]) == 2


def test_moving_the_profile_folder_keeps_working(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    profile.record_unlock("test-pack:first-step", adapter="test")
    moved = tmp_path / "elsewhere" / profile.profile_id
    shutil.copytree(profile.folder, moved)
    again = Profile(moved, profile.config)
    assert reducer.is_unlocked(again.state(), "test-pack:first-step")


def test_pack_paths_cannot_escape_the_pack(profile, tmp_path):
    folder = write_pack(tmp_path / "pack", achievements=[
        {"id": "sneaky", "name": "Sneaky", "icon": "../../../etc/passwd"}])
    with pytest.raises(ValueError):
        profile.install_pack(folder)


def test_invalid_packs_are_refused_with_a_reason(profile, tmp_path):
    with pytest.raises(PackError, match="duplicate"):
        profile.install_pack(write_pack(tmp_path / "p1", achievements=[
            {"id": "a", "name": "A"}, {"id": "a", "name": "A again"}]))
    with pytest.raises(PackError, match="points"):
        profile.install_pack(write_pack(tmp_path / "p2", achievements=[{"id": "a", "name": "A", "points": -5}]))


def test_reinstalling_a_pack_is_an_update_not_a_second_pack(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    event = profile.install_pack(write_pack(tmp_path / "pack2", version="1.1.0"))
    assert event["event_type"] == "pack.updated"
    assert len(profile.state()["packs"]) == 1


def test_unknown_achievement_is_an_error_not_a_silent_event(profile):
    with pytest.raises(ProfileError):
        profile.record_unlock("nope:nothing", adapter="test")


def test_reduction_does_not_depend_on_delivery_order(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    profile.record_unlock("test-pack:first-step", adapter="test")
    profile.record_progress("test-pack:collector", 2, adapter="test")
    events = profile.log.read().events
    forward = reducer.library(reducer.reduce(events))
    backward = reducer.library(reducer.reduce(list(reversed(events))))
    doubled = reducer.library(reducer.reduce(events + events))
    assert forward == backward == doubled


def test_event_hash_is_reproducible_without_this_code():
    import hashlib
    e = ev.make_event("profile.updated", profile_id=ev.new_id(), device_id=ev.new_id(), payload={"name": "Z"})
    body = {k: v for k, v in e.items() if k != "integrity"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False).encode()).hexdigest()
    assert e["integrity"]["event_hash"] == "sha256:" + digest


def test_a_partial_machine_file_gets_its_missing_parts(tmp_path):
    from openachievements.profile import MachineConfig
    cfg = MachineConfig(tmp_path / "m")
    cfg.dir.mkdir(parents=True)
    cfg.path.write_text('{"update": {"check": false}}', encoding="utf-8")
    data = cfg.load()
    assert data["devices"] == {} and data["sync"] == {} and data["active_profile"] is None
    assert data["update"] == {"check": False}


def test_profiles_default_to_the_home_folder_and_settings_to_appdata(monkeypatch):
    """The installer, README and About say where achievements are; keep them true."""
    import os
    from pathlib import Path
    from openachievements import profile as prof
    monkeypatch.delenv("OPENACHIEVEMENTS_HOME", raising=False)
    monkeypatch.delenv("OPENACHIEVEMENTS_CONFIG", raising=False)
    assert prof.default_home() == Path.home() / "OpenAchievements"
    if os.name == "nt":
        assert prof.default_config_dir() == Path(os.environ["APPDATA"]) / "OpenAchievements"


def test_two_processes_editing_machine_settings_never_lose_a_change(tmp_path):
    """The tray app and a command run alongside it are separate processes; each
    bumps its own counter 40 times and every bump must survive."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    from openachievements.profile import MachineConfig
    src = str(Path(__file__).resolve().parents[1] / "src")
    script = ("import sys; sys.path.insert(0, sys.argv[3]);"
              "from openachievements.profile import MachineConfig;"
              "cfg = MachineConfig(sys.argv[1]); key = sys.argv[2]\n"
              "for _ in range(40):\n"
              "    cfg.edit(lambda c: c.__setitem__(key, c.get(key, 0) + 1))\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "m"), key, src]) for key in ("a", "b")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    data = MachineConfig(tmp_path / "m").load()
    assert (data["a"], data["b"]) == (40, 40)
    assert not (tmp_path / "m" / ".machine.lock").exists()


def test_an_edit_inside_an_edit_does_not_wait_on_itself(tmp_path):
    from openachievements.profile import MachineConfig
    cfg = MachineConfig(tmp_path / "m")
    with cfg.editing() as outer:
        outer["x"] = 1
        cfg.set_device("p", "d")                 # nested: same thread, same transaction
    assert cfg.load()["devices"] == {"p": "d"}
