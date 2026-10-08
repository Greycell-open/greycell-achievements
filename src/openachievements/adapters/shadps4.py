"""PS4 trophies shadPS4 recorded on this computer.

shadPS4 keeps two things under its user folder (layout from shadPS4's own
source, src/core/libraries/np/np_trophy.cpp and src/common/path_util.cpp):

- `trophy/<NP id>/Xml/TROP_01.XML` (English) or `TROP.XML`: the trophy list,
  XML (`<trophyconf>`: `title-name`, `<trophy id ttype hidden>` with `<name>`
  and `<detail>`), read with the same reader as RPCS3's.
- `home/<user id>/trophy/<NP id>.xml`: one player's progress, the trophy
  elements again, an earned one carrying `unlockstate="true"` and
  `timestamp` (Unix seconds, from the system clock).

The user folder is `user` beside the program when it exists (shadPS4 makes it
there on Windows; found from the running program and remembered), else
%APPDATA%/shadPS4, ~/.local/share/shadPS4 (or $XDG_DATA_HOME), or
Application Support/shadPS4 on macOS. A home folder moved in shadPS4's
settings is not followed. Read-only. Games register as `psn-<id>`, shared with
the PSN import and the other PlayStation emulators (rpcs3.plan_set).
"""
from __future__ import annotations

import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from ..profile import Profile
from . import rpcs3
from .base import AdapterMetadata

ADAPTER_ID = "shadps4"
ADAPTER_VERSION = "0.1.0"
NAME_FILES = ("TROP_01.XML", "TROP_18.XML", "TROP.XML")
PROGRAMS = ("shadps4.exe",)
LOOK_EVERY = 60.0

METADATA = AdapterMetadata(
    adapter_id=ADAPTER_ID,
    adapter_version=ADAPTER_VERSION,
    support_level="community",
    authentication_method="none (local)",
    data_access_method="reads shadPS4's trophy list and per-user progress XML, read-only",
    capabilities=("unlocks", "unlock-times", "definitions"),
    rate_limits="looks for trophy sets once a minute; parses only files that changed",
    known_limitations=("a portable shadPS4 is found once the app has seen it running",
                       "a home folder moved in shadPS4's settings is not followed"),
    terms_or_policy_notes="Observation only. Never writes to, uploads, or copies a file.",
)


def parse_progress(raw: bytes) -> dict[int, float | None]:
    """A `home/<user>/trophy/<NP id>.xml` -> {trophy id: unix time or None}."""
    text = raw.decode("utf-8", errors="replace")
    start = text.find("<trophyconf")
    if start < 0:
        raise rpcs3.Rpcs3Error("not a trophy progress file (no <trophyconf>)")
    try:
        root = ET.fromstring(text[start:])
    except ET.ParseError as exc:
        raise rpcs3.Rpcs3Error(f"trophy progress is not valid XML ({exc})") from exc
    earned = {}
    for t in root.iter("trophy"):
        if (t.get("unlockstate") or "").strip().lower() not in ("true", "1"):
            continue
        try:
            tid = int(t.get("id", ""))
        except ValueError:
            continue
        try:
            when = int(t.get("timestamp") or 0)
        except ValueError:
            when = 0
        earned[tid] = float(when) if 0 < when < 2**40 else None
    return earned


def default_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        return [Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "shadPS4"]
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "shadPS4"]
    return [Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "shadPS4"]


def running_dirs(running: set[str]) -> list[Path]:
    """The `user` folder beside a running shadPS4; Windows only."""
    if sys.platform != "win32":
        return []
    return [Path(p).parent / "user" for p in running if Path(p).name.lower() in PROGRAMS]


def remembered(profile: Profile) -> list[Path]:
    return [Path(p) for p in profile.config.load().get("shadps4_dirs", {}).get(profile.profile_id, [])]


def remember(profile: Profile, folder: Path) -> None:
    with profile.config.editing() as config:
        mine = config.setdefault("shadps4_dirs", {}).setdefault(profile.profile_id, [])
        if str(folder) not in mine:
            mine.append(str(folder))


def trophy_sets(user_dirs: list[Path]) -> list[tuple[Path, Path]]:
    """(trophy list folder, progress file) for each set a player has progress in."""
    out = []
    for user_dir in user_dirs:
        try:
            players = [p for p in (user_dir / "home").iterdir() if p.is_dir()]
        except OSError:
            continue
        for player in players:
            try:
                saves = [s for s in (player / "trophy").iterdir() if s.suffix.lower() == ".xml"]
            except OSError:
                continue
            for save in saves:
                xml_dir = user_dir / "trophy" / save.stem / "Xml"
                if any((xml_dir / n).is_file() for n in NAME_FILES):
                    out.append((xml_dir, save))
    return out


def read_set(xml_dir: Path, save: Path) -> tuple[dict, dict]:
    names = next(xml_dir / n for n in NAME_FILES if (xml_dir / n).is_file())
    conf = rpcs3.parse_conf(rpcs3._read(names), np_id=save.stem)
    return conf, parse_progress(rpcs3._read(save))


@dataclass
class ShadPs4Watcher:
    """Finds shadPS4's trophy sets once a minute and records what changed.
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
        for xml_dir, save in self._sets:
            try:
                stamp = tuple((s.st_size, s.st_mtime_ns) for s in
                              [save.stat()] + [(xml_dir / n).stat() for n in NAME_FILES if (xml_dir / n).is_file()])
            except OSError:
                continue
            if self.seen.get(str(save)) == stamp:
                continue
            try:
                conf, earned = read_set(xml_dir, save)
            except (OSError, StopIteration, rpcs3.Rpcs3Error) as exc:
                self.problems[str(save)] = f"shadPS4 {save.stem}: {exc}"
                self.seen[str(save)] = stamp
                continue
            self.problems.pop(str(save), None)
            events = rpcs3.plan_set(self.profile, conf, earned, adapter=ADAPTER_ID,
                                    adapter_version=ADAPTER_VERSION, platform="PlayStation 4")
            if events:
                self.profile.commit(events)
                written.extend(e for e in events if e["event_type"] == "achievement.unlocked")
            self.seen[str(save)] = stamp
        return written
