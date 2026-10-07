"""Fireproof saves (The Room 4: Old Sins): the reader, the shipped rules, and
the two watcher fixes the first real play found: crash handlers taken for the
game, and one game's two programs timed twice."""
import base64
import os
import struct

import pytest

from openachievements import fireproof, reducer
from openachievements.adapters import autodetect as ad
from openachievements.adapters import executable as exe
from openachievements.adapters import savefile, saverules
from openachievements.catalog import steam as cat
from conftest import write_pack
from test_autodetect import catalogue, detector, make_game  # noqa: F401 - fixture
from test_catalog import fake_steam, page, row

BOM = "﻿"


def lzf(data: bytes) -> bytes:
    """A small greedy liblzf compressor, so the tests write real back references."""
    out, lit, i, seen = bytearray(), bytearray(), 0, {}

    def flush():
        for k in range(0, len(lit), 32):
            chunk = lit[k:k + 32]
            out.append(len(chunk) - 1)
            out.extend(chunk)
        lit.clear()
    while i < len(data):
        key = data[i:i + 3]
        ref = seen.get(key) if len(key) == 3 else None
        seen[key] = i
        if ref is not None and i - ref - 1 < 8192:
            length = 3
            while i + length < len(data) and length < 264 and data[ref + length] == data[i + length]:
                length += 1
            flush()
            off, n = i - ref - 1, length - 2
            if n < 7:
                out.append((n << 5) | (off >> 8))
            else:
                out += bytes([(7 << 5) | (off >> 8), n - 7])
            out.append(off & 0xFF)
            i += length
        else:
            lit.append(data[i])
            i += 1
    flush()
    return bytes(out)


LEVELS = ("FOY_Main", "KIT_Main", "STU_Main", "GAR_Main", "JPN_Main", "ART_Main", "CUR_Main", "MAR_Main",
          "FIN_Main", "MAG_Main")


def old_sins_save(levels=None, complete=False, version=2) -> bytes:
    """The shape of a real z.save: header, then one LZF stream of the slot and the scenes."""
    levels = {name: "Locked" for name in LEVELS} | (levels or {})
    slot = (f'{BOM}<?xml version="1.0" encoding="utf-8"?><PlayerSlot xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
            f'<SlotName>z</SlotName><CurrentSubLevel>2</CurrentSubLevel><GameComplete>{str(complete).lower()}'
            '</GameComplete><CheckpointsReached><int>1923484116</int></CheckpointsReached>'
            '<TotalPlayedTime>5314.694</TotalPlayedTime></PlayerSlot>')
    items = "".join(f"<item><key><string>{n}</string></key><value><LevelState>{s}</LevelState></value></item>"
                    for n, s in levels.items())
    dol = (f'{BOM}<?xml version="1.0" encoding="utf-8"?><SerialiseData><Components>'
           '<ComponentSaveBase xsi:type="SubLevelManagerSaveData" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
           f'<Key>GameControllerSubLevelManager</Key><TrackedLevelStates>{items}</TrackedLevelStates>'
           '</ComponentSaveBase></Components></SerialiseData>')
    foy = f'{BOM}<?xml version="1.0" encoding="utf-8"?><SerialiseData><SceneName>FOY_Main</SceneName></SerialiseData>'
    scenes = "".join(f"<SceneMemoryStream><SceneName>{n}</SceneName><ByteArray>"
                     f"{base64.b64encode(t.encode()).decode()}</ByteArray></SceneMemoryStream>"
                     for n, t in (("FOY_Main", foy), ("DOL_Main", dol)))
    array = f'{BOM}<?xml version="1.0" encoding="utf-8"?><ArrayOfSceneMemoryStream>{scenes}</ArrayOfSceneMemoryStream>'
    body = (slot + array).encode("utf-8")
    return struct.pack("<6I", version, 0, len(array), 0, 0, len(body)) + lzf(body)


# ---- the reader ---------------------------------------------------------------------------

def test_lzf_back_references_including_overlapping_ones():
    text = b"abcabcabcabc" + b"x" * 300 + b"<item><key>FOY</key></item><item><key>KIT</key></item>"
    assert fireproof.unlzf(lzf(text)) == text


def test_a_save_reads_into_slot_values_and_room_states():
    values, text = fireproof.parse(old_sins_save({"FOY_Main": "Complete", "KIT_Main": "Entered"}))
    assert values["slot"]["SlotName"] == "z" and values["slot"]["GameComplete"] == "false"
    assert values["levels"]["FOY_Main"] == "Complete" and values["levels"]["KIT_Main"] == "Entered"
    assert values["levels"]["GAR_Main"] == "Locked"
    assert "GameControllerSubLevelManager" in text          # the scenes are searchable for `contains` rules
    assert "CheckpointsReached" not in values["slot"]       # only simple values, never guessed lists


@pytest.mark.parametrize("damage", [
    lambda d: d[:10],                                         # shorter than its header
    lambda d: struct.pack("<I", 3) + d[4:],                   # a format this does not read
    lambda d: d[:len(d) // 2],                                # cut short mid-stream
    lambda d: d[:24] + bytes([0x20, 0xFF]) + d[26:],          # a reference before the start
])
def test_a_broken_save_is_an_error_not_a_crash(damage):
    with pytest.raises(fireproof.FireproofError):
        fireproof.parse(damage(old_sins_save()))


def test_xml_with_a_document_type_is_refused():
    body = (BOM + '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><PlayerSlot><SlotName>&a;</SlotName>'
            '</PlayerSlot>').encode()
    with pytest.raises(fireproof.FireproofError):
        fireproof.parse(struct.pack("<6I", 2, 0, 0, 0, 0, len(body)) + lzf(body))


def test_the_unpacked_size_is_bounded():
    with pytest.raises(fireproof.FireproofError):
        fireproof.unlzf(lzf(b"a" * 5000), limit=1000)


# ---- The Room 4: Old Sins, end to end ------------------------------------------------------

OLD_SINS = [row(n, d, f"r{i}", "50.0") for i, (n, d) in enumerate([
    ("Foyer Complete", "Completed the Foyer"), ("Study Complete", "Completed the Study"),
    ("Curiosity Room Complete", "Completed the Curiosity Room"), ("Kitchen Complete", "Completed the Kitchen"),
    ("Maritime Room Complete", "Completed the Maritime Room"), ("Garden Complete", "Completed the Garden"),
    ("Japanese Gallery Complete", "Completed the Japanese Gallery"),
    ("Art Studio Complete", "Completed the Art Studio"),
    ("The Room: Old Sins Complete ", "Completed The Room: Old Sins")])]   # Steam's own trailing space


@pytest.fixture
def old_sins(profile, tmp_path, monkeypatch):
    low = tmp_path / "LocalLow"
    monkeypatch.setattr(savefile, "root_folder", lambda name: low if name == "LOCALLOW" else None)
    games = {1361320: (9, page("The Room 4: Old Sins", OLD_SINS))}
    cat.add_to_profile(profile, cat.CatalogIndex(tmp_path / "c"), 1361320, fetch=fake_steam(games))
    folder = low / "Fireproof Studios" / "Old Sins"
    folder.mkdir(parents=True)
    return folder


def unlocked(profile):
    return sorted(e["achievement_id"] for e in profile.log.read().events if e["event_type"] == "achievement.unlocked")


def test_old_sins_unlocks_each_room_from_its_own_save(profile, old_sins):
    (old_sins / "z.save").write_bytes(old_sins_save({"FOY_Main": "Complete", "KIT_Main": "Entered"}))
    assert saverules.apply(profile) == ["steam-1361320"]
    pack = profile.state()["packs"]["steam-1361320"]
    assert all(a.get("rules") for a in pack["achievements"].values())   # the trailing-space name too
    watcher = savefile.SaveWatcher(profile)
    written = watcher.poll()
    assert [e["achievement_id"] for e in written] == ["steam-1361320:foyer-complete"]
    assert written[0]["payload"]["provenance"] == "save-derived"
    assert watcher.poll() == []                                         # unchanged save: not re-read
    (old_sins / "z.save").write_bytes(old_sins_save({"FOY_Main": "Complete", "KIT_Main": "Complete"}))
    assert [e["achievement_id"] for e in watcher.poll()] == ["steam-1361320:kitchen-complete"]
    assert not reducer.is_unlocked(profile.state(), "steam-1361320:study-complete")   # Entered is not done
    every = {n: "Complete" for n in LEVELS}
    (old_sins / "z.save").write_bytes(old_sins_save(every, complete=True))
    watcher.poll()
    assert len(unlocked(profile)) == 9


def test_old_sins_save_folder_is_allowed_once(profile, old_sins):
    saverules.apply(profile)
    save = profile.state()["packs"]["steam-1361320"]["saves"][0]
    assert savefile.granted_folder(profile, "steam-1361320", save) == old_sins.resolve()


def test_a_broken_old_sins_save_is_reported_and_unlocks_nothing(profile, old_sins):
    (old_sins / "z.save").write_bytes(old_sins_save()[:40])
    saverules.apply(profile)
    watcher = savefile.SaveWatcher(profile)
    assert watcher.poll() == []
    assert any("not a readable Fireproof save" in p for p in watcher.new_problems())


# ---- crash handlers are not the game, and one game is one session ---------------------------

def test_a_crash_handler_beside_the_game_is_not_taken_for_it(profile, catalogue, monkeypatch, tmp_path):  # noqa: F811
    game = make_game(tmp_path / "D", "Games/Hollow Knight/hollow_knight.exe",
                     ["UnityPlayer.dll", "hollow_knight_Data"])
    handler = os.path.normcase(str(tmp_path / "D" / "Games" / "Hollow Knight" / "UnityCrashHandler64.exe"))
    open(handler, "wb").write(b"MZ")
    found = detector(profile, catalogue, monkeypatch, tmp_path).poll({game, handler})
    assert [f["path"] for f in found] == [game]
    assert len(profile.state()["games"]["steam-367520"]["installations"]) == 1


def test_two_programs_of_one_game_are_one_session(profile, tmp_path):
    folder = tmp_path / "Games" / "Old Sins"
    folder.mkdir(parents=True)
    profile.install_pack(write_pack(tmp_path / "pack", pack_id="old-sins", game_id="old-sins",
                                    achievements=[{"id": "regular", "name": "Regular",
                                                   "rules": [{"signal": "sessions", "count": 2}]}]))
    paths = []
    for name in ("OldSins.exe", "UnityCrashHandler64.exe"):         # registered before the fix
        (folder / name).write_bytes(b"MZ " + name.encode())
        exe.register(profile, "old-sins", folder / name)
        paths.append(str((folder / name).resolve()).lower())
    w = exe.Watcher(profile)
    assert [e["event_type"] for e in w.poll(set(paths), now=0)] == ["session.started"]
    assert w.poll({paths[1]}, now=3600) == []                       # the game quit, its handler lingers
    ended = w.poll(set(), now=3605)
    assert [e["event_type"] for e in ended] == ["session.ended"] and ended[0]["payload"]["seconds"] == 3605
    assert not reducer.is_unlocked(profile.state(), "old-sins:regular")   # one sitting, not two
    assert profile.library()["games"][0]["playtime_seconds"] == 3605
