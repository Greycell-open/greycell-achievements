"""Every Steam emulator's achievement file (emulator_formats.py), shaped as the
community documents them, and the ones that live beside the game."""
import json
import struct
import time
import zlib
from pathlib import Path

import pytest

from openachievements import reducer
from openachievements.adapters import emulator, emulator_formats as fmt
from openachievements.catalog import steam as cat

T = 1712253396


def test_hoodlum_and_darksiders_lists():
    text = "[Achievements]\nACH_A=1\nACH_B=0\n[AchievementsUnlockTimes]\nACH_A=1712253396\nACH_B=0\n"
    assert fmt.parse(text.encode(), "ini") == {"ACH_A": float(T)}


def test_3dm_state_and_little_endian_hex_time():
    hex_time = struct.pack("<I", T).hex()
    text = f"[State]\nACH_A=0101\nACH_B=0000\n[Time]\nACH_A={hex_time}\nACH_B=00000000\n"
    assert fmt.parse(text.encode(), "ini") == {"ACH_A": float(T)}


def test_reloaded_sections_with_hex_state():
    one, zero, hex_time = "01000000", "00000000", struct.pack("<I", T).hex()
    text = (f"[ACH_A]\nState={one}\nCurProgress={zero}\nMaxProgress={zero}\nTime={hex_time}\n"
            f"[ACH_B]\nState={zero}\nCurProgress={zero}\nMaxProgress={zero}\nTime={zero}\n")
    assert fmt.parse(text.encode(), "ini") == {"ACH_A": float(T)}


def test_creamapi_cut_off_times_are_dropped():
    text = "[ACH_A]\nachieved=true\nunlocktime=1712253\n[ACH_B]\nachieved=false\nunlocktime=0\n"
    assert fmt.parse(text.encode(), "ini") == {"ACH_A": None}


def test_ali213_have_achieved():
    text = "[ACH_A]\nHaveAchieved=1\nHaveAchievedTime=1712253396\n[ACH_B]\nHaveAchieved=0\n"
    assert fmt.parse(text.encode(), "ini") == {"ACH_A": float(T)}


def test_tenoke_user_stats():
    text = ('[ACHIEVEMENTS]\n"Map_LondonPass" = {unlocked = true, time = 1712253396}\n'
            '"Map_ParisPass" = {unlocked = false, time = 0}\n\n[STATS]\n"Score" = {value = 3}\n')
    assert fmt.parse(text.encode(), "ini") == {"Map_LondonPass": float(T)}


def sse_bin(entries):
    """count, then 24-byte entries: crc32 LE, 4 bytes, unlock time, 8 bytes, value."""
    body = b"".join(struct.pack("<I", zlib.crc32(n.encode()) & 0xffffffff) + b"\0" * 4 + struct.pack("<i", t)
                    + b"\0" * 8 + struct.pack("<i", v) for n, t, v in entries)
    return struct.pack("<i", len(entries)) + body


def test_smartsteamemu_binary_names_by_crc():
    data = sse_bin([("ACH_A", T, 1), ("ACH_B", 0, 0), ("Score", T, 37)])
    assert fmt.parse(data, "sse") == {fmt.crc_name("ACH_A"): float(T)}
    with pytest.raises(ValueError):
        fmt.parse(data[:-3], "sse")


def test_nothing_unlocks_without_an_explicit_achieved():
    text = "[ACH_A]\nCurProgress=5\nMaxProgress=10\nUnlockTime=1712253396\n"
    assert fmt.parse(text.encode(), "ini") == {}


@pytest.fixture
def roots(tmp_path, monkeypatch):
    r = {k: tmp_path / k for k in ("PUBLIC_DOCUMENTS", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "DOCUMENTS")}
    monkeypatch.setattr(emulator, "_roots", lambda: [r])
    return r


def put(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


INI_ONE = "[ACH_A]\nAchieved=1\nUnlockTime=1712253396\n"


def test_every_fixed_folder_is_found(roots):
    put(roots["LOCALAPPDATA"] / "SKIDROW" / "10" / "achiev.ini", INI_ONE)
    put(roots["DOCUMENTS"] / "SkidRow" / "11" / "SteamEmu" / "achievements.ini", INI_ONE)
    put(roots["DOCUMENTS"] / "SkidRow" / "12" / "achievements.ini", INI_ONE)
    put(roots["PROGRAMDATA"] / "Steam" / "RLD!" / "13" / "achievements.ini", INI_ONE)
    put(roots["PROGRAMDATA"] / "Steam" / "Player" / "14" / "stats" / "achievements.ini", INI_ONE)
    put(roots["APPDATA"] / "CreamAPI" / "15" / "stats" / "CreamAPI.Achievements.cfg", INI_ONE)
    put(roots["APPDATA"] / "SmartSteamEmu" / "16" / "stats.bin", sse_bin([("ACH_A", T, 1)]))
    put(roots["PUBLIC_DOCUMENTS"] / "EMPRESS" / "17" / "remote" / "17" / "achievements.json",
        json.dumps({"ACH_A": {"earned": True, "earned_time": T}}))
    put(roots["PUBLIC_DOCUMENTS"] / "Steam" / "RUNE" / "18" / "achievements.ini", INI_ONE)
    found = {(e, a, f) for e, a, _p, f in emulator.achievement_files()}
    assert found == {("SKIDROW", 10, "ini"), ("SKIDROW", 12, "ini"), ("Reloaded/3DM", 13, "ini"),
                     ("Reloaded/3DM", 14, "ini"), ("CreamAPI", 15, "ini"), ("SmartSteamEmu", 16, "sse"),
                     ("EMPRESS", 17, "json"), ("RUNE", 18, "ini")}


def test_files_beside_the_game_are_found_from_the_emulator_settings(tmp_path, roots, monkeypatch):
    monkeypatch.setattr(fmt, "documents", lambda: roots["DOCUMENTS"])
    tenoke = tmp_path / "Games" / "Mini Airways"
    put(tenoke / "MiniAirways_Data" / "Plugins" / "x86_64" / "tenoke.ini", "[TENOKE]\nid = 2289650 # Mini Airways\n")
    put(tenoke / "SteamData" / "user_stats.ini", '[ACHIEVEMENTS]\n"ACH_A" = {unlocked = true, time = 1712253396}\n')
    ali = tmp_path / "Games" / "Ali"
    put(ali / "ALI213.ini", "[Settings]\nAppID = 20\nPlayerName = Player\nSaveType = 0\n")
    put(ali / "Profile" / "Player" / "Stats" / "achievements.ini", INI_ONE)
    ali_docs = tmp_path / "Games" / "AliDocs"
    put(ali_docs / "valve.ini", "[Settings]\nAppID = 21\nPlayerName = Me\nSaveType = 1\n")
    put(roots["DOCUMENTS"] / "VALVE" / "21" / "Me" / "Stats" / "achievements.ini", INI_ONE)
    ds = tmp_path / "Games" / "Ds"
    put(ds / "bin" / "ds.ini", "[GameSettings]\nAppId = 22\nUserDataFolder = .\n")
    put(ds / "SteamEmu" / "UserStats" / "achiev.ini", INI_ONE)
    hlm = tmp_path / "Games" / "Hlm"
    put(hlm / "hlm.ini", "[GameSettings]\nAppId = 23\nUserDataFolder = mydocs\nUserName = HOODLUM\n")
    put(roots["DOCUMENTS"] / "HOODLUM" / "23" / "SteamEmu" / "UserStats" / "achiev.ini",
        "[Achievements]\nACH_A=1\n[AchievementsUnlockTimes]\nACH_A=1712253396\n")
    lan = tmp_path / "Games" / "Lan"
    put(lan / "UniverseLAN.ini", "[GameSettings]\nAppID = 24\n")
    put(lan / "UniverseLANData" / "achievements.ini", INI_ONE)
    found = {(e, a) for e, a, _p, _f in emulator.achievement_files([tenoke, ali, ali_docs, ds, hlm, lan])}
    assert found == {("TENOKE", 2289650), ("ALI213", 20), ("ALI213", 21), ("DARKSiDERS", 22), ("Hoodlum", 23),
                     ("UniverseLAN", 24)}


SCHEMA = [{"internal_name": "ACH_A", "localized_name": "First Landing", "localized_desc": "", "icon": "", "hidden": False},
          {"internal_name": "ACH_B", "localized_name": "Second Landing", "localized_desc": "", "icon": "",
           "hidden": False}]


def install(profile, appid):
    def fetch(url):
        return 200, json.dumps({"response": {"achievements": SCHEMA}})
    cat.install_entry(profile, cat.fetch_game(appid, fetch, known_title="Game"))


def test_a_tenoke_unlock_beside_a_known_program_is_recorded(profile, tmp_path, roots):
    from openachievements.adapters import executable
    game = tmp_path / "Games" / "Mini Airways"
    exe = put(game / "MiniAirways.exe", b"MZ fake")
    put(game / "MiniAirways_Data" / "Plugins" / "x86_64" / "tenoke.ini", "[TENOKE]\nid = 30 # Game\n")
    install(profile, 30)
    executable.register(profile, "steam-30", exe)
    now = int(time.time())
    put(game / "SteamData" / "user_stats.ini", f'[ACHIEVEMENTS]\n"ACH_A" = {{unlocked = true, time = {now}}}\n')
    written = emulator.EmulatorWatcher(profile).poll()
    assert [e["achievement_id"] for e in written] == ["steam-30:first-landing"]
    assert written[0]["payload"]["mode"] == "TENOKE"
    assert emulator.EmulatorWatcher(profile).poll() == []                  # recorded once


def test_a_smartsteamemu_unlock_is_matched_through_its_crc(profile, roots):
    install(profile, 31)
    put(roots["APPDATA"] / "SmartSteamEmu" / "31" / "stats.bin", sse_bin([("ACH_B", T, 1)]))
    written = emulator.EmulatorWatcher(profile).poll()
    assert [e["achievement_id"] for e in written] == ["steam-31:second-landing"]
    assert reducer.is_unlocked(profile.state(), "steam-31:second-landing")
