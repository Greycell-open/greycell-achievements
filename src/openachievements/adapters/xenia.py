"""Xbox 360 achievements Xenia recorded on this computer.

Xenia (the Xbox 360 emulator, Canary builds) keeps each profile the way the
console does, as a folder: `<content root>/<XUID>/FFFE07D1/00010000/<XUID>/`,
holding one GPD per game, `<title id>.gpd`, plus the dashboard's
`FFFE07D1.gpd`. A game's GPD holds both its achievement list and what was
earned, so a game needs no catalogue and no network.

A GPD is an XDBF database, big-endian (layout from Xenia's own source,
src/xenia/kernel/xam/xdbf, which cites the free60 XDBF notes): a 24-byte
header (magic "XDBF", version, entry count, entries used, free count, free
used), then `entry count` entries of 18 bytes (u16 section, u64 id, u32
offset, u32 size), then `free count` 8-byte free slots, then the data the
offsets point into. Section 1 is achievements: a 0x1C-byte record (magic, id,
image id, gamerscore, flags, u64 unlock time as a Windows FILETIME) followed
by three UTF-16 strings (name, description, locked description). Flag 0x20000
is earned; 0x8 (show while locked) unset means a secret one. The game's name is string 0x8000 in section 5, else its entry in
the dashboard GPD's section 4 (a 0x28-byte record, then the name).

Where the content root is: Xenia Canary on Windows is portable by default, so
it is `content` beside the program, found from the running program and
remembered; also `Documents/Xenia/content`, `~/.local/share/Xenia/content` on
Linux, and a `content_root` set in Xenia's config file. Read-only.

A game registers under the id the Xbox import uses (`xbox-<title id>`), so a
game seen both ways is one game. Its list is installed only when the game has
none or the one it has came from here.
"""
from __future__ import annotations

import os
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import events as ev
from ..packs import PackError, split_for_events, validate_definitions
from ..profile import Profile
from .base import AdapterMetadata, utc_iso

ADAPTER_ID = "xenia"
ADAPTER_VERSION = "0.1.0"
MAX_FILE_BYTES = 8 * 1024 * 1024
LOOK_EVERY = 60.0
DASHBOARD = "FFFE07D1"
PROFILE_PACKAGE = "00010000"
SECTION_ACHIEVEMENT, SECTION_TITLE, SECTION_STRING = 1, 4, 5
TITLE_STRING_ID = 0x8000
ACHIEVED = 0x20000
SHOW_UNACHIEVED = 0x8                  # the console shows it while locked; without it, it is secret
FILETIME_TO_UNIX = 11644473600         # seconds from 1601-01-01 to 1970-01-01
PROGRAMS = ("xenia_canary.exe", "xenia.exe")
CONFIGS = ("xenia-canary.config.toml", "xenia.config.toml")

METADATA = AdapterMetadata(
    adapter_id=ADAPTER_ID,
    adapter_version=ADAPTER_VERSION,
    support_level="community",
    authentication_method="none (local)",
    data_access_method="reads Xenia's profile GPD files (XDBF), read-only",
    capabilities=("unlocks", "unlock-times", "definitions"),
    rate_limits="looks for profiles once a minute; parses only files that changed",
    known_limitations=(
        "Xenia's folder is found once the app has seen it running, or in Documents/Xenia or ~/.local/share/Xenia",
        "Xbox 360 only; needs a Xenia build that records achievements (Canary)",
    ),
    terms_or_policy_notes="Observation only. Never writes to, uploads, or copies a file.",
)


class XeniaError(ValueError):
    pass


# ---- the GPD ---------------------------------------------------------------------------

def _u16(raw: bytes, at: int) -> tuple[str, int]:
    """A NUL-terminated UTF-16BE string at `at`, and where the next one starts."""
    end = at
    while end + 1 < len(raw) and raw[end:end + 2] != b"\x00\x00":
        end += 2
    return raw[at:end].decode("utf-16-be", errors="replace"), end + 2


def entries(raw: bytes) -> list[tuple[int, int, bytes]]:
    """(section, id, data) for every used entry of an XDBF file."""
    if len(raw) < 24 or raw[:4] != b"XDBF":
        raise XeniaError("not a GPD (bad magic)")
    _magic, _version, count, used, free_count, _free_used = struct.unpack_from(">IIIIII", raw, 0)
    if count > 65536 or used > count or free_count > 65536:
        raise XeniaError("unexpected GPD header")
    data_at = 24 + count * 18 + free_count * 8
    if data_at > len(raw):
        raise XeniaError("GPD header runs past the end")
    out = []
    for i in range(used):
        section, eid, offset, size = struct.unpack_from(">HQII", raw, 24 + i * 18)
        start = data_at + offset
        if start + size > len(raw):
            raise XeniaError("GPD entry runs past the end")
        out.append((section, eid, raw[start:start + size]))
    return out


def parse_title_gpd(raw: bytes) -> dict:
    """A game's GPD -> {"title", "achievements": [{id, name, description,
    locked, gamerscore, earned, when}]}."""
    title, achievements = None, []
    for section, eid, data in entries(raw):
        if section == SECTION_STRING and eid == TITLE_STRING_ID:
            title = _u16(data, 0)[0].strip() or None
        elif section == SECTION_ACHIEVEMENT and len(data) >= 0x1C:
            _magic, aid, _image, score, flags, stamp = struct.unpack_from(">IIIIIQ", data, 0)
            name, at = _u16(data, 0x1C)
            description, at = _u16(data, at)
            locked, _ = _u16(data, at)
            earned = bool(flags & ACHIEVED)
            when = stamp / 10_000_000 - FILETIME_TO_UNIX if earned and stamp else None
            achievements.append({"id": aid, "name": name.strip(), "description": description.strip(),
                                 "locked": locked.strip(), "gamerscore": score, "earned": earned,
                                 "hidden": not flags & SHOW_UNACHIEVED,
                                 "when": when if when and when > 0 else None})
    if not achievements:
        raise XeniaError("GPD has no achievements")
    return {"title": title, "achievements": achievements}


def dashboard_titles(raw: bytes) -> dict[int, str]:
    """Title id -> name, from the dashboard GPD's played-titles section."""
    names = {}
    for section, eid, data in entries(raw):
        if section == SECTION_TITLE and len(data) > 0x28:
            name = _u16(data, 0x28)[0].strip()
            if name:
                names[eid & 0xFFFFFFFF] = name
    return names


# ---- where Xenia keeps them ------------------------------------------------------------

def _config_root(storage: Path) -> Path | None:
    for name in CONFIGS:
        try:
            text = (storage / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'^\s*content_root\s*=\s*"([^"]*)"', text, re.M)
        if m and m.group(1).strip():
            root = Path(m.group(1).strip())
            return root if root.is_absolute() else storage / root
    return None


def content_root(storage: Path) -> Path:
    return _config_root(storage) or storage / "content"


def default_storage() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        return [home / "Documents" / "Xenia"]
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return [data / "Xenia"]


def remembered(profile: Profile) -> list[Path]:
    return [Path(p) for p in profile.config.load().get("xenia_dirs", {}).get(profile.profile_id, [])]


def remember(profile: Profile, folder: Path) -> None:
    with profile.config.editing() as config:
        mine = config.setdefault("xenia_dirs", {}).setdefault(profile.profile_id, [])
        if str(folder) not in mine:
            mine.append(str(folder))


def running_dirs(running: set[str]) -> list[Path]:
    """Xenia folders from running programs; Windows only (full paths, case-free)."""
    if sys.platform != "win32":
        return []
    return [Path(p).parent for p in running if Path(p).name.lower() in PROGRAMS]


def profile_folders(storages: list[Path]) -> list[Path]:
    """Every profile folder holding GPDs, in every Xenia storage folder."""
    out = []
    for storage in storages:
        root = content_root(storage)
        try:
            xuids = [d for d in root.iterdir() if d.is_dir() and re.fullmatch(r"[0-9A-Fa-f]{16}", d.name)
                     and d.name.strip("0")]
        except OSError:
            continue
        for x in xuids:
            folder = x / DASHBOARD / PROFILE_PACKAGE / x.name
            if folder.is_dir() and folder not in out:
                out.append(folder)
    return out


# ---- into the library ------------------------------------------------------------------

def game_id_for(title_id: int) -> str:
    return f"xbox-{int(title_id)}"


def _read(path: Path) -> bytes:
    with open(path, "rb") as fh:
        raw = fh.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise XeniaError(f"{path.name} is larger than 8 MB")
    return raw


def plan_game(profile: Profile, title_id: int, game: dict, fallback_title: str | None) -> list[dict]:
    from ..reducer import is_unlocked
    state = profile.state()
    game_id = game_id_for(title_id)
    title = game["title"] or fallback_title or f"Xbox 360 title {title_id:08X}"
    make = lambda kind, payload, **kw: ev.make_event(kind, profile_id=profile.profile_id, device_id=profile.device_id,
                                                     payload=payload, adapter=ADAPTER_ID,
                                                     adapter_version=ADAPTER_VERSION, **kw)
    out = []
    if game_id not in state["games"]:
        out.append(make("game.registered", {"title": title, "platform": "Xbox 360",
                                            "external_ids": {"xbox": title_id}}, game_id=game_id))
    rows = [{"id": f"a{a['id']}", "name": a["name"] or f"Achievement {a['id']}",
             "description": a["description"] or a["locked"], "hidden": a["hidden"],
             "points": min(int(a["gamerscore"]), 10000)} for a in game["achievements"]]
    installed = state["packs"].get(game_id)
    mine = not installed or installed.get("removed") or installed.get("source") == ADAPTER_ID
    fields = ("name", "description", "hidden", "points")
    current = {k: {f: v.get(f) for f in fields} for k, v in (installed or {}).get("achievements", {}).items()}
    wanted = {r["id"]: {f: r.get(f) for f in fields} for r in rows}
    if mine and (not installed or installed.get("removed") or current != wanted):
        meta = {"id": game_id, "name": title, "game_ids": [game_id], "version": "1.0.0",
                "source": ADAPTER_ID, "supported_adapters": [ADAPTER_ID, "xbox"]}
        try:
            description = validate_definitions(meta, rows)
        except PackError:
            return out
        kind = "pack.updated" if installed and not installed.get("removed") else "pack.installed"
        out.extend(make(kind, payload) for payload in split_for_events(description))
        have = {r["id"] for r in rows}
    else:
        have = set((installed or {}).get("achievements", {}))
    for a in sorted((a for a in game["achievements"] if a["earned"]), key=lambda a: a["when"] or 0):
        aid = f"a{a['id']}"
        if aid not in have or is_unlocked(state, f"{game_id}:{aid}"):
            continue
        occurred = utc_iso(a["when"]) if a["when"] and a["when"] <= time.time() + 60 else None
        out.append(make("achievement.unlocked", {"provenance": "emulator", "mode": "xenia"},
                        achievement_id=f"{game_id}:{aid}", game_id=game_id, occurred_at=occurred or ev.now(),
                        external_event_id=f"{profile.profile_id}:xenia:{title_id:08X}:{a['id']}"))
    return out


@dataclass
class XeniaWatcher:
    """Finds Xenia profiles once a minute and records what changed in their
    GPDs. Shares the emulator switch (`openachievements emulators on|off`)."""
    profile: Profile
    running: object = None                                # injectable: () -> set of running program paths
    seen: dict = field(default_factory=dict)              # gpd path -> (size, mtime_ns)
    problems: dict = field(default_factory=dict)
    _reported: set = field(default_factory=set)
    _folders: list = field(default_factory=list)
    _look: float = -1e18

    def new_problems(self) -> list[str]:
        fresh = [m for k, m in self.problems.items() if (k, m) not in self._reported]
        self._reported.update(self.problems.items())
        return fresh

    def _storages(self) -> list[Path]:
        from .executable import running_executables
        for folder in running_dirs((self.running or running_executables)()):
            if folder not in remembered(self.profile):
                remember(self.profile, folder)
        out = []
        for d in remembered(self.profile) + default_storage():
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
            self._folders = profile_folders(self._storages())
        written = []
        for folder in self._folders:
            names: dict | None = None
            try:
                gpds = [p for p in folder.iterdir() if re.fullmatch(r"[0-9A-Fa-f]{8}\.gpd", p.name)
                        and p.stem.upper() != DASHBOARD]
            except OSError:
                continue
            for gpd in gpds:
                try:
                    st = gpd.stat()
                except OSError:
                    continue
                stamp = (st.st_size, st.st_mtime_ns)
                if self.seen.get(str(gpd)) == stamp:
                    continue
                try:
                    game = parse_title_gpd(_read(gpd))
                except (OSError, XeniaError) as exc:
                    self.problems[str(gpd)] = f"Xenia {gpd.stem}: {exc}"
                    self.seen[str(gpd)] = stamp
                    continue
                if names is None:
                    try:
                        names = dashboard_titles(_read(folder / f"{DASHBOARD}.gpd"))
                    except (OSError, XeniaError):
                        names = {}
                self.problems.pop(str(gpd), None)
                title_id = int(gpd.stem, 16)
                events = plan_game(self.profile, title_id, game, names.get(title_id))
                if events:
                    self.profile.commit(events)
                    written.extend(e for e in events if e["event_type"] == "achievement.unlocked")
                self.seen[str(gpd)] = stamp
        return written
