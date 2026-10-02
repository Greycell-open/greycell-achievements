"""The CLI end to end, bundles, and the local UI's protections."""
import json

import pytest
from fastapi.testclient import TestClient

from openachievements.bundle import export_bundle, restore_bundle
from openachievements.cli import main
from openachievements.local_app import create_local_app
from openachievements.profile import MachineConfig, Profile, ProfileError
from openachievements.server.app import create_app
from openachievements.server.store import Store
from conftest import EXAMPLE_PACK


def run(capsys, home, config, *args):
    code = main(["--home", str(home), "--config", str(config), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_walkthrough_with_the_example_pack(capsys, tmp_path):
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    assert run(capsys, home, cfg, "profile", "create", "Zein")[0] == 0
    code, out, _ = run(capsys, home, cfg, "pack", "install", str(EXAMPLE_PACK))
    assert code == 0 and "8 achievements" in out
    code, out, _ = run(capsys, home, cfg, "--json", "game", "list")
    game = json.loads(out)[0]
    assert game["title"] == "Cave Story" and game["unlocked"] == 0
    code, out, _ = run(capsys, home, cfg, "achievement", "list")
    assert "[ ] cave-story-community:balrog" in out and "Hidden achievement" in out
    code, out, _ = run(capsys, home, cfg, "export", "--format", "csv")
    assert code == 0
    assert run(capsys, home, cfg, "index", "rebuild")[0] == 0
    code, out, _ = run(capsys, home, cfg, "doctor")
    assert "0 problem(s)" in out


def test_cli_errors_are_one_line_not_a_traceback(capsys, tmp_path):
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    run(capsys, home, cfg, "profile", "create", "Zein")
    code, _, err = run(capsys, home, cfg, "achievement", "revoke", "nope:nothing")
    assert code == 1 and err.startswith("error:")


def test_achievements_cannot_be_unlocked_by_hand(capsys, tmp_path, profile):
    """They unlock from what a game records, never from a button or a command."""
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    run(capsys, home, cfg, "profile", "create", "Zein")
    with pytest.raises(SystemExit):
        main(["--home", str(home), "--config", str(cfg), "achievement", "unlock", "x:y"])
    with pytest.raises(SystemExit):
        main(["--home", str(home), "--config", str(cfg), "progress", "add", "x:y"])
    client = TestClient(create_local_app(profile, token="t"))
    for path in ("/v1/local/unlock", "/v1/local/progress"):
        assert client.post(path, json={"key": "x:y"}, headers={"X-OA-Token": "t"}).status_code in (404, 405)


def test_bundle_round_trip_to_a_clean_install(profile, tmp_path):
    profile.install_pack(EXAMPLE_PACK)
    profile.record_unlock("cave-story-community:balrog", adapter="test")
    bundle = export_bundle(profile, tmp_path / "out.zip")
    restored = restore_bundle(bundle, tmp_path / "elsewhere", MachineConfig(tmp_path / "m9"))
    assert restored.profile_id == profile.profile_id
    assert restored.library()["totals"] == profile.library()["totals"]
    with pytest.raises(ProfileError):
        restore_bundle(bundle, tmp_path / "elsewhere", MachineConfig(tmp_path / "m9"))


def test_local_ui_refuses_changes_without_its_token(profile):
    profile.install_pack(EXAMPLE_PACK)
    client = TestClient(create_local_app(profile, token="t0k3n"))
    page = client.get("/").text
    assert 'content="t0k3n"' in page
    assert client.get("/v1/library").json()["totals"]["achievements"] == 8
    profile.register_game("celeste", "Celeste")
    body = {"game_id": "celeste", "status": "backlog"}
    assert client.post("/v1/local/status", json=body).status_code == 403
    assert client.post("/v1/local/status", json=body, headers={"X-OA-Token": "wrong"}).status_code == 403
    assert client.post("/v1/local/status", json=body, headers={"X-OA-Token": "t0k3n"}).status_code == 200


def test_local_ui_ignores_other_host_names(profile):
    client = TestClient(create_local_app(profile, token="t"), base_url="http://evil.example")
    assert client.get("/v1/library").status_code == 403


def test_server_serves_the_web_client_and_its_assets(tmp_path):
    client = TestClient(create_app(Store(tmp_path / "s.sqlite")))
    assert "Greycell Achievements" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/..%2Fserver%2Fapp.py").status_code == 404
    assert client.get("/v1/mode").json()["mode"] == "server"
    assert "/v1/sync" in client.get("/openapi.json").json()["paths"]
