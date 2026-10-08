"""PS Vita trophies Vita3K recorded on this computer.

Vita3K keeps a user's trophies under its Vita file system,
`<vita fs>/ux0/user/<user>/trophy/` (layout from Vita3K's own source,
vita3k/np/src/trophy/collection.cpp and context.cpp):

- `conf/<NP id>/TROP_01.SFM` (English) or `TROP.SFM`: the trophy list, XML
  (`<trophyconf>`: `title-name`, then `<trophy id ttype hidden>` with `<name>`
  and `<detail>`), read with the same reader as RPCS3's.
- `data/<NP id>/TROPUSR.DAT`: Vita3K's own progress file, not the console's,
  in the computer's byte order: magic 0x12D5819A, then 128 earned bits (four
  u32), 128 hidden bits, group count, trophy count, platinum id, 16 group
  counts, 128 u64 unlock times (Unix seconds, `std::time`), 128 grades.

The Vita file system is `SDL_GetPrefPath("Vita3K", "Vita3K")` (Windows
%APPDATA%/Vita3K/Vita3K, Linux ~/.local/share/Vita3K/Vita3K, macOS
Application Support/Vita3K/Vita3K, there possibly with `fs`), or
`portable/fs` beside a portable Vita3K, which is found from the running
program and remembered. Read-only. Games register as `psn-<id>`, shared with
the PSN import and the other PlayStation emulators (rpcs3.plan_set).
"""
from __future__ import annotations

import os
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..profile import Profile
from . import rpcs3
from .base import AdapterMetadata

ADAPTER_ID = "vita3k"
ADAPTER_VERSION = "0.1.0"
MAGIC = 0x12D5819A
MAX_TROPHIES = 128
MAX_GROUPS = 16
PROGRESS_SIZE = 4 + 16 + 16 + 12 + MAX_GROUPS * 4 + MAX_TROPHIES * 8 + MAX_TROPHIES * 4
NAME_FILES = ("TROP_01.SFM", "TROP_18.SFM", "TROP.SFM")     # English (US), English (GB), the master list
PROGRAMS = ("vita3k.exe",)
LOOK_EVERY = 60.0

METADATA = AdapterMetadata(
    adapter_id=ADAPTER_ID,
    adapter_version=ADAPTER_VERSION,
    support_level="community",
    authentication_method="none (local)",
    data_access_method="reads Vita3K's trophy list and progress files, read-only",
    capabilities=("unlocks", "unlock-times", "definitions"),
    rate_limits="looks for trophy sets once a minute; parses only files that changed",
    known_limitations=("a portable Vita3K is found once the app has seen it running",),
    terms_or_policy_notes="Observation only. Never writes to, uploads, or copies a file.",
)


def parse_progress(raw: bytes) -> dict[int, float | None]:
    """Vita3K's TROPUSR.DAT -> {trophy id: unix time or None} for each earned one."""
    if len(raw) < PROGRESS_SIZE:
        raise rpcs3.Rpcs3Error("trophy progress file is too short")
    for order in ("<", ">"):                               # written in the host's order; little-endian in practice
        if struct.unpack_from(order + "I", raw, 0)[0] == MAGIC:
            break
    else:
        raise rpcs3.Rpcs3Error("not a Vita3K trophy progress file (bad magic)")
    bits = struct.unpack_from(order + "4I", raw, 4)
    times = struct.unpack_from(order + f"{MAX_TROPHIES}Q", raw, 4 + 16 + 16 + 12 + MAX_GROUPS * 4)
    earned = {}
    for tid in range(MAX_TROPHIES):
        if bits[tid >> 5] & (1 << (tid & 31)):
            when = times[tid]
            earned[tid] = float(when) if 0 < when < 2**40 else None
    return earned


def default_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return [base / "Vita3K" / "Vita3K"]
    if sys.platform == "darwin":
        pref = home / "Library" / "Application Support" / "Vita3K" / "Vita3K"
        return [pref, pref / "fs"]
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return [data / "Vita3K" / "Vita3K"]


def running_dirs(running: set[str]) -> list[Path]:
    """`portable/fs` beside a running Vita3K; Windows only (full, case-free paths)."""
    if sys.platform != "win32":
        return []
    return [Path(p).parent / "portable" / "fs" for p in running if Path(p).name.lower() in PROGRAMS]


def remembered(profile: Profile) -> list[Path]:
    return [Path(p) for p in profile.config.load().get("vita3k_dirs", {}).get(profile.profile_id, [])]


def remember(profile: Profile, folder: Path) -> None:
    with profile.config.editing() as config:
        mine = config.setdefault("vita3k_dirs", {}).setdefault(profile.profile_id, [])
        if str(folder) not in mine:
            mine.append(str(folder))


def trophy_sets(vita_fs_dirs: list[Path]) -> list[tuple[Path, Path]]:
    """(conf folder, progress file) for every trophy set with both, every user."""
    out = []
    for fs_dir in vita_fs_dirs:
        try:
            users = [u for u in (fs_dir / "ux0" / "user").iterdir() if u.is_dir()]
        except OSError:
            continue
        for user in users:
            try:
                confs = [c for c in (user / "trophy" / "conf").iterdir() if c.is_dir()]
            except OSError:
                continue
            for conf in confs:
                dat = user / "trophy" / "data" / conf.name / "TROPUSR.DAT"
                if dat.is_file() and any((conf / n).is_file() for n in NAME_FILES):
                    out.append((conf, dat))
    return out


def read_set(conf_dir: Path, dat: Path) -> tuple[dict, dict]:
    names = next(conf_dir / n for n in NAME_FILES if (conf_dir / n).is_file())
    conf = rpcs3.parse_conf(rpcs3._read(names), np_id=conf_dir.name)
    return conf, parse_progress(rpcs3._read(dat))


@dataclass
class Vita3kWatcher:
    """Finds Vita3K's trophy sets once a minute and records what changed.
    Shares the emulator switch."""
    profile: Profile
    running: object = None
    seen: dict = field(default_factory=dict)
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
        for folder in running_dirs((self.running or running_executables)()):
            if folder not in remembered(self.profile):
                remember(self.profile, folder)
        out = []
        for d in remembered(self.profile) + default_dirs():
            if d not in out:
                out.append(d)
        return out

    def poll(self) -> list[dict]:
        from .emulator import enabled
        if not enabled(self.profile):
            return []
        now = time.monotonic()
        if now - self._look >= LOOK_EVERY:
            self._look = now
            self._sets = trophy_sets(self._dirs())
        written = []
        for conf_dir, dat in self._sets:
            try:
                stamp = tuple((s.st_size, s.st_mtime_ns) for s in
                              [dat.stat()] + [(conf_dir / n).stat() for n in NAME_FILES if (conf_dir / n).is_file()])
            except OSError:
                continue
            if self.seen.get(str(dat)) == stamp:
                continue
            try:
                conf, earned = read_set(conf_dir, dat)
            except (OSError, StopIteration, rpcs3.Rpcs3Error) as exc:
                self.problems[str(dat)] = f"Vita3K {conf_dir.name}: {exc}"
                self.seen[str(dat)] = stamp
                continue
            self.problems.pop(str(dat), None)
            events = rpcs3.plan_set(self.profile, conf, earned, adapter=ADAPTER_ID,
                                    adapter_version=ADAPTER_VERSION, platform="PlayStation Vita")
            if events:
                self.profile.commit(events)
                written.extend(e for e in events if e["event_type"] == "achievement.unlocked")
            self.seen[str(dat)] = stamp
        return written
