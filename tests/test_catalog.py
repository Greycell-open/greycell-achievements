"""The public Steam catalogue: parsing, packs, the resumable crawl, installing."""
import json

import pytest

from openachievements import reducer
from openachievements.catalog import steam as cat
from openachievements.cli import main


def row(name, desc, icon, pct):
    return f"""
							<div class="achieveRow ">
					<div class="achieveImgHolder">
						<img src="https://shared.akamai.steamstatic.com/community_assets/images/apps/10/{icon}.jpg" width="64" height="64" border="0" />
					</div>
										<div class="achieveTxtHolder">
						<div class="achieveFill" style="width: 50%">
						</div>
						<div class="achievePercent">{pct}%</div>
						<div class="achieveTxt">
							<h3>{name}</h3>
							<h5>{desc}</h5>
						</div>
					</div>
					<div style="clear: both;"></div>
				</div>"""


def page(title, rows):
    return (f"<html><head><title>Steam Community :: {title} :: Achievements</title></head><body>"
            + "".join(rows) + "</body></html>")


GAME = page("Test &amp; Game", [
    row("First Steps", "Finish the tutorial", "aa11", "91.2"),
    row("Secret Ending", "", "bb22", "3.4"),              # hidden: Steam blanks the description
    row("Tom &amp; Jerry", "Say &quot;hi&quot;", "cc33", "12.0"),
    row("First Steps", "Finish it again", "dd44", "10.0"),  # same name, different achievement
])
NO_STATS = page("Error", []).replace("</body>", "<h3>No stats are available at this time.</h3></body>")


def fake_steam(games, calls=None, schemas=None):
    """appid -> (percent entries, page) | None (no achievements) | 'nostats'.
    `schemas`: appid -> GetGameAchievements rows; other apps get no schema."""
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if "GetGameAchievements" in url:
            appid = int(url.split("appid=")[1].split("&")[0])
            rows = (schemas or {}).get(appid)
            return 200, json.dumps({"response": {"achievements": rows} if rows else {}})
        appid = int(url.split("gameid=")[1] if "gameid=" in url else url.split("/stats/")[1].split("/")[0])
        game = games.get(appid)
        if game is None:
            return 403, "{}"
        if "gameid=" in url:
            n = 1 if game == "nostats" else game[0]
            return 200, json.dumps({"achievementpercentages": {"achievements": [{"name": f"A{i}", "percent": "1"}
                                                                                 for i in range(n)]}})
        return 200, NO_STATS if game == "nostats" else game[1]
    return fetch


def test_the_community_page_parses_into_rows():
    title, rows = cat.parse_community_page(GAME)
    assert title == "Test & Game"
    assert [r["name"] for r in rows] == ["First Steps", "Secret Ending", "Tom & Jerry", "First Steps"]
    assert rows[1]["description"] == ""
    assert rows[2]["description"] == 'Say "hi"'
    assert rows[0]["icon"].endswith("/10/aa11.jpg") and rows[0]["percent"] == "91.2"


def test_a_game_becomes_a_valid_pack_with_unique_ids():
    entry = cat.build_pack(10, "Test & Game", cat.parse_community_page(GAME)[1], crawled_at="2026-09-30T00:00:00.000Z")
    ids = [a["id"] for a in entry["achievements"]]
    assert ids == ["first-steps", "secret-ending", "tom-jerry", "first-steps-2"]
    assert entry["pack"]["id"] == "steam-10" and entry["pack"]["source"] == "steam-catalog"
    assert entry["pack"]["games"][0]["external_ids"] == {"steam": 10}
    assert "rarity" not in entry["achievements"][0]          # a percentage is not history: rarity.py keeps it
    assert [a["hidden"] for a in entry["achievements"]] == [False, True, False, False]   # blank = hidden


def test_the_crawl_sorts_games_and_resumes(tmp_path):
    games = {10: (4, GAME), 20: None, 30: "nostats"}
    calls = []
    s = cat.crawl([(10, None), (20, None), (30, None)], tmp_path, fake_steam(games, calls))
    assert (s.packs, s.without_achievements, s.unavailable, s.problems) == (1, 1, 1, [])
    assert len(cat.load(tmp_path)[10]["achievements"]) == 4
    # Nothing without achievements costs a page fetch.
    assert not any("/stats/20/" in c for c in calls)
    calls.clear()
    again = cat.crawl([(10, None), (20, None), (30, None)], tmp_path, fake_steam(games, calls))
    assert again.skipped_done == 3 and calls == []


def test_a_failure_is_recorded_and_retried_next_run(tmp_path):
    broken = page("Broken", [])                    # listed achievements, but no rows and no notice
    s = cat.crawl([(10, None)], tmp_path, fake_steam({10: (2, broken)}))
    assert s.packs == 0 and "community page showed none" in s.problems[0]
    fixed = cat.crawl([(10, None)], tmp_path, fake_steam({10: (4, GAME)}))
    assert fixed.packs == 1


def test_a_count_mismatch_keeps_the_pack_and_says_so(tmp_path):
    cat.crawl([(10, None)], tmp_path, fake_steam({10: (5, GAME)}))
    assert "5" in cat.load(tmp_path)[10]["note"]


def test_a_refresh_replaces_the_older_crawl_and_a_torn_line_is_ignored(tmp_path):
    cat.crawl([(10, None)], tmp_path, fake_steam({10: (4, GAME)}))
    newer = page("Test & Game", [row("Only One", "d", "ee55", "1.0")])
    cat.crawl([(10, None)], tmp_path, fake_steam({10: (1, newer)}), refresh=True)
    with open(tmp_path / cat.PACKS_FILE, "a", encoding="utf-8") as fh:
        fh.write('{"appid": 10, "achiev')          # a crash mid-write
    assert [a["name"] for a in cat.load(tmp_path)[10]["achievements"]] == ["Only One"]


def test_the_fetcher_paces_each_host_and_backs_off_on_429(monkeypatch):
    import urllib.error
    import urllib.request
    now = [0.0]
    slept, notes = [], []

    def sleep(s):
        slept.append(round(s, 2))
        now[0] += s
    answers = iter([urllib.error.HTTPError("u", 429, "slow down", {"Retry-After": "7"}, None), "ok", "ok"])

    class Resp:
        status = 200

        def __init__(self, body): self.body = body
        def read(self): return self.body.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return Resp(a)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    f = cat.PacedFetcher(pace=2.0, sleep=sleep, clock=lambda: now[0], notify=notes.append)
    assert f("https://a.example/x") == (200, "ok")
    assert f("https://a.example/y") == (200, "ok")
    assert f.backoffs == 1 and "429" in notes[0]
    assert any(7 <= s < 8 for s in slept)          # honoured Retry-After
    assert slept.count(2.0) >= 1                    # paced the second request to the same host


def test_a_catalogued_game_installs_and_renders(profile, tmp_path, capsys):
    cat.crawl([(10, None)], tmp_path / "catalog", fake_steam({10: (4, GAME)}))
    home, config = str(profile.folder.parent.parent), str(profile.config.dir)
    main(["--home", home, "--config", config, "catalog", "--dir", str(tmp_path / "catalog"), "list", "test"])
    assert "Test & Game" in capsys.readouterr().out
    main(["--home", home, "--config", config, "catalog", "--dir", str(tmp_path / "catalog"), "install", "10"])
    state = profile.state()
    assert state["games"]["steam-10"]["title"] == "Test & Game"
    assert len(state["packs"]["steam-10"]["achievements"]) == 4
    profile.record_unlock("steam-10:secret-ending", adapter="test")
    game = next(g for g in profile.library()["games"] if g["game_id"] == "steam-10")
    assert sum(a["unlocked"] for a in game["achievements"]) == 1
    with pytest.raises(SystemExit):
        main(["--home", home, "--config", config, "catalog", "--dir", str(tmp_path / "catalog"), "install", "99"])


# ---- first critic review of the catalogue -------------------------------------------

def test_a_row_without_an_icon_is_kept_and_never_stops_the_crawl(tmp_path):
    no_icon = GAME.replace('<img src="https://shared.akamai.steamstatic.com/community_assets/images/apps/10/aa11.jpg" '
                           'width="64" height="64" border="0" />', "")
    s = cat.crawl([(10, None), (20, None)], tmp_path, fake_steam({10: (4, no_icon), 20: (4, GAME)}))
    assert s.packs == 2 and s.problems == []
    assert "icon" not in cat.load(tmp_path)[10]["achievements"][0]


def test_any_unexpected_failure_is_contained_to_its_app(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AttributeError("something new")
    monkeypatch.setattr(cat, "build_pack", boom)
    s = cat.crawl([(10, None)], tmp_path, fake_steam({10: (4, GAME)}))
    assert "AttributeError" in s.problems[0]
    assert json.loads((tmp_path / cat.STATE_FILE).read_text())["10"]["status"] == "error"


def test_a_reply_that_is_not_an_answer_is_retried_not_recorded_as_none(tmp_path):
    def flaky(url):
        return 200, "<html>Steam is having a moment</html>"
    s = cat.crawl([(10, None)], tmp_path, flaky)
    assert s.without_achievements == 0 and "not the expected JSON" in s.problems[0]
    assert cat.crawl([(10, None)], tmp_path, fake_steam({10: (4, GAME)})).packs == 1


def test_a_short_page_is_kept_but_crawled_again(tmp_path):
    cat.crawl([(10, None)], tmp_path, fake_steam({10: (5, GAME)}))
    assert json.loads((tmp_path / cat.STATE_FILE).read_text())["10"]["status"] == "partial"
    again = cat.crawl([(10, None)], tmp_path, fake_steam({10: (4, GAME)}))
    assert again.packs == 1 and again.skipped_done == 0
    assert json.loads((tmp_path / cat.STATE_FILE).read_text())["10"]["status"] == "pack"


def test_a_crash_mid_line_is_repaired_before_resuming(tmp_path):
    (tmp_path / cat.PACKS_FILE).write_text('{"appid": 10, "crawl', encoding="utf-8")   # died mid-record
    cat.crawl([(10, None)], tmp_path, fake_steam({10: (4, GAME)}))
    assert len(cat.load(tmp_path)[10]["achievements"]) == 4
    lines = (tmp_path / cat.PACKS_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["appid"] == 10


def test_ids_survive_renames_icon_changes_and_reordering(tmp_path):
    cat.crawl([(10, None)], tmp_path, fake_steam({10: (4, GAME)}))
    first = {a["name"] + "|" + a.get("icon", ""): a["id"] for a in cat.load(tmp_path)[10]["achievements"]}
    later = page("Test & Game", [
        row("First Steps", "Finish it again", "dd44", "10.0"),            # the duplicate name, now first
        row("First Steps", "Finish the tutorial", "aa11", "91.2"),
        row("Secret Ending (fixed typo)", "", "bb22", "3.4"),              # renamed, same icon
        row("Tom &amp; Jerry", "Say &quot;hi&quot;", "zz99", "12.0"),      # new icon, same name
        row("Brand New", "Added in a patch", "ee55", "1.0"),
    ])
    cat.crawl([(10, None)], tmp_path, fake_steam({10: (5, later)}), refresh=True)
    ids = {a["name"] + "|" + a.get("icon", "").rsplit("/", 1)[-1]: a["id"] for a in cat.load(tmp_path)[10]["achievements"]}
    assert ids["First Steps|aa11.jpg"] == "first-steps" and ids["First Steps|dd44.jpg"] == "first-steps-2"
    assert ids["Secret Ending (fixed typo)|bb22.jpg"] == "secret-ending"
    assert ids["Tom & Jerry|zz99.jpg"] == "tom-jerry"
    assert ids["Brand New|ee55.jpg"] == "brand-new"


def test_an_unlock_keeps_pointing_at_its_achievement_after_a_rename(profile, tmp_path):
    import tempfile
    from pathlib import Path
    cat.crawl([(10, None)], tmp_path / "c", fake_steam({10: (4, GAME)}))

    def install():
        with tempfile.TemporaryDirectory() as tmp:
            profile.install_pack(cat.write_pack_folder(cat.load(tmp_path / "c")[10], Path(tmp) / "p"))
    install()
    profile.record_unlock("steam-10:secret-ending", adapter="test")
    renamed = GAME.replace("<h3>Secret Ending</h3>", "<h3>The Secret Ending</h3>")
    cat.crawl([(10, None)], tmp_path / "c", fake_steam({10: (4, renamed)}), refresh=True)
    install()
    game = next(g for g in profile.library()["games"] if g["game_id"] == "steam-10")
    shown = {a["name"]: a["unlocked"] for a in game["achievements"]}
    assert shown["The Secret Ending"] is True


def test_progress_is_reported_steadily(tmp_path):
    notes = []
    games = {i: None for i in range(1, 251)}
    cat.crawl([(i, None) for i in games], tmp_path, fake_steam(games), report=notes.append)
    progress = [n for n in notes if n.startswith("==")]
    assert len(progress) == 2 and progress[0].startswith("== 100/250 apps, last 100")


def _big_entry(appid, n):
    rows = [{"name": f"Achievement {i}", "description": f"Do thing number {i} " + "x" * 200,
             "icon": f"https://shared.akamai.steamstatic.com/community_assets/images/apps/{appid}/{i:040x}.jpg",
             "percent": "1.0"} for i in range(n)]
    return cat.build_pack(appid, "Huge Game", rows, crawled_at="2026-10-01T00:00:00.000Z")


@pytest.mark.parametrize("count", [520, 5000])
def test_packs_of_any_steam_size_install_through_the_profile(profile, tmp_path, count):
    import tempfile
    from pathlib import Path
    entry = _big_entry(440, count)
    with tempfile.TemporaryDirectory() as tmp:
        profile.install_pack(cat.write_pack_folder(entry, Path(tmp) / "p"))
    events = [e for e in profile.log.read().events if e["event_type"] == "pack.installed"]
    assert len(events) > 1 and all(e["payload"]["part"]["count"] == len(events) for e in events)
    assert len(profile.state()["packs"]["steam-440"]["achievements"]) == count
    profile.record_unlock("steam-440:achievement-7", adapter="test")
    assert profile.index.rebuild() == []
    assert len(profile.library()["games"][0]["achievements"]) == count


def test_a_half_delivered_pack_is_not_shown_until_complete(profile, tmp_path):
    from openachievements import events as ev
    from openachievements.packs import split_for_events, validate_definitions
    entry = _big_entry(440, 520)
    description = validate_definitions(entry["pack"], entry["achievements"])
    parts = [ev.make_event("pack.installed", profile_id=profile.profile_id, device_id=profile.device_id,
                           payload=payload) for payload in split_for_events(description)]
    profile.commit(parts[:-1])
    assert "steam-440" not in profile.state()["packs"]
    profile.commit(parts[-1:])
    assert len(profile.state()["packs"]["steam-440"]["achievements"]) == 520


def test_a_report_that_cannot_print_never_stops_the_crawl(tmp_path):
    def cp1252_console(message):
        message.encode("cp1252")                   # raises on a title such as "Ōkami"
    okami = page("\u014ckami HD", [row("Brush", "Use it", "aa11", "50.0")])
    s = cat.crawl([(10, None), (20, None)], tmp_path, fake_steam({10: (1, okami), 20: (4, GAME)}),
                  report=cp1252_console)
    assert s.packs == 2


def test_the_cli_prints_titles_its_console_cannot_encode(tmp_path, monkeypatch, capsys):
    import io
    import sys
    okami = page("\u014ckami HD", [row("Brush", "Use it", "aa11", "50.0")])
    cat.crawl([(10, None)], tmp_path, fake_steam({10: (1, okami)}))
    legacy = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", legacy)
    main(["catalog", "--dir", str(tmp_path), "list"])
    legacy.flush()
    assert b"kami HD" in legacy.buffer.getvalue()
