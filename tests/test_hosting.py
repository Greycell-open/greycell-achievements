"""The hosted instance: served under a path, personal pages, and the stats homepage."""
import pytest
from fastapi.testclient import TestClient

from openachievements import events as ev
from openachievements.profile import MachineConfig, Profile
from openachievements.server.app import create_app
from openachievements.server.store import Store
from openachievements.sync import connect
from conftest import write_pack
from test_sync import SERVER, FakeServer, client_for


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setenv("OA_BASE_PATH", "/openachievements/")
    monkeypatch.setenv("OA_SOURCE_URL", "https://example.org/app")
    return TestClient(create_app(Store(tmp_path / "s.sqlite")))


def test_the_page_works_under_a_path_and_at_a_personal_address(hosted):
    page = hosted.get("/").text
    assert '<base href="/openachievements/">' in page
    assert 'href="assets/style.css"' in page and '"/assets' not in page
    assert hosted.get("/u/zein").text == page                       # the client reads the name from the path
    assert hosted.get("/v1/mode").json()["source_url"] == "https://example.org/app"
    assert hosted.get("/openapi.json").json()["servers"][0]["url"] == "/openachievements"


def test_at_the_root_nothing_changes(tmp_path):
    client = TestClient(create_app(Store(tmp_path / "s.sqlite")))
    assert '<base href="/">' in client.get("/").text
    assert client.get("/v1/mode").json()["source_url"] is None


def account_with_unlocks(server, tmp_path, name, unlocks, public, manual=0):
    p = Profile.create(name, home=tmp_path / name, config=MachineConfig(tmp_path / f"m-{name}"))
    p.install_pack(write_pack(tmp_path / f"pack-{name}", achievements=[
        {"id": f"a{i}", "name": f"A{i}"} for i in range(unlocks + manual)]))
    for i in range(unlocks):
        p.record_unlock(f"test-pack:a{i}", adapter="test")
    for i in range(unlocks, unlocks + manual):                     # an older client's hand-made ones
        p.commit([ev.make_event("achievement.unlocked", profile_id=p.profile_id, device_id=p.device_id,
                                payload={"provenance": "manual"}, achievement_id=f"test-pack:a{i}")])
    connect(p, SERVER, name, "correct horse battery", register=True, transport_factory=server.transport)
    client_for(server, p).run()
    token = p.config.sync_settings(p.profile_id)["token"]
    server.client.put("/v1/privacy", headers={"Authorization": f"Bearer {token}"}, json={"public": public})


def test_the_homepage_counts_everyone_and_names_only_public_profiles(tmp_path):
    server = FakeServer(tmp_path)
    account_with_unlocks(server, tmp_path, "alice", 3, public=True, manual=2)
    account_with_unlocks(server, tmp_path, "bobby", 5, public=False)
    stats = server.client.get("/v1/stats").json()
    assert stats["accounts"] == 2 and stats["public_profiles"] == 1
    assert stats["unlocks"] == 8                                    # hand-made ones never count
    assert stats["top"] == [{"username": "alice", "unlocks": 3}]   # bobby is private: counted, never named
