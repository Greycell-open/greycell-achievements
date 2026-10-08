"""Achievements a Steam emulator recorded on this computer.

Some copies of Steam games run with a stand-in for Steam's own library (a
Steam emulator: CODEX, RUNE, OnlineFix, Goldberg, GSE, EMPRESS, SmartSteamEmu,
CreamAPI, Reloaded, 3DM, SKIDROW, TENOKE, ALI213, Hoodlum, DARKSiDERS,
UniverseLAN; where each keeps its file is in emulator_formats.py). When such a
game unlocks an achievement it calls Steam's ordinary API, and the stand-in
writes it to a small file on this computer: the game's own unlock, by Steam's
internal name, with its time. This reads those files, read-only, from fixed
places, and records each unlock against the game's Steam achievement list.
It works for every game without per-game rules, because the game itself says
which achievement it unlocked.

PS3 trophies from RPCS3 are read by rpcs3.py, which shares this module's
on/off switch.

It never ships, installs or changes an emulator, and never writes anything.
Most emulators keep their file in a fixed folder; a few (TENOKE, ALI213,
Hoodlum, DARKSiDERS) keep it beside the game, and those are found from the
emulator's settings file in the folder of a program the library already
knows (owner, 2026-10-08: "cover every repacker").

Steam's internal names are matched to the library's achievements by the
pack's `external_id`, else through Steam's keyless schema (internal name and
display name), which the rarity cache already fetches for each game and keeps
on this computer (`rarity.names`). With rarity switched off and no
`external_id`, a game's unlocks wait.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import events as ev
from ..profile import Profile, ProfileError
from .base import AdapterMetadata, utc_iso

ADAPTER_ID = "steam-emulator"
ADAPTER_VERSION = "0.1.0"
MAX_FILE_BYTES = 4 * 1024 * 1024
NAMES_RETRY = 3600.0                # a game whose names Steam did not give: asked again in an hour

METADATA = AdapterMetadata(
    adapter_id=ADAPTER_ID,
    adapter_version=ADAPTER_VERSION,
    support_level="community",
    authentication_method="none (local)",
    data_access_method="reads the achievement file a Steam emulator writes, read-only",
    capabilities=("unlocks", "unlock-times", "any-game"),
    rate_limits="looks at a few fixed folders every few seconds; parses only files that changed",
    known_limitations=(
        "only the emulators in emulator_formats, in their default folders or beside a known program",
        "matching needs Steam's achievement names for the game (rarity cache or the pack)",
    ),
    terms_or_policy_notes="Observation only. Never writes to, uploads, or copies a file.",
)

from . import emulator_formats as formats
from .emulator_formats import LOCATIONS, parse  # noqa: F401 - the formats live there; kept importable here

GAME_LOOK_EVERY = 60.0              # seconds between looks beside the library's own programs


def _roots() -> list[dict[str, Path]]:
    """The emulator roots on this computer: Windows' own, then each Proton
    prefix's (a Windows game on Linux keeps them inside its prefix)."""
    found = []
    if sys.platform == "win32":
        found.append(formats.windows_roots())
    from . import savefile
    for user in savefile._proton_roots().values():
        found.append({"PUBLIC_DOCUMENTS": user.parent / "Public" / "Documents",
                      "APPDATA": user / "AppData" / "Roaming", "LOCALAPPDATA": user / "AppData" / "Local",
                      "DOCUMENTS": user / "Documents", "PROGRAMDATA": user.parent.parent / "ProgramData"})
    return found


def achievement_files(game_dirs: list[Path] | None = None) -> list[tuple[str, int, Path, str]]:
    """(emulator, app id, file, format) for every achievement file present:
    the fixed folders, then beside the given game folders."""
    out = []
    for roots in _roots():
        out.extend(formats.fixed_files(roots))
    if game_dirs:
        out.extend(formats.game_files(game_dirs))
    seen, unique = set(), []
    for emulator, appid, path in out:
        if str(path) not in seen:
            seen.add(str(path))
            unique.append((emulator, appid, path, formats.file_format(path)))
    return unique


def game_dirs(profile: Profile) -> list[Path]:
    """The folders of this computer's registered game programs."""
    from .executable import local_installations
    dirs = []
    for inst in local_installations(profile):
        folder = Path(inst["path"]).parent
        if folder not in dirs:
            dirs.append(folder)
    return dirs


def enabled(profile: Profile) -> bool:
    return profile.config.load().get("emulators", {}).get(profile.profile_id, True)


def set_enabled(profile: Profile, on: bool) -> None:
    with profile.config.editing() as config:
        config.setdefault("emulators", {})[profile.profile_id] = on


def _key(name) -> str:
    return " ".join(str(name or "").lower().split())


@dataclass
class EmulatorWatcher:
    """Re-reads an achievement file only when its size or time changes, and
    records each unlock it has not recorded yet. A game not in the library
    joins it (Playing), and its unlocks follow once its achievement list is
    there. `poll()` is safe to call as often as you like."""
    profile: Profile
    catalog_dir: Path | None = None
    seen: dict = field(default_factory=dict)          # path -> (size, mtime_ns), once fully recorded
    problems: dict = field(default_factory=dict)
    _reported: set = field(default_factory=set)
    _asked: dict = field(default_factory=dict)        # appid -> monotonic time Steam was asked for names
    _game_files: list = field(default_factory=list)   # beside-the-game files, refreshed once a minute
    _game_look: float = -1e18

    def new_problems(self) -> list[str]:
        fresh = [m for k, m in self.problems.items() if (k, m) not in self._reported]
        self._reported.update(self.problems.items())
        return fresh

    def poll(self) -> list[dict]:
        if not enabled(self.profile):
            return []
        written = []
        now = time.monotonic()
        if now - self._game_look >= GAME_LOOK_EVERY:
            self._game_look = now
            try:
                self._game_files = [(e, a, p, formats.file_format(p))
                                    for e, a, p in formats.game_files(game_dirs(self.profile))]
            except OSError:
                self._game_files = []
        fixed = achievement_files()
        known = {str(f[2]) for f in fixed}
        for emulator, appid, path, fmt in fixed + [f for f in self._game_files if str(f[2]) not in known]:
            try:
                st = path.stat()
            except OSError:
                continue
            stamp = (st.st_size, st.st_mtime_ns)
            if self.seen.get(str(path)) == stamp:
                continue
            if st.st_size > MAX_FILE_BYTES:
                self.problems[str(path)] = f"{emulator} {appid}: achievement file is larger than 4 MB"
                self.seen[str(path)] = stamp
                continue
            try:
                with open(path, "rb") as fh:
                    unlocked = parse(fh.read(MAX_FILE_BYTES + 1), fmt)
            except (OSError, ValueError) as exc:
                self.problems[str(path)] = f"{emulator} {appid}: {exc}"
                self.seen[str(path)] = stamp           # not again until it changes
                continue
            self.problems.pop(str(path), None)
            new, complete = self._record(emulator, appid, unlocked)
            written.extend(new)
            if complete:
                self.seen[str(path)] = stamp           # else looked at again: names or list still coming
        return written

    def _game(self, appid: int) -> str | None:
        """The library's game for this app id, adding it when it is not there."""
        game_id = f"steam-{appid}"
        state = self.profile.state()
        if game_id in state["games"]:
            return game_id
        from ..catalog import steam as cat
        from .steam_local import known_names
        folder = Path(self.catalog_dir) if self.catalog_dir else cat.default_dir()
        try:
            index = cat.CatalogIndex(folder) if (folder / cat.PACKS_FILE).exists() else None
            if index is not None and index.get(appid):
                cat.add_to_profile(self.profile, index, appid)
            else:
                title = known_names(None, index, folder).get(appid) or f"Steam app {appid}"
                self.profile.register_game(game_id, title, platform="PC", external_ids={"steam": appid})
            self.profile.set_status(game_id, "playing")
        except (ProfileError, cat.CatalogError, OSError, ValueError) as exc:
            self.problems[f"game:{appid}"] = f"Steam app {appid}: could not add it to the library ({exc})"
            return None
        return game_id

    def _names(self, appid: int) -> dict:
        """Steam internal name -> display name, from the rarity cache, asking
        Steam once (an hour apart at most) when the cache has none."""
        from .. import privacy, rarity
        names = rarity.names(str(appid))
        if names is None and privacy.allowed(self.profile.config, "rarity"):
            now = time.monotonic()
            if now - self._asked.get(appid, -NAMES_RETRY) >= NAMES_RETRY:
                self._asked[appid] = now
                rarity.fetch(str(appid))
                names = rarity.names(str(appid))
        return names or {}

    def _record(self, emulator: str, appid: int, unlocked: dict) -> tuple[list[dict], bool]:
        """Unlock events for what is new; complete is False while some unlock
        could not be placed yet (no achievement list, or no names)."""
        from ..reducer import is_unlocked
        if not unlocked:
            return [], True
        game_id = self._game(appid)
        if game_id is None:
            return [], False
        state = self.profile.state()
        pack = state["packs"].get(game_id)
        if not pack or pack.get("removed"):
            return [], False                            # the watcher fetches its list; looked at again later
        by_external = {str(a.get("external_id")): aid for aid, a in pack["achievements"].items()
                       if a.get("external_id")}
        by_name: dict[str, list[str]] = {}
        for aid, a in pack["achievements"].items():
            by_name.setdefault(_key(a.get("name")), []).append(aid)
        names = None
        written, complete = [], True
        if any(k.startswith("crc:") for k in unlocked):          # SmartSteamEmu names achievements by CRC32
            known_ids = set(by_external)
            if len(known_ids) < len(pack["achievements"]):
                known_ids |= set(self._names(appid))
            by_crc = {formats.crc_name(n): n for n in known_ids}
            unlocked = {by_crc.get(k, k): v for k, v in unlocked.items()}
        for api_name, when in sorted(unlocked.items(), key=lambda kv: kv[1] or 0):
            aid = by_external.get(api_name)
            if aid is None:
                names = self._names(appid) if names is None else names
                matches = by_name.get(_key(names.get(api_name)), []) if names.get(api_name) else []
                aid = matches[0] if len(matches) == 1 else None
            if aid is None:
                complete = False
                self.problems[f"{appid}:{api_name}"] = (f"Steam app {appid}: {emulator} recorded {api_name}, "
                                                        "which is not matched to an achievement yet")
                continue
            self.problems.pop(f"{appid}:{api_name}", None)
            key = f"{game_id}:{aid}"
            if is_unlocked(state, key):
                continue
            written.append(self.profile.record("achievement.unlocked", {
                "provenance": "emulator", "mode": emulator},
                achievement_id=key, game_id=game_id,
                occurred_at=_occurred(when), adapter=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
                external_event_id=f"{self.profile.profile_id}:{appid}:{api_name}"))
            state = self.profile.state()
        return written, complete


def _occurred(when: float | None) -> str:
    """The emulator's unlock time, never in the future; now when it kept none."""
    if when is None or when > time.time() + 60:
        return ev.now()
    return utc_iso(when) or ev.now()
