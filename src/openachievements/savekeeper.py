"""The save keeper: a local cloud for game saves.

Every game whose save folder is known (found by its name, or declared by a
pack the player allowed) has that folder copied into the profile's
`backups/saves/<game>/` whenever it changes, once the game has finished
writing. Files are stored once by content (gzip), so fifty snapshots of a
save that changes a little cost little more than one. The profile folder
belongs to the player and outlives any game: uninstalling a game, or a
repack's uninstaller wiping its folder, loses nothing.

Putting saves back is automatic only when nothing can be lost:
  - a game's program is back (reinstalled) and its save folder is missing or
    empty: the newest snapshot is restored before the game is started;
  - a reinstalled game was started before that and made a fresh save: once
    the game closes, the fresh save is kept as a snapshot too, then the
    previous progress is put back.
Nothing is written while the game runs, and a save deleted on purpose while
the game stayed installed is not brought back. `restore` does the same on
request, always keeping what it replaces first.

Saves never leave this computer: backups are not events, are not synced and
are left out of exports.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .fsutil import write_bytes_atomic, write_json_atomic

KEEP_EVERY = 30.0                      # seconds between looks at the save folders
SETTLE = 10.0                          # a folder written in the last 10 s is still being saved
MAX_FILES = 2000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_FOLDER_BYTES = 1024 * 1024 * 1024
RECENT = 20                            # newest snapshots always kept, then one a day for DAILY days
DAILY = 60
_SKIP_SUFFIX = (".log", ".dmp", ".tmp", ".mdmp", ".etl")
_SKIP_DIRS = ("crashes", "crash", "logs", "shadercache", "shadercaches", "webcache", "cache", "caches", "analytics",
              "unity", "crashdumps")


class KeeperError(ValueError):
    pass


def keep_dir(profile) -> Path:
    return Path(profile.folder) / "backups" / "saves"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def scan(folder: Path) -> dict[str, tuple[int, float]] | None:
    """rel path -> (size, mtime) of the files worth keeping, or None when the
    folder is missing or too large to keep."""
    if not folder.is_dir():
        return None
    found, total, stack = {}, 0, [(folder, "")]
    while stack:
        here, rel = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for e in entries:
            name = e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    if name.lower() not in _SKIP_DIRS and not name.startswith("."):
                        stack.append((Path(e.path), f"{rel}{name}/"))
                    continue
                if not e.is_file(follow_symlinks=False) or name.lower().endswith(_SKIP_SUFFIX):
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_size > MAX_FILE_BYTES:
                continue
            total += st.st_size
            found[rel + name] = (st.st_size, st.st_mtime)
            if len(found) > MAX_FILES or total > MAX_FOLDER_BYTES:
                return None
    return found


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(folder: str) -> str:
    return hashlib.sha256(os.path.normcase(str(folder)).encode()).hexdigest()[:12]


class Keeper:
    """One profile's kept saves."""

    def __init__(self, profile, root: Path | None = None):
        self.profile = profile
        self.root = Path(root) if root else keep_dir(profile)

    # ---- storage ----

    def _game_dir(self, game_id: str) -> Path:
        safe = "".join(c for c in game_id if c.isalnum() or c in "-_.")
        return self.root / safe

    def _blob(self, game_id: str, sha: str) -> Path:
        return self._game_dir(game_id) / "blobs" / sha[:2] / f"{sha}.gz"

    def snapshots(self, game_id: str, folder: str | None = None) -> list[dict]:
        """Newest first."""
        out = []
        for path in (self._game_dir(game_id) / "snapshots").glob("*.json"):
            try:
                snap = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if folder is None or os.path.normcase(snap.get("folder", "")) == os.path.normcase(str(folder)):
                out.append(snap)
        return sorted(out, key=lambda s: (s.get("taken_at", ""), s.get("id", "")), reverse=True)

    def games(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "snapshots").is_dir())

    def take(self, game_id: str, folder: Path, reason: str = "changed", files: dict | None = None) -> dict | None:
        """Copy the folder's save files into the keeper. None when there is
        nothing to keep or it is the same as the newest snapshot."""
        files = scan(folder) if files is None else files
        if not files:
            return None
        listed = {}
        for rel, (size, mtime) in sorted(files.items()):
            try:
                data = (folder / rel).read_bytes()
            except OSError:
                continue                            # vanished while saving: the next look takes it
            sha = _sha(data)
            blob = self._blob(game_id, sha)
            if not blob.exists():
                write_bytes_atomic(blob, gzip.compress(data, compresslevel=6))
            listed[rel] = {"sha256": sha, "size": len(data), "mtime": mtime}
        if not listed:
            return None
        newest = next(iter(self.snapshots(game_id, str(folder))), None)
        if newest and {k: v["sha256"] for k, v in newest["files"].items()} == {k: v["sha256"] for k, v in listed.items()}:
            return None
        now = time.time()
        snap = {"id": f"{int(now * 1000)}-{_slug(str(folder))}", "game_id": game_id, "taken_at": _iso(now),
                "folder": str(folder), "reason": reason, "files": listed}
        write_json_atomic(self._game_dir(game_id) / "snapshots" / f"{snap['id']}.json", snap)
        self.prune(game_id)
        return snap

    def prune(self, game_id: str) -> None:
        """Keep the newest RECENT snapshots of each folder and one a day for
        DAILY days; drop the rest and any file no snapshot needs."""
        snaps = self.snapshots(game_id)
        keep, days, per_folder = [], set(), {}
        for s in snaps:
            n = per_folder[s["folder"]] = per_folder.get(s["folder"], 0) + 1
            day = (s["folder"], s["taken_at"][:10])
            if n <= RECENT or (day not in days and len(days) < DAILY):
                keep.append(s)
                days.add(day)
        dropped = [s for s in snaps if s not in keep]
        for s in dropped:
            try:
                (self._game_dir(game_id) / "snapshots" / f"{s['id']}.json").unlink()
            except OSError:
                pass
        if dropped:
            needed = {f["sha256"] for s in keep for f in s["files"].values()}
            for blob in (self._game_dir(game_id) / "blobs").glob("*/*.gz"):
                if blob.name[:-3] not in needed:
                    try:
                        blob.unlink()
                    except OSError:
                        pass

    def read(self, game_id: str, sha: str) -> bytes:
        data = gzip.decompress(self._blob(game_id, sha).read_bytes())
        if _sha(data) != sha:
            raise KeeperError(f"kept file {sha[:12]} is damaged")
        return data

    # ---- putting saves back ----

    def plan(self, game_id: str, snapshot_id: str | None = None, folder: Path | None = None) -> dict:
        snaps = self.snapshots(game_id)
        if snapshot_id:
            snaps = [s for s in snaps if s["id"] == snapshot_id or s["id"].startswith(snapshot_id)]
        if not snaps:
            raise KeeperError(f"no kept saves for {game_id}" + (f" matching {snapshot_id}" if snapshot_id else ""))
        snap = snaps[0]
        target = Path(folder or snap["folder"])
        current = scan(target) or {}
        actions = []
        for rel, f in sorted(snap["files"].items()):
            have = current.get(rel)
            if have and have[0] == f["size"] and _same(target / rel, f["sha256"]):
                continue
            actions.append({"file": rel, "action": "replace" if have else "add", "size": f["size"]})
        return {"snapshot": snap, "folder": str(target), "actions": actions,
                "replaces_existing": any(a["action"] == "replace" for a in actions) or bool(
                    set(current) - set(snap["files"]))}

    def restore(self, game_id: str, snapshot_id: str | None = None, folder: Path | None = None,
                running: bool = False) -> dict:
        """Put a snapshot back. Whatever the folder holds is kept first."""
        if running:
            raise KeeperError(f"{game_id} is running: close it first, a game overwrites its saves when it quits")
        plan = self.plan(game_id, snapshot_id, folder)
        target = Path(plan["folder"])
        if scan(target):
            self.take(game_id, target, reason="before-restore")
        for a in plan["actions"]:
            f = plan["snapshot"]["files"][a["file"]]
            dest = target.joinpath(*a["file"].split("/"))
            if ".." in a["file"].split("/") or not dest.resolve().is_relative_to(target.resolve()):
                raise KeeperError(f"{a['file']} would land outside {target}")
            write_bytes_atomic(dest, self.read(game_id, f["sha256"]))
            os.utime(dest, (f["mtime"], f["mtime"]))
        return plan


def _same(path: Path, sha: str) -> bool:
    try:
        return _sha(path.read_bytes()) == sha
    except OSError:
        return False


# ---- the watcher's part -------------------------------------------------------------------

def enabled(profile) -> bool:
    return profile.config.load().get("keeper", {}).get(profile.profile_id, True)


def set_enabled(profile, on: bool) -> None:
    with profile.config.editing() as config:
        config.setdefault("keeper", {})[profile.profile_id] = on


def save_folders(profile) -> dict[str, list[Path]]:
    """game id -> the save folders known for it on this computer: found by
    name (autodetect), or a pack's save folder this machine allowed."""
    from .adapters import savefile
    config = profile.config.load()
    out: dict[str, list[Path]] = {}
    prefix = f"{profile.profile_id}:"
    for key, folders in (config.get("save_candidates") or {}).items():
        if key.startswith(prefix):
            out.setdefault(key[len(prefix):], []).extend(Path(f) for f in folders[:1])
    for pack_id, pack in profile.state()["packs"].items():
        if pack.get("removed"):
            continue
        for save in pack.get("saves") or []:
            folder = savefile.granted_folder(profile, pack_id, save, config)
            game_id = (pack.get("game_ids") or [pack_id])[0]
            if folder is not None and folder not in out.get(game_id, []):
                out.setdefault(game_id, []).append(folder)
    # A folder inside another of the same game's (Townfall/Saved/SaveGames in
    # Townfall) is already kept with it: keeping both would copy it twice.
    for game_id, folders in out.items():
        def inside(f: Path, other: Path) -> bool:
            return f != other and os.path.normcase(str(f)).startswith(os.path.normcase(str(other)).rstrip("\\/") + os.sep)
        out[game_id] = [f for f in folders if not any(inside(f, o) for o in folders)]
    return out


@dataclass
class KeeperWatcher:
    """Each look: keep changed save folders, and put saves back where a
    reinstalled game has none. `poll(playing)` takes the games running now."""
    profile: object
    root: Path | None = None
    clock: object = time.time
    _last: float = -1e18
    _stamps: dict = field(default_factory=dict)          # folder -> scan result last kept
    _was_running: set = field(default_factory=set)
    _fresh: dict = field(default_factory=dict)           # game -> start time, when it started with no saves

    def poll(self, playing: set[str], force: bool = False) -> list[str]:
        """Messages for the log about what was kept or restored."""
        if not enabled(self.profile):
            return []
        now = self.clock()
        said = []
        for game_id in playing - self._was_running:          # just started: did it start with no saves?
            if self._empty(game_id) and self._keeper().snapshots(game_id):
                self._fresh[game_id] = now
        stopped = self._was_running - playing
        self._was_running = set(playing)
        if not force and now - self._last < KEEP_EVERY and not stopped:
            return said
        self._last = now
        keeper = self._keeper()
        for game_id, folders in save_folders(self.profile).items():
            if game_id in playing:
                continue                                       # mid-save, maybe: looked at when it stops
            for folder in folders:
                files = scan(folder)
                if not files or self._stamps.get(str(folder)) == files:
                    continue
                if now - max(m for _s, m in files.values()) < SETTLE:
                    continue
                if game_id in self._fresh and game_id in stopped:
                    continue                                    # replaced below, after it is kept
                snap = keeper.take(game_id, folder, files=files)
                self._stamps[str(folder)] = files
                if snap:
                    said.append(f"kept {game_id} saves ({len(snap['files'])} files) from {folder}")
        for game_id in list(self._fresh):
            if game_id in stopped:                              # a reinstalled game made a fresh save
                started = self._fresh.pop(game_id)
                said += self._put_back(keeper, game_id, fresh_since=started)
        said += self._reinstalled(keeper, playing)
        return said

    def _keeper(self) -> Keeper:
        return Keeper(self.profile, self.root)

    def _empty(self, game_id: str) -> bool:
        snaps = self._keeper().snapshots(game_id)
        return bool(snaps) and not scan(Path(snaps[0]["folder"]))

    def _put_back(self, keeper: Keeper, game_id: str, fresh_since: float | None = None) -> list[str]:
        """Restore the newest snapshot taken before the fresh start, keeping
        the fresh save as a snapshot first."""
        snaps = [s for s in keeper.snapshots(game_id) if s.get("reason") != "before-restore"]
        if fresh_since is not None:
            cutoff = _iso(fresh_since)
            snaps = [s for s in snaps if s["taken_at"] < cutoff]
            current = scan(Path(snaps[0]["folder"])) if snaps else None
            if current and min(m for _s, m in current.values()) < fresh_since:
                return []                                    # some files predate this run: not a fresh start
        if not snaps:
            return []
        try:
            keeper.restore(game_id, snaps[0]["id"])
        except (KeeperError, OSError) as exc:
            return [f"could not put {game_id} saves back: {exc}"]
        self._stamps.pop(snaps[0]["folder"], None)
        with self.profile.config.editing() as config:
            config.setdefault("keeper_restored", {})[f"{self.profile.profile_id}:{game_id}"] = snaps[0]["id"]
        return [f"put {game_id} saves back from {snaps[0]['taken_at']} into {snaps[0]['folder']}"]

    def _reinstalled(self, keeper: Keeper, playing: set[str]) -> list[str]:
        """A game whose program is on disk again, created after its newest
        snapshot, and whose save folder is gone or empty: restore it."""
        from .adapters import executable
        said = []
        done = self.profile.config.load().get("keeper_restored") or {}
        for inst in executable.local_installations(self.profile):
            game_id = inst["game_id"]
            if game_id in playing:
                continue
            snaps = [s for s in keeper.snapshots(game_id) if s.get("reason") != "before-restore"]
            if not snaps or done.get(f"{self.profile.profile_id}:{game_id}") == snaps[0]["id"]:
                continue
            try:
                created = Path(inst["path"]).stat().st_ctime
            except OSError:
                continue                                       # not installed now
            if _iso(created) <= snaps[0]["taken_at"] or scan(Path(snaps[0]["folder"])):
                continue
            said += self._put_back(keeper, game_id)
        return said
