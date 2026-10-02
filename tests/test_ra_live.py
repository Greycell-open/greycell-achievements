"""RetroAchievements live link, against a fake RetroAchievements API: never the real one."""
import urllib.parse

import pytest

from openachievements import credentials, notify
from openachievements.adapters import retroachievements as ra
from openachievements.adapters.base import AdapterError

KEY = "AbCdEf0123456789AbCdEf0123456789"
ULID = "00003EMFWR7XB8SDPEHB3K56ZQ"


@pytest.fixture(autouse=True)
def vault(monkeypatch):
    store = {}
    monkeypatch.setattr(credentials, "_set", lambda t, s: store.__setitem__(t, s))
    monkeypatch.setattr(credentials, "_get", lambda t: store.get(t))
    monkeypatch.setattr(credentials, "_delete", lambda t: store.pop(t, None))
    ra.STATUS.clear()
    return store


def game(gid, title, earned):
    achs = {str(i): {"ID": gid * 100 + i, "Title": f"Ach {i}", "Description": "Do it", "Points": 5,   # RA ids are site-wide
                     "BadgeName": str(i), "DisplayOrder": i, **earned.get(i, {})} for i in (1, 2)}
    return {"ID": gid, "Title": title, "ConsoleName": "SNES", "ImageIcon": "/Images/1.png", "Achievements": achs}


class FakeRa:
    def __init__(self, total_pages=1):
        self.calls, self.status = [], None
        self.games = {10: (game(10, "Mario", {1: {"DateEarned": "2026-09-01 10:00:00"}}), "2026-09-01T10:00:00+00:00"),
                      20: (game(20, "Zelda", {1: {"DateEarned": "2026-09-30 10:00:00",
                                                  "DateEarnedHardcore": "2026-09-30 10:00:00"}}),
                           "2026-09-30T10:00:00+00:00")}
        self.total_pages = total_pages

    def __call__(self, url):
        self.calls.append(url)
        if self.status:
            raise AdapterError(*self.status)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if q.get("y") != KEY:
            raise AdapterError("unauthorized", "refused the credentials (401)")
        if "API_GetUserProfile" in url:
            return {"User": "Zein", "ULID": ULID} if q["u"].lower() == "zein" else {}
        assert q["u"] == ULID                                                  # always the rename-proof id
        if "API_GetUserCompletionProgress" in url:
            rows = [{"GameID": gid, "Title": g["Title"], "ConsoleName": "SNES", "MostRecentAwardedDate": stamp}
                    for gid, (g, stamp) in self.games.items()]
            o = int(q.get("o", 0))
            per = max(1, -(-len(rows) // self.total_pages))
            return {"Count": len(rows[o:o + per]), "Total": len(rows), "Results": rows[o:o + per]}
        return self.games[int(q["g"])][0]


def run(w, rounds=20, step=6.0, start=0.0):
    t = [start]
    w.clock = lambda: t[0]
    out = []
    for _ in range(rounds):
        out += w.poll()
        t[0] += step
    return out


def test_connect_checks_the_key_and_keeps_it_only_in_the_credential_store(profile, vault):
    fake = FakeRa()
    with pytest.raises(ra.RaError, match="Web API key"):
        ra.connect(profile, "Zein", "short", fetch=fake)
    with pytest.raises(ra.RaError, match="refused"):
        ra.connect(profile, "Zein", "X" * 32, fetch=fake)
    with pytest.raises(ra.RaError, match="No RetroAchievements player"):
        ra.connect(profile, "Nobody", KEY, fetch=fake)
    assert ra.connect(profile, "zein", KEY, fetch=fake) == {"username": "Zein", "ulid": ULID}
    assert list(vault.values()) == [KEY]
    everything = b"".join(p.read_bytes() for p in profile.folder.rglob("*") if p.is_file())
    config = b"".join(p.read_bytes() for p in profile.config.dir.rglob("*") if p.is_file())
    assert KEY.encode() not in everything and KEY.encode() not in config


def test_the_first_sync_brings_games_sets_and_both_modes_latest_first(profile):
    fake = FakeRa(total_pages=2)
    ra.connect(profile, "Zein", KEY, fetch=fake)
    unlocked = run(ra.RaWatcher(profile, fetch=fake))
    games = {g["game_id"]: g for g in profile.library()["games"]}
    zelda = games["ra-20"]
    assert (zelda["title"], zelda["platform"], zelda["total"], zelda["points_total"]) == ("Zelda", "SNES", 2, 10)
    assert zelda["last_played"] == "2026-09-30T10:00:00.000Z"
    assert sorted(e["payload"]["mode"] for e in unlocked) == ["hardcore", "softcore", "softcore"]
    game_calls = [c for c in fake.calls if "GameInfoAndUserProgress" in c]
    assert "g=20" in game_calls[0] and "g=10" in game_calls[1]
    assert ra.STATUS[profile.profile_id]["last_sync"] and not ra.STATUS[profile.profile_id]["error"]


def test_never_two_requests_within_five_seconds(profile):
    fake = FakeRa()
    ra.connect(profile, "Zein", KEY, fetch=fake)
    n = len(fake.calls)
    run(ra.RaWatcher(profile, fetch=fake), rounds=5, step=1.0)
    assert len(fake.calls) - n == 1


def test_only_changed_games_are_fetched_and_nothing_counts_twice(profile):
    fake = FakeRa()
    ra.connect(profile, "Zein", KEY, fetch=fake)
    run(ra.RaWatcher(profile, fetch=fake))
    fake.calls.clear()
    assert run(ra.RaWatcher(profile, fetch=fake)) == []                  # a restart
    assert not any("GameInfoAndUserProgress" in c for c in fake.calls)
    g, _ = fake.games[10]
    g["Achievements"]["2"]["DateEarned"] = "2026-10-02 12:00:00"
    fake.games[10] = (g, "2026-10-02T12:00:00+00:00")
    new = run(ra.RaWatcher(profile, fetch=fake))
    assert [e["achievement_id"] for e in new] == ["ra-10:a1002"]
    assert [c for c in fake.calls if "GameInfoAndUserProgress" in c] == [
        c for c in fake.calls if "GameInfoAndUserProgress" in c and "g=10" in c]


def test_the_cli_import_and_the_live_link_share_ids(profile):
    fake = FakeRa()
    original = fake.__call__

    def by_name(url):                                     # the command line asks by username, not ULID
        return original(url.replace("u=Zein", f"u={ULID}"))
    ra.import_account(profile, "Zein", KEY, fetch=by_name, pause=0)
    ra.connect(profile, "Zein", KEY, fetch=fake)
    assert run(ra.RaWatcher(profile, fetch=fake)) == []


def test_a_refused_key_or_slow_down_is_shown_and_retried_later(profile):
    fake = FakeRa()
    ra.connect(profile, "Zein", KEY, fetch=fake)
    w = ra.RaWatcher(profile, fetch=fake)
    fake.status = ("http_error", "https://retroachievements.org/API/x.php answered 429")
    run(w, rounds=1)
    assert "slow down" in ra.STATUS[profile.profile_id]["error"]
    fake.status = ("unauthorized", "refused (401)")
    run(w, rounds=1, start=200.0)
    assert "refused the Web API key" in ra.STATUS[profile.profile_id]["error"]
    assert all(KEY not in p for p in w.new_problems())


def test_disconnect_deletes_the_key_and_keeps_the_achievements(profile, vault):
    fake = FakeRa()
    ra.connect(profile, "Zein", KEY, fetch=fake)
    run(ra.RaWatcher(profile, fetch=fake))
    ra.disconnect(profile)
    assert vault == {} and ra.linked(profile) is None
    assert any(g["game_id"] == "ra-20" for g in profile.library()["games"])


def test_ra_unlocks_sync_quietly(profile):
    fake = FakeRa()
    ra.connect(profile, "Zein", KEY, fetch=fake)
    unlocked = run(ra.RaWatcher(profile, fetch=fake))
    fresh = dict(unlocked[0], occurred_at=notify.datetime.now(notify.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    assert not notify.wanted(fresh, notify.DEFAULTS)


def test_the_local_app_connects_reports_and_disconnects(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    monkeypatch.setattr(ra, "_fetch_once", FakeRa())
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    assert local.get("/v1/local/retroachievements").json()["linked"] is False
    body = {"username": "Zein", "api_key": KEY}
    assert local.post("/v1/local/retroachievements/connect", json=body).status_code == 403
    assert local.post("/v1/local/retroachievements/connect", headers=h,
                      json={**body, "api_key": "X" * 32}).status_code == 400
    s = local.post("/v1/local/retroachievements/connect", headers=h, json=body).json()
    assert s["linked"] and s["username"] == "Zein" and s["has_key"] and KEY not in str(s)
    assert local.post("/v1/local/retroachievements/disconnect", headers=h).json()["linked"] is False


def test_the_pulse_carries_running_syncs_for_the_status_bar(profile):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    assert local.get("/v1/local/pulse").json()["syncs"] == []
    fake = FakeRa(total_pages=2)
    ra.connect(profile, "Zein", KEY, fetch=fake)
    w = ra.RaWatcher(profile, fetch=fake)
    run(w, rounds=3)                                            # two list pages and one game: mid-sync
    syncs = local.get("/v1/local/pulse").json()["syncs"]
    assert syncs == [{"key": "ra", "name": "RetroAchievements", "running": True, "done": 1, "total": 2,
                      "current": "Zelda", "error": None}]
    run(w, rounds=3, start=100.0)
    assert local.get("/v1/local/pulse").json()["syncs"] == []   # finished: the bar goes away
