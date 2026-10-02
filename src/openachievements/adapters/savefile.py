"""Save-file achievements.

For games no store client is watching: the game writes its progress to a save
file, and a pack's rules say which values in that save mean an achievement.
It works the same for every copy of a game, wherever it came from; nothing
here checks ownership, and nothing here bypasses anything.

A pack declares its saves relative to a known folder, never as a free path:

    "saves": [{"id": "main", "root": "LOCALLOW", "path": "Studio/Game",
               "pattern": "slot*.json", "format": "json"}]

and an achievement's rules read them:

    {"signal": "save", "save": "main", "field": "progress.chapter", "op": ">=", "value": 3}
    {"signal": "save", "save": "main", "contains": "BOSS_ALMA_DEFEATED"}

An achievement unlocks when one save file (one slot) satisfies all its rules.
Nothing is read until the person allows it. Packs come from other people and
arrive through sync, so a pack only says where it would like to look; each
location is read on this machine only after `openachievements save allow`
(which shows the exact folder) or `save locate` (which picks it). A grant is
for one resolved folder: if a pack update moves the location, it must be
allowed again. Files that resolve outside the granted folder (links) are
skipped.

Read-only, always: saves are opened for reading, bounded in size, and never
uploaded or copied. The unlock records the file's name and SHA-256 as evidence,
never its contents.
"""
from __future__ import annotations

import configparser
import fnmatch
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..fsutil import safe_child
from ..profile import Profile, ProfileError
from .base import AdapterMetadata

ADAPTER_VERSION = "0.1.0"
MAX_SAVE_BYTES = 64 * 1024 * 1024
MAX_FILES_PER_SAVE = 50

METADATA = AdapterMetadata(
    adapter_id="save-file",
    adapter_version=ADAPTER_VERSION,
    support_level="official",
    authentication_method="none (local)",
    data_access_method="reads the game's own save files, read-only",
    capabilities=("rule-unlocks", "any-installation-source"),
    rate_limits="checks save folders every few seconds while watching; parses only files that changed",
    known_limitations=(
        "needs a pack with save rules for the game; someone writes them once per game",
        "reads JSON, INI-style, plain-text and Unreal Engine (GVAS) saves; other binary formats need their own reader",
        "notices progress when the game saves, not the instant it happens",
    ),
    terms_or_policy_notes="Observation only. Never writes to, uploads, or copies a save.",
)


# ---- where saves live ---------------------------------------------------------------

def root_folder(root: str) -> Path | None:
    home = Path.home()
    if sys.platform == "win32":
        env = os.environ.get
        folders = {
            "LOCALAPPDATA": env("LOCALAPPDATA"), "APPDATA": env("APPDATA"),
            "LOCALLOW": str(home / "AppData" / "LocalLow"), "DOCUMENTS": str(home / "Documents"),
            "SAVED_GAMES": str(home / "Saved Games"),
        }
    elif sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        folders = {"LOCALAPPDATA": str(support), "APPDATA": str(support), "LOCALLOW": str(support),
                   "DOCUMENTS": str(home / "Documents"), "SAVED_GAMES": None}
    else:
        data = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
        config = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
        folders = {"LOCALAPPDATA": data, "APPDATA": config, "LOCALLOW": config,
                   "DOCUMENTS": str(home / "Documents"), "SAVED_GAMES": None}
    value = folders.get(root)
    return Path(value) if value else None


def save_folder(profile: Profile, pack_id: str, save: dict) -> Path | None:
    override = profile.config.load().get("save_locations", {}).get(f"{profile.profile_id}:{pack_id}:{save['id']}")
    if override:
        return Path(override)
    base = root_folder(save["root"])
    if base is None:
        return None
    try:
        return safe_child(base, save["path"])
    except ValueError:
        return None


def _key(profile: Profile, pack_id: str, save_id: str) -> str:
    return f"{profile.profile_id}:{pack_id}:{save_id}"


def locate(profile: Profile, pack_id: str, save_id: str, folder: Path | None) -> None:
    """This machine keeps that save somewhere else (or, with None, back to the
    default). Choosing a folder allows reading it; going back to the default
    withdraws the grant until `allow` is given for the default folder."""
    with profile.config.editing() as config:
        places = config.setdefault("save_locations", {})
        grants = config.setdefault("save_grants", {})
        key = _key(profile, pack_id, save_id)
        if folder is None:
            places.pop(key, None)
            grants.pop(key, None)
        else:
            places[key] = grants[key] = str(Path(folder).resolve())


def allow(profile: Profile, pack_id: str, save_id: str) -> Path:
    """Allow reading this save's folder as the pack declares it, on this
    machine. Returns the exact folder, so the person sees what they allowed."""
    pack = profile.state()["packs"].get(pack_id) or {}
    save = next((s for s in pack.get("saves") or [] if s["id"] == save_id), None)
    if pack.get("removed") or save is None:
        raise ProfileError(f"no installed pack {pack_id!r} with a save {save_id!r}")
    folder = save_folder(profile, pack_id, save)
    if folder is None:
        raise ProfileError(f"{pack_id}/{save_id} has no location on this operating system")
    with profile.config.editing() as config:
        config.setdefault("save_grants", {})[_key(profile, pack_id, save_id)] = str(folder.resolve())
    return folder.resolve()


def revoke(profile: Profile, pack_id: str, save_id: str) -> None:
    with profile.config.editing() as config:
        config.setdefault("save_grants", {}).pop(_key(profile, pack_id, save_id), None)


def granted_folder(profile: Profile, pack_id: str, save: dict, config: dict | None = None) -> Path | None:
    """The folder to read, only if this machine allowed exactly that folder."""
    config = profile.config.load() if config is None else config
    folder = save_folder(profile, pack_id, save)
    grant = config.get("save_grants", {}).get(_key(profile, pack_id, save["id"]))
    if folder is None or grant is None or str(folder.resolve()) != grant:
        return None
    return folder.resolve()


def save_files(folder: Path | None, pattern: str) -> list[Path]:
    """Matching files directly in `folder`, newest first, bounded. A file that
    resolves outside the folder (a link) is skipped; one that vanishes while
    listing (a game saving via rename) is skipped, not an error."""
    if folder is None or not folder.is_dir():
        return []
    folder = folder.resolve()
    found = []
    with os.scandir(folder) as entries:
        for entry in entries:
            try:
                if not (entry.is_file() and fnmatch.fnmatch(entry.name.lower(), pattern.lower())):
                    continue
                if Path(entry.path).resolve().parent != folder:
                    continue
                found.append((entry.stat().st_mtime, Path(entry.path)))
            except OSError:
                continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _mtime, path in found[:MAX_FILES_PER_SAVE]]


# ---- reading and rules ----------------------------------------------------------------

class SaveUnreadable(ValueError):
    pass


def _final_path(fd: int) -> str | None:
    """Where an open Windows file handle really points, links resolved."""
    import ctypes
    import msvcrt
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(fd), buf, len(buf), 0)
    if not n:
        return None
    text = buf.value
    return text[4:] if text.startswith("\\\\?\\") else text


def _read_confined(path: Path, folder: Path | None) -> bytes:
    """Open once, check that what was actually opened is a file directly in
    `folder`, and read through that same handle, so swapping the file for a
    link after it was listed cannot lead outside the allowed folder."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if folder is not None:
            if os.name == "nt":
                real = _final_path(fd)
                if real is None or os.path.normcase(os.path.dirname(real)) != os.path.normcase(str(folder)):
                    raise SaveUnreadable(f"{path.name} leads outside the allowed folder")
            elif os.path.normcase(os.path.realpath(os.path.dirname(path))) != os.path.normcase(str(folder)):
                raise SaveUnreadable(f"{path.name} leads outside the allowed folder")
        if os.fstat(fd).st_size > MAX_SAVE_BYTES:
            raise SaveUnreadable(f"{path.name} is larger than {MAX_SAVE_BYTES // (1024 * 1024)} MB")
        with os.fdopen(os.dup(fd), "rb") as fh:
            return fh.read(MAX_SAVE_BYTES + 1)
    finally:
        os.close(fd)


def read_save(path: Path, fmt: str, folder: Path | None = None) -> tuple[object, str]:
    """(parsed data, raw text). Raw text serves `contains` rules. With `folder`,
    the file must really be inside it when opened (links are not followed out)."""
    try:
        data = _read_confined(Path(path), Path(folder).resolve() if folder else None)
    except OSError as exc:
        raise SaveUnreadable(f"{Path(path).name} cannot be opened ({exc.strerror or exc})") from None
    raw = data.decode("utf-8", "replace").lstrip("﻿")
    if fmt == "json":
        try:
            return json.loads(raw), raw
        except ValueError as exc:
            raise SaveUnreadable(f"{path.name} is not JSON ({exc})") from None
    if fmt == "gvas":
        from ..gvas import GvasError, parse
        try:
            return parse(data), raw
        except GvasError as exc:
            raise SaveUnreadable(f"{path.name} is not a readable Unreal save ({exc})") from None
    if fmt == "ini":
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        try:
            parser.read_string(raw if raw.lstrip().startswith("[") else "[root]\n" + raw)
        except configparser.Error as exc:
            raise SaveUnreadable(f"{path.name} is not INI-style ({exc.__class__.__name__})") from None
        data = {name: dict(parser[name]) for name in parser.sections()}
        data.update(data.pop("root", {}))
        return data, raw
    return None, raw


_MISSING = object()


def lookup(data: object, dotted: str) -> object:
    """`a.b.2.c`: keys into objects, whole numbers into lists. A key may itself
    contain dots (Unreal saves have `NC.Character.HeadBob`): the longest key
    that matches wins."""
    parts = dotted.split(".")
    node, i = data, 0
    while i < len(parts):
        if isinstance(node, dict):
            for j in range(len(parts), i, -1):
                key = ".".join(parts[i:j])
                if key in node:
                    node, i = node[key], j
                    break
            else:
                return _MISSING
        elif isinstance(node, list) and parts[i].isdigit() and int(parts[i]) < len(node):
            node, i = node[int(parts[i])], i + 1
        else:
            return _MISSING
    return node


def _as_number(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def rule_holds(rule: dict, data: object, raw: str) -> bool:
    if "contains" in rule and "field" not in rule:
        return rule["contains"] in raw
    found = lookup(data, rule["field"]) if data is not None else _MISSING
    op = rule["op"]
    if op == "exists":
        return found is not _MISSING and found is not None
    if found is _MISSING:
        return False
    want = rule["value"]
    if op == "contains":
        return (str(want) in found) if isinstance(found, (str, list)) else False
    left, right = _as_number(found), _as_number(want)
    if left is None or right is None:
        left, right = str(found).lower(), str(want).lower()
        if op not in ("==", "!="):
            return False
    return {"==": left == right, "!=": left != right, ">=": left >= right, "<=": left <= right,
            ">": left > right, "<": left < right}[op]


def _written_at(mtime: float) -> str:
    from datetime import datetime, timezone
    from .. import events as ev
    when = datetime.fromtimestamp(mtime, timezone.utc)
    if when > datetime.now(timezone.utc):
        return ev.now()                     # a clock ahead of ours: never date an unlock in the future
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---- evaluation ---------------------------------------------------------------------------

def _save_packs(state: dict):
    for pack_id, pack in state["packs"].items():
        if not pack.get("removed") and pack.get("saves"):
            yield pack_id, pack


def check(profile: Profile, pack_id: str | None = None) -> list[dict]:
    """What the watcher would see, for people writing rules: each save's folder,
    its files, and which achievements each file satisfies."""
    report = []
    for pid, pack in _save_packs(profile.state()):
        if pack_id and pid != pack_id:
            continue
        for save in pack["saves"]:
            folder = save_folder(profile, pid, save)
            granted = granted_folder(profile, pid, save) is not None
            files = []
            if not granted:
                report.append({"pack_id": pid, "save": save["id"], "folder": str(folder) if folder else None,
                               "allowed": False, "exists": None, "files": []})
                continue
            try:
                listed = save_files(folder.resolve(), save["pattern"])
            except OSError as exc:
                listed = []
                files.append({"file": "(folder)", "error": f"cannot list: {exc.strerror or exc}"})
            for path in listed:
                try:
                    data, raw = read_save(path, save["format"], folder)
                except (OSError, SaveUnreadable) as exc:
                    files.append({"file": path.name, "error": str(exc)})
                    continue
                satisfied = [f"{pid}:{aid}" for aid, a in pack["achievements"].items()
                             if _satisfied(a, save["id"], data, raw)]
                files.append({"file": path.name, "satisfies": satisfied})
            report.append({"pack_id": pid, "save": save["id"], "folder": str(folder) if folder else None,
                           "allowed": True, "exists": bool(folder and folder.is_dir()), "files": files})
    return report


def _satisfied(definition: dict, save_id: str, data, raw) -> bool:
    rules = definition.get("rules") or []
    return bool(rules) and all(r.get("signal") == "save" and r.get("save") == save_id and rule_holds(r, data, raw)
                               for r in rules)


MAX_BYTES_PER_POLL = 256 * 1024 * 1024
MAX_PROBLEMS = 200


@dataclass
class SaveWatcher:
    """Re-reads a save only when its size or modified time changes, and unlocks
    whatever it now satisfies. `poll()` is safe to call as often as you like.

    Everything is keyed by (pack, save, file): two packs reading one save file
    both evaluate it. A file that cannot be read is remembered with its stamp,
    so it is not re-read until it changes, and reported once. One pack's
    unreadable folder never stops the others."""
    profile: Profile
    seen: dict = field(default_factory=dict)       # (pack_id, save_id, path, checksum) -> (size, mtime_ns)
    problems: dict = field(default_factory=dict)   # same key -> message
    _reported: set = field(default_factory=set)

    def _problem(self, key, message: str) -> None:
        if key not in self.problems and len(self.problems) >= MAX_PROBLEMS:
            return
        self.problems[key] = message

    def new_problems(self) -> list[str]:
        """Problems not shown before, each once."""
        fresh = [m for k, m in self.problems.items() if (k, m) not in self._reported]
        self._reported.update((k, m) for k, m in self.problems.items())
        return fresh

    def poll(self) -> list[dict]:
        from ..reducer import is_unlocked
        state = self.profile.state()
        config = self.profile.config.load()
        written, budget = [], MAX_BYTES_PER_POLL
        for pack_id, pack in _save_packs(state):
            for save in pack["saves"]:
                folder = granted_folder(self.profile, pack_id, save, config)
                if folder is None:
                    continue                      # not allowed on this machine: never read
                try:
                    paths = save_files(folder, save["pattern"])
                except OSError as exc:
                    self._problem((pack_id, save["id"], "(folder)"), f"{pack_id}/{save['id']}: cannot list "
                                  f"{folder}: {exc.strerror or exc}")
                    continue
                for path in paths:
                    # The pack's checksum is part of the key: new rules re-read an unchanged save.
                    key = (pack_id, save["id"], str(path), pack.get("checksum"))
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    stamp = (st.st_size, st.st_mtime_ns)
                    if self.seen.get(key) == stamp:
                        continue
                    if st.st_size > budget:
                        continue                  # over this poll's budget: read next time
                    self.seen[key] = stamp
                    try:
                        data, raw = read_save(path, save["format"], folder)
                    except (OSError, SaveUnreadable) as exc:
                        self._problem(key, f"{pack_id}/{save['id']}/{path.name}: {exc}")
                        continue
                    budget -= st.st_size
                    self.problems.pop(key, None)
                    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
                    for ach_id, definition in pack["achievements"].items():
                        ach_key = f"{pack_id}:{ach_id}"
                        if is_unlocked(state, ach_key):
                            continue
                        try:
                            ok = _satisfied(definition, save["id"], data, raw)
                        except (KeyError, TypeError, ValueError):
                            continue        # a malformed rule from elsewhere skips itself, never the watcher
                        if ok:
                            written.append(self.profile.record("achievement.unlocked", {
                                "provenance": "save-derived",
                                "evidence": f"{save['id']}/{path.name} sha256:{digest[:16]}"},
                                achievement_id=ach_key, game_id=(pack.get("game_ids") or [None])[0],
                                # When the game wrote it, not when we read it: an old save
                                # read for the first time is history, and never pops up.
                                occurred_at=_written_at(st.st_mtime),
                                adapter="save-file", adapter_version=ADAPTER_VERSION,
                                external_event_id=f"{self.profile.profile_id}:{ach_key}"))
                            state = self.profile.state()
        return written
