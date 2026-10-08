"""PlayStation 3 trophies RPCS3 recorded on this computer.

RPCS3, the PS3 emulator, keeps each game's trophies the way the console does,
under its own folder: `dev_hdd0/home/<user>/trophy/<NP communication id>/`.
Two files there are all this needs, and both are the game's own:

- `TROPCONF.SFM`: the trophy list, XML (`<trophyconf>`: `npcommid`,
  `title-name`, then one `<trophy id ttype hidden>` with `<name>` and
  `<detail>` each). So a game needs no catalogue and no network.
- `TROPUSR.DAT`: what was earned, binary and big-endian. A 48-byte header
  (magic 81 8F 54 AD, then the table count at byte 8), table headers of 32
  bytes from byte 0x30 (type, entry size, ?, entry count, u64 offset), and the
  type 6 table. An entry is its table's entry size plus its own 16-byte
  header (0x60 + 0x10 for type 6, as RPCS3 writes `sizeof - 0x10`): trophy id
  at +16, state at +20 (1 = earned), and the unlock time at +40, in PS3 ticks
  (microseconds since 0001-01-01 UTC). Layout from RPCS3's own source (rpcs3/Loader/TROPUSR.h and
  sceNpTrophy.cpp, which writes the same tick to both timestamps); the folder
  layout matches Achievement Watcher's RPCS3 parser.

RPCS3 is portable on Windows, so its folder is not in a fixed place. It is
found from the running program (`rpcs3.exe`) and remembered, besides the
default folders on Linux and macOS. `dev_hdd0` can be moved in RPCS3's
`vfs.yml`, which is read. Everything is read-only.

A game registers under the same id the PSN import uses (`psn-<id>`), so a
game seen both ways is one game. The trophy list is installed only when the
game has none or the one it has came from here, so a list the PSN import
installed is never replaced.
"""
from __future__ import annotations

import os
import re
import struct
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .. import events as ev
from ..packs import PackError, split_for_events, validate_definitions
from ..profile import Profile
from .base import AdapterMetadata, utc_iso

ADAPTER_ID = "rpcs3"
ADAPTER_VERSION = "0.1.0"
MAX_FILE_BYTES = 4 * 1024 * 1024
LOOK_EVERY = 60.0                   # seconds between looks for RPCS3 folders and new trophy sets
MAGIC = 0x818F54AD
TICKS_TO_UNIX = 62135596800         # seconds from 0001-01-01 to 1970-01-01
GRADES = {"P": "platinum", "G": "gold", "S": "silver", "B": "bronze"}
POINTS = {"bronze": 15, "silver": 30, "gold": 90, "platinum": 300}     # the PSN import's values

METADATA = AdapterMetadata(
    adapter_id=ADAPTER_ID,
    adapter_version=ADAPTER_VERSION,
    support_level="community",
    authentication_method="none (local)",
    data_access_method="reads RPCS3's own trophy files (TROPCONF.SFM, TROPUSR.DAT), read-only",
    capabilities=("unlocks", "unlock-times", "definitions"),
    rate_limits="looks for trophy sets once a minute; parses only files that changed",
    known_limitations=(
        "RPCS3's folder is found once the app has seen rpcs3.exe running (or in the Linux/macOS default)",
        "PS3 only: RPCS3 is a PS3 emulator",
    ),
    terms_or_policy_notes="Observation only. Never writes to, uploads, or copies a file.",
)


class Rpcs3Error(ValueError):
    pass


# ---- the two files -------------------------------------------------------------------

def parse_conf(raw: bytes) -> dict:
    """TROPCONF.SFM -> {"np_id", "title", "trophies": [{id, name, detail, grade, hidden}]}."""
    text = raw.decode("utf-8", errors="replace")
    start = text.find("<trophyconf")
    if start < 0:
        raise Rpcs3Error("not a trophy list (no <trophyconf>)")
    try:
        root = ET.fromstring(text[start:])                 # from the element: a signature comment can come first
    except ET.ParseError as exc:
        raise Rpcs3Error(f"trophy list is not valid XML ({exc})") from exc
    np_id = (root.findtext("npcommid") or "").strip()
    if not re.fullmatch(r"[A-Za-z]{4}\d{5}_\d{2}", np_id):
        raise Rpcs3Error(f"unexpected NP communication id {np_id!r}")
    trophies = []
    for t in root.iter("trophy"):
        try:
            tid = int(t.get("id", ""))
        except ValueError:
            continue
        trophies.append({"id": tid, "name": (t.findtext("name") or "").strip(),
                         "detail": (t.findtext("detail") or "").strip(),
                         "grade": GRADES.get((t.get("ttype") or "").upper()),
                         "hidden": (t.get("hidden") or "").lower() == "yes"})
    if not trophies:
        raise Rpcs3Error("trophy list has no trophies")
    return {"np_id": np_id, "title": (root.findtext("title-name") or np_id).strip(), "trophies": trophies}


def parse_usr(raw: bytes) -> dict[int, float | None]:
    """TROPUSR.DAT -> {trophy id: unix time or None} for each earned trophy."""
    if len(raw) < 0x30 or struct.unpack_from(">I", raw, 0)[0] != MAGIC:
        raise Rpcs3Error("not a trophy record (bad magic)")
    tables = struct.unpack_from(">I", raw, 8)[0]
    if tables > 16:
        raise Rpcs3Error("unexpected table count")
    earned: dict[int, float | None] = {}
    for i in range(tables):
        at = 0x30 + i * 32
        if at + 32 > len(raw):
            raise Rpcs3Error("table headers run past the end")
        kind, size, _unk, count, offset = struct.unpack_from(">IIIIQ", raw, at)
        if kind != 6:
            continue
        stride = size + 0x10                               # the entry size leaves out the entry's own header
        if stride < 48 or count > 1024 or offset + count * stride > len(raw):
            raise Rpcs3Error("trophy table runs past the end")
        for n in range(count):
            entry = offset + n * stride
            tid, state = struct.unpack_from(">II", raw, entry + 16)
            if state != 1:
                continue
            tick = struct.unpack_from(">Q", raw, entry + 40)[0]
            when = tick / 1_000_000 - TICKS_TO_UNIX if 0 < tick < 0xFFFFFFFFFFFFFFFF else None
            earned[tid] = when if when is not None and when > 0 else None
    return earned


# ---- where RPCS3 keeps them -----------------------------------------------------------

def default_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "rpcs3"]
    if sys.platform == "win32":
        return []                                          # portable: found from the running program
    return [home / ".config" / "rpcs3", home / ".var" / "app" / "net.rpcs3.RPCS3" / "config" / "rpcs3"]


def hdd0(rpcs3_dir: Path) -> Path:
    """dev_hdd0 for an RPCS3 folder, following vfs.yml when it moved it."""
    for vfs in (rpcs3_dir / "config" / "vfs.yml", rpcs3_dir / "vfs.yml"):
        try:
            text = vfs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"^\s*/dev_hdd0/\s*:\s*(.+?)\s*$", text, re.M)
        if m:
            value = m.group(1).strip().strip("'\"").replace("$(EmulatorDir)", str(rpcs3_dir) + os.sep)
            if value:
                return Path(value)
    return rpcs3_dir / "dev_hdd0"


def trophy_sets(rpcs3_dirs: list[Path]) -> list[Path]:
    """Every trophy folder holding both files, for every user, in every RPCS3 folder."""
    out = []
    for d in rpcs3_dirs:
        home = hdd0(d) / "home"
        try:
            users = [u for u in home.iterdir() if u.is_dir() and u.name.isdigit()]
        except OSError:
            continue
        for user in users:
            try:
                games = [g for g in (user / "trophy").iterdir() if g.is_dir()]
            except OSError:
                continue
            out.extend(g for g in games if (g / "TROPCONF.SFM").is_file() and (g / "TROPUSR.DAT").is_file())
    return out


def remembered(profile: Profile) -> list[Path]:
    return [Path(p) for p in profile.config.load().get("rpcs3_dirs", {}).get(profile.profile_id, [])]


def remember(profile: Profile, folder: Path) -> None:
    with profile.config.editing() as config:
        mine = config.setdefault("rpcs3_dirs", {}).setdefault(profile.profile_id, [])
        if str(folder) not in mine:
            mine.append(str(folder))


def running_dirs(running: set[str]) -> list[Path]:
    """RPCS3 folders from running programs. Windows only: there the OS gives a
    full path, and its case does not matter."""
    if sys.platform != "win32":
        return []
    return [Path(p).parent for p in running if Path(p).name.lower() == "rpcs3.exe"]


# ---- into the library -----------------------------------------------------------------

def game_id_for(np_id: str) -> str:
    return "psn-" + np_id.lower()


def _read(path: Path) -> bytes:
    with open(path, "rb") as fh:
        raw = fh.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise Rpcs3Error(f"{path.name} is larger than 4 MB")
    return raw


def plan_set(profile: Profile, conf: dict, earned: dict) -> list[dict]:
    """Events for one trophy set: the game, its list when it needs one, and
    each earned trophy not yet unlocked."""
    from ..reducer import is_unlocked
    state = profile.state()
    game_id = game_id_for(conf["np_id"])
    make = lambda kind, payload, **kw: ev.make_event(kind, profile_id=profile.profile_id, device_id=profile.device_id,
                                                     payload=payload, adapter=ADAPTER_ID,
                                                     adapter_version=ADAPTER_VERSION, **kw)
    out = []
    if game_id not in state["games"]:
        out.append(make("game.registered", {"title": conf["title"], "platform": "PlayStation 3",
                                            "external_ids": {"psn": conf["np_id"]}}, game_id=game_id))
    rows = [{"id": f"t{t['id']}", "name": t["name"] or f"Trophy {t['id']}", "description": t["detail"],
             "hidden": t["hidden"], "points": POINTS.get(t["grade"], 0), "category": t["grade"]}
            for t in conf["trophies"]]
    installed = state["packs"].get(game_id)
    mine = not installed or installed.get("removed") or installed.get("source") == ADAPTER_ID
    fields = ("name", "description", "hidden", "points")
    current = {k: {f: v.get(f) for f in fields} for k, v in (installed or {}).get("achievements", {}).items()}
    wanted = {r["id"]: {f: r.get(f) for f in fields} for r in rows}
    if mine and (not installed or installed.get("removed") or current != wanted):
        meta = {"id": game_id, "name": conf["title"], "game_ids": [game_id], "version": "1.0.0",
                "source": ADAPTER_ID, "supported_adapters": [ADAPTER_ID, "psn"]}
        try:
            description = validate_definitions(meta, rows)
        except PackError:
            return out
        kind = "pack.updated" if installed and not installed.get("removed") else "pack.installed"
        out.extend(make(kind, payload) for payload in split_for_events(description))
        have = {r["id"] for r in rows}
    else:
        have = set((installed or {}).get("achievements", {}))
    for tid, when in sorted(earned.items(), key=lambda kv: kv[1] or 0):
        aid = f"t{tid}"
        if aid not in have or is_unlocked(state, f"{game_id}:{aid}"):
            continue
        occurred = utc_iso(when) if when and when <= time.time() + 60 else None
        out.append(make("achievement.unlocked", {"provenance": "emulator", "mode": "rpcs3"},
                        achievement_id=f"{game_id}:{aid}", game_id=game_id, occurred_at=occurred or ev.now(),
                        external_event_id=f"{profile.profile_id}:rpcs3:{conf['np_id']}:{tid}"))
    return out


@dataclass
class Rpcs3Watcher:
    """Finds RPCS3's trophy sets once a minute and records what changed.
    Shares the emulator switch (`openachievements emulators on|off`)."""
    profile: Profile
    running: object = None                                # injectable: () -> set of running program paths
    seen: dict = field(default_factory=dict)              # trophy folder -> (stamps of both files)
    problems: dict = field(default_factory=dict)
    _reported: set = field(default_factory=set)
    _sets: list = field(default_factory=list)
    _look: float = -1e18

    def new_problems(self) -> list[str]:
        fresh = [m for k, m in self.problems.items() if (k, m) not in self._reported]
        self._reported.update(self.problems.items())
        return fresh

    def _dirs(self) -> list[Path]:
        from .executable import running_executables
        procs = (self.running or running_executables)()
        for folder in running_dirs(procs):
            if folder not in remembered(self.profile):
                remember(self.profile, folder)
        dirs = []
        for d in remembered(self.profile) + default_dirs():
            if d not in dirs:
                dirs.append(d)
        return dirs

    def poll(self) -> list[dict]:
        from .emulator import enabled
        if not enabled(self.profile):
            return []
        now = time.monotonic()
        if now - self._look >= LOOK_EVERY:
            self._look = now
            self._sets = trophy_sets(self._dirs())
        written = []
        for folder in self._sets:
            try:
                stamp = tuple((s.st_size, s.st_mtime_ns) for s in
                              ((folder / "TROPCONF.SFM").stat(), (folder / "TROPUSR.DAT").stat()))
            except OSError:
                continue
            if self.seen.get(str(folder)) == stamp:
                continue
            try:
                conf = parse_conf(_read(folder / "TROPCONF.SFM"))
                earned = parse_usr(_read(folder / "TROPUSR.DAT"))
            except (OSError, Rpcs3Error) as exc:
                self.problems[str(folder)] = f"RPCS3 {folder.name}: {exc}"
                self.seen[str(folder)] = stamp            # not again until it changes
                continue
            self.problems.pop(str(folder), None)
            events = plan_set(self.profile, conf, earned)
            if events:
                self.profile.commit(events)
                written.extend(e for e in events if e["event_type"] == "achievement.unlocked")
            self.seen[str(folder)] = stamp
        return written
