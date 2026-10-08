"""Ubisoft achievements from LumaPlay's registry values, named from Ubisoft Connect's cache.
The registry and the cache folder are injected: never the real ones."""
import io
import zipfile

from openachievements.adapters import emulator, ubisoft

APP = 635


def archive(folder, app=APP, n=7, lines=None, lang="en-US"):
    lines = lines or ["1\tFirst Steps\tFinish the tutorial", "2\tSharpshooter\tLand 100 headshots",
                      "3\tGhost\tFinish a mission unseen"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{lang}_loc.txt", "\r\n".join(lines))
        z.writestr("fr-FR_loc.txt", "1\tPremiers pas\tx\r\n2\tTireur\ty\r\n3\tFantome\tz")
        z.writestr("achievements.dat", b"\x00" * 16)
        z.writestr("1.png", b"\x89PNG")
    path = folder / "cache" / "achievements" / f"{app}_{n}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())
    return path


def watcher(profile, unlocks, connect, titles=None):
    w = ubisoft.UbisoftWatcher(profile, read_unlocks=lambda: unlocks, read_connect=lambda: connect,
                               read_titles=lambda: titles or {APP: "Far Cry Test"})
    return w


def test_lumaplay_values_give_the_earned_numbers():
    assert ubisoft.unlocked_ids({"ACH_1": 1, "ACH_2": 0, "3": 1, "Name": 1, "ach_4": "1"}) == {1, 3, 4}


def test_the_english_list_is_read_from_the_cached_archive(tmp_path):
    rows = ubisoft.read_archive(archive(tmp_path).read_bytes())
    assert [(r["id"], r["name"]) for r in rows] == [(1, "First Steps"), (2, "Sharpshooter"), (3, "Ghost")]
    assert rows[1]["description"] == "Land 100 headshots"


def test_another_language_is_used_when_there_is_no_english(tmp_path):
    rows = ubisoft.read_archive(archive(tmp_path, lang="de-DE", lines=["1\tErste\tx"]).read_bytes())
    assert [r["name"] for r in rows] == ["Erste"]


def test_a_game_joins_with_its_names_and_earned_ones_unlock(profile, tmp_path):
    archive(tmp_path)
    unlocked = watcher(profile, {APP: {2}}, tmp_path).poll()
    state = profile.state()
    gid = ubisoft.game_id_for(APP)
    assert state["games"][gid]["title"] == "Far Cry Test" and state["games"][gid]["external_ids"] == {"ubisoft": APP}
    assert {k: v["name"] for k, v in state["packs"][gid]["achievements"].items()} == {
        "a1": "First Steps", "a2": "Sharpshooter", "a3": "Ghost"}
    assert [e["achievement_id"] for e in unlocked] == [f"{gid}:a2"]
    assert unlocked[0]["payload"] == {"provenance": "emulator", "mode": "lumaplay"}


def test_the_newest_cached_archive_wins(profile, tmp_path):
    archive(tmp_path, n=3, lines=["1\tOld Name\tx", "2\tOld Two\ty"])
    archive(tmp_path, n=9)
    watcher(profile, {APP: {1}}, tmp_path).poll()
    assert profile.state()["packs"][ubisoft.game_id_for(APP)]["achievements"]["a1"]["name"] == "First Steps"


def test_a_game_without_cached_names_is_reported_not_invented(profile, tmp_path):
    w = watcher(profile, {APP: {1, 2}}, tmp_path)
    assert w.poll() == []
    assert ubisoft.game_id_for(APP) not in profile.state()["games"]
    problems = w.new_problems()
    assert len(problems) == 1 and "not on this computer" in problems[0]


def test_a_new_unlock_is_picked_up_on_the_next_look(profile, tmp_path):
    archive(tmp_path)
    unlocks = {APP: {1}}
    w = watcher(profile, unlocks, tmp_path)
    w.poll()
    unlocks[APP] = {1, 3}
    w._look = -1e18
    assert [e["achievement_id"] for e in w.poll()] == [f"{ubisoft.game_id_for(APP)}:a3"]
    w._look = -1e18
    assert w.poll() == []                                         # nothing changed


def test_a_broken_archive_is_reported(profile, tmp_path):
    path = archive(tmp_path)
    path.write_bytes(b"not a zip")
    w = watcher(profile, {APP: {1}}, tmp_path)
    assert w.poll() == []
    assert "not a zip" in w.new_problems()[0]


def test_the_emulator_switch_turns_it_off(profile, tmp_path):
    archive(tmp_path)
    emulator.set_enabled(profile, False)
    assert watcher(profile, {APP: {1}}, tmp_path).poll() == []


def test_off_windows_there_is_nothing_to_read(monkeypatch):
    monkeypatch.setattr(ubisoft.sys, "platform", "linux")
    assert ubisoft.lumaplay() == {} and ubisoft.connect_dir() is None and ubisoft.installed_titles() == {}
