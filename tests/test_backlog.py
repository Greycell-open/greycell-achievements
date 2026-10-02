"""The backlog: game status, and adding catalogued games from the library UI."""
import pytest
from fastapi.testclient import TestClient

from openachievements import events as ev
from openachievements.catalog import steam as cat
from openachievements.cli import main
from openachievements.local_app import create_local_app
from openachievements.profile import ProfileError
from test_catalog import GAME, fake_steam, page, row

TOKEN = "t" * 32


@pytest.fixture
def catalogue(tmp_path):
    games = {10: (4, GAME), 20: (1, page("Portal", [row("Aperture", "Begin", "aa", "90.0")])),
             30: (1, page("Portal 2", [row("Wake Up", "Wake", "bb", "95.0")]))}
    cat.crawl([(a, None) for a in games], tmp_path / "catalog", fake_steam(games))
    return tmp_path / "catalog"


@pytest.fixture
def client(profile, catalogue):
    return TestClient(create_local_app(profile, token=TOKEN, catalog_dir=catalogue))


def test_status_is_set_changed_and_cleared(profile):
    profile.register_game("celeste", "Celeste")
    profile.set_status("celeste", "backlog")
    profile.set_status("celeste", "playing")
    game = profile.library()["games"][0]
    assert game["status"] == "playing" and game["status_at"]
    profile.set_status("celeste", "none")
    assert "status" not in profile.library()["games"][0]
    with pytest.raises(ProfileError):
        profile.set_status("celeste", "finished-ish")
    with pytest.raises(ProfileError):
        profile.set_status("no-such-game", "backlog")


def test_a_status_event_from_elsewhere_is_validated(profile):
    profile.register_game("celeste", "Celeste")
    good = ev.make_event("game.status_changed", profile_id=profile.profile_id, device_id=profile.device_id,
                         payload={"status": "backlog"}, game_id="celeste")
    bad = dict(good, payload={"status": "owned"})
    bad["integrity"] = {"event_hash": ev.compute_hash(bad)}          # correctly hashed, wrong content
    with pytest.raises(ev.EventError, match="status"):
        profile.commit([bad])


def test_the_index_searches_titles_and_ranks_exact_matches_first(catalogue):
    index = cat.CatalogIndex(catalogue)
    assert [r["title"] for r in index.search("portal")] == ["Portal", "Portal 2"]
    assert index.search("test game")[0]["appid"] == 10
    assert index.get(20)["achievements"][0]["name"] == "Aperture"


def test_a_lost_index_is_rebuilt_from_the_packs(catalogue):
    (catalogue / cat.INDEX_FILE).unlink()
    assert len(cat.CatalogIndex(catalogue)) == 3
    (catalogue / cat.INDEX_FILE).write_text("20\t0\t5\t1\tPortal\n", encoding="utf-8")   # does not reach the end
    assert len(cat.CatalogIndex(catalogue)) == 3


def test_searching_and_adding_from_the_library_page(profile, client):
    found = client.get("/v1/local/catalog", params={"q": "portal"}).json()
    assert found["catalogued"] == 3 and [r["in_library"] for r in found["results"]] == [False, False]
    refused = client.post("/v1/local/catalog/add", json={"appid": 20, "status": "playing"})
    assert refused.status_code == 403                     # no page token: any website could have sent it
    added = client.post("/v1/local/catalog/add", json={"appid": 20, "status": "playing"},
                        headers={"X-OA-Token": TOKEN}).json()
    assert added["game_id"] == "steam-20" and added["achievements"] == 1
    game = next(g for g in client.get("/v1/library").json()["games"] if g["game_id"] == "steam-20")
    assert game["status"] == "playing" and game["total"] == 1
    assert client.get("/v1/local/catalog", params={"q": "portal"}).json()["results"][0]["in_library"] is True
    moved = client.post("/v1/local/status", json={"game_id": "steam-20", "status": "completed"},
                        headers={"X-OA-Token": TOKEN})
    assert moved.status_code == 200
    assert profile.library()["games"][0]["status"] == "completed"
    bad = client.post("/v1/local/catalog/add", json={"appid": 999}, headers={"X-OA-Token": TOKEN})
    assert bad.status_code == 400


def test_adding_an_installed_game_again_only_changes_its_status(profile, catalogue):
    index = cat.CatalogIndex(catalogue)
    cat.add_to_profile(profile, index, 10, status="backlog")
    before = len(profile.log.read().events)
    cat.add_to_profile(profile, index, 10, status="playing")
    events = profile.log.read().events[before:]
    assert [e["event_type"] for e in events] == ["game.status_changed"]


def test_the_cli_backlog(profile, catalogue, capsys):
    base = ["--home", str(profile.folder.parent.parent), "--config", str(profile.config.dir)]
    main(base + ["catalog", "--dir", str(catalogue), "install", "30", "--status", "wishlist"])
    main(base + ["game", "status", "steam-30", "playing"])
    capsys.readouterr()
    main(base + ["game", "list", "--status", "playing"])
    assert "Portal 2" in capsys.readouterr().out


def test_the_page_and_its_scripts_are_never_served_stale(client):
    for path in ("/", "/assets/app.js", "/assets/style.css"):
        assert client.get(path).headers["cache-control"] == "no-cache"


def test_a_game_the_crawl_has_not_reached_is_fetched_from_steam_now(profile, catalogue):
    games = {40: (1, page("Townfall", [row("Welcome", "", "cc", "88.4")]))}
    added = cat.add_to_profile(profile, cat.CatalogIndex(catalogue), 40, status="playing", fetch=fake_steam(games))
    assert added == {"game_id": "steam-40", "title": "Townfall", "achievements": 1, "status": "playing"}
    assert "steam-40" in profile.state()["packs"]
    with pytest.raises(cat.CatalogError):
        cat.add_to_profile(profile, cat.CatalogIndex(catalogue), 50, fetch=fake_steam({50: (0, None)}))
    with pytest.raises(cat.CatalogError):
        cat.add_to_profile(profile, cat.CatalogIndex(catalogue), 60)
