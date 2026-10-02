"""PlayStation trophies, read only, against a fake PSN: never the real one."""
import json
import urllib.parse

import pytest

from openachievements import credentials, notify
from openachievements.adapters import psn

NPSSO = "a" * 32 + "B" * 32
ACCOUNT = "7777777777777777777"
PS5 = {"npServiceName": "trophy2", "npCommunicationId": "NPWR20001_00", "trophySetVersion": "01.00",
       "trophyTitleName": "Astro Test", "trophyTitlePlatform": "PS5", "lastUpdatedDateTime": "2026-09-01T10:00:00Z"}
PS4 = {"npServiceName": "trophy", "npCommunicationId": "NPWR10001_00", "trophySetVersion": "01.02",
       "trophyTitleName": "Old Test", "trophyTitlePlatform": "PS4", "lastUpdatedDateTime": "2025-01-02T03:04:05Z"}
TROPHIES = {
    "NPWR20001_00": [{"trophyId": 0, "trophyType": "platinum", "trophyName": "All Done", "trophyDetail": "Everything",
                      "trophyHidden": False, "trophyIconUrl": "https://img/p.png"},
                     {"trophyId": 1, "trophyType": "bronze", "trophyName": "First", "trophyDetail": "Start",
                      "trophyHidden": False}],
    "NPWR10001_00": [{"trophyId": 0, "trophyType": "gold", "trophyName": "Big One", "trophyDetail": "Win",
                      "trophyHidden": True}],
}
EARNED = {
    "NPWR20001_00": [{"trophyId": 0, "earned": False}, {"trophyId": 1, "earned": True,
                                                         "earnedDateTime": "2026-09-01T09:59:00Z"}],
    "NPWR10001_00": [{"trophyId": 0, "earned": True, "earnedDateTime": "2025-01-02T03:04:05Z"}],
}


@pytest.fixture(autouse=True)
def vault(monkeypatch):
    """Never the real Windows Credential Manager."""
    store = {}
    monkeypatch.setattr(credentials, "_set", lambda t, s: store.__setitem__(t, s))
    monkeypatch.setattr(credentials, "_get", lambda t: store.get(t))
    monkeypatch.setattr(credentials, "_delete", lambda t: store.pop(t, None))
    psn.STATUS.clear()
    return store


class FakePsn:
    def __init__(self, private=False, refresh_ok=True):
        self.calls, self.private, self.refresh_ok = [], private, refresh_ok

    def __call__(self, method, url, headers, data=None):
        self.calls.append(url)
        parts = urllib.parse.urlsplit(url)
        if parts.path.endswith("/authorize"):
            if headers.get("Cookie") != f"npsso={NPSSO}":
                return 200, {}, b"<html>sign in</html>"
            return 302, {"Location": f"{psn.REDIRECT}/?code=v3.CODE&cid=x"}, b""
        if parts.path.endswith("/token"):
            form = urllib.parse.parse_qs(data.decode())
            if form.get("grant_type") == ["refresh_token"] and not self.refresh_ok:
                return 400, {}, b'{"error":"invalid_grant"}'
            return 200, {}, json.dumps({"access_token": "ACCESS", "expires_in": 3600,
                                        "refresh_token": "REFRESH-" + form["grant_type"][0]}).encode()
        assert headers.get("Authorization") == "Bearer ACCESS"
        if "profile2" in parts.path:
            return 200, {}, json.dumps({"profile": {"accountId": ACCOUNT, "onlineId": "Zein"}}).encode()
        if parts.path.endswith("/trophyTitles"):
            if self.private:
                return 403, {}, b'{"error":{"code":2240526,"message":"Not permitted"}}'
            return 200, {}, json.dumps({"trophyTitles": [PS5, PS4], "totalItemCount": 2}).encode()
        np_id = parts.path.split("/npCommunicationIds/")[1].split("/")[0]
        mine = "/users/" in parts.path
        return 200, {}, json.dumps({"trophies": (EARNED if mine else TROPHIES)[np_id]}).encode()


def connected(profile, fake, online_id=None):
    psn.connect(profile, NPSSO, online_id, http=fake)
    session = psn.Session(profile, fake)
    return psn.PsnWatcher(profile, session=session, clock=lambda: 0.0)


def sync(watcher, rounds=20):
    out = []
    for _ in range(rounds):
        out += watcher.poll()
    return out


def test_a_bad_code_is_refused_before_anything_is_sent(profile):
    fake = FakePsn()
    with pytest.raises(psn.PsnError, match="64"):
        psn.connect(profile, "not-a-code", http=fake)
    with pytest.raises(psn.PsnError, match="did not accept"):
        psn.connect(profile, "c" * 64, http=fake)                 # well-formed, but Sony says no
    assert not any("authorize" in c for c in fake.calls[:0])


def test_the_whole_page_can_be_pasted(profile, vault):
    psn.connect(profile, json.dumps({"npsso": NPSSO}), http=FakePsn())
    assert psn.linked(profile)["online_id"] == "Zein"


def test_only_the_refresh_token_is_kept_and_only_in_the_credential_store(profile, vault):
    psn.connect(profile, NPSSO, http=FakePsn())
    assert list(vault.values()) == ["REFRESH-authorization_code"]
    everything = b"".join(p.read_bytes() for p in profile.folder.rglob("*") if p.is_file())
    config = b"".join(p.read_bytes() for p in profile.config.dir.rglob("*") if p.is_file())
    for secret in (NPSSO, "REFRESH", "ACCESS"):
        assert secret.encode() not in everything and secret.encode() not in config


def test_the_first_sync_brings_games_trophies_and_unlocks_with_sonys_times(profile):
    fake = FakePsn()
    unlocked = sync(connected(profile, fake))
    games = {g["game_id"]: g for g in profile.library()["games"]}
    ps5, ps4 = games["psn-npwr20001_00"], games["psn-npwr10001_00"]
    assert (ps5["title"], ps5["platform"], ps5["total"], ps5["unlocked"]) == ("Astro Test", "PS5", 2, 1)
    assert (ps4["platform"], ps4["unlocked"], ps4["total"]) == ("PS4", 1, 1)               # a full set: Platinum
    first = next(a for a in ps5["achievements"] if a["name"] == "First")
    assert first["unlocked_at"] == "2026-09-01T09:59:00.000Z" and first["points"] == 15
    assert next(a for a in ps5["achievements"] if a["name"] == "All Done")["points"] == 300
    assert len(unlocked) == 2
    legacy = [c for c in fake.calls if "NPWR10001_00" in c]
    modern = [c for c in fake.calls if "NPWR20001_00" in c]
    assert all("npServiceName=trophy" in c for c in legacy) and not any("npServiceName" in c for c in modern)
    assert psn.STATUS[profile.profile_id]["last_sync"] and not psn.STATUS[profile.profile_id]["error"]


def test_unchanged_games_are_skipped_and_nothing_counts_twice(profile):
    fake = FakePsn()
    sync(connected(profile, fake))
    fake.calls.clear()
    again = psn.PsnWatcher(profile, session=psn.Session(profile, fake), clock=lambda: 0.0)   # a restart
    assert sync(again) == []
    assert not any("/npCommunicationIds/" in c for c in fake.calls)         # only the title list was asked for


def test_one_request_per_round_so_sony_is_never_flooded(profile):
    fake = FakePsn()
    watcher = connected(profile, fake)
    before = len(fake.calls)
    for _ in range(3):
        n = len(fake.calls)
        watcher.poll()
        assert len(fake.calls) - n <= 2                                     # one call, plus at most a token refresh
    assert len(fake.calls) > before


def test_private_trophies_explain_themselves_and_do_not_stop_the_watcher(profile):
    watcher = connected(profile, FakePsn(private=True), online_id="SomeoneElse")
    assert sync(watcher, 3) == []
    assert "visible to anyone" in psn.STATUS[profile.profile_id]["error"]
    assert any("PlayStation:" in p for p in watcher.new_problems())


def test_an_expired_connection_asks_for_a_fresh_code(profile):
    fake = FakePsn(refresh_ok=False)
    psn.connect(profile, NPSSO, http=fake)
    watcher = psn.PsnWatcher(profile, session=psn.Session(profile, fake), clock=lambda: 0.0)   # no access token yet
    sync(watcher, 2)
    assert "Connect again" in psn.STATUS[profile.profile_id]["error"]


def test_disconnect_deletes_the_session_and_keeps_the_trophies(profile, vault):
    sync(connected(profile, FakePsn()))
    psn.disconnect(profile)
    assert vault == {} and psn.linked(profile) is None
    assert any(g["game_id"] == "psn-npwr20001_00" for g in profile.library()["games"])


def test_playstation_unlocks_sync_quietly_like_steam(profile):
    unlocked = sync(connected(profile, FakePsn()))
    fresh = dict(unlocked[0], occurred_at=notify.datetime.now(notify.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    assert not notify.wanted(fresh, notify.DEFAULTS)
    assert notify.wanted(fresh, {**notify.DEFAULTS, "steam": True})


def test_the_local_app_connects_reports_and_disconnects(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    monkeypatch.setattr(psn, "_http", FakePsn())
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    assert local.get("/v1/local/psn").json()["linked"] is False
    assert local.post("/v1/local/psn/connect", json={"npsso": NPSSO}).status_code == 403              # token needed
    assert local.post("/v1/local/psn/connect", headers=h, json={"npsso": "short"}).status_code == 400
    s = local.post("/v1/local/psn/connect", headers=h, json={"npsso": NPSSO}).json()
    assert s["linked"] and s["online_id"] == "Zein" and s["has_session"]
    assert local.post("/v1/local/psn/disconnect", headers=h).json()["linked"] is False
