"""Xbox 360 achievements Xenia recorded, read from GPDs laid out as Xenia's XDBF loader reads them."""
import struct
from datetime import datetime, timezone

from openachievements.adapters import emulator, xbox, xenia

XUID = "E030000012345678"
TITLE = 0x4D5307E6
WHEN = datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc).timestamp()


def s16(text):
    return text.encode("utf-16-be") + b"\x00\x00"


def filetime(unix):
    return int((unix + xenia.FILETIME_TO_UNIX) * 10_000_000)


def achievement(aid, name, desc, locked, score, earned_at=None, secret=False):
    flags = (0 if secret else xenia.SHOW_UNACHIEVED) | (xenia.ACHIEVED if earned_at else 0) | 3
    head = struct.pack(">IIIIIQ", 0x1C, aid, aid, score, flags, filetime(earned_at) if earned_at else 0)
    return head + s16(name) + s16(desc) + s16(locked)


def xdbf(items, spare_entries=2, free_slots=1):
    """An XDBF file: header, `used + spare` entry slots, free slots, data.
    Offsets count from the start of the data, as XdbfFile reads them."""
    count = len(items) + spare_entries
    table, data = b"", b""
    for section, eid, payload in items:
        table += struct.pack(">HQII", section, eid, len(data), len(payload))
        data += payload
    table += b"\x00" * 18 * spare_entries
    free = struct.pack(">II", len(data), 0) + b"\x00" * 8 * (free_slots - 1)
    header = struct.pack(">4sIIIII", b"XDBF", 0x10000, count, len(items), free_slots, 1)
    return header + table + free + data


def title_gpd(earned=None, name="Test Halo"):
    earned = earned or {}
    items = [(xenia.SECTION_STRING, xenia.TITLE_STRING_ID, s16(name))] if name else []
    items += [
        (xenia.SECTION_ACHIEVEMENT, 1, achievement(1, "Started", "Begin the campaign", "Begin it", 10, earned.get(1))),
        (xenia.SECTION_ACHIEVEMENT, 2, achievement(2, "Legend", "Finish on Legendary", "Finish it", 50, earned.get(2))),
        (xenia.SECTION_ACHIEVEMENT, 3, achievement(3, "Hidden Skull", "Find the skull", "Secret", 20, earned.get(3),
                                                   secret=True)),
    ]
    return xdbf(items)


def dashboard(names):
    items = [(xenia.SECTION_TITLE, tid, struct.pack(">I", tid) + b"\x00" * 0x24 + s16(name))
             for tid, name in names.items()]
    return xdbf(items)


def install(storage, gpd_bytes, title=TITLE, dash=None):
    folder = storage / "content" / XUID / xenia.DASHBOARD / xenia.PROFILE_PACKAGE / XUID
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{title:08X}.gpd").write_bytes(gpd_bytes)
    (folder / f"{xenia.DASHBOARD}.gpd").write_bytes(dash or dashboard({title: "Dashboard Name"}))
    return folder


def watcher(profile, storage):
    xenia.remember(profile, storage)
    return xenia.XeniaWatcher(profile, running=lambda: set())


def test_a_gpd_reads_its_title_achievements_and_what_was_earned():
    game = xenia.parse_title_gpd(title_gpd({2: WHEN}))
    assert game["title"] == "Test Halo"
    by_id = {a["id"]: a for a in game["achievements"]}
    assert by_id[2]["earned"] and abs(by_id[2]["when"] - WHEN) < 1 and by_id[2]["gamerscore"] == 50
    assert not by_id[1]["earned"] and by_id[1]["when"] is None
    assert by_id[3]["hidden"] and not by_id[1]["hidden"]
    assert by_id[1]["description"] == "Begin the campaign" and by_id[1]["locked"] == "Begin it"


def test_the_dashboard_names_a_game_without_its_own_title_string():
    assert xenia.dashboard_titles(dashboard({TITLE: "From The Dashboard"})) == {TITLE: "From The Dashboard"}


def test_a_file_that_is_not_a_gpd_is_refused():
    for bad in (b"", b"XDBF" + b"\x00" * 10, b"NOPE" + b"\x00" * 40, title_gpd()[:60]):
        try:
            xenia.parse_title_gpd(bad)
        except xenia.XeniaError:
            continue
        raise AssertionError(f"accepted {bad[:8]!r}")


def test_a_game_joins_with_its_list_and_earned_ones_unlock(profile, tmp_path):
    install(tmp_path, title_gpd({2: WHEN}))
    w = watcher(profile, tmp_path)
    unlocked = w.poll()
    gid = xbox.game_id_for(TITLE)
    state = profile.state()
    assert state["games"][gid]["title"] == "Test Halo" and state["games"][gid]["external_ids"] == {"xbox": TITLE}
    pack = state["packs"][gid]
    assert set(pack["achievements"]) == {"a1", "a2", "a3"}
    assert pack["achievements"]["a2"]["points"] == 50 and pack["achievements"]["a3"]["hidden"] is True
    assert [e["achievement_id"] for e in unlocked] == [f"{gid}:a2"]
    assert unlocked[0]["payload"] == {"provenance": "emulator", "mode": "xenia"}
    assert unlocked[0]["occurred_at"].startswith("2026-10-01T18:30")
    assert w.poll() == []


def test_the_dashboard_name_is_used_when_the_game_has_none(profile, tmp_path):
    install(tmp_path, title_gpd(name=None), dash=dashboard({TITLE: "Halo From Dashboard"}))
    watcher(profile, tmp_path).poll()
    assert profile.state()["games"][xbox.game_id_for(TITLE)]["title"] == "Halo From Dashboard"


def test_a_new_unlock_is_picked_up_when_the_gpd_changes(profile, tmp_path):
    folder = install(tmp_path, title_gpd({2: WHEN}))
    w = watcher(profile, tmp_path)
    w.poll()
    (folder / f"{TITLE:08X}.gpd").write_bytes(title_gpd({2: WHEN, 3: WHEN + 30}))
    assert [e["achievement_id"] for e in w.poll()] == [f"{xbox.game_id_for(TITLE)}:a3"]


def test_a_list_the_xbox_import_installed_is_never_replaced(profile, tmp_path):
    title = {"titleId": TITLE, "name": "Halo (Xbox Live)"}
    defs = [{"id": i, "name": f"Live {i}", "description": "", "gamerscore": 5} for i in (1, 2, 3)]
    events = xbox.plan_title(profile, profile.state(), set(), {"xuid": "1"}, title, defs, [])
    profile.commit(events)
    install(tmp_path, title_gpd({2: WHEN}))
    unlocked = watcher(profile, tmp_path).poll()
    pack = profile.state()["packs"][xbox.game_id_for(TITLE)]
    assert pack["achievements"]["a2"]["name"] == "Live 2"
    assert [e["achievement_id"] for e in unlocked] == [f"{xbox.game_id_for(TITLE)}:a2"]


def test_content_root_from_the_config_file_is_followed(profile, tmp_path):
    elsewhere = tmp_path / "big-disk" / "xcontent"
    folder = elsewhere / XUID / xenia.DASHBOARD / xenia.PROFILE_PACKAGE / XUID
    folder.mkdir(parents=True)
    (folder / f"{TITLE:08X}.gpd").write_bytes(title_gpd({1: WHEN}))
    storage = tmp_path / "xenia"
    storage.mkdir()
    (storage / "xenia-canary.config.toml").write_text(f'[Storage]\ncontent_root = "{elsewhere.as_posix()}"\n')
    assert [e["achievement_id"] for e in watcher(profile, storage).poll()] == [f"{xbox.game_id_for(TITLE)}:a1"]


def test_xenia_running_on_windows_is_remembered(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(xenia.sys, "platform", "win32")
    w = xenia.XeniaWatcher(profile, running=lambda: {str(tmp_path / "xenia_canary.exe").lower()})
    w.poll()
    assert [str(p).lower() for p in xenia.remembered(profile)] == [str(tmp_path).lower()]


def test_the_emulator_switch_turns_it_off(profile, tmp_path):
    install(tmp_path, title_gpd({2: WHEN}))
    emulator.set_enabled(profile, False)
    assert watcher(profile, tmp_path).poll() == []


def test_a_broken_gpd_is_reported_once_and_others_still_read(profile, tmp_path):
    folder = install(tmp_path, title_gpd({2: WHEN}))
    (folder / "4D530001.gpd").write_bytes(b"XDBF garbage")
    w = watcher(profile, tmp_path)
    assert [e["achievement_id"] for e in w.poll()] == [f"{xbox.game_id_for(TITLE)}:a2"]
    problems = w.new_problems()
    assert len(problems) == 1 and "4D530001" in problems[0]
    w.poll()
    assert w.new_problems() == []
