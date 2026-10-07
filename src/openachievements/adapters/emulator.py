"""Achievements a Steam emulator recorded on this computer.

Some copies of Steam games run with a stand-in for Steam's own library (the
CODEX, RUNE, Goldberg, GSE, EMPRESS and OnlineFix emulators). When such a
game unlocks an achievement it calls Steam's ordinary API, and the stand-in
writes it to a small file on this computer: the game's own unlock, by Steam's
internal name, with its time. This reads those files, read-only, from fixed
places, and records each unlock against the game's Steam achievement list.
It works for every game without per-game rules, because the game itself says
which achievement it unlocked.

It never ships, installs, changes or looks for an emulator, and never touches
a game's files: only the emulator's own achievement file is read.

Steam's internal names are matched to the library's achievements by the
pack's `external_id`, else through Steam's keyless schema (internal name and
display name), which the rarity cache already fetches for each game and keeps
on this computer (`rarity.names`). With rarity switched off and no
`external_id`, a game's unlocks wait.
"""
from __future__ import annotations

import configparser
import json
import os
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
        "only the emulators listed in LOCATIONS, in their default folders",
        "matching needs Steam's achievement names for the game (rarity cache or the pack)",
    ),
    terms_or_policy_notes="Observation only. Never writes to, uploads, or copies a file.",
)

# (emulator, root, folder holding one folder per app id, file inside that folder, format)
LOCATIONS = (
    ("CODEX", "PUBLIC_DOCUMENTS", "Steam/CODEX", "achievements.ini", "ini"),
    ("CODEX", "APPDATA", "Steam/CODEX", "achievements.ini", "ini"),
    ("RUNE", "PUBLIC_DOCUMENTS", "Steam/RUNE", "achievements.ini", "ini"),
    ("OnlineFix", "PUBLIC_DOCUMENTS", "OnlineFix", "Stats/Achievements.ini", "ini"),
    ("Goldberg", "APPDATA", "Goldberg SteamEmu Saves", "achievements.json", "json"),
    ("GSE", "APPDATA", "GSE Saves", "achievements.json", "json"),
    ("EMPRESS", "APPDATA", "EMPRESS", "remote/{appid}/achievements.json", "json"),
)
_TRUE = ("1", "true", "yes")


def _roots() -> list[dict[str, Path]]:
    """The emulator roots on this computer: Windows' own, then each Proton
    prefix's (a Windows game on Linux keeps them inside its prefix)."""
    found = []
    if sys.platform == "win32":
        public = os.environ.get("PUBLIC") or str(Path(os.environ.get("SystemDrive", "C:") + "\\") / "Users" / "Public")
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        found.append({"PUBLIC_DOCUMENTS": Path(public) / "Documents", "APPDATA": Path(appdata)})
    from . import savefile
    for user in savefile._proton_roots().values():
        found.append({"PUBLIC_DOCUMENTS": user.parent / "Public" / "Documents",
                      "APPDATA": user / "AppData" / "Roaming"})
    return found


def achievement_files() -> list[tuple[str, int, Path, str]]:
    """(emulator, app id, file, format) for every achievement file present."""
    out = []
    for roots in _roots():
        for emulator, root, folder, inner, fmt in LOCATIONS:
            base = roots.get(root)
            if base is None:
                continue
            base = base.joinpath(*folder.split("/"))
            try:
                entries = [e for e in os.scandir(base) if e.name.isdigit() and e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for e in entries:
                path = Path(e.path).joinpath(*inner.format(appid=e.name).split("/"))
                if path.is_file():
                    out.append((emulator, int(e.name), path, fmt))
    return out


def _text(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8", "replace").lstrip("﻿")


def _when(value) -> float | None:
    try:
        t = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return t if t > 0 else None


def parse(data: bytes, fmt: str) -> dict[str, float | None]:
    """Steam internal name -> unlock time (Unix seconds, or None when the file
    kept none), for the achievements the file marks as unlocked."""
    text = _text(data)
    unlocked: dict[str, float | None] = {}
    if fmt == "ini":
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(text)
        except configparser.Error as exc:
            raise ValueError(f"not INI-style ({exc.__class__.__name__})") from None
        for name in parser.sections():
            if name.lower() == "steamachievements":
                continue                                  # the emulator's own index of names
            section = parser[name]
            if str(section.get("achieved", "")).strip().lower() in _TRUE:
                unlocked[name] = _when(section.get("unlocktime") or section.get("timestamp")
                                       or section.get("unlock_time"))
        return unlocked
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"not JSON ({exc})") from None
    items = doc.items() if isinstance(doc, dict) else (
        (i.get("name"), i) for i in doc if isinstance(i, dict)) if isinstance(doc, list) else ()
    for name, item in items:
        if not name or not isinstance(item, dict):
            continue
        if any(item.get(k) in (True, 1, "1", "true") for k in ("earned", "achieved", "unlocked")):
            unlocked[str(name)] = _when(item.get("earned_time") or item.get("unlock_time")
                                        or item.get("UnlockTime") or item.get("time"))
    return unlocked


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

    def new_problems(self) -> list[str]:
        fresh = [m for k, m in self.problems.items() if (k, m) not in self._reported]
        self._reported.update(self.problems.items())
        return fresh

    def poll(self) -> list[dict]:
        if not enabled(self.profile):
            return []
        written = []
        for emulator, appid, path, fmt in achievement_files():
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
