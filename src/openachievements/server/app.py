"""The Greycell Achievements sync server.

One process, one SQLite file, no other service. It is for players who want
to sync their own computers through a server they run themselves; Greycell
hosts no instance and the app never suggests one (owner decision 2026-10-01). OpenAPI is served at /openapi.json and /docs.

Configuration, all environment variables:

  OA_DATA_DIR             where the database lives (default ./data)
  OA_REGISTRATION         open | closed | first-only (default open)
  OA_MAX_BATCH            events per sync request (default 500)
  OA_MAX_EVENTS           events stored per profile, the hosted quota (default 250000)
  OA_LOGIN_ATTEMPTS       failed logins per username per 15 minutes (default 10)
  OA_BASE_PATH            served under this path behind a proxy that strips it, e.g.
                          /achievements (default: at the root)
  OA_SOURCE_URL           where the app's source and downloads are, shown on the
                          signed-out page (default: none shown)
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import __version__
from .. import events as ev
from .. import reducer
from .store import Conflict, Store, data_dir

PROTOCOL_VERSION = "1.0"
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        self.registration = os.environ.get("OA_REGISTRATION", "open")
        self.max_batch = _int_env("OA_MAX_BATCH", 500)
        self.max_events = _int_env("OA_MAX_EVENTS", 250_000)
        self.login_attempts = _int_env("OA_LOGIN_ATTEMPTS", 10)
        self.base_path = "/" + os.environ.get("OA_BASE_PATH", "").strip("/") if os.environ.get("OA_BASE_PATH", "").strip("/") else ""
        self.source_url = os.environ.get("OA_SOURCE_URL", "").strip()


# ---- request and response models (these are the OpenAPI contract) -----------------

class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=10, max_length=200)


class SessionRequest(Credentials):
    password: str = Field(min_length=1, max_length=200)
    device_id: str | None = Field(default=None, max_length=36)
    device_name: str | None = Field(default=None, max_length=80)


class SyncRequest(BaseModel):
    protocol_version: str
    profile_id: str
    device_id: str
    cursor: str | None = None
    events: list[dict[str, Any]] = []


class Privacy(BaseModel):
    public: bool = False
    show_library: bool = True
    show_recent: bool = True


class DeleteRequest(BaseModel):
    password: str


def encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"seq": seq}).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        seq = json.loads(base64.urlsafe_b64decode(padded))["seq"]
        return seq if isinstance(seq, int) and seq >= 0 else 0
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, {"code": "bad_cursor", "message": "cursor is not one this server issued"})


def create_app(store: Store | None = None, settings: Settings | None = None) -> FastAPI:
    store = store or Store(data_dir() / "openachievements.sqlite")
    settings = settings or Settings()
    failures: dict[str, deque] = defaultdict(deque)

    app = FastAPI(title="Greycell Achievements sync server", version=__version__, root_path=settings.base_path,
                  description="Local-first achievement sync. The client's profile folder is the source of truth; "
                              "this server is a peer that stores and relays events.")

    @app.exception_handler(Conflict)
    async def _conflict(_req: Request, exc: Conflict):
        return JSONResponse(status_code=409, content={"detail": {"code": exc.code, "message": str(exc)}})

    def account(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, {"code": "no_token", "message": "sign in first"})
        row = store.account_for_token(authorization[7:].strip())
        if row is None:
            raise HTTPException(401, {"code": "bad_token", "message": "this device is signed out or revoked"})
        return row

    # ---- health and capabilities -------------------------------------------------

    @app.get("/healthz", response_class=PlainTextResponse, tags=["health"])
    def healthz():
        return "ok"

    @app.get("/readyz", tags=["health"])
    def readyz():
        return {"ready": store.ready()}

    @app.get("/v1/mode", tags=["health"])
    def mode():
        return {"mode": "server", "registration": _registration_open(), "source_url": settings.source_url or None}

    stats_cache: dict = {"at": 0.0, "value": None}

    @app.get("/v1/stats", tags=["library"])
    def stats():
        """What the signed-out page shows: totals, and public profiles by unlocks.
        Counted from stored events, without hand-made unlocks; cached a minute."""
        if stats_cache["value"] is None or time.monotonic() - stats_cache["at"] > 60:
            stats_cache["value"], stats_cache["at"] = store.stats(), time.monotonic()
        return stats_cache["value"]

    @app.get("/v1/capabilities", tags=["sync"])
    def capabilities():
        return {
            "server": "open-achievements", "server_version": __version__,
            "protocol_versions": [PROTOCOL_VERSION], "event_schema_versions": [ev.SCHEMA_VERSION],
            "registration": _registration_open(), "server_time": ev.now(),
            "limits": {"max_batch": settings.max_batch, "max_events": settings.max_events,
                       "max_event_bytes": ev.MAX_EVENT_BYTES},
            "features": ["cursor-sync", "idempotent-upload", "library", "privacy", "export", "account-deletion",
                         "public-profiles"],
        }

    def _registration_open() -> bool:
        if settings.registration == "closed":
            return False
        if settings.registration == "first-only":
            return store.account_count() == 0
        return True

    # ---- accounts and sessions -------------------------------------------------------

    @app.post("/v1/accounts", status_code=201, tags=["accounts"])
    def register(body: Credentials):
        if not _registration_open():
            raise HTTPException(403, {"code": "registration_closed", "message": "this server is not taking new accounts"})
        if not USERNAME_RE.match(body.username):
            raise HTTPException(400, {"code": "bad_username", "message": "3 to 32 letters, digits, _ . or -"})
        account_id = store.create_account(body.username, body.password)
        return {"account_id": account_id, "username": body.username}

    @app.post("/v1/sessions", tags=["accounts"])
    def login(body: SessionRequest):
        key = body.username.lower()
        window = failures[key]
        while window and time.monotonic() - window[0] > 900:
            window.popleft()
        if len(window) >= settings.login_attempts:
            raise HTTPException(429, {"code": "too_many_attempts", "message": "too many failed sign-ins, wait 15 minutes"})
        account_id = store.verify_login(body.username, body.password)
        if account_id is None:
            window.append(time.monotonic())
            raise HTTPException(401, {"code": "bad_credentials", "message": "wrong username or password"})
        window.clear()
        kind = "device" if body.device_id else "web"
        # Browser sessions get their own id too, so each one can be signed out
        # from the Devices list like a computer can.
        token = store.issue_token(account_id, body.device_id or ev.new_id(), body.device_name, kind)
        return {"token": token, "username": body.username, "profile_id": store.profile_for(account_id)}

    @app.delete("/v1/sessions/current", status_code=204, tags=["accounts"])
    def logout(row=Depends(account)):
        store.revoke_token(row["token_hash"])

    @app.get("/v1/me", tags=["accounts"])
    def me(row=Depends(account)):
        profile_id = store.profile_for(row["account_id"])
        return {"username": row["username"], "profile_id": profile_id,
                "privacy": json.loads(row["privacy"]),
                "event_count": store.event_count(profile_id) if profile_id else 0}

    @app.get("/v1/devices", tags=["accounts"])
    def devices(row=Depends(account)):
        return {"devices": store.devices(row["account_id"])}

    @app.delete("/v1/devices/{device_id}", tags=["accounts"])
    def revoke_device(device_id: str, row=Depends(account)):
        return {"revoked_tokens": store.revoke_device(row["account_id"], device_id)}

    @app.put("/v1/privacy", tags=["accounts"])
    def set_privacy(body: Privacy, row=Depends(account)):
        store.set_privacy(row["account_id"], body.model_dump())
        return body.model_dump()

    @app.delete("/v1/accounts/me", status_code=204, tags=["accounts"])
    def delete_account(body: DeleteRequest, row=Depends(account)):
        if store.verify_login(row["username"], body.password) is None:
            raise HTTPException(403, {"code": "bad_credentials", "message": "confirm with your password"})
        store.delete_account(row["account_id"])

    # ---- sync --------------------------------------------------------------------------

    @app.post("/v1/sync", tags=["sync"])
    def sync(body: SyncRequest, row=Depends(account)):
        if body.protocol_version != PROTOCOL_VERSION:
            raise HTTPException(400, {"code": "unsupported_protocol",
                                      "message": f"this server speaks protocol {PROTOCOL_VERSION}"})
        if len(body.events) > settings.max_batch:
            raise HTTPException(413, {"code": "batch_too_large", "message": f"send at most {settings.max_batch} events"})
        store.bind_profile(row["account_id"], body.profile_id)
        since = decode_cursor(body.cursor)

        valid, rejected = [], []
        for raw in body.events:
            try:
                valid.append(ev.validate(raw, profile_id=body.profile_id))
            except ev.EventError as exc:
                rejected.append({"event_id": raw.get("event_id") if isinstance(raw, dict) else None,
                                 "code": exc.code, "message": str(exc)})
        stored, known, over = store.store_events(body.profile_id, valid, settings.max_events)
        for event_id in over:
            rejected.append({"event_id": event_id, "code": "quota_exceeded",
                             "message": "this profile has reached the server's event limit"})
        uploaded = set(stored) | set(known)

        remote, last_seq = [], since
        batch = store.events_after(body.profile_id, since, settings.max_batch + 1)
        more = len(batch) > settings.max_batch
        for seq, event in batch[:settings.max_batch]:
            last_seq = seq
            if event["event_id"] not in uploaded:
                remote.append(event)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "accepted_event_ids": stored + known,
            "rejected_events": rejected,
            "remote_events": remote,
            "next_cursor": encode_cursor(last_seq),
            "more": more,
            "server_time": ev.now(),
            "server_capabilities": {"protocol_versions": [PROTOCOL_VERSION]},
        }

    # ---- reading --------------------------------------------------------------------------

    @app.get("/v1/library", tags=["library"])
    def library(row=Depends(account)):
        profile_id = store.profile_for(row["account_id"])
        events = store.all_events(profile_id) if profile_id else []
        return reducer.library(reducer.reduce(events))

    @app.get("/v1/export", tags=["library"])
    def export(row=Depends(account)):
        profile_id = store.profile_for(row["account_id"])
        lines = [json.dumps(e, sort_keys=True, ensure_ascii=False) for e in (store.all_events(profile_id) if profile_id else [])]
        return PlainTextResponse("\n".join(lines) + ("\n" if lines else ""), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": "attachment; filename=open-achievements-events.jsonl"})

    @app.get("/v1/public/{username}", tags=["library"])
    def public_library(username: str):
        found = store.public_profile(username)
        if found is None:
            raise HTTPException(404, {"code": "not_found", "message": "no public profile by that name"})
        account_id, privacy = found
        profile_id = store.profile_for(account_id)
        view = reducer.library(reducer.reduce(store.all_events(profile_id) if profile_id else []))
        public = {"username": username, "totals": view["totals"]}
        if privacy.get("show_library", True):
            public["games"] = reducer.without_secrets(
                [{k: v for k, v in g.items() if k not in ("installations", "external_ids")} for g in view["games"]])
        return public


    # ---- the web client --------------------------------------------------------------------

    def page() -> HTMLResponse:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__OA_BASE__", settings.base_path + "/"), headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def index():
        return page()

    @app.get("/u/{username}", include_in_schema=False)
    def personal_page(username: str):
        return page()                                  # the client shows /v1/public/<username>

    @app.get("/assets/{name}", include_in_schema=False)
    def asset(name: str):
        target = (WEB_DIR / name).resolve()
        if target.parent != WEB_DIR or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target, headers={"Cache-Control": "no-cache"})

    return app
