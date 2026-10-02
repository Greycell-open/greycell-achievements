"""Sync, driven through the real FastAPI app (acceptance steps 1-19)."""
import pytest
from fastapi.testclient import TestClient

from openachievements import reducer
from openachievements.profile import MachineConfig, Profile
from openachievements.server.app import Settings, create_app
from openachievements.server.store import Store
from openachievements.sync import SyncClient, SyncError, check_url, connect, restore
from conftest import write_pack

SERVER = "http://localhost:8787"


class FakeServer:
    """The real app in-process, with a switch to take it offline."""

    def __init__(self, tmp_path, **settings):
        s = Settings()
        for k, v in settings.items():
            setattr(s, k, v)
        self.client = TestClient(create_app(Store(tmp_path / "server.sqlite"), s))
        self.down = False

    def transport(self, base_url, token=None):
        server = self

        class T:
            def __init__(self):
                self.base_url = check_url(base_url)
                self.token = token

            def request(self, method, path, body=None):
                if server.down:
                    raise SyncError("unreachable", "server is down")
                headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
                resp = server.client.request(method, path, json=body, headers=headers)
                if resp.status_code >= 400:
                    detail = resp.json().get("detail", {})
                    raise SyncError(detail.get("code", "http_error"), detail.get("message", ""), resp.status_code)
                return resp.json() if resp.content else {}
        return T()


@pytest.fixture
def server(tmp_path):
    return FakeServer(tmp_path)


def client_for(server, profile):
    settings = profile.config.sync_settings(profile.profile_id)
    return SyncClient(profile, server.transport(settings["server"], settings["token"]))


def test_offline_unlock_survives_rebuild_then_syncs_to_a_second_device_exactly_once(tmp_path, server):
    # 1-2: profile and pack on device 1
    d1 = Profile.create("Zein", home=tmp_path / "d1", config=MachineConfig(tmp_path / "m1"))
    d1.install_pack(write_pack(tmp_path / "pack"))
    # 3-6: unlock with no server at all, then "restart"
    d1.record_unlock("test-pack:first-step", adapter="test")
    d1 = Profile(d1.folder, d1.config)
    assert reducer.is_unlocked(d1.state(), "test-pack:first-step")
    # 7-9: delete the index and rebuild from files
    d1.index.path.unlink()
    assert d1.index.rebuild() == []
    assert reducer.is_unlocked(d1.state(), "test-pack:first-step")
    # 10-11: connect and sync
    connect(d1, SERVER, "zein", "correct horse battery", register=True, transport_factory=server.transport)
    first = client_for(server, d1).run()
    assert first.uploaded == len(d1.log.read().events) and not first.rejected
    # 12-14: second device restores the same account
    d2 = restore(SERVER, "zein", "correct horse battery", tmp_path / "d2", MachineConfig(tmp_path / "m2"),
                 transport_factory=server.transport)
    assert d2.profile_id == d1.profile_id and d2.device_id != d1.device_id
    unlocks = [e for e in d2.log.read().events if e["event_type"] == "achievement.unlocked"]
    assert len(unlocks) == 1
    assert reducer.is_unlocked(d2.state(), "test-pack:first-step")
    # 15-19: server down, unlock on device 2, server back, both converge
    server.down = True
    d2.record_progress("test-pack:collector", 3, adapter="test")
    with pytest.raises(SyncError):
        client_for(server, d2).run()
    server.down = False
    client_for(server, d2).run()
    client_for(server, d1).run()
    client_for(server, d2).run()
    lib1, lib2 = d1.library(), d2.library()
    assert lib1["games"] == lib2["games"]
    assert lib1["totals"]["unlocked"] == 2
    assert len(d1.log.read().events) == len(d2.log.read().events)


def test_resending_everything_is_harmless(tmp_path, server):
    p = Profile.create("Zein", home=tmp_path / "d1", config=MachineConfig(tmp_path / "m1"))
    connect(p, SERVER, "zein", "correct horse battery", register=True, transport_factory=server.transport)
    client_for(server, p).run()
    (p.config.dir / "sync" / f"{p.profile_id}.json").unlink()   # forget what was sent
    again = client_for(server, p).run()
    assert not again.rejected
    events = server.client.get("/v1/export", headers={"Authorization": f"Bearer {p.config.sync_settings(p.profile_id)['token']}"})
    assert len(events.text.strip().splitlines()) == len(p.log.read().events)


def test_a_tampered_event_is_rejected_with_a_reason_and_nothing_else_is_lost(tmp_path, server):
    p = Profile.create("Zein", home=tmp_path / "d1", config=MachineConfig(tmp_path / "m1"))
    session = connect(p, SERVER, "zein", "correct horse battery", register=True, transport_factory=server.transport)
    good = p.log.read().events
    bad = dict(good[0], payload={"name": "forged"})
    reply = server.client.post("/v1/sync", headers={"Authorization": f"Bearer {session['token']}"}, json={
        "protocol_version": "1.0", "profile_id": p.profile_id, "device_id": p.device_id,
        "cursor": None, "events": [bad, good[1]]}).json()
    assert reply["accepted_event_ids"] == [good[1]["event_id"]]
    assert reply["rejected_events"][0]["code"] == "bad_hash"


def test_an_account_cannot_sync_someone_elses_profile(tmp_path, server):
    a = Profile.create("A", home=tmp_path / "a", config=MachineConfig(tmp_path / "ma"))
    b = Profile.create("B", home=tmp_path / "b", config=MachineConfig(tmp_path / "mb"))
    connect(a, SERVER, "alice", "correct horse battery", register=True, transport_factory=server.transport)
    client_for(server, a).run()
    connect(b, SERVER, "bobby", "correct horse battery", register=True, transport_factory=server.transport)
    token = b.config.sync_settings(b.profile_id)["token"]
    resp = server.client.post("/v1/sync", headers={"Authorization": f"Bearer {token}"}, json={
        "protocol_version": "1.0", "profile_id": a.profile_id, "device_id": b.device_id, "cursor": None, "events": []})
    assert resp.status_code == 409


def test_remote_servers_must_use_https():
    with pytest.raises(SyncError):
        check_url("http://example.com")
    assert check_url("https://example.com/") == "https://example.com"
    assert check_url("http://localhost:8787") == "http://localhost:8787"


def test_revoked_device_is_signed_out(tmp_path, server):
    p = Profile.create("Zein", home=tmp_path / "d1", config=MachineConfig(tmp_path / "m1"))
    session = connect(p, SERVER, "zein", "correct horse battery", register=True, transport_factory=server.transport)
    auth = {"Authorization": f"Bearer {session['token']}"}
    assert server.client.delete(f"/v1/devices/{p.device_id}", headers=auth).json()["revoked_tokens"] == 1
    assert server.client.get("/v1/me", headers=auth).status_code == 401


def test_deleting_the_hosted_account_leaves_the_local_profile(tmp_path, server):
    p = Profile.create("Zein", home=tmp_path / "d1", config=MachineConfig(tmp_path / "m1"))
    p.install_pack(write_pack(tmp_path / "pack"))
    p.record_unlock("test-pack:first-step", adapter="test")
    session = connect(p, SERVER, "zein", "correct horse battery", register=True, transport_factory=server.transport)
    client_for(server, p).run()
    auth = {"Authorization": f"Bearer {session['token']}"}
    assert server.client.request("DELETE", "/v1/accounts/me", headers=auth,
                                 json={"password": "correct horse battery"}).status_code == 204
    assert server.client.post("/v1/sessions", json={"username": "zein", "password": "correct horse battery"}).status_code == 401
    assert reducer.is_unlocked(Profile(p.folder, p.config).state(), "test-pack:first-step")


def test_profiles_are_private_until_made_public(tmp_path, server):
    p = Profile.create("Zein", home=tmp_path / "d1", config=MachineConfig(tmp_path / "m1"))
    session = connect(p, SERVER, "zein", "correct horse battery", register=True, transport_factory=server.transport)
    client_for(server, p).run()
    assert server.client.get("/v1/public/zein").status_code == 404
    server.client.put("/v1/privacy", headers={"Authorization": f"Bearer {session['token']}"}, json={"public": True})
    assert server.client.get("/v1/public/zein").json()["totals"]["games"] == 0


def test_repeated_bad_passwords_are_throttled(tmp_path):
    server = FakeServer(tmp_path, login_attempts=3)
    server.client.post("/v1/accounts", json={"username": "zein", "password": "correct horse battery"})
    codes = [server.client.post("/v1/sessions", json={"username": "zein", "password": "nope"}).status_code
             for _ in range(4)]
    assert codes == [401, 401, 401, 429]


def test_quota_is_reported_not_silently_dropped(tmp_path):
    server = FakeServer(tmp_path, max_events=3)
    p = Profile.create("Zein", home=tmp_path / "d1", config=MachineConfig(tmp_path / "m1"))
    p.install_pack(write_pack(tmp_path / "pack"))
    connect(p, SERVER, "zein", "correct horse battery", register=True, transport_factory=server.transport)
    result = client_for(server, p).run()
    assert result.uploaded == 3
    assert any(r["code"] == "quota_exceeded" for r in result.rejected)


def test_registration_can_be_closed_for_a_private_server(tmp_path):
    server = FakeServer(tmp_path, registration="first-only")
    assert server.client.post("/v1/accounts", json={"username": "owner", "password": "correct horse battery"}).status_code == 201
    assert server.client.post("/v1/accounts", json={"username": "other", "password": "correct horse battery"}).status_code == 403


def test_the_sync_token_lives_in_the_credential_store_not_machine_json(tmp_path, _private_credential_store):
    from openachievements.profile import MachineConfig
    cfg = MachineConfig(tmp_path / "m")
    cfg.save({"sync": {"p1": {"server": "https://sync.example", "username": "z", "token": "OLD-SECRET"}}})
    assert cfg.sync_settings("p1")["token"] == "OLD-SECRET"                # an older file still works...
    assert "OLD-SECRET" not in cfg.path.read_text(encoding="utf-8")      # ...and is migrated out of the file
    cfg.set_sync("p1", {"server": "https://sync.example", "username": "z", "token": "NEW"})
    assert "NEW" not in cfg.path.read_text(encoding="utf-8") and cfg.sync_settings("p1")["token"] == "NEW"
    cfg.set_sync("p1", {})                                                # disconnect
    assert cfg.sync_settings("p1") == {} and not any("p1" in k for k in _private_credential_store)
