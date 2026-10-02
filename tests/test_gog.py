"""GOG achievements, read only, against a fake gog.com shaped like the real pages: never the real one."""
import json
import urllib.parse

import pytest

from openachievements import notify
from openachievements.adapters import gog

USER, UID = "Zein", "50675816926743123"
OXEN = {"game": {"id": "1457519353", "title": "Oxenfree", "achievementSupport": True},
        "stats": {UID: {"achievementsPercentage": 50, "playtime": 20, "lastSession": "2026-09-30T20:00:00+00:00"}}}
FRESH = {"game": {"id": "2000", "title": "Untouched", "achievementSupport": True},
         "stats": {UID: {"playtime": 1, "lastSession": "2026-01-01T10:00:00+00:00"}}}
OLD = {"game": {"id": "1207663993", "title": "Blitzkrieg", "achievementSupport": False},
       "stats": {UID: {"playtime": 81, "lastSession": "2019-04-06T21:37:45+00:00"}}}


def ach(aid, name, visible=True, unlocked=None):
    return {"achievement": {"id": aid, "name": name, "description": f"{name} text", "visible": visible,
                            "imageUrlLocked": "https://images.gog.com/l.png",
                            "imageUrlUnlocked": f"https://images.gog.com/{aid}.png", "rarity": 10.8},
            "stats": {UID: {"isUnlocked": unlocked is not None, "unlockDate": unlocked}}}


def page(name, value):
    return (f"<html><script>window.profilesData = window.profilesData || {{}};\n"
            f"window.profilesData.{name} = {json.dumps(value)};\nwindow.profilesData.mode = \"standard\";"
            f"</script></html>").encode()


class FakeGog:
    def __init__(self, private=False, pages=1, changed=False, throttle=0):
        self.calls, self.private, self.pages, self.throttle = [], private, pages, throttle
        self.achievements = {"1457519353": [ach("11", "It's A Me", unlocked=1513779316),
                                            ach("12", "Secret achievement", visible=False)],
                             "2000": [ach("21", "First")]}
        self.changed = changed

    def __call__(self, method, url, headers, data=None):
        assert method == "GET" and data is None and "Authorization" not in headers and "Cookie" not in headers
        self.calls.append(url)
        if self.throttle:
            self.throttle -= 1
            return 429, {}, b""
        parts = urllib.parse.urlsplit(url)
        path = parts.path.split("/")
        if path[2] != USER:
            return 404, {}, b"{}"
        if parts.path == f"/u/{USER}":
            return 200, {}, page("profileUser", {"username": USER, "userId": UID})
        if parts.path.endswith("/games/stats"):
            if self.private:
                return 403, {}, b'""'
            n = int(urllib.parse.parse_qs(parts.query)["page"][0])
            items = [OXEN, OLD] if n == 1 else [FRESH]
            return 200, {}, json.dumps({"page": n, "pages": self.pages, "_embedded": {"items": items}}).encode()
        return 200, {}, page("achievements", self.achievements[path[4]])


def watcher(profile, fake):
    t = [0.0]
    w = gog.GogWatcher(profile, client=gog.Client(fake), clock=lambda: t[0])
    return w, t


def run(w, t, rounds=20, step=11.0):
    out = []
    for _ in range(rounds):
        out += w.poll()
        t[0] += step
    return out


@pytest.fixture(autouse=True)
def clean():
    gog.STATUS.clear()


def test_a_username_or_a_profile_link_connects_and_nothing_secret_is_kept(profile):
    fake = FakeGog()
    assert gog.connect(profile, f"https://www.gog.com/u/{USER}/", http=fake) == {"username": USER, "user_id": UID}
    assert gog.linked(profile)["user_id"] == UID
    with pytest.raises(gog.GogError, match="not a GOG username"):
        gog.connect(profile, "a b<script>", http=fake)
    with pytest.raises(gog.GogError, match="No GOG profile"):
        gog.connect(profile, "Nobody", http=fake)


def test_a_private_profile_explains_what_to_change(profile):
    with pytest.raises(gog.GogError, match="visible to everyone"):
        gog.connect(profile, USER, http=FakeGog(private=True))
    assert gog.linked(profile) is None


def test_games_and_achievements_come_in_with_gogs_unlock_times(profile):
    fake = FakeGog(pages=2)
    gog.connect(profile, USER, http=fake)
    w, t = watcher(profile, fake)
    unlocked = run(w, t)
    games = {g["game_id"]: g for g in profile.library()["games"]}
    oxen = games["gog-1457519353"]
    assert (oxen["title"], oxen["platform"], oxen["total"], oxen["unlocked"]) == ("Oxenfree", "GOG", 2, 1)
    first = next(a for a in oxen["achievements"] if a["name"] == "It's A Me")
    assert first["unlocked_at"] == "2017-12-20T14:15:16.000Z"
    assert games["gog-2000"]["unlocked"] == 0                     # second page of the list, nothing unlocked yet
    assert "gog-1207663993" not in games                           # no achievements on GOG, not listed here
    assert len(unlocked) == 1
    assert not any("/game/1207663993" in c for c in fake.calls)
    assert gog.STATUS[profile.profile_id]["last_sync"] and not gog.STATUS[profile.profile_id]["error"]


def test_games_with_progress_are_read_first(profile):
    fake = FakeGog(pages=2)
    gog.connect(profile, USER, http=fake)
    w, t = watcher(profile, fake)
    run(w, t)
    pages = [c for c in fake.calls if "/game/" in c]
    assert pages[0].endswith("/game/1457519353") and pages[1].endswith("/game/2000")


def test_never_two_requests_within_ten_seconds(profile):
    fake = FakeGog()
    gog.connect(profile, USER, http=fake)
    w, t = watcher(profile, fake)
    n = len(fake.calls)
    run(w, t, rounds=10, step=1.0)                                 # ten polls inside ten seconds
    assert len(fake.calls) - n == 1


def test_unchanged_games_are_skipped_and_nothing_counts_twice(profile):
    fake = FakeGog()
    gog.connect(profile, USER, http=fake)
    run(*watcher(profile, fake))
    fake.calls.clear()
    assert run(*watcher(profile, fake)) == []                      # a restart: only the list is asked for
    assert not any("/game/" in c for c in fake.calls)


def test_a_new_unlock_on_gog_arrives_once(profile):
    fake = FakeGog()
    gog.connect(profile, USER, http=fake)
    run(*watcher(profile, fake))
    fake.achievements["1457519353"][1] = ach("12", "Hidden Ending", unlocked=1790000000)
    OXEN_NOW = json.loads(json.dumps(OXEN))
    OXEN_NOW["stats"][UID]["achievementsPercentage"] = 100
    fake_list = fake.__call__

    def changed(method, url, headers, data=None):
        if url.split("?")[0].endswith("/games/stats"):
            fake.calls.append(url)
            return 200, {}, json.dumps({"page": 1, "pages": 1, "_embedded": {"items": [OXEN_NOW]}}).encode()
        return fake_list(method, url, headers, data)

    new = run(*watcher(profile, changed))
    assert [e["achievement_id"] for e in new] == ["gog-1457519353:a12"]
    oxen = next(g for g in profile.library()["games"] if g["game_id"] == "gog-1457519353")
    assert oxen["unlocked"] == 2 and any(a["name"] == "Hidden Ending" for a in oxen["achievements"])
    assert run(*watcher(profile, changed)) == []


def test_when_gog_says_slow_down_the_watcher_reports_and_carries_on(profile):
    fake = FakeGog(throttle=1)
    with pytest.raises(gog.GogError, match="slow down"):
        gog.connect(profile, USER, http=fake)
    fake = FakeGog()
    gog.connect(profile, USER, http=fake)
    fake.throttle = 1
    w, t = watcher(profile, fake)
    run(w, t, rounds=1)
    assert "slow down" in gog.STATUS[profile.profile_id]["error"]
    assert any(p.startswith("GOG:") for p in w.new_problems())


def test_a_page_gog_changed_is_reported_not_crashed_on(profile):
    fake = FakeGog()
    gog.connect(profile, USER, http=fake)
    fake.achievements["1457519353"] = "not a list"
    run(*watcher(profile, fake))
    assert "changed its profile pages" in gog.STATUS[profile.profile_id]["error"]


def test_disconnect_keeps_the_achievements(profile):
    fake = FakeGog()
    gog.connect(profile, USER, http=fake)
    run(*watcher(profile, fake))
    gog.disconnect(profile)
    assert gog.linked(profile) is None
    assert any(g["game_id"] == "gog-1457519353" for g in profile.library()["games"])


def test_gog_unlocks_sync_quietly(profile):
    fake = FakeGog()
    gog.connect(profile, USER, http=fake)
    unlocked = run(*watcher(profile, fake))
    fresh = dict(unlocked[0], occurred_at=notify.datetime.now(notify.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    assert not notify.wanted(fresh, notify.DEFAULTS)


def test_the_local_app_connects_reports_and_disconnects(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    monkeypatch.setattr(gog, "_http", FakeGog())
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    h = {"X-OA-Token": "t" * 32}
    assert local.get("/v1/local/gog").json()["linked"] is False
    assert local.post("/v1/local/gog/connect", json={"username": USER}).status_code == 403
    assert local.post("/v1/local/gog/connect", headers=h, json={"username": "Nobody"}).status_code == 400
    s = local.post("/v1/local/gog/connect", headers=h, json={"username": USER}).json()
    assert s["linked"] and s["username"] == USER and s["profile_url"] == f"https://www.gog.com/u/{USER}"
    assert local.post("/v1/local/gog/disconnect", headers=h).json()["linked"] is False
