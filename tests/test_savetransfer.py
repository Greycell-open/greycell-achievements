"""Moving saves between installs of the same game: dry run, the move, its
checks, and undo."""
import json
import os
import time
from pathlib import Path

import pytest

from openachievements import savekeeper, savetransfer as st
from openachievements import steamfiles as sf
from openachievements.adapters import autodetect as ad
from openachievements.adapters import savefile


@pytest.fixture
def two_installs(profile, tmp_path, monkeypatch):
    """A repacked copy (GSE saves) linked into the Steam game, and Steam Cloud's folder for it."""
    roaming = tmp_path / "Roaming"
    steam = tmp_path / "Steam"
    monkeypatch.setattr(savefile, "root_folder", lambda name: roaming if name == "APPDATA" else None)
    monkeypatch.setattr(sf, "steam_dir", lambda: steam)
    profile.register_game("steam-367520", "Hollow Knight")
    profile.register_game("local-hollow-knight", "Hollow Knight")
    profile.link_games("local-hollow-knight", "steam-367520")
    gse = roaming / "GSE Saves" / "367520" / "remote"
    gse.mkdir(parents=True)
    cloud = steam / "userdata" / "117930076" / "367520" / "remote"
    (steam / "userdata" / "117930076").mkdir(parents=True)
    return {"gse": gse, "cloud": cloud}


def write(folder: Path, name: str, data: bytes, age: float) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def test_every_install_of_a_linked_game_is_listed(profile, two_installs):
    write(two_installs["gse"], "user1.dat", b"repack progress", 600)
    places = {p["folder"]: p for p in st.installs(profile, "local-hollow-knight")}
    assert places[str(two_installs["gse"])]["label"] == "GSE emulator" and places[str(two_installs["gse"])]["files"] == 1
    cloud = places[str(two_installs["cloud"])]
    assert cloud["label"] == "Steam Cloud" and cloud["synced"] and not cloud["exists"]   # Steam has not saved yet
    assert st.group(profile, "local-hollow-knight") == ["steam-367520", "local-hollow-knight"]


def test_a_repack_save_moves_to_steam_checked_and_undone(profile, two_installs):
    write(two_installs["gse"], "user1.dat", b"repack progress", 600)
    plan = st.plan(profile, "steam-367520", str(two_installs["cloud"]), folder=str(two_installs["gse"]))
    assert [(w["file"], w["action"]) for w in plan["writes"]] == [("user1.dat", "add")]
    assert [w["code"] for w in plan["warnings"]] == ["store_synced"]
    assert not two_installs["cloud"].exists()                                   # a dry run writes nothing
    done = st.transfer(profile, "steam-367520", str(two_installs["cloud"]), folder=str(two_installs["gse"]))
    assert (two_installs["cloud"] / "user1.dat").read_bytes() == b"repack progress"
    assert done["kept_before"] is None
    st.undo(profile, "steam-367520", done["transfer"])
    assert not (two_installs["cloud"] / "user1.dat").exists()                    # what the move added is gone
    with pytest.raises(st.TransferError, match="already undone"):
        st.undo(profile, "steam-367520", done["transfer"])


def test_a_newer_destination_is_never_replaced_silently(profile, two_installs):
    write(two_installs["gse"], "user1.dat", b"old", 3600)
    write(two_installs["cloud"], "user1.dat", b"newer steam progress", 60)
    plan = st.plan(profile, "steam-367520", str(two_installs["cloud"]), folder=str(two_installs["gse"]))
    assert "destination_newer" in [w["code"] for w in plan["warnings"]]
    with pytest.raises(st.TransferError, match="more recently"):
        st.transfer(profile, "steam-367520", str(two_installs["cloud"]), folder=str(two_installs["gse"]))
    assert (two_installs["cloud"] / "user1.dat").read_bytes() == b"newer steam progress"
    done = st.transfer(profile, "steam-367520", str(two_installs["cloud"]), folder=str(two_installs["gse"]),
                       allow_newer=True)
    assert (two_installs["cloud"] / "user1.dat").read_bytes() == b"old"
    assert done["kept_before"]                                                   # Steam's progress was kept first
    st.undo(profile, "steam-367520", done["transfer"])
    assert (two_installs["cloud"] / "user1.dat").read_bytes() == b"newer steam progress"


def test_undo_refuses_once_the_game_has_saved_over_the_move(profile, two_installs):
    write(two_installs["gse"], "user1.dat", b"moved", 600)
    done = st.transfer(profile, "steam-367520", str(two_installs["cloud"]), folder=str(two_installs["gse"]))
    write(two_installs["cloud"], "user1.dat", b"played on after the move", 0)
    with pytest.raises(st.TransferError, match="saved since the move"):
        st.undo(profile, "steam-367520", done["transfer"])
    assert (two_installs["cloud"] / "user1.dat").read_bytes() == b"played on after the move"


def test_nothing_moves_while_the_game_runs_and_files_stay_inside(profile, two_installs):
    write(two_installs["gse"], "user1.dat", b"x", 600)
    with pytest.raises(st.TransferError, match="running"):
        st.transfer(profile, "steam-367520", str(two_installs["cloud"]), folder=str(two_installs["gse"]),
                    running=True)
    assert not two_installs["cloud"].exists()
    with pytest.raises(st.TransferError, match="same folder"):
        st.plan(profile, "steam-367520", str(two_installs["gse"]), folder=str(two_installs["gse"]))


def test_a_kept_copy_can_be_the_source_and_different_files_are_flagged(profile, two_installs):
    write(two_installs["gse"], "user1.dat", b"kept", 600)
    keeper = savekeeper.Keeper(profile)
    snap = keeper.take("steam-367520", two_installs["gse"])
    write(two_installs["cloud"], "profile.sav", b"another layout", 3600 * 24)
    plan = st.plan(profile, "local-hollow-knight", str(two_installs["cloud"]), snapshot=snap["id"])
    assert "different_files" in [w["code"] for w in plan["warnings"]] and plan["left_alone"] == ["profile.sav"]
    assert st.sources(profile, "local-hollow-knight")[0]["id"] == snap["id"]


def test_the_cli_lists_installs_moves_and_undoes(profile, two_installs, home, machine, capsys):
    from openachievements import cli
    write(two_installs["gse"], "user1.dat", b"via cli", 600)
    base = ["--home", str(home), "--config", str(machine.dir), "save"]
    assert cli.main(base + ["installs", "steam-367520"]) == 0
    listed = capsys.readouterr().out
    assert "GSE emulator" in listed and "Steam Cloud" in listed
    number = next(line.split(".")[0] for line in listed.splitlines() if "Steam Cloud" in line)
    assert cli.main(base + ["transfer", "steam-367520", "--from", str(two_installs["gse"]), "--to", number,
                            "--dry-run"]) == 0
    assert "Would move" in capsys.readouterr().out and not two_installs["cloud"].exists()
    assert cli.main(base + ["transfer", "steam-367520", "--from", str(two_installs["gse"]), "--to", number]) == 0
    out = capsys.readouterr().out
    assert (two_installs["cloud"] / "user1.dat").read_bytes() == b"via cli"
    transfer_id = out.split("save undo steam-367520 ")[1].split()[0]
    assert cli.main(base + ["undo", "steam-367520", transfer_id]) == 0
    assert not (two_installs["cloud"] / "user1.dat").exists()


def test_the_page_checks_moves_and_undoes(profile, two_installs, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements import watching
    from openachievements.local_app import create_local_app
    monkeypatch.setattr(watching, "LIVE", {"playing": [], "at": 1.0})
    write(two_installs["gse"], "user1.dat", b"via page", 600)
    client = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    assert client.get("/v1/local/games/steam-367520/installs").status_code == 403
    r = client.get("/v1/local/games/steam-367520/installs", headers=h).json()
    to = next(p["folder"] for p in r["installs"] if p["label"] == "Steam Cloud")
    body = {"to": to, "folder": str(two_installs["gse"])}
    plan = client.post("/v1/local/games/steam-367520/transfer", headers=h, json=body).json()
    assert plan["writes"][0]["action"] == "add" and not two_installs["cloud"].exists()
    done = client.post("/v1/local/games/steam-367520/transfer", headers=h, json={**body, "dry_run": False}).json()
    assert (two_installs["cloud"] / "user1.dat").read_bytes() == b"via page"
    u = client.post(f"/v1/local/games/steam-367520/transfer/{done['transfer']}/undo", headers=h)
    assert u.status_code == 200 and not (two_installs["cloud"] / "user1.dat").exists()
    kept = client.get("/v1/local/games/steam-367520/kept", headers=h).json()   # linked copies show on the game
    assert any(sn["folder"] == str(two_installs["gse"]) for sn in kept["snapshots"])


def test_the_saves_panel_covers_games_linked_into_it(profile, two_installs, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from openachievements import watching
    from openachievements.local_app import create_local_app
    monkeypatch.setattr(watching, "LIVE", {"playing": [], "at": 1.0})
    local = tmp_path / "LocalLow" / "Team Cherry" / "Hollow Knight"
    write(local, "user1.dat", b"drm-free", 600)
    with profile.config.editing() as config:
        config.setdefault("save_candidates", {})[f"{profile.profile_id}:local-hollow-knight"] = [str(local)]
    client = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    assert client.get("/v1/local/games/steam-367520/kept", headers=h).json()["folders"] == [str(local)]
    assert client.post("/v1/local/games/steam-367520/keep", headers=h).json() == {"taken": 1}
    snap = client.get("/v1/local/games/steam-367520/kept", headers=h).json()["snapshots"][0]
    write(local, "user1.dat", b"changed", 0)
    r = client.post("/v1/local/games/steam-367520/restore", headers=h, json={"snapshot": snap["id"]})
    assert r.status_code == 200 and (local / "user1.dat").read_bytes() == b"drm-free"


def test_repack_saves_are_kept_for_steam_games(profile, two_installs, monkeypatch):
    from openachievements.adapters import stores
    write(two_installs["gse"], "user1.dat", b"x", 600)
    monkeypatch.setattr(stores, "everything", lambda: [])
    monkeypatch.setattr(stores, "itch_db", lambda: None)
    d = ad.AutoDetector(profile)
    d._matcher = ad.TitleMatcher(None, {})
    d.discover_store_saves()
    assert ad.store_saves(profile, "steam-367520") == [str(two_installs["gse"])]
