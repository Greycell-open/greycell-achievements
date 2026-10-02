"""Every played game: Steam's play history, games found by their saves, recent first."""
import os
import time
from pathlib import Path

from openachievements import steamfiles as sf
from openachievements.adapters import autodetect as ad
from openachievements.adapters import steam_local
from test_autodetect import fake_steam_install

NOW = 1790800000                              # 2026-09-30 in Unix seconds


def write_localconfig(root: Path, account: int, apps: dict):
    folder = root / "userdata" / str(account) / "config"
    folder.mkdir(parents=True, exist_ok=True)
    body = "".join(f'\t\t\t\t\t"{a}"\n\t\t\t\t\t{{\n\t\t\t\t\t\t"LastPlayed"\t\t"{v[0]}"\n'
                   f'\t\t\t\t\t\t"Playtime"\t\t"{v[1]}"\n\t\t\t\t\t}}\n' for a, v in apps.items())
    (folder / "localconfig.vdf").write_text(
        '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
        f'\t\t\t\t"apps"\n\t\t\t\t{{\n{body}\t\t\t\t}}\n\t\t\t}}\n\t\t}}\n\t}}\n}}\n', encoding="utf-8")


def test_steams_play_history_is_read(tmp_path):
    write_localconfig(tmp_path, 42, {10: (NOW - 3600, 600), 20: (NOW - 90 * 86400, 30), 30: (0, 0)})
    played = sf.read_played(tmp_path, 42)
    assert played == {10: {"last_played": NOW - 3600, "playtime_minutes": 600},
                      20: {"last_played": NOW - 90 * 86400, "playtime_minutes": 30}}


def test_every_played_game_arrives_with_its_last_played_time(profile, tmp_path):
    root = fake_steam_install(tmp_path / "Steam")
    write_localconfig(root, 42, {10: (NOW - 3600, 600), 20: (NOW - 90 * 86400, 30), 77: (NOW - 7200, 5)})
    info = steam_local.link(profile, root)
    result = steam_local.sync_played(profile, root, {**info, "account_id": 42}, None,
                                     {10: "Test Game", 20: "Old Game"}, now=NOW)
    assert result["added"] == 3 and result["unnamed"] == [77] and result["playing"] == 2
    games = {g["game_id"]: g for g in profile.library()["games"]}
    assert games["steam-10"]["status"] == "playing" and games["steam-20"].get("status") is None
    assert games["steam-10"]["playtime_seconds"] == 600 * 60
    assert games["steam-77"]["title"] == "Steam app 77"
    # Nothing changed: nothing written. Played again: last played moves, a chosen status stays.
    before = len(profile.log.read().events)
    assert steam_local.sync_played(profile, root, {**info, "account_id": 42}, None, {}, now=NOW)["updated"] == 0
    assert len(profile.log.read().events) == before
    profile.set_status("steam-20", "dropped")
    write_localconfig(root, 42, {10: (NOW - 3600, 600), 20: (NOW - 60, 45), 77: (NOW - 7200, 5)})
    steam_local.sync_played(profile, root, {**info, "account_id": 42}, None, {}, now=NOW)
    g20 = next(g for g in profile.library()["games"] if g["game_id"] == "steam-20")
    from openachievements.adapters.base import utc_iso
    assert g20["status"] == "dropped" and g20["last_played"] == utc_iso(NOW - 60)


def test_missing_titles_are_filled_from_the_store(profile, tmp_path):
    profile.register_game("steam-77", "Steam app 77")
    replies = {"77": {"success": True, "data": {"name": "Delisted Classic", "type": "game"}}}
    assert steam_local.fill_names(profile, [77], fetch=lambda url: replies, pause=0) == 1
    assert profile.state()["games"]["steam-77"]["title"] == "Delisted Classic"


def test_the_library_knows_each_games_latest_sign_of_play(profile):
    profile.register_game("celeste", "Celeste")
    profile.record("game.metadata_updated", {"last_played": "2026-09-01T00:00:00.000Z"}, game_id="celeste")
    profile.record("game.metadata_updated", {"last_played": "2025-01-01T00:00:00.000Z"}, game_id="celeste")
    game = profile.library()["games"][0]
    assert game["last_played"] == "2026-09-01T00:00:00.000Z" == game["last_activity"]   # older never wins


# ---- games found by the saves they left --------------------------------------------------

class Index:
    entries = {1636440: (0, 0, 1, "SILENT HILL: Townfall"), 367520: (0, 0, 1, "Hollow Knight"),
               111: (0, 0, 1, "SAVED"), 222: (0, 0, 1, "Quiet Game")}


def save_folder(root: Path, rel: str, files=("slot1.sav",), when=None):
    folder = root / rel
    folder.mkdir(parents=True, exist_ok=True)
    for f in files:
        (folder / f).write_bytes(b"x")
        if when:
            os.utime(folder / f, (when, when))
    return folder


def test_games_are_found_by_their_saves_and_generic_folders_are_not(tmp_path):
    roots = [tmp_path / "Local", tmp_path / "LocalLow"]
    save_folder(roots[0], "Townfall/Saved/SaveGames", ["Slot0.sav"], when=NOW)
    save_folder(roots[1], "Team Cherry/Hollow Knight", ["user1.dat"])                 # .dat alone is not a save
    save_folder(roots[1], "Team Cherry/Hollow Knight", ["user1_save.dat"], when=NOW - 86400)
    save_folder(roots[0], "SomeEngine/Saved", ["x.sav"])                              # "SAVED" is a folder, not a game
    save_folder(roots[0], "Quiet Game", ["readme.txt", "settings.json"])               # no save-like files
    found = {h["ident"]: h for h in ad.discover_saved_games(ad.TitleMatcher(Index, {}), roots)}
    assert set(found) == {("steam", 1636440), ("steam", 367520)}
    assert found[("steam", 1636440)]["when"] == NOW and found[("steam", 1636440)]["folder"].endswith("Townfall")


def test_a_subtitle_is_a_name_only_when_it_is_unique():
    class Two:
        entries = {1: (0, 0, 1, "Alpha: Homecoming"), 2: (0, 0, 1, "Beta: Homecoming"), 3: (0, 0, 1, "Gamma: Short")}
    m = ad.TitleMatcher(Two, {})
    assert m.match(["Homecoming"]) is None and "short" not in m.by_name


def test_reading_the_shared_state_never_changes_it(profile, tmp_path):
    """State is cached and shared for speed; every normal flow must only read it."""
    import json
    from openachievements import reducer
    from openachievements.catalog import steam as cat
    from test_catalog import GAME, fake_steam
    cat.crawl([(10, None)], tmp_path / "c", fake_steam({10: (4, GAME)}))
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 10, status="backlog")
    profile.record_unlock("steam-10:first-steps", adapter="test")
    profile.library(); profile.set_status("steam-10", "playing")
    steam_local.add_catalogue_packs(profile, cat.CatalogIndex(tmp_path / "c"))
    fresh = reducer.reduce(profile.log.read().events)
    as_json = lambda s: json.dumps(s, sort_keys=True, default=sorted)
    assert as_json(profile.state()) == as_json(fresh)


# ---- achievement lists for games the crawl has not reached --------------------------------

def test_library_games_get_their_list_from_steam_newest_first(profile, tmp_path):
    from fastapi.testclient import TestClient
    from openachievements.catalog import steam as cat
    from openachievements.local_app import create_local_app
    from test_catalog import GAME, fake_steam, page, row
    for appid, when in ((1636440, "2026-09-30T00:00:00.000Z"), (20, "2020-01-01T00:00:00.000Z"), (30, None)):
        profile.register_game(f"steam-{appid}", f"App {appid}")
        if when:
            profile.record("game.metadata_updated", {"last_played": when}, game_id=f"steam-{appid}")
    calls = []
    steam = fake_steam({1636440: (1, page("SILENT HILL: Townfall", [row("First Steps", "Begin", "aa", "50.0")])),
                        20: (4, GAME)}, calls)
    f = cat.PackFetcher(profile, fetch=steam)
    f.want(None)
    f._thread.join(5)
    assert f.fetched == [1636440, 20]                        # newest first; app 30 has no list on Steam
    town = next(g for g in profile.library()["games"] if g["game_id"] == "steam-1636440")
    assert town["total"] == 1 and town["title"] == "App 1636440"
    f.want(None)                                              # nothing left to do: nothing asked again
    assert f._thread is None or not f._thread.is_alive()
    # A copy Steam does not track unlocks from what an adapter saw (a save), never by hand.
    profile.record_unlock("steam-1636440:first-steps", adapter="save-file", provenance="save-derived")
    a = next(g for g in profile.library()["games"] if g["game_id"] == "steam-1636440")["achievements"][0]
    assert a["unlocked"] and a["provenance"] == ["save-derived"]
    client = TestClient(create_local_app(profile, token="t" * 32))
    assert client.post("/v1/local/games/celeste/fetch-achievements", headers={"X-OA-Token": "t" * 32}).status_code == 400
