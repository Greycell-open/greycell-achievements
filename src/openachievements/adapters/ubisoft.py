"""Ubisoft achievements LumaPlay recorded, named from Ubisoft Connect's own cache.

Some copies of Ubisoft games run with LumaPlay, a stand-in for Ubisoft
Connect's library. It records unlocks in the Windows registry:
`HKCU\\SOFTWARE\\LumaPlay\\<user>\\<app id>\\Achievements`, one value per
achievement (`ACH_<n>` or `<n>`), 1 when earned, with no time.

Those are numbers only. The names come from Ubisoft Connect's local cache, if
it is installed: `<Ubisoft Connect>\\cache\\achievements\\<app id>_<n>.zip`
holds `<language>_loc.txt` files, one achievement per line, tab-separated
(id, name, description). Both places are as Achievement Watcher reads them
(app/parser/uplay.js). Its other name source, a server run by that project's
author, is deliberately not used: nothing here goes over the network.

A game whose names are not on this computer is reported and left alone,
rather than filled with "Achievement 3" placeholders. Windows only (the
registry). Read-only.
"""
from __future__ import annotations

import io
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import events as ev
from ..packs import PackError, split_for_events, validate_definitions
from ..profile import Profile
from .base import AdapterMetadata

ADAPTER_ID = "ubisoft-emulator"
ADAPTER_VERSION = "0.1.0"
LOOK_EVERY = 60.0
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
LAUNCHER_KEY = r"SOFTWARE\WOW6432Node\Ubisoft\Launcher"
LUMAPLAY_KEY = r"SOFTWARE\LumaPlay"
LANGUAGES = ("en-US", "en-GB")

METADATA = AdapterMetadata(
    adapter_id=ADAPTER_ID,
    adapter_version=ADAPTER_VERSION,
    support_level="community",
    authentication_method="none (local)",
    data_access_method="reads LumaPlay's registry values and Ubisoft Connect's cached achievement lists, read-only",
    capabilities=("unlocks", "definitions"),
    rate_limits="reads the registry and the cache folder once a minute",
    known_limitations=(
        "Windows only",
        "names need Ubisoft Connect installed with the game's achievement list cached",
        "LumaPlay records no unlock time; the time it was first seen is used",
    ),
    terms_or_policy_notes="Observation only. Never writes to, uploads, or copies a file; never uses the network.",
)


class UbisoftError(ValueError):
    pass


# ---- the two sources -------------------------------------------------------------------

def parse_loc(text: str) -> list[dict]:
    """A `<language>_loc.txt` -> [{id, name, description}]."""
    out = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 2 or not cols[0].strip().isdigit():
            continue
        out.append({"id": int(cols[0]), "name": cols[1].strip(),
                    "description": cols[2].strip() if len(cols) > 2 else ""})
    return out


def read_archive(raw: bytes) -> list[dict]:
    """A cached achievements zip -> the English list (else the first language)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise UbisoftError(f"not a zip ({exc})") from exc
    locs = {}
    for info in archive.infolist():
        m = re.fullmatch(r"([a-z]{2}-[A-Z]{2})_loc\.txt", Path(info.filename).name)
        if m and info.file_size <= MAX_TEXT_BYTES:
            locs[m.group(1)] = info
    if not locs:
        raise UbisoftError("no achievement text in the archive")
    lang = next((l for l in LANGUAGES if l in locs), sorted(locs)[0])
    text = archive.read(locs[lang]).decode("utf-8-sig", errors="replace")
    rows = parse_loc(text)
    if not rows:
        raise UbisoftError(f"{lang}_loc.txt lists no achievements")
    return rows


def unlocked_ids(values: dict) -> set[int]:
    """LumaPlay's values for one game -> the earned achievement numbers."""
    out = set()
    for name, value in values.items():
        m = re.fullmatch(r"(?:ACH_)?(\d+)", str(name), re.I)
        try:
            if m and int(value) == 1:
                out.add(int(m.group(1)))
        except (TypeError, ValueError):
            continue
    return out


# ---- the registry and the cache folder -------------------------------------------------

def _winreg():
    import winreg                                       # Windows only
    return winreg


def _subkeys(root, path: str) -> list[str]:
    reg = _winreg()
    try:
        with reg.OpenKey(root, path) as key:
            out, i = [], 0
            while True:
                try:
                    out.append(reg.EnumKey(key, i))
                except OSError:
                    return out
                i += 1
    except OSError:
        return []


def _values(root, path: str) -> dict:
    reg = _winreg()
    try:
        with reg.OpenKey(root, path) as key:
            out, i = {}, 0
            while True:
                try:
                    name, value, _kind = reg.EnumValue(key, i)
                except OSError:
                    return out
                out[name] = value
                i += 1
    except OSError:
        return {}


def lumaplay() -> dict[int, set[int]]:
    """App id -> earned achievement numbers, across LumaPlay's users."""
    if sys.platform != "win32":
        return {}
    root = _winreg().HKEY_CURRENT_USER
    out: dict[int, set[int]] = {}
    for user in _subkeys(root, LUMAPLAY_KEY):
        for app in _subkeys(root, f"{LUMAPLAY_KEY}\\{user}"):
            if app.isdigit():
                out.setdefault(int(app), set()).update(
                    unlocked_ids(_values(root, f"{LUMAPLAY_KEY}\\{user}\\{app}\\Achievements")))
    return out


def connect_dir() -> Path | None:
    """Ubisoft Connect's install folder, from its registry entry."""
    if sys.platform != "win32":
        return None
    value = _values(_winreg().HKEY_LOCAL_MACHINE, LAUNCHER_KEY).get("InstallDir")
    return Path(value) if value else None


def installed_titles() -> dict[int, str]:
    """App id -> name from the folder Ubisoft Connect installed it into."""
    if sys.platform != "win32":
        return {}
    root = _winreg().HKEY_LOCAL_MACHINE
    out = {}
    for app in _subkeys(root, f"{LAUNCHER_KEY}\\Installs"):
        folder = _values(root, f"{LAUNCHER_KEY}\\Installs\\{app}").get("InstallDir")
        if app.isdigit() and folder:
            out[int(app)] = Path(str(folder).rstrip("\\/")).name
    return out


def archives(connect: Path | None) -> dict[int, Path]:
    """App id -> its cached achievement archive (the highest-numbered one)."""
    if connect is None:
        return {}
    found: dict[int, tuple[int, Path]] = {}
    try:
        for p in (connect / "cache" / "achievements").iterdir():
            m = re.fullmatch(r"(\d+)_(\d+)\.zip", p.name)
            if m and (int(m.group(1)) not in found or int(m.group(2)) > found[int(m.group(1))][0]):
                found[int(m.group(1))] = (int(m.group(2)), p)
    except OSError:
        return {}
    return {app: p for app, (_n, p) in found.items()}


# ---- into the library ------------------------------------------------------------------

def game_id_for(app_id: int) -> str:
    return f"ubisoft-{int(app_id)}"


def plan_game(profile: Profile, app_id: int, title: str, rows: list[dict], earned: set[int]) -> list[dict]:
    from ..reducer import is_unlocked
    state = profile.state()
    game_id = game_id_for(app_id)
    make = lambda kind, payload, **kw: ev.make_event(kind, profile_id=profile.profile_id, device_id=profile.device_id,
                                                     payload=payload, adapter=ADAPTER_ID,
                                                     adapter_version=ADAPTER_VERSION, **kw)
    out = []
    if game_id not in state["games"]:
        out.append(make("game.registered", {"title": title, "platform": "PC",
                                            "external_ids": {"ubisoft": app_id}}, game_id=game_id))
    defs = [{"id": f"a{r['id']}", "name": r["name"] or f"Achievement {r['id']}",
             "description": r["description"], "hidden": False, "points": 0} for r in rows]
    installed = state["packs"].get(game_id)
    mine = not installed or installed.get("removed") or installed.get("source") == ADAPTER_ID
    fields = ("name", "description")
    current = {k: {f: v.get(f) for f in fields} for k, v in (installed or {}).get("achievements", {}).items()}
    wanted = {d["id"]: {f: d.get(f) for f in fields} for d in defs}
    if mine and (not installed or installed.get("removed") or current != wanted):
        meta = {"id": game_id, "name": title, "game_ids": [game_id], "version": "1.0.0",
                "source": ADAPTER_ID, "supported_adapters": [ADAPTER_ID]}
        try:
            description = validate_definitions(meta, defs)
        except PackError:
            return out
        kind = "pack.updated" if installed and not installed.get("removed") else "pack.installed"
        out.extend(make(kind, payload) for payload in split_for_events(description))
        have = {d["id"] for d in defs}
    else:
        have = set((installed or {}).get("achievements", {}))
    for n in sorted(earned):
        aid = f"a{n}"
        if aid not in have or is_unlocked(state, f"{game_id}:{aid}"):
            continue
        out.append(make("achievement.unlocked", {"provenance": "emulator", "mode": "lumaplay"},
                        achievement_id=f"{game_id}:{aid}", game_id=game_id, occurred_at=ev.now(),
                        external_event_id=f"{profile.profile_id}:lumaplay:{app_id}:{n}"))
    return out


@dataclass
class UbisoftWatcher:
    """Reads LumaPlay and the cache once a minute and records what changed.
    Shares the emulator switch. Sources are injectable for tests."""
    profile: Profile
    read_unlocks: object = None                           # () -> {app id: {numbers}}
    read_connect: object = None                           # () -> Ubisoft Connect folder or None
    read_titles: object = None                            # () -> {app id: name}
    seen: dict = field(default_factory=dict)              # app id -> (earned, archive stamp)
    problems: dict = field(default_factory=dict)
    _reported: set = field(default_factory=set)
    _look: float = -1e18

    def new_problems(self) -> list[str]:
        fresh = [m for k, m in self.problems.items() if (k, m) not in self._reported]
        self._reported.update(self.problems.items())
        return fresh

    def poll(self) -> list[dict]:
        from .emulator import enabled
        if not enabled(self.profile):
            return []
        now = time.monotonic()
        if now - self._look < LOOK_EVERY:
            return []
        self._look = now
        unlocks = (self.read_unlocks or lumaplay)()
        if not unlocks:
            return []
        lists = archives((self.read_connect or connect_dir)())
        titles = None
        written = []
        for app, earned in sorted(unlocks.items()):
            archive = lists.get(app)
            try:
                stamp = (frozenset(earned), (archive.stat().st_size, archive.stat().st_mtime_ns) if archive else None)
            except OSError:
                stamp = (frozenset(earned), None)
            if self.seen.get(app) == stamp:
                continue
            self.seen[app] = stamp
            if archive is None:
                if earned:
                    self.problems[f"names:{app}"] = (f"Ubisoft app {app}: LumaPlay recorded {len(earned)} unlock(s), "
                                                     "but its achievement names are not on this computer "
                                                     "(they come from Ubisoft Connect's cache)")
                continue
            try:
                if archive.stat().st_size > MAX_ARCHIVE_BYTES:
                    raise UbisoftError("archive is larger than 32 MB")
                rows = read_archive(archive.read_bytes())
            except (OSError, UbisoftError) as exc:
                self.problems[str(archive)] = f"Ubisoft app {app}: {exc}"
                continue
            self.problems.pop(f"names:{app}", None)
            self.problems.pop(str(archive), None)
            if titles is None:
                titles = (self.read_titles or installed_titles)()
            events = plan_game(self.profile, app, titles.get(app) or f"Ubisoft game {app}", rows, earned)
            if events:
                self.profile.commit(events)
                written.extend(e for e in events if e["event_type"] == "achievement.unlocked")
        return written
