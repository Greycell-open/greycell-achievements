"""The local page's save routes: kept copies, putting them back, what changed,
and rules (savekeeper.py, savelearn.py). Every route needs the page token:
save contents are private, and restoring writes into a game's folder."""
from __future__ import annotations

from pathlib import Path

from fastapi import Header, HTTPException
from pydantic import BaseModel


class RestoreBody(BaseModel):
    snapshot: str | None = None


class TransferBody(BaseModel):
    to: str
    snapshot: str | None = None             # a kept copy
    folder: str | None = None               # or what a folder holds now
    dry_run: bool = True
    allow_newer: bool = False


class KeeperBody(BaseModel):
    on: bool | None = None
    folder: str | None = None              # "" goes back to the profile's own folder


class RuleBody(BaseModel):
    achievement: str
    folder: str
    file: str
    field: str
    op: str = "=="
    value: str | int | float | bool
    format: str | None = None


def _running(profile, game_id: str) -> bool:
    from . import watching
    if watching.LIVE.get("at"):
        return game_id in watching.LIVE.get("playing", [])
    from .cli_saves import _running as scan
    return scan(profile, game_id)


def _open_folder(path: Path) -> None:
    """The folder in the file manager: Explorer on Windows (no console
    window), the desktop's opener on Linux."""
    import os
    import sys
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))                     # noqa: S606 (a folder, opened in Explorer)
        return
    import shutil
    from . import linux_desktop
    opener = shutil.which("xdg-open")
    if not opener or not linux_desktop._spawn([opener, str(path)]):
        raise OSError("no file manager to open it with")


def add_routes(app, profile, check) -> None:
    from . import savekeeper, savelearn

    def fail(exc: Exception, code: int = 400):
        raise HTTPException(code, {"code": "bad_request", "message": str(exc)}) from None

    @app.get("/v1/local/saves")
    def saves_overview(x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        return savekeeper.overview(profile)

    @app.post("/v1/local/saves")
    def saves_change(body: KeeperBody, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        if body.on is not None:
            savekeeper.set_enabled(profile, body.on)
        if body.folder is not None:
            try:
                savekeeper.move_to(profile, body.folder)
            except (savekeeper.KeeperError, OSError) as exc:
                fail(exc)
        return savekeeper.overview(profile)

    @app.post("/v1/local/saves/open")
    def saves_open(x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        try:
            _open_folder(savekeeper.keep_dir(profile))
        except OSError as exc:
            fail(exc)
        return {"opened": True}

    # A game's page shows it together with every game linked into it (a DRM-free
    # copy linked into the Steam game): their save folders and kept copies too.
    def members(game_id: str) -> list[str]:
        from .savetransfer import group
        return group(profile, game_id) if game_id in profile.state()["games"] else [game_id]

    @app.get("/v1/local/games/{game_id}/kept")
    def kept(game_id: str, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        keeper, known, ids = savekeeper.Keeper(profile), savekeeper.save_folders(profile), members(game_id)
        snaps = sorted((s for g in ids for s in keeper.snapshots(g)), key=lambda s: s["taken_at"], reverse=True)
        folders = list(dict.fromkeys(str(f) for g in ids for f in known.get(g, [])))
        return {"on": savekeeper.enabled(profile), "folders": folders,
                "running": any(_running(profile, g) for g in ids),
                "snapshots": [{"id": s["id"], "taken_at": s["taken_at"], "reason": s.get("reason"),
                               "folder": s["folder"], "files": len(s["files"]),
                               "size": sum(f["size"] for f in s["files"].values())} for s in snaps]}

    @app.post("/v1/local/games/{game_id}/keep")
    def keep(game_id: str, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        known, keeper = savekeeper.save_folders(profile), savekeeper.Keeper(profile)
        pairs = [(g, f) for g in members(game_id) for f in known.get(g) or []]
        if not pairs:
            fail(ValueError("no save folder is known for this game yet"), 404)
        taken = [s for s in (keeper.take(g, Path(f), reason="asked") for g, f in pairs) if s]
        return {"taken": len(taken)}

    @app.post("/v1/local/games/{game_id}/restore")
    def restore(game_id: str, body: RestoreBody, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        keeper, ids = savekeeper.Keeper(profile), members(game_id)
        owner = next((g for g in ids if body.snapshot and any(s["id"] == body.snapshot for s in keeper.snapshots(g))),
                     game_id)
        try:
            plan = keeper.restore(owner, body.snapshot, running=any(_running(profile, g) for g in ids))
        except (savekeeper.KeeperError, OSError) as exc:
            fail(exc)
        return {"folder": plan["folder"], "written": len(plan["actions"]), "kept_before": plan["replaces_existing"],
                "taken_at": plan["snapshot"]["taken_at"]}

    @app.get("/v1/local/games/{game_id}/installs")
    def game_installs(game_id: str, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        from . import savetransfer
        return {"installs": savetransfer.installs(profile, game_id),
                "sources": savetransfer.sources(profile, game_id, limit=60),
                "moves": savetransfer.history(profile, game_id)[:10]}

    @app.post("/v1/local/games/{game_id}/transfer")
    def game_transfer(game_id: str, body: TransferBody, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        from . import savetransfer
        try:
            if body.dry_run:
                return savetransfer.plan(profile, game_id, body.to, body.snapshot, body.folder)
            running = any(_running(profile, g) for g in savetransfer.group(profile, game_id))
            return savetransfer.transfer(profile, game_id, body.to, body.snapshot, body.folder, running=running,
                                         allow_newer=body.allow_newer)
        except (savekeeper.KeeperError, OSError) as exc:
            fail(exc)

    @app.post("/v1/local/games/{game_id}/transfer/{transfer_id}/undo")
    def game_transfer_undo(game_id: str, transfer_id: str, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        from . import savetransfer
        try:
            running = any(_running(profile, g) for g in savetransfer.group(profile, game_id))
            return savetransfer.undo(profile, game_id, transfer_id, running=running)
        except (savekeeper.KeeperError, OSError) as exc:
            fail(exc)

    @app.get("/v1/local/games/{game_id}/changes")
    def changes(game_id: str, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        try:
            return {"changes": savelearn.changes(profile, game_id, limit=60)}
        except (savekeeper.KeeperError, OSError) as exc:
            fail(exc)

    @app.get("/v1/local/games/{game_id}/suggestions")
    def suggestions(game_id: str, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        try:
            return {"suggestions": savelearn.suggest(profile, game_id)}
        except (savekeeper.KeeperError, OSError) as exc:
            fail(exc)

    @app.post("/v1/local/games/{game_id}/rules")
    def add_rule(game_id: str, body: RuleBody, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        if body.op not in ("==", "!=", ">=", "<=", ">", "<", "contains", "exists"):
            fail(ValueError(f"unknown comparison {body.op}"))
        try:
            path = savelearn.add_rule(profile, game_id, body.achievement, body.folder, body.file, body.field,
                                      body.op, body.value, fmt=body.format)
        except (savekeeper.KeeperError, OSError, ValueError) as exc:
            fail(exc)
        return {"rules": str(path)}
