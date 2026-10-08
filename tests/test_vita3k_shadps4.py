"""PS Vita trophies from Vita3K and PS4 trophies from shadPS4, from files laid
out as each emulator writes them (see the adapters' docstrings for sources)."""
import struct
from datetime import datetime, timezone

from openachievements.adapters import emulator, rpcs3, shadps4, vita3k

WHEN = int(datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc).timestamp())
VITA_NP = "NPWR03001_00"
PS4_NP = "NPWR11223_00"


def conf_xml(np=None, title="Test Game", names=("Platinum One", "First Blood", "Hidden Path")):
    head = f"<npcommid>{np}</npcommid>" if np else ""
    rows = "".join(f'<trophy id="{i:03d}" hidden="{"yes" if i == 2 else "no"}" ttype="{"PBG"[i]}">'
                   f"<name>{n}</name><detail>Detail {i}</detail></trophy>" for i, n in enumerate(names))
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<trophyconf version="1.1">{head}<title-name>{title}</title-name>{rows}</trophyconf>'


# ---- Vita3K -------------------------------------------------------------------------

def vita_progress(earned, order="<"):
    """Vita3K's save_trophy_progress_file, field by field."""
    bits = [0, 0, 0, 0]
    times = [0] * vita3k.MAX_TROPHIES
    for tid, when in earned.items():
        bits[tid >> 5] |= 1 << (tid & 31)
        times[tid] = when
    return (struct.pack(order + "I", vita3k.MAGIC) + struct.pack(order + "4I", *bits)
            + struct.pack(order + "4I", 0, 0, 0, 0) + struct.pack(order + "3I", 1, 3, 0)
            + struct.pack(order + "16I", 3, *[0] * 15) + struct.pack(order + "128Q", *times)
            + struct.pack(order + "128I", 1, 4, 2, *[0] * 125))


def vita_install(fs_dir, earned, np=VITA_NP, name_file="TROP.SFM"):
    user = fs_dir / "ux0" / "user" / "00" / "trophy"
    (user / "conf" / np).mkdir(parents=True)
    (user / "conf" / np / name_file).write_text(conf_xml(), encoding="utf-8")
    (user / "data" / np).mkdir(parents=True)
    (user / "data" / np / "TROPUSR.DAT").write_bytes(vita_progress(earned))
    return user / "data" / np / "TROPUSR.DAT"


def vita_watcher(profile, fs_dir):
    vita3k.remember(profile, fs_dir)
    return vita3k.Vita3kWatcher(profile, running=lambda: set())


def test_vita3k_progress_reads_earned_bits_and_unix_times():
    assert vita3k.parse_progress(vita_progress({1: WHEN, 40: WHEN + 5})) == {1: WHEN, 40: WHEN + 5}
    assert vita3k.parse_progress(vita_progress({1: WHEN}, order=">")) == {1: WHEN}
    assert len(vita_progress({})) == vita3k.PROGRESS_SIZE


def test_vita3k_bad_files_are_refused():
    for bad in (b"", b"\x00" * vita3k.PROGRESS_SIZE, vita_progress({1: WHEN})[:100]):
        try:
            vita3k.parse_progress(bad)
        except rpcs3.Rpcs3Error:
            continue
        raise AssertionError("accepted a bad progress file")


def test_vita3k_game_joins_and_earned_trophies_unlock(profile, tmp_path):
    vita_install(tmp_path, {1: WHEN})
    unlocked = vita_watcher(profile, tmp_path).poll()
    gid = rpcs3.game_id_for(VITA_NP)
    state = profile.state()
    assert state["games"][gid]["title"] == "Test Game" and state["games"][gid]["platform"] == "PlayStation Vita"
    assert state["packs"][gid]["achievements"]["t2"]["hidden"] is True
    assert [e["achievement_id"] for e in unlocked] == [f"{gid}:t1"]
    assert unlocked[0]["payload"] == {"provenance": "emulator", "mode": "vita3k"}
    assert unlocked[0]["occurred_at"].startswith("2026-10-02T09:00")


def test_vita3k_english_names_are_preferred(profile, tmp_path):
    dat = vita_install(tmp_path, {0: WHEN})
    conf = dat.parent.parent.parent / "conf" / VITA_NP
    (conf / "TROP_01.SFM").write_text(conf_xml(title="English Title", names=("Plat EN", "One EN", "Two EN")),
                                      encoding="utf-8")
    vita_watcher(profile, tmp_path).poll()
    gid = rpcs3.game_id_for(VITA_NP)
    assert profile.state()["games"][gid]["title"] == "English Title"


def test_vita3k_new_unlock_is_picked_up(profile, tmp_path):
    dat = vita_install(tmp_path, {1: WHEN})
    w = vita_watcher(profile, tmp_path)
    w.poll()
    dat.write_bytes(vita_progress({1: WHEN, 2: WHEN + 60}))
    assert [e["achievement_id"] for e in w.poll()] == [f"{rpcs3.game_id_for(VITA_NP)}:t2"]


def test_vita3k_portable_folder_is_remembered_from_the_running_program(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(vita3k.sys, "platform", "win32")
    w = vita3k.Vita3kWatcher(profile, running=lambda: {str(tmp_path / "vita3k.exe").lower()})
    w.poll()
    assert [str(p).lower() for p in vita3k.remembered(profile)] == [str(tmp_path / "portable" / "fs").lower()]


# ---- shadPS4 ------------------------------------------------------------------------

def ps4_save(earned):
    rows = "".join(f'<trophy id="{i:03d}" hidden="no" ttype="{"PBG"[i]}"'
                   + (f' unlockstate="true" timestamp="{earned[i]}"' if i in earned else "")
                   + f"><name>T{i}</name><detail>D{i}</detail></trophy>" for i in range(3))
    return f'<?xml version="1.0"?>\n<trophyconf version="1.1"><title-name>Save Copy</title-name>{rows}</trophyconf>'


def ps4_install(user_dir, earned, np=PS4_NP, player="1000"):
    xml = user_dir / "trophy" / np / "Xml"
    xml.mkdir(parents=True)
    (xml / "TROP.XML").write_text(conf_xml(np=np, title="PS4 Test"), encoding="utf-8")
    save = user_dir / "home" / player / "trophy" / f"{np}.xml"
    save.parent.mkdir(parents=True)
    save.write_text(ps4_save(earned), encoding="utf-8")
    return save


def ps4_watcher(profile, user_dir):
    shadps4.remember(profile, user_dir)
    return shadps4.ShadPs4Watcher(profile, running=lambda: set())


def test_shadps4_progress_reads_unlockstate_and_time():
    assert shadps4.parse_progress(ps4_save({1: WHEN}).encode()) == {1: WHEN}
    assert shadps4.parse_progress(ps4_save({}).encode()) == {}


def test_shadps4_game_joins_and_earned_trophies_unlock(profile, tmp_path):
    ps4_install(tmp_path, {1: WHEN})
    unlocked = ps4_watcher(profile, tmp_path).poll()
    gid = rpcs3.game_id_for(PS4_NP)
    state = profile.state()
    assert state["games"][gid]["title"] == "PS4 Test" and state["games"][gid]["platform"] == "PlayStation 4"
    assert {k: v["name"] for k, v in state["packs"][gid]["achievements"].items()} == {
        "t0": "Platinum One", "t1": "First Blood", "t2": "Hidden Path"}
    assert [e["achievement_id"] for e in unlocked] == [f"{gid}:t1"]
    assert unlocked[0]["payload"] == {"provenance": "emulator", "mode": "shadps4"}


def test_shadps4_new_unlock_is_picked_up(profile, tmp_path):
    save = ps4_install(tmp_path, {1: WHEN})
    w = ps4_watcher(profile, tmp_path)
    w.poll()
    save.write_text(ps4_save({1: WHEN, 0: WHEN + 9}), encoding="utf-8")
    assert [e["achievement_id"] for e in w.poll()] == [f"{rpcs3.game_id_for(PS4_NP)}:t0"]


def test_shadps4_user_folder_beside_the_program_is_remembered(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(shadps4.sys, "platform", "win32")
    w = shadps4.ShadPs4Watcher(profile, running=lambda: {str(tmp_path / "shadPS4.exe").lower()})
    w.poll()
    assert [str(p).lower() for p in shadps4.remembered(profile)] == [str(tmp_path / "user").lower()]


def test_a_list_another_playstation_emulator_installed_may_be_refreshed(profile, tmp_path):
    """Both are local PlayStation sources; neither is the PSN import."""
    ps4_install(tmp_path, {1: WHEN})
    ps4_watcher(profile, tmp_path).poll()
    pack = profile.state()["packs"][rpcs3.game_id_for(PS4_NP)]
    assert pack["source"] == "shadps4"


def test_the_emulator_switch_turns_both_off(profile, tmp_path):
    vita_install(tmp_path / "v", {1: WHEN})
    ps4_install(tmp_path / "p", {1: WHEN})
    emulator.set_enabled(profile, False)
    assert vita_watcher(profile, tmp_path / "v").poll() == [] and ps4_watcher(profile, tmp_path / "p").poll() == []
