"""Dashboard pictures: fetched once from a platform's image host, kept on this computer. Never the network here."""
from openachievements import art

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


class Fetch:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, url):
        self.calls.append(url)
        answer = self.answers.get(url)
        if answer is None:
            raise OSError("404")
        return answer


def test_only_platform_image_hosts_are_fetched():
    assert art.allowed("https://shared.akamai.steamstatic.com/a.jpg")
    assert art.allowed("https://media.retroachievements.org/Badge/1.png")
    for bad in ("http://shared.akamai.steamstatic.com/a.jpg", "https://evil.example/a.jpg",
                "https://steamstatic.com.evil.example/a.jpg", "file:///C:/Windows/win.ini", ""):
        assert not art.allowed(bad), bad


def test_a_picture_is_fetched_once_then_served_from_this_computer(tmp_path):
    url = "https://shared.akamai.steamstatic.com/community_assets/x.jpg"
    fetch = Fetch({url: ("image/jpeg", JPEG)})
    first = art.cached(url, fetch, tmp_path)
    assert first.read_bytes() == JPEG
    assert art.cached(url, fetch, tmp_path) == first and len(fetch.calls) == 1


class Gone(Fetch):
    def __call__(self, url):
        self.calls.append(url)
        if url in self.answers:
            return self.answers[url]
        import urllib.error
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


def test_not_an_image_or_too_big_or_missing_is_remembered_a_day(tmp_path):
    page = "https://shared.akamai.steamstatic.com/page.jpg"
    big = "https://shared.akamai.steamstatic.com/big.jpg"
    gone = "https://shared.akamai.steamstatic.com/gone.jpg"
    fetch = Gone({page: ("text/html", b"<html>"), big: ("image/jpeg", b"x" * (art.MAX_BYTES + 1))})
    for url in (page, big, gone):
        assert art.cached(url, fetch, tmp_path, now=1000.0) is None
        assert art.cached(url, fetch, tmp_path, now=2000.0) is None          # not asked again within a day
    assert len(fetch.calls) == 3
    assert art.cached(gone, fetch, tmp_path, now=1000.0 + art.MISS_FOR + 1) is None
    assert len(fetch.calls) == 4                                              # a day later: asked again


def test_steam_banner_comes_from_steams_own_copy_first(tmp_path):
    root = tmp_path / "Steam"
    local = root / "appcache" / "librarycache" / "400"
    local.mkdir(parents=True)
    (local / "header.jpg").write_bytes(JPEG)
    fetch = Fetch({})
    assert art.steam_banner("400", root, fetch=fetch, folder=tmp_path / "cache") == local / "header.jpg"
    assert fetch.calls == []


def test_steam_banner_falls_back_to_the_store_address_for_newer_games(tmp_path, monkeypatch):
    monkeypatch.setattr(art.time, "sleep", lambda s: None)
    hashed = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1636440/abc/header.jpg?t=1"
    fetch = Fetch({hashed: ("image/jpeg", JPEG)})
    details = lambda url: {"1636440": {"success": True, "data": {"header_image": hashed}}}
    found = art.steam_banner("1636440", None, fetch=fetch, get_json=details, folder=tmp_path)
    assert found.read_bytes() == JPEG
    assert fetch.calls[0] == art.STEAM_HEADER.format(appid="1636440") and fetch.calls[1] == hashed


def test_a_store_answer_pointing_elsewhere_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(art.time, "sleep", lambda s: None)
    fetch = Fetch({})
    details = lambda url: {"9": {"success": True, "data": {"header_image": "https://evil.example/h.jpg"}}}
    assert art.steam_banner("9", None, fetch=fetch, get_json=details, folder=tmp_path) is None
    assert all("evil" not in c for c in fetch.calls)


def test_other_platforms_use_their_recorded_picture(tmp_path):
    icon = "https://media.retroachievements.org/Images/110040.png"
    fetch = Fetch({icon: ("image/png", PNG)})
    found = art.game_picture({"game_id": "ra-270", "icon_url": icon}, None, fetch=fetch, folder=tmp_path)
    assert found.read_bytes() == PNG
    assert art.game_picture({"game_id": "local-thing"}, None, fetch=fetch, folder=tmp_path) is None


def test_the_local_app_serves_pictures_and_refuses_other_addresses(profile, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    monkeypatch.setattr(art, "cache_dir", lambda: tmp_path / "cache")
    url = "https://shared.akamai.steamstatic.com/community_assets/x.png"
    monkeypatch.setattr(art, "_fetch", Fetch({url: ("image/png", PNG)}))
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    r = local.get("/v1/local/art", params={"u": url})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert local.get("/v1/local/art", params={"u": "https://evil.example/x.png"}).status_code == 404
    assert local.get("/v1/local/art/game/no-such-game").status_code == 404


def test_the_saves_note_knows_when_a_game_has_save_rules(profile):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    assert local.get("/v1/local/games/steam-1636440/saves").json()["rules"] is True     # Townfall ships rules
    assert local.get("/v1/local/games/steam-1/saves").json()["rules"] is False


def test_a_redirect_off_the_image_hosts_is_never_requested():
    import urllib.error
    asked = []

    def opener(req, timeout):
        asked.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://evil.example/secret"}, None)
    import pytest
    with pytest.raises(ValueError):
        art._fetch("https://shared.akamai.steamstatic.com/start.jpg", open_url=opener)
    assert asked == ["https://shared.akamai.steamstatic.com/start.jpg"]


def test_a_redirect_between_image_hosts_is_followed():
    import io, urllib.error
    asked = []

    class Resp(io.BytesIO):
        headers = {"Content-Type": "image/jpeg"}

    def opener(req, timeout):
        asked.append(req.full_url)
        if len(asked) == 1:
            raise urllib.error.HTTPError(req.full_url, 301, "Moved", {"Location": "https://cdn.cloudflare.steamstatic.com/a.jpg"}, None)
        return Resp(JPEG)
    assert art._fetch("https://shared.akamai.steamstatic.com/a.jpg", open_url=opener) == ("image/jpeg", JPEG)
    assert asked[1] == "https://cdn.cloudflare.steamstatic.com/a.jpg"


def test_a_banner_found_through_the_store_shows_again_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(art.time, "sleep", lambda s: None)
    hashed = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1636440/abc/header.jpg?t=1"
    fetch = Fetch({hashed: ("image/jpeg", JPEG)})
    store = []
    details = lambda url: store.append(url) or {"1636440": {"success": True, "data": {"header_image": hashed}}}
    art.steam_banner("1636440", None, fetch=fetch, get_json=details, folder=tmp_path)
    offline = Fetch({})

    def no_store(url):
        raise OSError("offline")
    again = art.steam_banner("1636440", None, fetch=offline, get_json=no_store, folder=tmp_path)
    assert again.read_bytes() == JPEG and len(store) == 1 and offline.calls == []


def test_a_steam_game_linked_into_another_keeps_its_banner(profile, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    from openachievements import steamfiles
    root = tmp_path / "Steam"
    (root / "appcache" / "librarycache" / "10").mkdir(parents=True)
    (root / "appcache" / "librarycache" / "10" / "header.jpg").write_bytes(JPEG)
    monkeypatch.setattr(steamfiles, "steam_dir", lambda: root)
    monkeypatch.setattr(art, "cache_dir", lambda: tmp_path / "cache")
    profile.register_game("my-game", "My Game", platform="PC")
    profile.register_game("steam-10", "My Game", platform="PC (Steam)", external_ids={"steam": 10})
    profile.link_games("steam-10", "my-game")
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    r = local.get("/v1/local/art/game/my-game")
    assert r.status_code == 200 and r.content == JPEG


def test_a_network_blip_is_retried_not_remembered(tmp_path):
    import socket, urllib.error
    url = "https://shared.akamai.steamstatic.com/x.jpg"
    for failure in (urllib.error.URLError("offline"), socket.timeout("slow"),
                    urllib.error.HTTPError(url, 429, "Too Many", {}, None),
                    urllib.error.HTTPError(url, 503, "Busy", {}, None)):
        def down(u, failure=failure):
            raise failure
        assert art.cached(url, down, tmp_path) is None
        assert art.cached(url, Fetch({url: ("image/jpeg", JPEG)}), tmp_path).read_bytes() == JPEG
        (tmp_path / (art._name(url) + ".img")).unlink()


def test_the_store_lookup_checks_redirects_too():
    import urllib.error, pytest
    asked = []

    def opener(req, timeout):
        asked.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://evil.example/x"}, None)
    with pytest.raises(art.Refused):
        art._get(art.STEAM_DETAILS.format(appid=1), lambda r, l: None, open_url=opener)
    assert len(asked) == 1 and "evil" not in asked[0]


def test_a_steam_game_two_links_away_keeps_its_banner(profile, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    from openachievements import steamfiles
    root = tmp_path / "Steam"
    (root / "appcache" / "librarycache" / "10").mkdir(parents=True)
    (root / "appcache" / "librarycache" / "10" / "header.jpg").write_bytes(JPEG)
    monkeypatch.setattr(steamfiles, "steam_dir", lambda: root)
    monkeypatch.setattr(art, "cache_dir", lambda: tmp_path / "cache")
    for gid in ("displayed", "middle"):
        profile.register_game(gid, "Same Game", platform="PC")
    profile.register_game("steam-10", "Same Game", platform="PC (Steam)")
    profile.link_games("steam-10", "middle")
    profile.link_games("middle", "displayed")
    local = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    assert local.get("/v1/local/art/game/displayed").content == JPEG
