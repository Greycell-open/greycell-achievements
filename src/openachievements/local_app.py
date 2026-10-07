"""`openachievements serve`: the library UI over the local profile, offline.

Same web client as the server, in "local" mode: no account, no network, the
profile folder is read and written directly.

It listens on 127.0.0.1 only, and every request that changes anything must
carry a token generated at launch and embedded in the page. Without that, any
website open in the same browser could POST to localhost and change things.
The Host header is checked too, which stops DNS-rebinding tricks.
"""
from __future__ import annotations

import secrets
import threading
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .profile import Profile, ProfileError

WEB_DIR = Path(__file__).resolve().parent / "web"
# Browsers must check for a newer page or script each time (cheap: an unchanged
# file answers 304), or an update to the app stays invisible behind the cache.
NO_CACHE = {"Cache-Control": "no-cache"}

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
# Set by the desktop app where it has no tray (Linux): {"quit": callable, "autostart": module}.
APP_CONTROLS: dict = {}


class StatusBody(BaseModel):
    game_id: str
    status: str


class NotifyBody(BaseModel):
    enabled: bool | None = None
    sound: bool | None = None
    steam: bool | None = None
    unlock_sound: str | None = None
    platinum_sound: str | None = None
    rare_sound: str | None = None
    rare_below: float | None = None


class PreviewBody(BaseModel):
    kind: str
    name: str


class XboxClientBody(BaseModel):
    client_id: str


class RaConnectBody(BaseModel):
    username: str
    api_key: str


class GogConnectBody(BaseModel):
    username: str


class PsnConnectBody(BaseModel):
    npsso: str
    online_id: str | None = None


class AddBody(BaseModel):
    appid: int
    status: str | None = None


class AutostartBody(BaseModel):
    on: bool


class PrivacyBody(BaseModel):
    updates: bool | None = None
    pictures: bool | None = None
    rarity: bool | None = None
    stats: bool | None = None


def create_local_app(profile: Profile, token: str | None = None, catalog_dir: Path | None = None,
                     steam_signin=None) -> FastAPI:
    from .adapters import steam_account
    from .catalog import steam as cat
    signin = steam_signin or steam_account.SteamSignIn()
    from .adapters import xbox as xbox_adapter
    xbox_signin = xbox_adapter.SignIn()
    token = token or secrets.token_urlsafe(24)
    catalog_dir = Path(catalog_dir) if catalog_dir else cat.default_dir()
    cached = {"index": None, "size": -1}

    def catalogue() -> "cat.CatalogIndex":
        # The crawl keeps adding games; reopen the (small) index when the data grows.
        packs = catalog_dir / cat.PACKS_FILE
        size = packs.stat().st_size if packs.exists() else 0
        if cached["index"] is None or size != cached["size"]:
            cached["index"], cached["size"] = cat.CatalogIndex(catalog_dir), size
        return cached["index"]
    app = FastAPI(title="Greycell Achievements (local)", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def only_this_machine(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ALLOWED_HOSTS and host != "testserver":
            return HTMLResponse("This server only answers on 127.0.0.1.", status_code=403)
        return await call_next(request)

    def check(x_oa_token: str | None):
        if not x_oa_token or not secrets.compare_digest(x_oa_token, token):
            raise HTTPException(403, {"code": "bad_token", "message": "reload the page"})

    @app.get("/", include_in_schema=False)
    def index():
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__OA_LOCAL_TOKEN__", token).replace("__OA_BASE__", "/"), headers=NO_CACHE)

    @app.get("/assets/{name}", include_in_schema=False)
    def asset(name: str):
        target = (WEB_DIR / name).resolve()
        if target.parent != WEB_DIR or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target, headers=NO_CACHE)

    @app.get("/v1/mode")
    def mode():
        controls = APP_CONTROLS.get("autostart")
        try:
            autostart = controls.autostart_enabled() if controls else None
        except OSError:
            autostart = None
        return {"mode": "local", "profile_id": profile.profile_id,
                "sync": bool(profile.config.sync_settings(profile.profile_id).get("server")),
                "app": {"quit": bool(APP_CONTROLS.get("quit")), "autostart": autostart}}

    @app.get("/v1/local/privacy")
    def privacy_state():
        from . import privacy
        config = profile.config.load()
        return {**privacy.settings(config), "notice": privacy.notice_due(config)}

    @app.post("/v1/local/privacy/notice")
    def privacy_notice(x_oa_token: str | None = Header(default=None)):
        """The first-run notice about anonymous stats was shown: not again."""
        check(x_oa_token)
        from . import privacy
        privacy.notice_seen(profile.config)
        return {"notice": False}

    @app.post("/v1/local/privacy")
    def privacy_change(body: PrivacyBody, x_oa_token: str | None = Header(default=None)):
        """The player's switches for what goes online without being asked."""
        check(x_oa_token)
        from . import privacy
        return privacy.change(profile.config, **body.model_dump(exclude_none=True))

    @app.post("/v1/local/app/autostart")
    def app_autostart(body: AutostartBody, x_oa_token: str | None = Header(default=None)):
        """Start at login, where the app has no tray to switch it in (Linux)."""
        check(x_oa_token)
        controls = APP_CONTROLS.get("autostart")
        if not controls:
            raise HTTPException(400, {"code": "bad_request", "message": "Start at login is set in the tray here."})
        try:
            controls.set_autostart(body.on)
            if not body.on:                  # turned off by the player: not switched on again
                profile.config.edit(lambda c: c.setdefault("autostart", {}).__setitem__("offered", True))
        except OSError as exc:
            raise HTTPException(400, {"code": "bad_request", "message": str(exc)})
        return {"autostart": controls.autostart_enabled()}

    @app.post("/v1/local/app/quit")
    def app_quit(x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        if not APP_CONTROLS.get("quit"):
            raise HTTPException(400, {"code": "bad_request", "message": "Quit from the tray here."})
        threading.Timer(0.3, APP_CONTROLS["quit"]).start()     # after this answer has gone out
        return {"quitting": True}

    @app.get("/v1/library")
    def library():
        return profile.library()

    @app.get("/v1/local/xbox")
    def xbox_status():
        return xbox_adapter.status(profile)

    @app.post("/v1/local/xbox/client")
    def xbox_client(body: XboxClientBody, x_oa_token: str | None = Header(default=None)):
        """The player's own Azure app registration (a public id, not a secret)."""
        check(x_oa_token)
        try:
            xbox_adapter.set_client(profile, body.client_id)
        except ProfileError as exc:
            raise HTTPException(400, {"code": "bad_request", "message": str(exc)}) from None
        return xbox_adapter.status(profile)

    @app.get("/v1/local/xbox/signin", include_in_schema=False)
    def xbox_signin_start(request: Request):
        port = request.url.port or 8788
        try:
            return RedirectResponse(xbox_signin.start(profile, f"http://localhost:{port}/v1/local/xbox/return"),
                                    status_code=302)
        except ProfileError as exc:
            return RedirectResponse("/?xbox_error=" + urllib.parse.quote(str(exc)), status_code=302)

    @app.get("/v1/local/xbox/return", include_in_schema=False)
    def xbox_signin_return(request: Request):
        try:
            xbox_signin.finish(profile, dict(request.query_params))
        except ProfileError as exc:
            return RedirectResponse("/?xbox_error=" + urllib.parse.quote(str(exc)), status_code=302)
        return RedirectResponse("/?xbox=signed-in", status_code=302)

    @app.post("/v1/local/xbox/disconnect")
    def xbox_disconnect(x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        xbox_adapter.disconnect(profile)
        return xbox_adapter.status(profile)

    @app.get("/v1/local/psn")
    def psn_status():
        from .adapters import psn
        return psn.status(profile)

    @app.post("/v1/local/psn/connect")
    def psn_connect(body: PsnConnectBody, x_oa_token: str | None = Header(default=None)):
        """Exchange a pasted NPSSO code for a session (kept in the OS credential
        store) and remember whose trophies to read. The code itself is not kept."""
        check(x_oa_token)
        from .adapters import psn
        try:
            psn.connect(profile, body.npsso, body.online_id)
        except ProfileError as exc:
            raise HTTPException(400, {"code": "bad_request", "message": str(exc)}) from None
        return psn.status(profile)

    @app.post("/v1/local/psn/disconnect")
    def psn_disconnect(x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        from .adapters import psn
        psn.disconnect(profile)
        return psn.status(profile)

    @app.get("/v1/local/retroachievements")
    def ra_status():
        from .adapters import retroachievements as ra
        return ra.status(profile)

    @app.post("/v1/local/retroachievements/connect")
    def ra_connect(body: RaConnectBody, x_oa_token: str | None = Header(default=None)):
        """Check a RetroAchievements username and Web API key; the key goes only
        to the OS credential store."""
        check(x_oa_token)
        from .adapters import retroachievements as ra
        try:
            ra.connect(profile, body.username, body.api_key)
        except ProfileError as exc:
            raise HTTPException(400, {"code": "bad_request", "message": str(exc)}) from None
        return ra.status(profile)

    @app.post("/v1/local/retroachievements/disconnect")
    def ra_disconnect(x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        from .adapters import retroachievements as ra
        ra.disconnect(profile)
        return ra.status(profile)

    @app.get("/v1/local/gog")
    def gog_status():
        from .adapters import gog
        return gog.status(profile)

    @app.post("/v1/local/gog/connect")
    def gog_connect(body: GogConnectBody, x_oa_token: str | None = Header(default=None)):
        """Remember a public GOG profile to read. Nothing secret is involved."""
        check(x_oa_token)
        from .adapters import gog
        try:
            gog.connect(profile, body.username)
        except ProfileError as exc:
            raise HTTPException(400, {"code": "bad_request", "message": str(exc)}) from None
        return gog.status(profile)

    @app.post("/v1/local/gog/disconnect")
    def gog_disconnect(x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        from .adapters import gog
        gog.disconnect(profile)
        return gog.status(profile)

    @app.get("/v1/local/notify")
    def notify_settings():
        from . import notify, sounds
        return {**notify.settings(profile.config.load()), "choices": sounds.choices(),
                "custom": {k: notify.custom_file(profile, k).exists() for k in ("unlock", "platinum", "rare")},
                "rare_choices": list(notify.RARE_CHOICES)}

    @app.post("/v1/local/notify")
    def notify_change(body: NotifyBody, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        from . import notify, sounds
        try:
            current = notify.change(profile, **{k: v for k, v in body.model_dump().items() if v is not None})
        except ValueError as exc:
            raise HTTPException(400, {"code": "bad_request", "message": str(exc)}) from None
        return {**current, "choices": sounds.choices()}

    @app.post("/v1/local/notify/preview")
    def notify_preview(body: PreviewBody, x_oa_token: str | None = Header(default=None)):
        """Play a sound on this computer, so the choice can be heard first."""
        check(x_oa_token)
        from . import notify, sounds, toast
        kind = body.kind if body.kind in ("platinum", "rare") else "unlock"
        table = sounds.TABLES[kind][0]()
        extra = {"kind": "rare"} if kind == "rare" else {}
        if body.name == sounds.CUSTOM:
            path = notify.custom_file(profile, kind)
            if not path.exists():
                raise HTTPException(400, {"code": "bad_request", "message": "choose a sound file first"})
            toast._play_chime(kind == "platinum", "custom", str(path), **extra)
            return {"ok": True}
        if body.name not in table:
            raise HTTPException(400, {"code": "bad_request", "message": "no such sound"})
        toast._play_chime(kind == "platinum", body.name, **extra)
        return {"ok": True}

    @app.post("/v1/local/notify/custom/{kind}")
    async def notify_custom(kind: str, request: Request, x_oa_token: str | None = Header(default=None)):
        """The player's own WAV for unlock or Platinum: checked, then kept in
        this machine's settings folder. Never synced, never uploaded."""
        check(x_oa_token)
        from . import notify, sounds
        if kind not in ("unlock", "platinum", "rare"):
            raise HTTPException(404, {"code": "not_found", "message": "unlock, rare or platinum"})
        data = await request.body()
        try:
            sounds.check_custom_wav(data)
        except ValueError as exc:
            raise HTTPException(400, {"code": "bad_request", "message": str(exc)}) from None
        from .fsutil import write_bytes_atomic
        path = notify.custom_file(profile, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(path, data)
        current = notify.change(profile, **{f"{kind}_sound": sounds.CUSTOM})
        return {**current, "choices": sounds.choices(), "custom": {kind: True}}

    @app.get("/v1/local/pulse")
    def pulse():
        """Cheap enough to ask every few seconds: a stamp that changes whenever
        anything is recorded (the event files' sizes), and the games the
        watcher sees running. The page reloads the library only on a new stamp."""
        import hashlib
        from . import watching
        stamp = hashlib.sha1(repr(profile.log.signature()).encode()).hexdigest()[:16]
        games = profile.state()["games"]
        live = watching.LIVE
        from . import __version__, update
        try:                                     # what the last check found; never asks the network here
            found = update.check(profile.config, network=False)
        except Exception:  # noqa: BLE001 - the pulse never fails over updates
            found = None
        return {"stamp": stamp, "watching": live["at"] is not None, "version": __version__,
                "update": found if found and found.get("available") else None,
                "playing": [{"game_id": g, "title": (games.get(g) or {}).get("title") or g}
                            for g in live["playing"]],
                "syncs": syncs()}

    def syncs() -> list[dict]:
        """Account syncs in progress or stuck, for the page's status bar. They run
        in the watcher; the page only shows them."""
        from .adapters import gog, psn, retroachievements, xbox
        out = []
        for key, name, module in (("psn", "PlayStation", psn), ("xbox", "Xbox", xbox), ("gog", "GOG", gog),
                                  ("ra", "RetroAchievements", retroachievements)):
            s = module.STATUS.get(profile.profile_id) or {}
            if module.linked(profile) and (s.get("running") or s.get("error")):
                out.append({"key": key, "name": name, "running": bool(s.get("running")), "done": s.get("done", 0),
                            "total": s.get("total", 0), "current": s.get("current"), "error": s.get("error")})
        return out

    @app.get("/v1/local/catalog")
    def catalog_search(q: str = "", limit: int = 30):
        index = catalogue()
        owned = profile.state()["games"]
        rows = index.search(q, limit=max(1, min(limit, 100))) if q.strip() else []
        for r in rows:
            r["in_library"] = f"steam-{r['appid']}" in owned
        return {"results": rows, "catalogued": len(index)}

    @app.post("/v1/local/catalog/add")
    def catalog_add(body: AddBody, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        try:
            added = cat.add_to_profile(profile, catalogue(), body.appid, status=body.status)
        except (cat.CatalogError, ProfileError) as exc:
            raise HTTPException(400, {"code": "bad_request", "message": str(exc)}) from None
        from . import community                              # the player added it: an anonymous count
        community.note(profile.config, "added", "steam", appid=body.appid)
        if body.status in ("backlog", "completed"):
            community.note(profile.config, body.status, "steam", appid=body.appid)
        return added

    @app.post("/v1/local/status")
    def status(body: StatusBody, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        try:
            event = profile.set_status(body.game_id, body.status)
        except ProfileError as exc:
            raise HTTPException(400, {"code": "bad_request", "message": str(exc)}) from None
        if body.status in ("backlog", "completed"):          # the player chose the shelf: an anonymous count
            from . import community
            community.note(profile.config, body.status, community.platform_of(body.game_id),
                           appid=community.steam_ref(body.game_id))
        return {"event": event}

    @app.post("/v1/local/games/{game_id}/fetch-achievements")
    def fetch_achievements(game_id: str, x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        if not game_id.startswith("steam-") or not game_id[6:].isdigit():
            raise HTTPException(400, {"code": "bad_request", "message": "only Steam games have a public list"})
        if cat.PackFetcher(profile).fetch_one(int(game_id[6:])):
            return {"ok": True}
        raise HTTPException(404, {"code": "no_list", "message": "Steam has no public achievement list for this game"})

    @app.get("/v1/local/games/{game_id}/saves")
    def game_saves(game_id: str):
        from .adapters import autodetect, saverules
        pack = profile.state()["packs"].get(game_id) or {}
        has_rules = bool(pack.get("saves")) or game_id in saverules.bundled()
        return {"found": autodetect.save_candidates(profile, game_id), "rules": has_rules}

    @app.post("/v1/local/update/install")
    def update_install(x_oa_token: str | None = Header(default=None)):
        """The dashboard's Install update: the Windows app downloads the checked
        installer, runs it and closes; the installer starts the new version."""
        check(x_oa_token)
        from . import self_update, update
        status = update.check(profile.config)
        if not status["installable"] or not self_update.QUIT:
            raise HTTPException(400, {"code": "bad_request",
                                      "message": "There is no update this copy can install by itself."})
        started = self_update.install_in_background(status["available"], self_update.QUIT)
        return {"started": started, "progress": dict(self_update.STATE)}

    def _picture(path):
        if path is None:
            raise HTTPException(404)
        head = path.read_bytes()[:12]
        kind = ("image/png" if head.startswith(b"\x89PNG") else "image/gif" if head.startswith(b"GIF8")
                else "image/webp" if head[8:12] == b"WEBP" else "image/jpeg")
        return FileResponse(path, media_type=kind, headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/v1/local/art")
    def art(u: str = ""):
        """An achievement or game picture from a platform's image host, kept on
        this computer after the first time (art.py). Other addresses: 404."""
        from . import art as pictures, privacy
        return _picture(pictures.cached(u, online=privacy.allowed(profile.config, "pictures")))

    @app.get("/v1/local/art/game/{game_id}")
    def game_art(game_id: str):
        from . import art as pictures, steamfiles
        from .adapters import steam_local
        games = profile.state()["games"]
        game = games.get(game_id)
        if not game:
            raise HTTPException(404)
        link = steam_local.linked(profile) or {}
        root = Path(link["root"]) if link.get("root") else steamfiles.steam_dir()
        # The card shows games linked into this one too (the library folds them
        # in): their Steam banner counts, as does a Steam id this game records.
        from .reducer import _link_target
        linked = [gid for gid, g in games.items()                    # whole chains, as the library folds them
                  if gid != game_id and g.get("linked_to") and _link_target(games, gid) == game_id]
        steam = (game.get("external_ids") or {}).get("steam")
        if steam and not game_id.startswith("steam-"):
            linked.append(f"steam-{steam}")
        entry = {"game_id": game_id, "icon_url": game.get("icon_url"),
                 "linked_games": [{"game_id": gid} for gid in linked]}
        from . import privacy
        return _picture(pictures.game_picture(entry, root, online=privacy.allowed(profile.config, "pictures")))

    @app.get("/v1/local/update")
    def update_status(force: bool = False):
        """Is there a newer version? `force` (About, Check for updates) asks now
        instead of using today's answer."""
        from . import update
        try:
            from . import self_update
            return {**update.check(profile.config, force=force), "progress": dict(self_update.STATE)}
        except Exception:  # noqa: BLE001 - an update check never breaks the page
            return {"current": update.__version__, "available": None, "repo": None}

    @app.get("/v1/local/steam")
    def steam_state():
        from .adapters import steam_local
        info = steam_local.linked(profile)
        return {"linked": bool(info), "steam_id": info and info["steam_id"],
                "account": steam_account.account(profile)}

    @app.get("/v1/local/steam/signin", include_in_schema=False)
    def steam_signin(request: Request):
        return RedirectResponse(signin.start(str(request.base_url)), status_code=302)

    @app.get("/v1/local/steam/return", include_in_schema=False)
    def steam_return(request: Request):
        try:
            steam_id = signin.finish(str(request.base_url), dict(request.query_params))
        except ProfileError as exc:
            return RedirectResponse("/?steam_error=" + urllib.parse.quote(str(exc)), status_code=302)
        steam_account.remember(profile, steam_id)
        # Signing in does the rest: follow this PC's Steam for that same account.
        # No key, ever: what comes in is what Steam on this PC knows.
        from .adapters import steam_local
        try:
            current = steam_local.linked(profile)
            if not current or current["steam_id"] != steam_id:
                steam_local.link(profile, steam_id=steam_id)
                steam_local.SteamLocalWatcher(profile, catalog_dir=catalog_dir).poll()
        except ProfileError:
            pass                                   # Steam is not on this PC, or this account never played here
        return RedirectResponse("/?steam=signed-in", status_code=302)

    @app.post("/v1/local/steam/forget")
    def steam_forget(x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        steam_account.forget(profile)
        return {"ok": True}

    @app.post("/v1/local/steam/link")
    def steam_link(x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        from .adapters import steam_local
        try:
            info = steam_local.link(profile, steam_id=steam_account.account(profile))
        except ProfileError as exc:
            raise HTTPException(400, {"code": "bad_request", "message": str(exc)}) from None
        watcher = steam_local.SteamLocalWatcher(profile, catalog_dir=catalog_dir)
        written = watcher.poll()
        return {**info, "unlocks": len(written), "problems": list(watcher.problems.values())[:5]}

    @app.post("/v1/local/sync")
    def sync(x_oa_token: str | None = Header(default=None)):
        check(x_oa_token)
        from .sync import SyncClient, SyncError
        try:
            return SyncClient(profile).run().__dict__
        except SyncError as exc:
            raise HTTPException(502, {"code": exc.code, "message": str(exc)}) from None

    from .local_saves import add_routes
    add_routes(app, profile, check)
    return app
