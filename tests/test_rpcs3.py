"""PS3 trophies RPCS3 recorded, read from files laid out as RPCS3 writes them."""
import struct
from datetime import datetime, timezone

from openachievements.adapters import emulator, psn, rpcs3

NP = "NPWR00001_00"
WHEN = datetime(2026, 10, 8, 20, 15, tzinfo=timezone.utc).timestamp()
CONF = f"""<!--Sce-Np-Trophy-Signature: c2lnbmF0dXJl-->
<?xml version="1.0" encoding="UTF-8"?>
<trophyconf version="1.1" policy="large">
<npcommid>{NP}</npcommid>
<trophyset-version>01.00</trophyset-version>
<title-name>Test Racer</title-name>
<trophy id="000" hidden="no" ttype="P" pid="-1"><name>Champion</name><detail>Every trophy</detail></trophy>
<trophy id="001" hidden="no" ttype="B" pid="000"><name>First Lap</name><detail>Finish a lap</detail></trophy>
<trophy id="002" hidden="yes" ttype="G" pid="000"><name>Secret Track</name><detail>Find it</detail></trophy>
</trophyconf>
"""


def tick(unix):
    return int((unix + rpcs3.TICKS_TO_UNIX) * 1_000_000)


def usr(states):
    """TROPUSR.DAT as RPCS3's TROPUSRLoader::Save writes it: header, two table
    headers, the type 4 table, then the type 6 table. states: id -> unix time
    when earned, None when not."""
    n = len(states)
    header = struct.pack(">IIII32x", rpcs3.MAGIC, 0x10000, 2, 0)
    offset4 = 0x30 + 2 * 32
    offset6 = offset4 + n * 0x60                                  # sizeof(TROPUSREntry4)
    # RPCS3 writes sizeof(entry) - 0x10: 0x60 - 0x10 for type 4, 0x70 - 0x10 for type 6
    tables = (struct.pack(">IIIIQQ", 4, 0x50, 1, n, offset4, 0)
              + struct.pack(">IIIIQQ", 6, 0x60, 1, n, offset6, 0))
    t4 = b"".join(struct.pack(">IIIIIII68x", 4, 0x50, i, 0, i, 4, 0xFFFFFFFF) for i in range(n))
    t6 = b""
    for i, (tid, when) in enumerate(sorted(states.items())):
        stamp = tick(when) if when else 0
        t6 += struct.pack(">IIIIIIIIQQ64x", 6, 0x60, i, 0, tid, 1 if when else 0, 0, 0, stamp, stamp)
    assert len(t4) == n * 0x60 and len(t6) == n * 0x70           # sizeof(TROPUSREntry4/6)
    return header + tables + t4 + t6


def install(root, states, user="00000001", np=NP, conf=CONF):
    folder = root / "dev_hdd0" / "home" / user / "trophy" / np
    folder.mkdir(parents=True)
    (folder / "TROPCONF.SFM").write_text(conf, encoding="utf-8")
    (folder / "TROPUSR.DAT").write_bytes(usr(states))
    return folder


def watcher(profile, root):
    rpcs3.remember(profile, root)
    return rpcs3.Rpcs3Watcher(profile, running=lambda: set())


def test_the_trophy_list_is_read_after_its_signature_comment():
    conf = rpcs3.parse_conf(CONF.encode())
    assert conf["np_id"] == NP and conf["title"] == "Test Racer"
    assert [(t["id"], t["grade"], t["hidden"]) for t in conf["trophies"]] == [
        (0, "platinum", False), (1, "bronze", False), (2, "gold", True)]


def test_only_earned_trophies_are_read_with_their_time():
    earned = rpcs3.parse_usr(usr({0: None, 1: WHEN, 2: None}))
    assert list(earned) == [1] and abs(earned[1] - WHEN) < 1


def test_a_file_that_is_not_a_trophy_record_is_refused():
    for bad in (b"", b"\x00" * 64, usr({1: WHEN})[:0x50]):
        try:
            rpcs3.parse_usr(bad)
        except rpcs3.Rpcs3Error:
            continue
        raise AssertionError(f"accepted {bad[:8]!r}")


def test_a_game_joins_with_its_trophies_and_earned_ones_unlock(profile, tmp_path):
    install(tmp_path, {0: None, 1: WHEN, 2: None})
    w = watcher(profile, tmp_path)
    unlocked = w.poll()
    state = profile.state()
    game = state["games"]["psn-npwr00001_00"]
    assert game["title"] == "Test Racer" and game["external_ids"] == {"psn": NP}
    pack = state["packs"]["psn-npwr00001_00"]
    assert set(pack["achievements"]) == {"t0", "t1", "t2"}
    assert pack["achievements"]["t2"]["hidden"] is True and pack["achievements"]["t0"]["points"] == 300
    assert [e["achievement_id"] for e in unlocked] == ["psn-npwr00001_00:t1"]
    assert unlocked[0]["payload"] == {"provenance": "emulator", "mode": "rpcs3"}
    assert unlocked[0]["occurred_at"].startswith("2026-10-08T20:15")
    assert w.poll() == []                                         # nothing changed, nothing read


def test_a_new_unlock_is_picked_up_when_the_file_changes(profile, tmp_path):
    folder = install(tmp_path, {0: None, 1: WHEN, 2: None})
    w = watcher(profile, tmp_path)
    w.poll()
    (folder / "TROPUSR.DAT").write_bytes(usr({0: None, 1: WHEN, 2: WHEN + 60}))
    assert [e["achievement_id"] for e in w.poll()] == ["psn-npwr00001_00:t2"]


def test_a_list_the_psn_import_installed_is_never_replaced(profile, tmp_path):
    title = {"npCommunicationId": NP, "trophyTitleName": "Test Racer (PSN)", "trophySetVersion": "01.00"}
    trophies = [{"trophyId": i, "trophyType": "bronze", "trophyName": f"PSN {i}"} for i in range(3)]
    profile.commit(psn.plan_title(profile, profile.state(), set(), {"account_id": "1"}, title, trophies, []))
    install(tmp_path, {0: None, 1: WHEN, 2: None})
    unlocked = watcher(profile, tmp_path).poll()
    pack = profile.state()["packs"]["psn-npwr00001_00"]
    assert pack["achievements"]["t1"]["name"] == "PSN 1"           # the PSN list stays
    assert [e["achievement_id"] for e in unlocked] == ["psn-npwr00001_00:t1"]


def test_dev_hdd0_moved_in_vfs_yml_is_followed(profile, tmp_path):
    elsewhere = tmp_path / "big-disk"
    install(elsewhere, {0: None, 1: WHEN, 2: None})
    rpcs3_dir = tmp_path / "rpcs3"
    (rpcs3_dir / "config").mkdir(parents=True)
    (rpcs3_dir / "config" / "vfs.yml").write_text(f"/dev_hdd0/: {elsewhere / 'dev_hdd0'}/\n/dev_hdd1/: x\n")
    assert [e["achievement_id"] for e in watcher(profile, rpcs3_dir).poll()] == ["psn-npwr00001_00:t1"]


def test_rpcs3_running_on_windows_is_remembered(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(rpcs3.sys, "platform", "win32")
    install(tmp_path, {1: WHEN})
    w = rpcs3.Rpcs3Watcher(profile, running=lambda: {str(tmp_path / "rpcs3.exe").lower(), "c:\\other\\game.exe"})
    w.poll()
    assert [str(p).lower() for p in rpcs3.remembered(profile)] == [str(tmp_path).lower()]


def test_the_emulator_switch_turns_it_off(profile, tmp_path):
    install(tmp_path, {1: WHEN})
    emulator.set_enabled(profile, False)
    assert watcher(profile, tmp_path).poll() == []
    assert "psn-npwr00001_00" not in profile.state()["games"]


def test_a_broken_set_is_reported_once_and_others_still_read(profile, tmp_path):
    install(tmp_path, {1: WHEN})
    bad = install(tmp_path, {1: WHEN}, np="NPWR00002_00")
    (bad / "TROPUSR.DAT").write_bytes(b"not a trophy file")
    w = watcher(profile, tmp_path)
    assert [e["achievement_id"] for e in w.poll()] == ["psn-npwr00001_00:t1"]
    problems = w.new_problems()
    assert len(problems) == 1 and "NPWR00002_00" in problems[0]
    w.poll()
    assert w.new_problems() == []
