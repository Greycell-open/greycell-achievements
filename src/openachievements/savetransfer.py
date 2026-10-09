"""Moving saves between installs of the same game.

A game can live in more than one place on one computer: a DRM-free or
repacked copy and the Steam copy, a copy the itch app installed and one from
Steam, an Xbox app copy. Each keeps its saves in its own folder. This carries
a save from one to another, the player's choice every time:

- the installs are the save folders known for the game and for every game
  linked into it (save keeper folders, the folders stores keep, and for Steam
  games Steam Cloud's `userdata/<user>/<appid>/remote` and the Goldberg / GSE
  emulators' `<appid>/remote`, which have the same layout, so a repack's save
  lines up with Steam's file for file);
- the source is a kept copy, or what a folder holds now (kept first);
- a dry run shows every file written, replaced or left alone, and warns when
  the destination is newer, holds differently named files, or is kept in step
  by a store's own cloud (which may put its copy back);
- the move refuses while the game runs, and refuses to replace a newer save
  unless the player says so; what the destination held is kept first; every
  written file is read back and compared;
- undo puts back what was replaced and removes what the move added, but only
  while those files are still what the move wrote: once the game has saved
  over them, undoing would destroy new progress, so it refuses and says which.

Records live beside the kept saves (`<keeper>/<game>/transfers/<id>.json`):
not events, not synced, like the saves themselves. Nothing here makes a store
unlock anything; a store's own achievements still come only from that store.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .fsutil import write_bytes_atomic, write_json_atomic
from .savekeeper import Keeper, KeeperError, _iso, _sha, here, save_folders, scan

EMULATOR_SAVES = (("Goldberg emulator", "Goldberg SteamEmu Saves"), ("GSE emulator", "GSE Saves"))


class TransferError(KeeperError):
    pass


def group(profile, game_id: str) -> list[str]:
    """The game and every game shown with it (linked either way)."""
    from .finder import shown_game
    games = profile.state()["games"]
    shown = shown_game(games, game_id)
    return [shown] + sorted(g for g in games if g != shown and shown_game(games, g) == shown)


def _label(folder: str) -> tuple[str, bool]:
    """A name for where a save folder belongs, and whether a store syncs it."""
    from .adapters import stores
    low = folder.replace("\\", "/").lower()
    if stores.store_synced(folder):
        if "/userdata/" in low:
            return "Steam Cloud", True
        if "ubisoft" in low:
            return "Ubisoft Connect", True
        return "Xbox app", True
    for name, base in EMULATOR_SAVES:
        if f"/{base.lower()}/" in low:
            return name, False
    return "Save folder", False


def _steam_places(appid: int) -> list[str]:
    """Steam Cloud's folder for each Steam account on this PC, and the
    emulators' folders, whether or not the game has saved there yet."""
    from . import steamfiles as sf
    from .adapters import savefile
    out = []
    root = sf.steam_dir()
    try:
        users = [u for u in (root / "userdata").iterdir() if u.is_dir() and u.name.isdigit()] if root else []
    except OSError:
        users = []
    out += [str(u / str(appid) / "remote") for u in users]
    roaming = savefile.root_folder("APPDATA")
    for _name, base in EMULATOR_SAVES:
        folder = roaming / base / str(appid) if roaming is not None else None
        if folder is not None and folder.is_dir():
            out.append(str(folder / "remote"))
    return out


def installs(profile, game_id: str) -> list[dict]:
    """Every place this game's saves can be on this computer, kept or not."""
    members = group(profile, game_id)
    known = save_folders(profile)
    seen, out = set(), []

    def add(member: str, folder: str) -> None:
        key = os.path.normcase(str(folder))
        if key in seen:
            return
        seen.add(key)
        files = scan(Path(folder)) or {}
        label, synced = _label(str(folder))
        out.append({"folder": str(folder), "game_id": member, "label": label, "synced": synced,
                    "exists": Path(folder).is_dir(), "files": len(files),
                    "newest": _iso(max(m for _s, m in files.values())) if files else None})

    for member in members:
        for folder in known.get(member, []):
            add(member, str(folder))
        if member.startswith("steam-") and member[6:].isdigit():
            for folder in _steam_places(int(member[6:])):
                add(member, folder)
    return out


def sources(profile, game_id: str, limit: int = 200) -> list[dict]:
    """Kept copies across the game and its linked games, newest first."""
    keeper = Keeper(profile)
    out = []
    for member in group(profile, game_id):
        for s in keeper.snapshots(member):
            out.append({"game_id": member, "id": s["id"], "taken_at": s["taken_at"], "folder": s["folder"],
                        "reason": s.get("reason"), "files": len(s["files"]),
                        "size": sum(f["size"] for f in s["files"].values())})
    return sorted(out, key=lambda s: s["taken_at"], reverse=True)[:limit]


def _source(profile, game_id: str, snapshot: str | None, folder: str | None, take: bool) -> tuple[str, dict]:
    """(game the copy belongs to, the snapshot) for a kept copy or a folder."""
    keeper = Keeper(profile)
    members = group(profile, game_id)
    if snapshot:
        for member in members:
            found = [s for s in keeper.snapshots(member) if s["id"] == snapshot]
            if found:
                return member, found[0]
        raise TransferError(f"no kept copy {snapshot} for this game")
    if not folder:
        raise TransferError("say which copy to move: a kept copy or a folder")
    place = next((i for i in installs(profile, game_id) if os.path.normcase(i["folder"]) == os.path.normcase(folder)),
                 None)
    member = place["game_id"] if place else members[0]
    files = scan(Path(folder))
    if not files:
        raise TransferError(f"nothing to move: {folder} holds no saves")
    if take:
        keeper.take(member, Path(folder), reason="transfer-source", files=files)
        snap = next(iter(keeper.snapshots(member, folder)), None)
        if snap is None:
            raise TransferError(f"could not keep a copy of {folder}")
        return member, snap
    listed = {rel: {"sha256": None, "size": size, "mtime": mtime} for rel, (size, mtime) in files.items()}
    return member, {"id": None, "folder": folder, "taken_at": _iso(max(m for _s, m in files.values())),
                    "files": listed}


def plan(profile, game_id: str, to: str, snapshot: str | None = None, folder: str | None = None,
         take: bool = False) -> dict:
    """What a move would do. Nothing is written (unless `take`, used by
    `transfer`, keeps a copy of a source folder first)."""
    member, snap = _source(profile, game_id, snapshot, folder, take)
    dest = Path(to)
    if os.path.normcase(str(here(snap["folder"]))) == os.path.normcase(str(dest)) and not snapshot:
        raise TransferError("the copy and the destination are the same folder")
    current = scan(dest) or {}
    writes = []
    for rel, f in sorted(snap["files"].items()):
        if ".." in rel.split("/"):
            raise TransferError(f"{rel} would land outside {dest}")
        have = current.get(rel)
        same = have and f["sha256"] and have[0] == f["size"] and _sha_of(dest / rel) == f["sha256"]
        writes.append({"file": rel, "size": f["size"], "action": "same" if same else "replace" if have else "add"})
    source_newest = max((f["mtime"] for f in snap["files"].values()), default=0)
    dest_newest = max((m for _s, m in current.values()), default=0)
    label, synced = _label(str(dest))
    warnings = []
    if current and dest_newest > source_newest:
        warnings.append({"code": "destination_newer", "message":
                         f"The destination was saved more recently ({_iso(dest_newest)}) than this copy "
                         f"({_iso(source_newest)}). Moving would replace newer progress."})
    if current and not set(snap["files"]) & set(current):
        warnings.append({"code": "different_files", "message":
                         "The destination holds differently named files: this install may not read these saves."})
    if synced:
        warnings.append({"code": "store_synced", "message":
                         f"{label} keeps this folder in step with its cloud. Start the game there once after "
                         f"the move so the store uploads it; if the store offers its cloud copy instead, "
                         f"choose the local one."})
    return {"source": {"game_id": member, "snapshot": snap["id"], "folder": snap["folder"],
                       "taken_at": snap["taken_at"]},
            "to": str(dest), "label": label, "writes": writes,
            "left_alone": sorted(set(current) - set(snap["files"])), "warnings": warnings}


def _sha_of(path: Path) -> str | None:
    try:
        return _sha(path.read_bytes())
    except OSError:
        return None


def _records(keeper: Keeper, game_id: str) -> Path:
    return keeper._game_dir(game_id) / "transfers"


def transfer(profile, game_id: str, to: str, snapshot: str | None = None, folder: str | None = None,
             running: bool = False, allow_newer: bool = False) -> dict:
    if running:
        raise TransferError("the game is running: close it first, a game overwrites its saves when it quits")
    keeper = Keeper(profile)
    p = plan(profile, game_id, to, snapshot, folder, take=True)
    if any(w["code"] == "destination_newer" for w in p["warnings"]) and not allow_newer:
        raise TransferError(p["warnings"][0]["message"] + " Confirm to move anyway.")
    dest = Path(p["to"])
    shown = group(profile, game_id)[0]
    before = None
    if scan(dest):
        keeper.take(shown, dest, reason="before-transfer")
        before = next(iter(keeper.snapshots(shown, str(dest))), None)
    member, snap = p["source"]["game_id"], next(
        s for s in keeper.snapshots(p["source"]["game_id"]) if s["id"] == p["source"]["snapshot"])
    written = {}
    for w in p["writes"]:
        f = snap["files"][w["file"]]
        target = dest.joinpath(*w["file"].split("/"))
        if w["action"] != "same":
            write_bytes_atomic(target, keeper.read(member, f["sha256"]))
            os.utime(target, (f["mtime"], f["mtime"]))
        if _sha_of(target) != f["sha256"]:
            raise TransferError(f"{w['file']} did not read back as written; what was there is kept "
                                f"({before['id'] if before else 'nothing was there'})")
        written[w["file"]] = {"sha256": f["sha256"], "existed": w["action"] != "add"}
    now = time.time()
    record = {"id": f"{int(now * 1000)}", "game_id": shown, "at": _iso(now), "to": str(dest),
              "source": p["source"], "before": before["id"] if before else None, "files": written,
              "undone": None}
    write_json_atomic(_records(keeper, shown) / f"{record['id']}.json", record)
    return {**p, "transfer": record["id"], "kept_before": record["before"]}


def history(profile, game_id: str) -> list[dict]:
    keeper = Keeper(profile)
    out = []
    for path in _records(keeper, group(profile, game_id)[0]).glob("*.json"):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r["id"], reverse=True)


def undo(profile, game_id: str, transfer_id: str, running: bool = False) -> dict:
    """Put the destination back as it was before the move, if the game has
    not saved over the moved files since."""
    if running:
        raise TransferError("the game is running: close it first")
    keeper = Keeper(profile)
    shown = group(profile, game_id)[0]
    path = _records(keeper, shown) / f"{transfer_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise TransferError(f"no move {transfer_id} for this game") from None
    if record.get("undone"):
        raise TransferError(f"move {transfer_id} was already undone")
    dest = Path(record["to"])
    changed = [rel for rel, f in record["files"].items()
               if _sha_of(dest.joinpath(*rel.split("/"))) not in (f["sha256"], None)]
    if changed:
        raise TransferError("the game has saved since the move, so undoing would lose that progress: "
                            + ", ".join(sorted(changed)[:5]) + ". Restore an older kept copy instead.")
    before = next((s for s in keeper.snapshots(shown) if s["id"] == record["before"]), None) \
        if record["before"] else None
    if record["before"] and before is None:
        raise TransferError("the copy kept before the move is missing")
    if scan(dest):
        keeper.take(shown, dest, reason="before-undo")
    for rel, f in record["files"].items():
        target = dest.joinpath(*rel.split("/"))
        old = (before or {}).get("files", {}).get(rel)
        if old:
            write_bytes_atomic(target, keeper.read(shown, old["sha256"]))
            os.utime(target, (old["mtime"], old["mtime"]))
        elif not f["existed"]:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
    record["undone"] = _iso(time.time())
    write_json_atomic(path, record)
    return record
