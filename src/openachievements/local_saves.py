"""The local page's save routes: kept copies, putting them back, what changed,
and rules (savekeeper.py, savelearn.py). Every route needs the page token:
save contents are private, and restoring writes into a game's folder."""
from __future__ import annotations

from pathlib import Path

from fastapi import Header, HTTPException
from pydantic import BaseModel


class RestoreBody(BaseModel):
    snapshot: str | None = None


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


def add_routes(app, profile, check) -> None:
    from . import savekeeper, savelearn

    def fail(exc: Exception, code: int = 400):
        raise HTTPException(code, {"code": "bad_request", "message": str(exc)}) from None

    @app.get("/v1/local/games/{game_id}/kept")
    def kept(game_id: str, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        snaps = savekeeper.Keeper(profile).snapshots(game_id)
        folders = [str(f) for f in savekeeper.save_folders(profile).get(game_id, [])]
        return {"on": savekeeper.enabled(profile), "folders": folders, "running": _running(profile, game_id),
                "snapshots": [{"id": s["id"], "taken_at": s["taken_at"], "reason": s.get("reason"),
                               "folder": s["folder"], "files": len(s["files"]),
                               "size": sum(f["size"] for f in s["files"].values())} for s in snaps]}

    @app.post("/v1/local/games/{game_id}/keep")
    def keep(game_id: str, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        folders = savekeeper.save_folders(profile).get(game_id) or []
        if not folders:
            fail(ValueError("no save folder is known for this game yet"), 404)
        keeper = savekeeper.Keeper(profile)
        taken = [s for s in (keeper.take(game_id, Path(f), reason="asked") for f in folders) if s]
        return {"taken": len(taken)}

    @app.post("/v1/local/games/{game_id}/restore")
    def restore(game_id: str, body: RestoreBody, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        try:
            plan = savekeeper.Keeper(profile).restore(game_id, body.snapshot, running=_running(profile, game_id))
        except (savekeeper.KeeperError, OSError) as exc:
            fail(exc)
        return {"folder": plan["folder"], "written": len(plan["actions"]), "kept_before": plan["replaces_existing"],
                "taken_at": plan["snapshot"]["taken_at"]}

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
