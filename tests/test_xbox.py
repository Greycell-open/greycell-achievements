"""Xbox achievements, read only, against a fake Microsoft and Xbox Live: never the real ones."""
import base64
import hashlib
import json
import urllib.parse

import pytest

from openachievements import credentials, notify
from openachievements.adapters import xbox

CLIENT = "1a2b3c4d-1234-5678-9abc-0123456789ab"
XUID = "2533274800000000"
MODERN = {"titleId": "1234567", "name": "Halo Test", "devices": ["XboxSeries", "PC"],
          "achievement": {"currentAchievements": 1, "totalAchievements": 2, "currentGamerscore": 10},
          "titleHistory": {"lastTimePlayed": "2026-09-30T20:00:00Z"}}
X360 = {"titleId": "1161890128", "name": "Old Test", "devices": ["Xbox360"],
        "achievement": {"currentAchievements": 1, "totalAchievements": 2, "currentGamerscore": 20}}
NONE = {"titleId": "42", "name": "App", "devices": ["XboxOne"], "achievement": {"totalAchievements": 0}}


@pytest.fixture(autouse=True)
def vault(monkeypatch):
    store = {}
    monkeypatch.setattr(credentials, "_set", lambda t, s: store.__setitem__(t, s))
    monkeypatch.setattr(credentials, "_get", lambda t: store.get(t))
    monkeypatch.setattr(credentials, "_delete", lambda t: store.pop(t, None))
    xbox.STATUS.clear()
    return store


class FakeXbox:
    def __init__(self, xerr=None, throttle=0):
        self.calls, self.xerr, self.throttle, self.verifier = [], xerr, throttle, None
        self.challenge = None

    def __call__(self, method, url, headers, data=None):
        self.calls.append(url)
        parts = urllib.parse.urlsplit(url)
        if parts.path.endswith("/oauth2/v2.0/token"):
            form = {k: v[0] for k, v in urllib.parse.parse_qs(data.decode()).items()}
            assert form["client_id"] == CLIENT and form["scope"] == xbox.SCOPE
            if form["grant_type"] == "authorization_code":
                digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
                if form["code"] != "GOOD" or digest.decode() != self.challenge:
                    return 400, {}, b'{"error":"invalid_grant"}'
            return 200, {}, json.dumps({"access_token": "MS", "refresh_token": "MSREFRESH-" + form["grant_type"]}).encode()
        if url == xbox.USER_AUTH:
            assert json.loads(data)["Properties"]["RpsTicket"] == "d=MS"
            return 200, {}, b'{"Token":"USER"}'
        if url == xbox.XSTS:
            if self.xerr:
                return 401, {}, json.dumps({"XErr": self.xerr}).encode()
            return 200, {}, json.dumps({"Token": "XSTS", "DisplayClaims": {"xui": [{"uhs": "UHS", "xid": XUID,
                                                                                     "gtg": "ZeinTag"}]}}).encode()
        assert headers["Authorization"] == "XBL3.0 x=UHS;XSTS"
        if self.throttle:
            self.throttle -= 1
            return 429, {"Retry-After": "30"}, b""
        if "titlehub" in url:
            return 200, {}, json.dumps({"titles": [MODERN, X360, NONE]}).encode()
        title = urllib.parse.parse_qs(parts.query)["titleId"][0]
        contract = headers["x-xbl-contract-version"]
        if title == "1234567":
            assert contract == "2"
            return 200, {}, json.dumps({"achievements": [
                {"id": "1", "name": "First Steps", "description": "Start", "isSecret": False, "progressState": "Achieved",
                 "progression": {"timeUnlocked": "2026-09-30T19:55:00Z"},
                 "rewards": [{"type": "Gamerscore", "value": "10"}],
                 "mediaAssets": [{"type": "Icon", "url": "https://img/1.png"}]},
                {"id": "2", "name": "Secret End", "lockedDescription": "Hidden", "isSecret": True,
                 "progressState": "NotStarted", "rewards": [{"type": "Gamerscore", "value": "90"}]}]}).encode()
        assert contract == "1"
        if parts.path.endswith("/titleachievements"):
            return 200, {}, json.dumps({"achievements": [
                {"id": 1, "name": "Old One", "description": "Did it", "gamerscore": 20, "isSecret": False},
                {"id": 2, "name": "Old Two", "description": "Not yet", "gamerscore": 30, "isSecret": False}]}).encode()
        return 200, {}, json.dumps({"achievements": [
            {"id": 1, "name": "Old One", "gamerscore": 20, "unlocked": True, "timeUnlocked": "2012-05-01T10:00:00Z"}]}).encode()


def signed_in(profile, fake):
    xbox.set_client(profile, CLIENT)
    signin = xbox.SignIn()
    url = signin.start(profile, "http://localhost:8788/v1/local/xbox/return")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    fake.challenge = q["code_challenge"]
    signin.finish(profile, {"code": "GOOD", "state": q["state"]}, http=fake)
    return signin, q


def watcher(profile, fake, clock):
    return xbox.XboxWatcher(profile, session=xbox.Session(profile, fake), clock=clock)


def run(w, rounds=20, step=11.0, start=0.0):
    t = [start]
    w.clock = lambda: t[0]
    out = []
    for _ in range(rounds):
        out += w.poll()
        t[0] += step
    return out


def test_only_a_real_client_id_is_accepted(profile):
    with pytest.raises(xbox.XboxError, match="Application"):
        xbox.set_client(profile, "my-app")
    assert xbox.set_client(profile, CLIENT.upper()) == CLIENT


def test_signing_in_is_microsofts_with_pkce_and_a_single_use_state(profile, vault):
    fake = FakeXbox()
    signin, q = signed_in(profile, fake)
    assert q["client_id"] == CLIENT and q["code_challenge_method"] == "S256" and q["scope"] == xbox.SCOPE
    assert q["redirect_uri"] == "http://localhost:8788/v1/local/xbox/return"
    assert xbox.linked(profile)["gamertag"] == "ZeinTag" and xbox.linked(profile)["xuid"] == XUID
    assert list(vault.values()) == ["MSREFRESH-authorization_code"]
    with pytest.raises(xbox.XboxError, match="not started here"):
        signin.finish(profile, {"code": "GOOD", "state": q["state"]}, http=fake)       # a state works once
    everything = b"".join(p.read_bytes() for p in profile.folder.rglob("*") if p.is_file())
    config = b"".join(p.read_bytes() for p in profile.config.dir.rglob("*") if p.is_file())
    for secret in (b"MSREFRESH", b"XSTS", b"UHS"):
        assert secret not in everything and secret not in config


def test_an_account_without_xbox_is_explained(profile):
    with pytest.raises(xbox.XboxError, match="no Xbox profile"):
        signed_in(profile, FakeXbox(xerr=2148916233))


def test_modern_and_360_titles_come_in_with_gamerscore_and_xboxs_times(profile):
    fake = FakeXbox()
    signed_in(profile, fake)
    unlocked = run(watcher(profile, fake, None))
    games = {g["game_id"]: g for g in profile.library()["games"]}
    halo, old = games["xbox-1234567"], games["xbox-1161890128"]
    assert (halo["title"], halo["total"], halo["unlocked"], halo["points_total"]) == ("Halo Test", 2, 1, 100)
    first = next(a for a in halo["achievements"] if a["name"] == "First Steps")
    assert first["unlocked_at"] == "2026-09-30T19:55:00.000Z" and first["points"] == 10
    assert (old["platform"], old["total"], old["unlocked"]) == ("Xbox 360", 2, 1)
    assert "xbox-42" not in games                                           # no achievements, not a game here
    assert len(unlocked) == 2


def test_unchanged_titles_are_skipped_and_nothing_counts_twice(profile):
    fake = FakeXbox()
    signed_in(profile, fake)
    run(watcher(profile, fake, None))
    fake.calls.clear()
    assert run(watcher(profile, fake, None)) == []
    assert not any("achievements.xboxlive.com" in c for c in fake.calls)


def test_when_xbox_says_slow_down_it_waits_and_keeps_its_place(profile):
    fake = FakeXbox(throttle=1)
    signed_in(profile, fake)
    w = watcher(profile, fake, None)
    t = [0.0]
    w.clock = lambda: t[0]
    w.poll()                                                                # 429
    assert "slow down" in xbox.STATUS[profile.profile_id]["error"]
    n = len(fake.calls)
    t[0] = 20.0
    w.poll()
    assert len(fake.calls) == n                                             # still waiting out Retry-After
    t[0] = 31.0
    w.poll()
    assert len(fake.calls) > n


def test_disconnect_deletes_the_session_and_keeps_the_achievements(profile, vault):
    fake = FakeXbox()
    signed_in(profile, fake)
    run(watcher(profile, fake, None))
    xbox.disconnect(profile)
    assert vault == {} and xbox.linked(profile) is None
    assert any(g["game_id"] == "xbox-1234567" for g in profile.library()["games"])


def test_xbox_unlocks_sync_quietly(profile):
    fake = FakeXbox()
    signed_in(profile, fake)
    unlocked = run(watcher(profile, fake, None))
    fresh = dict(unlocked[0], occurred_at=notify.datetime.now(notify.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    assert not notify.wanted(fresh, notify.DEFAULTS)


def test_the_local_app_saves_the_id_starts_sign_in_and_refuses_a_stranger(profile):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788",
                       follow_redirects=False)
    h = {"X-OA-Token": "t" * 32}
    assert local.post("/v1/local/xbox/client", json={"client_id": CLIENT}).status_code == 403
    assert local.post("/v1/local/xbox/client", headers=h, json={"client_id": "nope"}).status_code == 400
    assert local.post("/v1/local/xbox/client", headers=h, json={"client_id": CLIENT}).json()["client_id"] == CLIENT
    go = local.get("/v1/local/xbox/signin")
    assert go.headers["location"].startswith(xbox.MS + "/authorize?")
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8788%2Fv1%2Flocal%2Fxbox%2Freturn" in go.headers["location"]
    back = local.get("/v1/local/xbox/return", params={"code": "x", "state": "forged"})
    assert back.headers["location"].startswith("/?xbox_error=")
