"""Linking a Steam account: Sign in through Steam, and no key anywhere."""
import pathlib
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from openachievements import steamfiles
from openachievements.adapters import steam_account
from openachievements.local_app import create_local_app
from openachievements.profile import ProfileError
from test_autodetect import fake_steam_install

BASE = "http://127.0.0.1:8788/"
SID = "76561197960287930"
TOKEN = {"X-OA-Token": "t" * 32}


@pytest.fixture(autouse=True)
def no_real_steam(monkeypatch):
    """Keep this PC's real Steam folder out of the tests."""
    monkeypatch.setattr(steamfiles, "steam_dir", lambda: None)


def steams_answer(start_url, *, sid=SID, mode="id_res", identity=None):
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start_url).query))
    return_to = q["openid.return_to"]
    claimed = f"https://steamcommunity.com/openid/id/{sid}"
    params = {"openid.ns": q["openid.ns"], "openid.mode": mode, "openid.return_to": return_to,
              "openid.claimed_id": claimed, "openid.identity": identity or claimed,
              "openid.sig": "sig", "openid.signed": "signed"}
    params.update(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(return_to).query)))
    return params


def test_a_genuine_sign_in_gives_the_account_once():
    posted = []
    s = steam_account.SteamSignIn(post=lambda url, f: posted.append(f) or "ns:x\nis_valid:true\n")
    answer = steams_answer(s.start(BASE))
    assert s.finish(BASE, answer) == SID
    assert posted[0]["openid.mode"] == "check_authentication"
    with pytest.raises(ProfileError, match="not started here"):
        s.finish(BASE, answer)                               # a state is used once


@pytest.mark.parametrize("tamper, message", [
    (lambda a: a.update(state="made-up"), "not started here"),
    (lambda a: a.update({"openid.mode": "cancel"}), "cancelled"),
    (lambda a: a.update({"openid.return_to": "https://evil.example/cb"}), "another address"),
    (lambda a: a.update({"openid.claimed_id": "https://evil.example/id/1"}), "did not name"),
    (lambda a: a.update({"openid.identity": "https://steamcommunity.com/openid/id/76561197960000000"}), "did not name"),
])
def test_a_forged_or_odd_answer_is_refused(tamper, message):
    s = steam_account.SteamSignIn(post=lambda url, f: "is_valid:true")
    answer = steams_answer(s.start(BASE))
    tamper(answer)
    with pytest.raises(ProfileError, match=message):
        s.finish(BASE, answer)


def test_steam_must_confirm_and_a_slow_sign_in_expires():
    s = steam_account.SteamSignIn(post=lambda url, f: "is_valid:false")
    with pytest.raises(ProfileError, match="did not confirm"):
        s.finish(BASE, steams_answer(s.start(BASE)))
    now = [0.0]
    slow = steam_account.SteamSignIn(post=lambda url, f: "is_valid:true", clock=lambda: now[0])
    answer = steams_answer(slow.start(BASE))
    now[0] = steam_account.STATE_TTL + 1
    with pytest.raises(ProfileError, match="too long"):
        slow.finish(BASE, answer)


def local_app(profile, **kw):
    signin = steam_account.SteamSignIn(post=lambda url, f: "is_valid:true")
    return TestClient(create_local_app(profile, token="t" * 32, steam_signin=signin, **kw),
                      base_url="http://127.0.0.1:8788", follow_redirects=False)


def sign_in(local, sid=SID):
    start = local.get("/v1/local/steam/signin")
    return local.get("/v1/local/steam/return", params=steams_answer(start.headers["location"], sid=sid))


def test_sign_in_follows_this_pcs_steam_for_that_account(profile, tmp_path, monkeypatch):
    root = fake_steam_install(tmp_path / "Steam")
    monkeypatch.setattr(steamfiles, "steam_dir", lambda: root)
    sid = str(steamfiles.STEAMID64_BASE + 42)
    local = local_app(profile, catalog_dir=tmp_path / "nocat")
    assert sign_in(local, sid).headers["location"] == "/?steam=signed-in"
    state = local.get("/v1/local/steam").json()
    assert state == {"linked": True, "steam_id": sid, "account": sid}
    assert "steam-10:first" in profile.state()["unlocks"]          # its achievements came in at once


def test_sign_in_never_follows_a_different_account_on_this_pc(profile, tmp_path, monkeypatch):
    root = fake_steam_install(tmp_path / "Steam")                  # account 42 plays here
    monkeypatch.setattr(steamfiles, "steam_dir", lambda: root)
    local = local_app(profile, catalog_dir=tmp_path / "nocat")
    assert sign_in(local, SID).headers["location"] == "/?steam=signed-in"
    state = local.get("/v1/local/steam").json()
    assert state == {"linked": False, "steam_id": None, "account": SID}
    assert local.post("/v1/local/steam/link", headers=TOKEN).status_code == 400   # "Look again" stays on SID
    assert profile.state()["unlocks"] == {}


def test_sign_in_without_steam_on_this_pc_still_links_the_account(profile):
    local = local_app(profile)
    assert sign_in(local).headers["location"] == "/?steam=signed-in"
    assert local.get("/v1/local/steam").json() == {"linked": False, "steam_id": None, "account": SID}
    assert local.post("/v1/local/steam/forget", headers=TOKEN).status_code == 200
    assert local.get("/v1/local/steam").json()["account"] is None


def test_there_is_no_key_route_anywhere(profile, tmp_path):
    """Owner decision 2026-10-01: Steam is sign-in only. No endpoint, command,
    server service or module takes a Steam Web API key."""
    from openachievements.cli import build_parser
    from openachievements.server.app import Settings, create_app
    from openachievements.server.store import Store
    local = local_app(profile)
    assert local.post("/v1/local/steam/key", headers=TOKEN, json={"key": "0" * 32}).status_code in (404, 405)
    assert local.post("/v1/local/steam/import", headers=TOKEN).status_code in (404, 405)
    server = TestClient(create_app(Store(tmp_path / "s.sqlite"), Settings()))
    assert "steam-library" not in server.get("/v1/capabilities").json()["features"]
    assert server.get(f"/v1/steam/owned/{SID}").status_code == 404
    with pytest.raises(SystemExit):
        build_parser().parse_args(["steam", "key"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["import", "steam", SID])
    src = pathlib.Path(steam_account.__file__).resolve().parents[1]
    code = "".join(f.read_text(encoding="utf-8") for f in src.rglob("*.py"))
    assert "OA_STEAM_KEY" not in code and "GetOwnedGames" not in code
