"""Client side of sync.

Standard library only, so the core installs anywhere. The loop:

1. every local event the server has not acknowledged is pending;
2. send up to `max_batch` of them with the last cursor;
3. append whatever the server sends back to the local log, like any event;
4. store the new cursor and the acknowledged ids;
5. repeat until nothing is pending and the server says there is no more.

What this machine has sent and where it got to lives in the machine config,
not the profile folder. Losing it costs a re-upload, never data: the server
treats a repeated event as already accepted.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import __version__
from .fsutil import read_json, write_json_atomic
from .profile import Profile

PROTOCOL_VERSION = "1.0"


class SyncError(RuntimeError):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


def check_url(url: str) -> str:
    """HTTPS for anything remote. Plain HTTP is allowed only to this machine,
    for self-hosting and testing."""
    parsed = urlparse(url.rstrip("/"))
    if parsed.scheme == "https" and parsed.netloc:
        return url.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return url.rstrip("/")
    raise SyncError("insecure_url", "remote servers must use https:// (http is allowed only for localhost)")


class Transport:
    """HTTP with retries, exponential backoff and jitter. Swappable in tests."""

    def __init__(self, base_url: str, token: str | None = None, retries: int = 4):
        self.base_url = check_url(base_url)
        self.token = token
        self.retries = retries

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "User-Agent": f"open-achievements/{__version__}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = _detail(exc)
                if exc.code in (429, 502, 503, 504) and attempt < self.retries:
                    time.sleep(min(30, 2 ** attempt) + random.random())
                    continue
                raise SyncError(detail.get("code", "http_error"), detail.get("message", str(exc)), exc.code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < self.retries:
                    time.sleep(min(30, 2 ** attempt) + random.random())
                    continue
                raise SyncError("unreachable", f"could not reach {self.base_url}: {exc}") from None
        raise SyncError("unreachable", "gave up")


def _detail(exc: urllib.error.HTTPError) -> dict:
    try:
        body = json.loads(exc.read())
        detail = body.get("detail", body)
        return detail if isinstance(detail, dict) else {"message": str(detail)}
    except (ValueError, AttributeError):
        return {}


@dataclass
class SyncResult:
    uploaded: int = 0
    downloaded: int = 0
    rejected: list[dict] = field(default_factory=list)
    rounds: int = 0


class SyncClient:
    def __init__(self, profile: Profile, transport: Transport | None = None):
        self.profile = profile
        settings = profile.config.sync_settings(profile.profile_id)
        if transport is None:
            if not settings.get("server") or not settings.get("token"):
                raise SyncError("not_connected", "connect to a server first: openachievements server connect <url>")
            transport = Transport(settings["server"], settings["token"])
        self.transport = transport
        self.state_path = profile.config.dir / "sync" / f"{profile.profile_id}.json"

    def _load(self) -> dict:
        state = read_json(self.state_path, {})
        if state.get("server") != self.transport.base_url:
            # A different server starts from zero; everything is offered again.
            state = {"server": self.transport.base_url, "cursor": None, "acked": [], "rejected": {}}
        return state

    def pending(self) -> list[dict]:
        state = self._load()
        done = set(state["acked"]) | set(state["rejected"])
        return [e for e in self.profile.log.read(quarantine=False).events if e["event_id"] not in done]

    def run(self, max_batch: int = 500, max_rounds: int = 1000) -> SyncResult:
        caps = self.transport.request("GET", "/v1/capabilities")
        if PROTOCOL_VERSION not in caps.get("protocol_versions", []):
            raise SyncError("unsupported_protocol", f"server does not speak protocol {PROTOCOL_VERSION}")
        max_batch = min(max_batch, caps.get("limits", {}).get("max_batch", max_batch))
        state = self._load()
        result = SyncResult()
        acked = set(state["acked"])
        for _ in range(max_rounds):
            done = acked | set(state["rejected"])
            outgoing = [e for e in self.profile.log.read(quarantine=False).events if e["event_id"] not in done]
            batch = outgoing[:max_batch]
            reply = self.transport.request("POST", "/v1/sync", {
                "protocol_version": PROTOCOL_VERSION, "profile_id": self.profile.profile_id,
                "device_id": self.profile.device_id, "cursor": state["cursor"], "events": batch,
            })
            result.rounds += 1
            accepted = set(reply.get("accepted_event_ids", []))
            result.uploaded += len(accepted & {e["event_id"] for e in batch})
            acked |= accepted
            for rej in reply.get("rejected_events", []):
                if rej.get("event_id") and rej.get("code") != "quota_exceeded":
                    state["rejected"][rej["event_id"]] = rej
                result.rejected.append(rej)
            remote = reply.get("remote_events", [])
            if remote:
                result.downloaded += self.profile.commit(remote)
                acked |= {e["event_id"] for e in remote}
                # Increments from several devices can reach a target together.
                # Any unlock written here is pending and goes up next round.
                self.profile.reconcile_progress()
            state["cursor"] = reply.get("next_cursor", state["cursor"])
            state["acked"] = sorted(acked)
            state["last_sync"] = reply.get("server_time")
            write_json_atomic(self.state_path, state)
            quota_hit = any(r.get("code") == "quota_exceeded" for r in reply.get("rejected_events", []))
            done = acked | set(state["rejected"])
            still_pending = any(e["event_id"] not in done for e in self.profile.log.read(quarantine=False).events)
            if (not still_pending or quota_hit) and not reply.get("more"):
                break
        return result


def connect(profile: Profile, server: str, username: str, password: str, *, register: bool = False,
            transport_factory=Transport) -> dict:
    """Sign this device in to a server (creating the account if asked) and
    remember the server and token in the machine config. The password is not
    stored anywhere."""
    anon = transport_factory(server)
    if register:
        anon.request("POST", "/v1/accounts", {"username": username, "password": password})
    session = anon.request("POST", "/v1/sessions", {
        "username": username, "password": password,
        "device_id": profile.device_id, "device_name": profile.state()["devices"].get(profile.device_id, {}).get("name"),
    })
    bound = session.get("profile_id")
    if bound and bound != profile.profile_id:
        raise SyncError("profile_mismatch", "that account already syncs a different profile; "
                        "restore it on this device with 'openachievements profile restore' instead")
    profile.config.set_sync(profile.profile_id, {"server": anon.base_url, "username": username,
                                                  "token": session["token"]})
    return session


def restore(server: str, username: str, password: str, home, config, *, transport_factory=Transport) -> Profile:
    """A new device: download an account's whole history into a fresh local
    folder with the same profile id, then sync normally from then on."""
    from . import events as ev
    anon = transport_factory(server)
    session = anon.request("POST", "/v1/sessions", {"username": username, "password": password,
                                                    "device_id": None, "device_name": "restore"})
    profile_id = session.get("profile_id")
    if not profile_id:
        raise SyncError("nothing_to_restore", "that account has not synced a profile yet")
    folder = home / "profiles" / profile_id
    folder.mkdir(parents=True, exist_ok=True)
    authed = transport_factory(server, session["token"])
    pulled, cursor = [], None
    while True:
        reply = authed.request("POST", "/v1/sync", {"protocol_version": PROTOCOL_VERSION, "profile_id": profile_id,
                                                    "device_id": ev.new_id(), "cursor": cursor, "events": []})
        pulled.extend(reply.get("remote_events", []))
        cursor = reply.get("next_cursor")
        if not reply.get("more"):
            break
    authed.request("DELETE", "/v1/sessions/current")
    created = next((e for e in pulled if e["event_type"] == "profile.created"), None)
    write_json_atomic(folder / "profile.json", {
        "profile_id": profile_id, "name": (created or {}).get("payload", {}).get("name", username),
        "created_at": (created or {}).get("occurred_at", ev.now()),
        "format": "open-achievements-profile", "format_version": "1.0", "restored_from": anon.base_url,
    })
    from .eventlog import EventLog
    EventLog(folder, profile_id).append(pulled)
    profile = Profile(folder, config)
    connect(profile, server, username, password, transport_factory=transport_factory)
    # Everything just downloaded is already on the server: start from here
    # instead of offering it all back on the first sync.
    write_json_atomic(config.dir / "sync" / f"{profile_id}.json", {
        "server": anon.base_url, "cursor": cursor, "acked": sorted(e["event_id"] for e in pulled), "rejected": {}})
    return profile
