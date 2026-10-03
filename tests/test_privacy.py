"""The player's switches for what goes online without being asked: update
checks, pictures, Steam rarity. Off means no request at all; what is already
on this computer keeps working."""
import pytest

from openachievements import art, privacy, rarity, self_update


def no_network(*a, **k):
    raise AssertionError("went online although the player switched it off")


def test_everything_is_on_until_the_player_switches_it_off(profile):
    assert privacy.settings(profile.config.load()) == {"updates": True, "pictures": True, "rarity": True}
    assert privacy.change(profile.config, pictures=False, rarity=False) == \
        {"updates": True, "pictures": False, "rarity": False}
    assert privacy.change(profile.config, updates=False)["updates"] is False
    assert profile.config.load()["update"]["check"] is False                  # the same switch the CLI has
    with pytest.raises(ValueError):
        privacy.change(profile.config, telemetry=True)


def test_no_update_look_at_all_when_switched_off_not_even_at_start(profile, monkeypatch):
    privacy.change(profile.config, updates=False)
    monkeypatch.setattr(self_update.update, "check", no_network)
    assert self_update.look_once(profile.config, lambda: None, lambda: False, force=True) == "off"


def test_pictures_off_uses_only_what_is_already_here(tmp_path):
    url = "https://shared.akamai.steamstatic.com/a.jpg"
    assert art.cached(url, fetch=no_network, folder=tmp_path, online=False) is None
    assert not list(tmp_path.iterdir())                                       # not remembered as missing either
    (tmp_path / (art._name(url) + ".img")).write_bytes(b"\xff\xd8 picture")
    assert art.cached(url, fetch=no_network, folder=tmp_path, online=False).read_bytes() == b"\xff\xd8 picture"


def test_banners_off_still_use_steams_own_copy_on_this_computer(tmp_path):
    root = tmp_path / "Steam"
    (root / "appcache" / "librarycache" / "10").mkdir(parents=True)
    (root / "appcache" / "librarycache" / "10" / "header.jpg").write_bytes(b"jpg")
    assert art.steam_banner("10", root, fetch=no_network, get_json=no_network, folder=tmp_path / "c", online=False)
    assert art.steam_banner("20", root, fetch=no_network, get_json=no_network, folder=tmp_path / "c",
                            online=False) is None


def test_rarity_off_asks_steam_nothing(profile, tmp_path):
    profile.register_game("steam-10", "G", platform="PC (Steam)")
    privacy.change(profile.config, rarity=False)
    crawler = rarity.RarityCrawler(profile, http=no_network, folder=tmp_path, clock=lambda: 1000.0)
    assert crawler.poll() is None
    assert rarity.for_unlock("steam-10", "X", http=no_network, folder=tmp_path, online=False) is None


def test_the_page_shows_and_changes_the_switches_with_its_token(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from openachievements.local_app import create_local_app
    page = TestClient(create_local_app(profile, token="t" * 32), base_url="http://127.0.0.1:8788")
    assert page.get("/v1/local/privacy").json() == {"updates": True, "pictures": True, "rarity": True}
    assert page.post("/v1/local/privacy", json={"pictures": False}).status_code == 403
    r = page.post("/v1/local/privacy", json={"pictures": False}, headers={"X-OA-Token": "t" * 32})
    assert r.json() == {"updates": True, "pictures": False, "rarity": True}
    monkeypatch.setattr(art, "_fetch", no_network)
    assert page.get("/v1/local/art", params={"u": "https://shared.akamai.steamstatic.com/new.jpg"}).status_code == 404
