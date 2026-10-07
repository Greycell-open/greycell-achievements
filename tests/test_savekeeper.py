"""The save keeper (copies of every game's saves, put back on reinstall), the
new save readers (.NET BinaryFormatter, Easy Save 3, JSON per line) and rules
learned from the kept versions."""
import json
import os
import struct
import time
from pathlib import Path

import pytest

from openachievements import nrbf, reducer, savekeeper, savelearn, savevalues
from openachievements.adapters import executable, savefile, saverules
from openachievements.cli import main
from conftest import write_pack


# ---- .NET BinaryFormatter -----------------------------------------------------------------

def lps(text: str) -> bytes:
    raw = text.encode()
    return bytes([len(raw)]) + raw


def binary_formatter(level=3, done=True) -> bytes:
    """A SaveData {Level (int, backing field), name (string), done (bool),
    scores (int[] by reference)} written the way BinaryFormatter writes it."""
    out = b"\x00" + struct.pack("<iiii", 1, -1, 1, 0)
    out += b"\x0c" + struct.pack("<i", 2) + lps("Assembly-CSharp")
    members = ["<Level>k__BackingField", "name", "done", "scores"]
    out += b"\x05" + struct.pack("<i", 1) + lps("SaveData") + struct.pack("<i", len(members))
    out += b"".join(lps(m) for m in members)
    out += bytes([0, 1, 0, 7]) + bytes([8, 1, 8])                   # types, then int32, bool, int32[]
    out += struct.pack("<i", 2)                                     # library id
    out += struct.pack("<i", level) + b"\x06" + struct.pack("<i", 3) + lps("z") + bytes([done])
    out += b"\x09" + struct.pack("<i", 4)                            # scores: a reference
    out += b"\x0f" + struct.pack("<iiB", 4, 3, 8) + struct.pack("<iii", 10, 20, 30)
    return out + b"\x0b"


def test_a_binaryformatter_save_reads_into_plain_values():
    data = binary_formatter()
    assert nrbf.is_nrbf(data) and savevalues.sniff("save.dat", data) == "nrbf"
    assert nrbf.parse(data) == {"Level": 3, "name": "z", "done": True, "scores": [10, 20, 30]}


@pytest.mark.parametrize("damage", [lambda d: d[:40], lambda d: d[:-30],
                                    lambda d: d.replace(struct.pack("<iiB", 4, 3, 8), struct.pack("<iiB", 4, 10**8, 8))])
def test_a_broken_binaryformatter_save_is_an_error_not_a_crash(damage):
    with pytest.raises(nrbf.NrbfError):
        nrbf.parse(damage(binary_formatter()))


def test_es3_and_json_per_line_and_formats_by_content():
    es3 = b'{"Profile":{"__type":"ProfileSaveData","value":{"gameCompletions":2,"name":{"__type":"string","value":"z"}}}}'
    assert savevalues.sniff("SaveFile.es3", es3) == "es3"
    assert savevalues.values("SaveFile.es3", es3)[1] == {"Profile.gameCompletions": 2, "Profile.name": "z"}
    lines = b'{"coins": 5}\n{"floor": 3}\n'
    assert savevalues.values("LBAL.save", lines) == ("json", {"0.coins": 5, "1.floor": 3})
    assert savevalues.sniff("save_0.sav", b'{"a": 1}') == "json"
    assert savevalues.values("x.bin", b"\x93\x01garbage")[0] == "binary"


# ---- keeping and putting back ---------------------------------------------------------------

@pytest.fixture
def game(profile, tmp_path, monkeypatch):
    """A DRM-free game with its save folder under LocalLow, known to the keeper."""
    low = tmp_path / "LocalLow"
    monkeypatch.setattr(savefile, "root_folder", lambda name: low if name == "LOCALLOW" else None)
    profile.install_pack(write_pack(tmp_path / "pack", pack_id="oldsins", game_id="oldsins", achievements=[
        {"id": "kitchen", "name": "Kitchen Complete", "description": "Completed the Kitchen"},
        {"id": "study", "name": "Study Complete", "description": "Completed the Study"}]))
    folder = low / "Studio" / "Old Sins"
    folder.mkdir(parents=True)
    with profile.config.editing() as config:
        config.setdefault("save_candidates", {})[f"{profile.profile_id}:oldsins"] = [str(folder)]
    return folder


def write_save(folder: Path, values: dict, age: float = 60):
    path = folder / "slot.json"
    path.write_text(json.dumps(values))
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def test_a_changed_save_is_kept_once_and_unchanged_is_not(profile, game):
    keeper = savekeeper.Keeper(profile)
    write_save(game, {"level": 1})
    assert keeper.take("oldsins", game)["files"]["slot.json"]["size"] > 0
    assert keeper.take("oldsins", game) is None                      # the same again: nothing new
    write_save(game, {"level": 2})
    assert keeper.take("oldsins", game) and len(keeper.snapshots("oldsins")) == 2
    blobs = list((keeper.root / "oldsins" / "blobs").glob("*/*.gz"))
    assert len(blobs) == 2 and str(keeper.root).startswith(str(profile.folder))


def test_restore_puts_files_back_with_their_times_and_keeps_what_was_there(profile, game):
    keeper = savekeeper.Keeper(profile)
    first = write_save(game, {"level": 5})
    when = first.stat().st_mtime
    keeper.take("oldsins", game)
    write_save(game, {"level": 1})                                    # a fresh start
    plan = keeper.restore("oldsins")
    assert json.loads(first.read_text()) == {"level": 5} and abs(first.stat().st_mtime - when) < 1
    assert plan["replaces_existing"]
    kept = keeper.snapshots("oldsins")
    assert kept[0]["reason"] == "before-restore"                      # level 1 is kept too
    with pytest.raises(savekeeper.KeeperError):
        keeper.restore("oldsins", running=True)


def test_a_kept_file_cannot_be_restored_outside_its_folder(profile, game):
    keeper = savekeeper.Keeper(profile)
    write_save(game, {"level": 5})
    snap = keeper.take("oldsins", game)
    snap["files"]["../escape.json"] = snap["files"].pop("slot.json")
    (keeper.root / "oldsins" / "snapshots" / f"{snap['id']}.json").write_text(json.dumps(snap))
    with pytest.raises(savekeeper.KeeperError):
        keeper.restore("oldsins")
    assert not (game.parent / "escape.json").exists()


def test_old_snapshots_are_thinned_and_their_files_dropped(profile, game, monkeypatch):
    monkeypatch.setattr(savekeeper, "RECENT", 3)
    monkeypatch.setattr(savekeeper, "DAILY", 0)
    keeper = savekeeper.Keeper(profile)
    for n in range(6):
        write_save(game, {"level": n})
        keeper.take("oldsins", game)
        time.sleep(0.002)
    assert len(keeper.snapshots("oldsins")) == 3
    assert len(list((keeper.root / "oldsins" / "blobs").glob("*/*.gz"))) == 3


def register_exe(profile, tmp_path):
    exe = tmp_path / "Games" / "Old Sins" / "OldSins.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ old sins")
    executable.register(profile, "oldsins", exe)
    return exe


def age_snapshots(keeper, game_id, days=3):
    for path in (keeper.root / game_id / "snapshots").glob("*.json"):
        snap = json.loads(path.read_text())
        snap["taken_at"] = "2020-01-0%dT00:00:00Z" % days
        path.write_text(json.dumps(snap))


def test_the_watcher_keeps_changes_and_restores_a_reinstalled_game(profile, game, tmp_path):
    watcher = savekeeper.KeeperWatcher(profile)
    save = write_save(game, {"level": 7})
    assert any("kept oldsins" in m for m in watcher.poll(set(), force=True))
    register_exe(profile, tmp_path)
    age_snapshots(watcher._keeper(), "oldsins")                       # kept before the reinstall below
    save.unlink()
    game.rmdir()                                                      # uninstalled: the folder went with it
    said = watcher.poll(set(), force=True)
    assert any("put oldsins saves back" in m for m in said)
    assert json.loads(save.read_text()) == {"level": 7}
    save.write_text(json.dumps({"level": 8}))                         # played on: never put back over that
    os.utime(save, (time.time() - 60, time.time() - 60))
    assert not any("put" in m for m in watcher.poll(set(), force=True))
    assert json.loads(save.read_text()) == {"level": 8}


def test_a_save_deleted_while_the_game_stayed_installed_is_not_brought_back(profile, game, tmp_path):
    register_exe(profile, tmp_path)                                   # installed before it was kept
    time.sleep(0.05)
    watcher = savekeeper.KeeperWatcher(profile)
    save = write_save(game, {"level": 7})
    watcher.poll(set(), force=True)
    save.unlink()
    assert not any("put" in m for m in watcher.poll(set(), force=True))
    assert not save.exists()


def test_a_reinstalled_game_started_fresh_gets_its_progress_back_when_it_closes(profile, game, tmp_path):
    watcher = savekeeper.KeeperWatcher(profile)
    save = write_save(game, {"level": 7})
    watcher.poll(set(), force=True)
    age_snapshots(watcher._keeper(), "oldsins")
    save.unlink()
    watcher.poll({"oldsins"}, force=True)                             # started with no saves
    save.write_text(json.dumps({"level": 1}))                         # the game's fresh save
    said = watcher.poll(set(), force=True)                            # closed
    assert any("put oldsins saves back" in m for m in said)
    assert json.loads(save.read_text()) == {"level": 7}
    fresh = [s for s in watcher._keeper().snapshots("oldsins") if s["reason"] == "before-restore"]
    assert fresh                                                      # the fresh save is kept too


def test_nothing_is_written_while_the_game_runs(profile, game, tmp_path):
    watcher = savekeeper.KeeperWatcher(profile)
    write_save(game, {"level": 7})
    assert watcher.poll({"oldsins"}, force=True) == []                # mid-save maybe: not even copied
    assert watcher._keeper().snapshots("oldsins") == []


# ---- learning rules -----------------------------------------------------------------------

def kept_versions(profile, game, versions):
    """Keep each version, then date them an hour apart from 2026-10-06 10:00."""
    keeper = savekeeper.Keeper(profile)
    for values in versions:
        write_save(game, values)
        keeper.take("oldsins", game)
        time.sleep(0.002)
    snaps = sorted(keeper.snapshots("oldsins"), key=lambda s: s["id"])
    for n, snap in enumerate(snaps):
        snap["taken_at"] = f"2026-10-06T{10 + n:02d}:00:00Z"
        (keeper.root / "oldsins" / "snapshots" / f"{snap['id']}.json").write_text(json.dumps(snap))
    return keeper


VERSIONS = [{"time": 10, "levels": {"KIT": "Entered", "STU": "Locked"}},
            {"time": 20, "levels": {"KIT": "Entered", "STU": "Entered"}},
            {"time": 30, "levels": {"KIT": "Complete", "STU": "Entered"}},
            {"time": 40, "levels": {"KIT": "Complete", "STU": "Entered"}, "x": 1}]


def test_changes_show_what_the_game_wrote(profile, game):
    kept_versions(profile, game, VERSIONS)
    found = [(c["field"], c["old"], c["new"]) for c in savelearn.changes(profile, "oldsins")]
    assert ("levels.KIT", "Entered", "Complete") in found and ("levels.STU", "Locked", "Entered") in found


def test_a_rule_is_learned_from_an_unlock_the_game_reported_another_way(profile, game):
    kept_versions(profile, game, VERSIONS)
    profile.record("achievement.unlocked", {"provenance": "emulator"}, achievement_id="oldsins:kitchen",
                   game_id="oldsins", occurred_at="2026-10-06T11:30:00Z", adapter="steam-emulator")
    found = savelearn.suggest(profile, "oldsins")
    assert len(found) == 1
    s = found[0]
    assert (s["achievement_id"], s["field"], s["op"], s["value"]) == ("kitchen", "levels.KIT", "==", "Complete")
    assert s["confidence"] == "high"                                  # "time" changes every save: noise


def test_an_accepted_rule_unlocks_from_the_save_on_its_own(profile, game, tmp_path):
    kept_versions(profile, game, VERSIONS[:2])
    path = savelearn.add_rule(profile, "oldsins", "Study Complete", str(game), "slot.json", "levels.STU", "==",
                              "Complete")
    rules = json.loads(path.read_text())
    assert rules["saves"][0] == {"id": "slot", "root": "LOCALLOW", "path": "Studio/Old Sins", "pattern": "slot.json",
                                 "format": "json"}
    pack = profile.state()["packs"]["oldsins"]
    assert pack["achievements"]["study"]["rules"][0]["field"] == "levels.STU"
    write_save(game, {"levels": {"KIT": "Complete", "STU": "Complete"}})
    written = savefile.SaveWatcher(profile).poll()                  # allowed once, like shipped rules
    assert [e["achievement_id"] for e in written] == ["oldsins:study"]
    assert saverules.apply(profile) == []


def test_the_cli_lists_kept_saves_and_restores(profile, game, home, machine, capsys):
    write_save(game, {"level": 5})
    args = ["--home", str(home), "--config", str(machine.dir), "save"]
    assert main(args + ["keep", "oldsins"]) == 0
    assert "Kept 1" in capsys.readouterr().out
    assert main(args + ["kept"]) == 0
    assert "oldsins: 1 snapshot" in capsys.readouterr().out
    (game / "slot.json").unlink()
    assert main(args + ["restore", "oldsins", "--dry-run"]) == 0
    assert "Would write 1" in capsys.readouterr().out and not (game / "slot.json").exists()
    assert main(args + ["restore", "oldsins"]) == 0
    assert json.loads((game / "slot.json").read_text()) == {"level": 5}


# ---- the page's save routes -----------------------------------------------------------------

def test_the_page_keeps_restores_and_turns_a_change_into_a_rule(profile, game, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements import watching
    from openachievements.local_app import create_local_app
    monkeypatch.setattr(watching, "LIVE", {"playing": [], "at": 1.0})
    client = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    auth = {"X-OA-Token": "t" * 32}
    assert client.get("/v1/local/games/oldsins/kept").status_code == 403          # save contents are private
    write_save(game, {"levels": {"STU": "Entered"}})
    assert client.post("/v1/local/games/oldsins/keep", headers=auth).json() == {"taken": 1}
    write_save(game, {"levels": {"STU": "Complete"}})
    client.post("/v1/local/games/oldsins/keep", headers=auth)
    kept = client.get("/v1/local/games/oldsins/kept", headers=auth).json()
    assert len(kept["snapshots"]) == 2 and kept["folders"] == [str(game)] and kept["running"] is False
    change = next(c for c in client.get("/v1/local/games/oldsins/changes", headers=auth).json()["changes"]
                  if c["field"] == "levels.STU")
    r = client.post("/v1/local/games/oldsins/rules", headers=auth, json={
        "achievement": "study", "folder": change["folder"], "file": change["file"], "field": change["field"],
        "op": "==", "value": change["new"], "format": change["format"]})
    assert r.status_code == 200
    assert profile.state()["packs"]["oldsins"]["achievements"]["study"]["rules"][0]["value"] == "Complete"
    older = kept["snapshots"][1]["id"]
    monkeypatch.setattr(watching, "LIVE", {"playing": ["oldsins"], "at": 1.0})
    refused = client.post("/v1/local/games/oldsins/restore", headers=auth, json={"snapshot": older})
    assert refused.status_code == 400 and "running" in refused.json()["detail"]["message"]
    monkeypatch.setattr(watching, "LIVE", {"playing": [], "at": 1.0})
    done = client.post("/v1/local/games/oldsins/restore", headers=auth, json={"snapshot": older}).json()
    assert done["written"] == 1 and done["kept_before"] is True
    assert json.loads((game / "slot.json").read_text()) == {"levels": {"STU": "Entered"}}


def test_a_save_folder_inside_another_of_the_same_game_is_kept_once(profile, game, tmp_path):
    inner = game / "Saved" / "SaveGames"
    inner.mkdir(parents=True)
    profile.install_pack(write_pack(tmp_path / "rules", pack_id="oldsins-saves", game_id="oldsins",
                                    saves=[{"id": "slot", "root": "LOCALLOW", "path": "Studio/Old Sins/Saved/SaveGames",
                                            "pattern": "*.sav", "format": "gvas"}]))
    savefile.allow(profile, "oldsins-saves", "slot")                  # found by name, and declared by rules
    assert savekeeper.save_folders(profile)["oldsins"] == [game]


# ---- where saves are kept: the Saves window, moving them, another computer ----------------

def test_a_path_under_another_users_home_is_read_as_this_users(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "me"))
    me = tmp_path / "me"
    if os.name == "nt":
        assert savekeeper.here(r"C:\Users\Old\AppData\LocalLow\Fireproof Studios\Old Sins") == \
            me / "AppData" / "LocalLow" / "Fireproof Studios" / "Old Sins"
        assert savekeeper.here("/home/old/.local/share/Game") == Path("/home/old/.local/share/Game")
    else:
        assert savekeeper.here("/home/old/.local/share/Game") == me / ".local" / "share" / "Game"
    assert savekeeper.here(r"D:\Games\Saves") == Path(r"D:\Games\Saves")              # not a home: unchanged
    assert savekeeper.here(str(me / "Saves")) == me / "Saves"
    public = r"C:\Users\Public\Documents\Steam\RUNE\1361320"                 # everyone's, never rebased
    assert savekeeper.here(public) == Path(public)


def test_saves_kept_on_another_computer_come_back_under_this_users_home(profile, game, monkeypatch, tmp_path):
    keeper = savekeeper.Keeper(profile)
    write_save(game, {"level": 9})
    snap = keeper.take("oldsins", game)
    snap["folder"] = (r"C:\Users\Old\AppData\LocalLow\Studio\Old Sins" if os.name == "nt"   # as the other PC wrote it
                      else "/home/old/AppData/LocalLow/Studio/Old Sins")
    (keeper.root / "oldsins" / "snapshots" / f"{snap['id']}.json").write_text(json.dumps(snap))
    (game / "slot.json").unlink()
    game.rmdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "me"))
    plan = keeper.restore("oldsins")
    landed = tmp_path / "me" / "AppData" / "LocalLow" / "Studio" / "Old Sins" / "slot.json"
    assert plan["folder"] == str(landed.parent) and json.loads(landed.read_text()) == {"level": 9}


def test_moving_kept_saves_copies_them_and_leaves_the_players_own_files(profile, game, tmp_path):
    write_save(game, {"level": 3})
    savekeeper.Keeper(profile).take("oldsins", game)
    old = savekeeper.keep_dir(profile)
    synced = tmp_path / "OneDrive" / "Game saves"
    synced.mkdir(parents=True)
    (synced / "notes.txt").write_text("mine")                                       # the player's own file
    assert savekeeper.move_to(profile, str(synced)) == synced
    assert savekeeper.keep_dir(profile) == synced and not (old / "oldsins").exists()
    assert savekeeper.Keeper(profile).snapshots("oldsins")[0]["files"]["slot.json"]
    write_save(game, {"level": 4})                                                  # kept in the new place from now on
    assert savekeeper.Keeper(profile).take("oldsins", game) and len(savekeeper.Keeper(profile).snapshots("oldsins")) == 2
    savekeeper.move_to(profile, "")                                                 # back to the profile folder
    assert savekeeper.keep_dir(profile) == old and len(savekeeper.Keeper(profile).snapshots("oldsins")) == 2
    assert (synced / "notes.txt").read_text() == "mine" and not (synced / "oldsins").exists()
    with pytest.raises(savekeeper.KeeperError):
        savekeeper.move_to(profile, str(old / "inside"))
    with pytest.raises(savekeeper.KeeperError):
        savekeeper.move_to(profile, "relative/folder")


def test_moving_into_a_folder_another_computer_filled_adds_to_it(profile, game, tmp_path):
    write_save(game, {"level": 3})
    savekeeper.Keeper(profile).take("oldsins", game)
    synced = tmp_path / "Dropbox"
    other = savekeeper.Keeper(profile, synced)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write_save(elsewhere, {"level": 1})
    other.take("townfall", elsewhere)                                               # the other PC's copies
    savekeeper.move_to(profile, str(synced))
    assert savekeeper.Keeper(profile).games() == ["oldsins", "townfall"]


def test_the_saves_window_shows_where_and_what_and_moves_them(profile, game, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from openachievements import local_saves
    from openachievements.local_app import create_local_app
    client = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    auth = {"X-OA-Token": "t" * 32}
    assert client.get("/v1/local/saves").status_code == 403
    write_save(game, {"level": 2})
    client.post("/v1/local/games/oldsins/keep", headers=auth)
    s = client.get("/v1/local/saves", headers=auth).json()
    assert s["on"] is True and s["default"] is True and s["folder"] == str(savekeeper.keep_dir(profile))
    assert [(g["game_id"], g["copies"], g["present"]) for g in s["games"]] == [("oldsins", 1, True)]
    assert s["stored"] > 0 and s["waiting"] == []
    assert client.post("/v1/local/saves", headers=auth, json={"on": False}).json()["on"] is False
    moved = client.post("/v1/local/saves", headers=auth, json={"folder": str(tmp_path / "Saves")}).json()
    assert moved["folder"] == str(tmp_path / "Saves") and moved["default"] is False and moved["games"]
    bad = client.post("/v1/local/saves", headers=auth, json={"folder": "nope"})
    assert bad.status_code == 400 and "full path" in bad.json()["detail"]["message"]
    opened = []
    monkeypatch.setattr(local_saves, "_open_folder", lambda p: opened.append(p))
    assert client.post("/v1/local/saves/open", headers=auth).json() == {"opened": True}
    assert opened == [tmp_path / "Saves"]
