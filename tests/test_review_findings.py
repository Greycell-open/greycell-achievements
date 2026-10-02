"""Regressions for the first critic review (2026-09-30), one per finding."""
import json

import pytest
from fastapi.testclient import TestClient

from openachievements import events as ev
from openachievements import reducer
from openachievements.adapters import executable as exe
from openachievements.packs import PackError
from openachievements.profile import MachineConfig, Profile
from openachievements.server.app import Settings, create_app
from openachievements.server.store import Store
from openachievements.sync import SyncClient, connect, restore
from conftest import write_pack
from test_sync import SERVER, FakeServer, client_for


def test_progress_from_two_devices_unlocks_once_after_sync(tmp_path):
    server = FakeServer(tmp_path)
    d1 = Profile.create("Zein", home=tmp_path / "d1", config=MachineConfig(tmp_path / "m1"))
    d1.install_pack(write_pack(tmp_path / "pack"))
    connect(d1, SERVER, "zein", "correct horse battery", register=True, transport_factory=server.transport)
    client_for(server, d1).run()
    d2 = restore(SERVER, "zein", "correct horse battery", tmp_path / "d2", MachineConfig(tmp_path / "m2"),
                 transport_factory=server.transport)
    d1.record_progress("test-pack:collector", 2, adapter="test")          # target is 3
    d2.record_progress("test-pack:collector", 1, adapter="test")
    for device in (d1, d2, d1, d2):
        client_for(server, device).run()
    for device in (d1, d2):
        state = device.state()
        assert state["progress"]["test-pack:collector"]["value"] == 3
        assert reducer.is_unlocked(state, "test-pack:collector")
        assert len(state["unlocks"]["test-pack:collector"]["records"]) == 1


def test_a_revoked_progress_unlock_is_not_unlocked_again_or_rewritten(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    profile.record_progress("test-pack:collector", 3, adapter="test")
    profile.revoke("test-pack:collector", "counted wrong")
    before = len(profile.log.read().events)
    assert profile.reconcile_progress() == []
    assert len(profile.log.read().events) == before


def test_a_targeted_revocation_holds_even_if_its_clock_is_earlier(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    unlock = profile.record_unlock("test-pack:first-step", adapter="test")
    earlier = "2000-01-01T00:00:00.000Z"
    revoke = ev.make_event("achievement.revoked", profile_id=profile.profile_id, device_id=profile.device_id,
                           payload={"revokes": [unlock["event_id"]]}, achievement_id="test-pack:first-step",
                           occurred_at=earlier)
    profile.commit([revoke])
    assert not reducer.is_unlocked(profile.state(), "test-pack:first-step")


def test_an_index_that_missed_an_append_rebuilds_itself(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    # Simulate a crash between the durable append and the index update.
    lost = ev.make_event("achievement.unlocked", profile_id=profile.profile_id, device_id=profile.device_id,
                         payload={"provenance": "imported"}, achievement_id="test-pack:first-step",
                         adapter="steam")
    profile.log.append([lost])
    reopened = Profile(profile.folder, profile.config)
    assert reducer.is_unlocked(reopened.state(), "test-pack:first-step")
    lib = reopened.library()
    assert lib["totals"]["unlocked"] == 1


def test_a_full_account_still_accepts_retries_of_what_it_already_has(tmp_path):
    server = FakeServer(tmp_path, max_events=3)
    p = Profile.create("Zein", home=tmp_path / "d1", config=MachineConfig(tmp_path / "m1"))
    p.install_pack(write_pack(tmp_path / "pack"))
    session = connect(p, SERVER, "zein", "correct horse battery", register=True, transport_factory=server.transport)
    client_for(server, p).run()
    stored = server.client.get("/v1/export", headers={"Authorization": f"Bearer {session['token']}"}).text
    first = json.loads(stored.splitlines()[0])
    reply = server.client.post("/v1/sync", headers={"Authorization": f"Bearer {session['token']}"}, json={
        "protocol_version": "1.0", "profile_id": p.profile_id, "device_id": p.device_id,
        "cursor": None, "events": [first]}).json()
    assert reply["accepted_event_ids"] == [first["event_id"]]
    assert reply["rejected_events"] == []


def test_bad_rule_fields_are_refused_at_install(profile, tmp_path):
    for bad in ({"signal": "playtime", "minutes": "not-a-number"}, {"signal": "playtime", "minutes": 0},
                {"signal": "sessions", "count": -1}, {"signal": "sessions"}, {"nope": 1}, "launch"):
        with pytest.raises(PackError):
            profile.install_pack(write_pack(tmp_path / f"p{hash(str(bad))}",
                                            achievements=[{"id": "a", "name": "A", "rules": [bad]}]))
    ok = write_pack(tmp_path / "future", achievements=[{"id": "a", "name": "A",
                                                        "rules": [{"signal": "boss-defeated", "boss": "x"}]}])
    profile.install_pack(ok)       # unknown signals are left for future adapters


def test_a_bad_rule_arriving_from_elsewhere_cannot_crash_the_watcher(profile, tmp_path, monkeypatch):
    binary = tmp_path / "game.exe"
    binary.write_bytes(b"MZ")
    profile.install_pack(write_pack(tmp_path / "pack", achievements=[{"id": "ok", "name": "OK",
                                                                      "rules": [{"signal": "launch"}]}]))
    pack = {k: v for k, v in profile.state()["packs"]["test-pack"].items() if k != "removed"}
    pack["achievements"] = [{"id": "ok", "name": "OK", "rules": [{"signal": "launch"}]},
                            {"id": "bad", "name": "Bad", "rules": [{"signal": "playtime", "minutes": "x"}]}]
    # Such an event is now refused before it can be stored anywhere...
    with pytest.raises(ev.EventError):
        profile.record("pack.updated", pack)
    # ...and if older data holds one anyway, the watcher skips it and carries on.
    exe.register(profile, "test-game", binary)
    real_state = profile.state

    def state_with_bad_rule():
        state = real_state()
        state["packs"]["test-pack"]["achievements"]["bad"] = {"id": "bad", "name": "Bad",
                                                              "rules": [{"signal": "playtime", "minutes": "x"}]}
        return state
    monkeypatch.setattr(profile, "state", state_with_bad_rule)
    written = exe.Watcher(profile).poll({str(binary.resolve()).lower()}, now=0)
    assert [e.get("achievement_id") for e in written if e["event_type"] == "achievement.unlocked"] == ["test-pack:ok"]


def test_a_well_hashed_but_malformed_payload_is_refused_everywhere(profile, tmp_path):
    event = {"schema_version": "1.0", "event_id": ev.new_id(), "event_type": "achievement.unlocked",
             "profile_id": profile.profile_id, "device_id": profile.device_id, "game_id": None,
             "achievement_id": "test-pack:first-step", "occurred_at": ev.now(), "recorded_at": ev.now(),
             "source": {"adapter": "manual"}, "payload": {"provenance": {"not": "text"}}}
    event["integrity"] = {"event_hash": ev.compute_hash(event)}
    with pytest.raises(ev.EventError, match="provenance"):
        profile.commit([event])
    server = FakeServer(tmp_path)
    session = connect(profile, SERVER, "zein", "correct horse battery", register=True,
                      transport_factory=server.transport)
    reply = server.client.post("/v1/sync", headers={"Authorization": f"Bearer {session['token']}"}, json={
        "protocol_version": "1.0", "profile_id": profile.profile_id, "device_id": profile.device_id,
        "cursor": None, "events": [event]}).json()
    assert reply["rejected_events"][0]["code"] == "bad_payload"
    # Written straight into the folder by something else: quarantined, library still renders.
    with open(profile.log.files()[-1], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")
    problems = profile.index.rebuild()
    assert any("bad_payload" in p for p in problems)
    assert profile.library()["profile"]["name"] == "Zein"


def test_revoke_from_a_device_with_a_slow_clock_still_holds(tmp_path, monkeypatch):
    server = FakeServer(tmp_path)
    d1 = Profile.create("Zein", home=tmp_path / "d1", config=MachineConfig(tmp_path / "m1"))
    d1.install_pack(write_pack(tmp_path / "pack"))
    connect(d1, SERVER, "zein", "correct horse battery", register=True, transport_factory=server.transport)
    unlock = d1.record_unlock("test-pack:first-step", adapter="test")
    client_for(server, d1).run()
    d2 = restore(SERVER, "zein", "correct horse battery", tmp_path / "d2", MachineConfig(tmp_path / "m2"),
                 transport_factory=server.transport)
    # d2's clock runs well behind d1's: its revocation sorts before the unlock.
    monkeypatch.setattr(ev, "now", lambda: "2000-01-01T00:00:00.000Z")
    revocation = d2.revoke("test-pack:first-step", "misclick")
    monkeypatch.undo()
    assert revocation["payload"]["revokes"] == [unlock["event_id"]]
    assert ev.sort_key(revocation) < ev.sort_key(unlock)
    for device in (d2, d1):
        client_for(server, device).run()
    for device in (d1, d2):
        assert not reducer.is_unlocked(device.state(), "test-pack:first-step")
    with pytest.raises(Exception, match="not unlocked"):
        d1.revoke("test-pack:first-step")


def test_a_revocation_naming_either_copy_of_a_shared_unlock_reaches_it(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    key = "test-pack:first-step"
    copies = [ev.make_event("achievement.unlocked", profile_id=profile.profile_id, device_id=profile.device_id,
                            achievement_id=key, adapter="steam", external_event_id="same",
                            occurred_at=at) for at in ("2020-01-01T00:00:00.000Z", "2020-01-02T00:00:00.000Z")]
    profile.commit(copies)
    revoke = ev.make_event("achievement.revoked", profile_id=profile.profile_id, device_id=profile.device_id,
                           payload={"revokes": [copies[1]["event_id"]]}, achievement_id=key)
    profile.commit([revoke])
    assert not reducer.is_unlocked(profile.state(), key)


def test_a_second_writer_cannot_be_marked_indexed_while_the_first_is_indexing(profile, tmp_path, monkeypatch):
    import threading
    from openachievements.eventlog import EventLog
    profile.install_pack(write_pack(tmp_path / "pack"))
    in_add, release = threading.Event(), threading.Event()
    real_add = profile.index.add

    def slow_add(*args, **kwargs):
        in_add.set()
        release.wait(5)
        return real_add(*args, **kwargs)
    monkeypatch.setattr(profile.index, "add", slow_add)
    a = threading.Thread(target=profile.record_unlock, args=("test-pack:first-step",),
                         kwargs={"adapter": "test"})
    a.start()
    assert in_add.wait(5)
    # Writer B: another process on the same folder that appends and then dies
    # before it can index anything.
    other = EventLog(profile.log.root.parent, profile.profile_id)
    lost = ev.make_event("progress.incremented", profile_id=profile.profile_id, device_id=profile.device_id,
                         payload={"amount": 1}, achievement_id="test-pack:collector", adapter="test")
    b = threading.Thread(target=other.append, args=([lost],))
    b.start()
    b.join(0.5)
    assert b.is_alive(), "B appended while A was between its append and its index update"
    release.set()
    a.join(5)
    b.join(5)
    assert lost["event_id"] in profile.index.event_ids()
    assert profile.state()["progress"]["test-pack:collector"]["value"] == 1


# ---- third critic review (2026-09-30) -------------------------------------------

def _hashed(profile, **fields):
    event = {"schema_version": "1.0", "event_id": ev.new_id(), "event_type": "achievement.unlocked",
             "profile_id": profile.profile_id, "device_id": profile.device_id, "game_id": None,
             "achievement_id": None, "occurred_at": ev.now(), "recorded_at": ev.now(),
             "source": {"adapter": "manual"}, "payload": {}}
    event.update(fields)
    event["integrity"] = {"event_hash": ev.compute_hash(event)}
    return event


def _refused_on_every_path(profile, tmp_path, event, code):
    with pytest.raises(ev.EventError) as err:
        profile.commit([event])
    assert err.value.code == code
    server = FakeServer(tmp_path)
    session = connect(profile, SERVER, "zein", "correct horse battery", register=True,
                      transport_factory=server.transport)
    reply = server.client.post("/v1/sync", headers={"Authorization": f"Bearer {session['token']}"}, json={
        "protocol_version": "1.0", "profile_id": profile.profile_id, "device_id": profile.device_id,
        "cursor": None, "events": [event]}).json()
    assert reply["rejected_events"][0]["code"] == code
    with open(profile.log.files()[-1], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")
    problems = profile.index.rebuild()
    assert any(code in p for p in problems)
    assert profile.library()["profile"]["name"] == "Zein"


def test_an_unlock_without_an_achievement_is_refused_everywhere(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    _refused_on_every_path(profile, tmp_path, _hashed(profile), "missing_field")


def test_reserved_event_types_are_refused_until_they_have_a_schema(profile):
    with pytest.raises(ev.EventError) as err:
        profile.commit([_hashed(profile, event_type="save.transfer_completed", achievement_id=None)])
    assert err.value.code == "reserved_type"


def test_a_pack_payload_of_the_wrong_shape_is_refused_not_a_crash(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    pack = {k: v for k, v in profile.state()["packs"]["test-pack"].items() if k != "removed"}
    pack["achievements"] = list(pack["achievements"].values())
    pack["authors"] = 1
    event = _hashed(profile, event_type="pack.updated", payload=pack)
    _refused_on_every_path(profile, tmp_path, event, "bad_payload")


_HOLD = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from openachievements.fsutil import file_lock
with file_lock(Path(sys.argv[2]), stale_after=0.2):
    Path(sys.argv[3]).write_text("held")
    time.sleep(3)
"""


def test_a_live_holder_keeps_its_lock_however_long_it_takes(tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path
    from openachievements.fsutil import LockTimeout, file_lock
    lock, ready = tmp_path / "lock", tmp_path / "ready"
    src = str(Path(__file__).resolve().parents[1] / "src")
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, src, str(lock), str(ready)])
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert time.monotonic() < deadline, "holder never took the lock"
            time.sleep(0.05)
        time.sleep(0.5)                          # well past stale_after
        with pytest.raises(LockTimeout):
            with file_lock(lock, timeout=1.0, stale_after=0.2):
                pass
    finally:
        holder.wait(20)
    assert not lock.exists()                     # the holder released its own lock


def test_a_dead_holders_lock_is_broken_and_only_the_owner_releases(tmp_path):
    import os
    import socket
    from openachievements.fsutil import file_lock
    lock = tmp_path / "lock"
    lock.write_text(json.dumps({"host": socket.gethostname(), "pid": 2 ** 22 + 12345, "token": "dead"}))
    with file_lock(lock, timeout=2.0):
        mine = json.loads(lock.read_text())
        assert mine["pid"] == os.getpid()
        # Someone else replaces the lock while we hold it: our exit must not delete theirs.
        lock.write_text(json.dumps({"host": "elsewhere", "pid": 1, "token": "theirs"}))
    assert json.loads(lock.read_text())["token"] == "theirs"


# ---- final review of the backlog, live capture and recognition (2026-10-01) ------------

def test_a_backlogged_catalogue_game_is_still_recognised_when_it_runs(profile, tmp_path, monkeypatch):
    from test_autodetect import SYSTEM, make_game
    from test_catalog import fake_steam as fs, page, row
    from openachievements import steamfiles as sf
    from openachievements.adapters import autodetect as ad, savefile
    from openachievements.catalog import steam as cat
    games = {367520: (1, page("Hollow Knight", [row("Keen", "Hit", "aa", "80.0")]))}
    cat.crawl([(367520, None)], tmp_path / "c", fs(games))
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 367520, status="backlog")
    monkeypatch.setattr(ad, "_SYSTEM", tuple(s for s in SYSTEM if "appdata" not in s))
    monkeypatch.setattr(sf, "steam_dir", lambda: None)
    monkeypatch.setattr(savefile, "root_folder", lambda name: None)
    exe = make_game(tmp_path / "D", "Games/Hollow Knight/hollow_knight.exe", ["UnityPlayer.dll"])
    found = ad.AutoDetector(profile, catalog_dir=tmp_path / "c").poll({exe})
    assert found and found[0]["game_id"] == "steam-367520"
    assert profile.state()["games"]["steam-367520"]["status"] == "playing"


def test_a_program_rejected_before_its_title_was_crawled_gets_another_look(profile, tmp_path, monkeypatch):
    from test_autodetect import SYSTEM, make_game
    from test_catalog import fake_steam as fs, page, row
    from openachievements import steamfiles as sf
    from openachievements.adapters import autodetect as ad, savefile
    from openachievements.catalog import steam as cat
    cat.crawl([(1, None)], tmp_path / "c", fs({1: (1, page("Other Game", [row("A", "a", "aa", "1.0")]))}))
    monkeypatch.setattr(ad, "_SYSTEM", tuple(s for s in SYSTEM if "appdata" not in s))
    monkeypatch.setattr(sf, "steam_dir", lambda: None)
    monkeypatch.setattr(savefile, "root_folder", lambda name: None)
    exe = make_game(tmp_path / "D", "Games/Hollow Knight/hollow_knight.exe", ["UnityPlayer.dll"])
    d = ad.AutoDetector(profile, catalog_dir=tmp_path / "c")
    assert d.poll({exe}) == []                                    # not catalogued yet
    cat.crawl([(367520, None)], tmp_path / "c", fs({367520: (1, page("Hollow Knight", [row("K", "k", "bb", "1.0")]))}))
    d._matcher_at = -1e9                                          # the periodic refresh comes round
    assert d.poll({exe})[0]["game_id"] == "steam-367520"


def test_a_status_from_a_device_with_a_slow_clock_is_kept(profile):
    registered = ev.make_event("game.registered", profile_id=profile.profile_id, device_id=profile.device_id,
                               payload={"title": "Celeste"}, game_id="celeste", occurred_at="2026-10-01T12:00:00.000Z")
    status = ev.make_event("game.status_changed", profile_id=profile.profile_id, device_id=ev.new_id(),
                           payload={"status": "playing"}, game_id="celeste", occurred_at="2026-10-01T11:55:00.000Z")
    profile.commit([status, registered])
    assert profile.state()["games"]["celeste"]["status"] == "playing"
    assert profile.index.rebuild() == [] and profile.library()["games"][0]["status"] == "playing"


def test_a_save_swapped_for_a_link_after_listing_is_not_followed(tmp_path):
    from openachievements.adapters import savefile
    folder = tmp_path / "saves"
    folder.mkdir()
    (folder / "slot1.json").write_text('{"chapter": 1}', encoding="utf-8")
    listed = savefile.save_files(folder, "slot*.json")
    assert [p.name for p in listed] == ["slot1.json"]
    secret = tmp_path / "secret.json"
    secret.write_text('{"chapter": 9}', encoding="utf-8")
    (folder / "slot1.json").unlink()
    try:
        (folder / "slot1.json").symlink_to(secret)                 # swapped between listing and reading
    except (OSError, NotImplementedError):
        pytest.skip("this system does not allow creating symlinks")
    with pytest.raises(savefile.SaveUnreadable):
        savefile.read_save(listed[0], "json", folder)
    assert savefile.read_save(secret, "json", tmp_path)[0] == {"chapter": 9}   # an ordinary file inside reads fine


def test_history_imported_on_a_retry_does_not_mark_a_game_playing(profile, tmp_path, monkeypatch):
    from openachievements.adapters import steam_local
    from openachievements.fsutil import LockTimeout
    from test_autodetect import fake_steam_install
    root = fake_steam_install(tmp_path / "Steam")
    steam_local.link(profile, root)
    real, calls = profile.commit, {"n": 0}

    def busy_first(events):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LockTimeout("busy")
        return real(events)
    monkeypatch.setattr(profile, "commit", busy_first)
    w = steam_local.SteamLocalWatcher(profile, catalog_dir=tmp_path / "nocat")
    w.poll()
    assert w.poll()                                            # history arrives on the retry
    assert "status" not in profile.state()["games"]["steam-10"]
    fake_steam_install(root, unlocked_bits=(0, 2))            # now the game really records a new unlock
    w.poll()
    assert profile.state()["games"]["steam-10"]["status"] == "playing"


def test_pack_parts_that_disagree_never_install_part_of_a_pack(profile):
    from openachievements.packs import split_for_events, validate_definitions
    from test_catalog import _big_entry
    entry = _big_entry(440, 520)
    parts = split_for_events(validate_definitions(entry["pack"], entry["achievements"]))
    assert len(parts) > 1
    liar = dict(parts[0], part={**parts[0]["part"], "count": 1})   # claims the set is complete alone
    events = [ev.make_event("pack.installed", profile_id=profile.profile_id, device_id=profile.device_id,
                            payload=p) for p in [liar] + parts[1:]]
    profile.commit(events)
    assert "steam-440" not in profile.state()["packs"]
    shuffled = dict(parts[0], achievements=parts[0]["achievements"][1:])   # right count, altered content
    profile.commit([ev.make_event("pack.installed", profile_id=profile.profile_id, device_id=profile.device_id,
                                  payload=p) for p in [shuffled] + parts[1:]])
    assert "steam-440" not in profile.state()["packs"]


def test_a_steam_game_linked_into_another_is_one_game_to_recognition(profile):
    from openachievements.adapters import autodetect as ad
    profile.register_game("celeste", "Celeste")
    profile.register_game("steam-504230", "Celeste", platform="PC (Steam)")
    profile.link_games("steam-504230", "celeste")

    class Index:
        entries = {504230: (0, 0, 1, "Celeste")}
    assert ad.TitleMatcher(Index, profile.state()["games"]).match(["Celeste"]) == ("game", "celeste")


def test_threads_of_one_app_writing_at_once_all_succeed(profile, tmp_path):
    """The watcher, background fetches and the page all write to one profile."""
    import threading
    profile.install_pack(write_pack(tmp_path / "pack"))
    errors, done = [], []

    def writer(n):
        try:
            for i in range(15):
                profile.record("game.metadata_updated", {"last_played": f"2026-01-{(i % 28) + 1:02d}T00:00:00.000Z"},
                               game_id="test-game")
            done.append(n)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and len(done) == 6
    assert sum(e["event_type"] == "game.metadata_updated" for e in profile.log.read().events) == 90
    assert not (profile.log.root / ".append.lock").exists()


def test_a_rebuild_never_leaves_readers_an_empty_index(profile, tmp_path, monkeypatch):
    """The library page showed nothing: a rebuild deleted the index and refilled
    it, and a reader in another process cached an empty library in between."""
    import sqlite3
    profile.install_pack(write_pack(tmp_path / "pack"))
    profile.library()
    seen = []
    real_read = profile.log.read

    def read_during_rebuild(*a, **kw):
        db = sqlite3.connect(profile.index.path)          # what another process would open now
        seen.append(db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        db.close()
        return real_read(*a, **kw)
    monkeypatch.setattr(profile.log, "read", read_during_rebuild)
    profile.index.rebuild()
    assert seen and seen[0] > 0
    assert profile.library()["games"]


def test_a_reader_waiting_on_a_writer_does_not_rebuild_after_it(profile, tmp_path, monkeypatch):
    profile.install_pack(write_pack(tmp_path / "pack"))
    rebuilds = []
    real = profile.index._rebuild
    monkeypatch.setattr(profile.index, "_rebuild", lambda: rebuilds.append(1) or real())
    real_current = profile.index._current
    calls = {"n": 0}

    def stale_once():                      # the reader looks while a writer is mid-append
        calls["n"] += 1
        return False if calls["n"] == 1 else real_current()
    monkeypatch.setattr(profile.index, "_current", stale_once)
    profile.library()
    assert rebuilds == []


def test_a_write_while_the_library_is_built_is_not_lost(profile, tmp_path, monkeypatch):
    from openachievements import index as ix
    profile.install_pack(write_pack(tmp_path / "pack"))
    real = ix.reducer.library
    wrote = []

    def library_then_a_write(state):
        view = real(state)
        if not wrote:
            wrote.append(profile.record("game.status_changed", {"status": "playing", "note": None},
                                        game_id="test-game"))
        return view
    monkeypatch.setattr(ix.reducer, "library", library_then_a_write)
    assert profile.library()["games"][0].get("status") == "playing"
    monkeypatch.setattr(ix.reducer, "library", real)
    assert profile.library()["games"][0].get("status") == "playing"
