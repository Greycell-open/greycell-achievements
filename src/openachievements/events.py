"""The event envelope: the one durable record of everything that happens.

Achievement history is a list of immutable events. This
module builds them, hashes them and checks them. It has no filesystem, network
or clock side effects beyond `now()`, so the reducer and the server can both
rely on it.

The hash is SHA-256 over a canonical JSON form of the envelope without its
`integrity` block: sorted keys, no insignificant whitespace, UTF-8. Anyone can
recompute it without this code, which is the point.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.0"

EVENT_TYPES = frozenset({
    "profile.created",
    "profile.updated",
    "device.registered",
    "game.registered",
    "game.installation_registered",
    "game.installation_updated",
    "game.metadata_updated",
    "game.status_changed",
    "pack.installed",
    "pack.updated",
    "pack.removed",
    "progress.incremented",
    "progress.set",
    "achievement.unlocked",
    "achievement.revoked",
    "evidence.attached",
    "platform.account_linked",
    "platform.import_completed",
    "save.backup_created",
    "save.transfer_started",
    "save.transfer_completed",
    "save.transfer_failed",
    "sync.peer_added",
    "sync.completed",
    "conflict.detected",
    "conflict.resolved",
    "session.started",
    "session.ended",
})

# Named in the specification but without a payload schema yet. Refused with a
# clear code until each one is defined, rather than stored and ignored.
RESERVED_TYPES = frozenset({
    "evidence.attached", "save.backup_created", "save.transfer_started", "save.transfer_completed",
    "save.transfer_failed", "sync.peer_added", "sync.completed", "conflict.detected", "conflict.resolved",
})

# Envelope fields an event type means nothing without. An event missing one
# would be stored, synced everywhere, and then silently skipped by the reducer.
_REQUIRED_FIELDS = {
    "game.registered": "game_id", "game.metadata_updated": "game_id", "game.status_changed": "game_id",
    "game.installation_registered": "game_id", "game.installation_updated": "game_id",
    "session.started": "game_id", "session.ended": "game_id",
    "progress.incremented": "achievement_id", "progress.set": "achievement_id",
    "achievement.unlocked": "achievement_id", "achievement.revoked": "achievement_id",
}

# Where a game stands in the player's backlog. "none" clears it.
GAME_STATUSES = ("wishlist", "backlog", "playing", "completed", "dropped", "none")

# Bounds that keep one malformed or hostile event from costing anything real.
MAX_EVENT_BYTES = 64 * 1024
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


class EventError(ValueError):
    """An event that must not be stored. `code` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def now() -> str:
    """UTC, ISO 8601, millisecond precision, always ending in Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id() -> str:
    return str(uuid.uuid4())


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_hash(event: dict) -> str:
    body = {k: v for k, v in event.items() if k != "integrity"}
    return "sha256:" + hashlib.sha256(canonical(body)).hexdigest()


def is_slug(value: Any) -> bool:
    """Game, pack and achievement ids: lowercase, readable, bounded."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def make_event(
    event_type: str,
    *,
    profile_id: str,
    device_id: str,
    payload: dict | None = None,
    game_id: str | None = None,
    achievement_id: str | None = None,
    occurred_at: str | None = None,
    adapter: str = "manual",
    adapter_version: str = "0.1.0",
    external_account_id: str | None = None,
    external_event_id: str | None = None,
    previous_event_hash: str | None = None,
) -> dict:
    recorded = now()
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": new_id(),
        "event_type": event_type,
        "profile_id": profile_id,
        "device_id": device_id,
        "game_id": game_id,
        "achievement_id": achievement_id,
        "occurred_at": occurred_at or recorded,
        "recorded_at": recorded,
        "source": {
            "adapter": adapter,
            "adapter_version": adapter_version,
            "external_account_id": external_account_id,
            "external_event_id": external_event_id,
        },
        "payload": payload or {},
    }
    event["integrity"] = {"previous_event_hash": previous_event_hash, "event_hash": compute_hash(event)}
    validate(event)
    return event


def validate(event: Any, *, profile_id: str | None = None) -> dict:
    """Raise EventError unless `event` is a well-formed, untampered envelope."""
    if not isinstance(event, dict):
        raise EventError("not_an_object", "an event must be a JSON object")
    if len(canonical(event)) > MAX_EVENT_BYTES:
        raise EventError("too_large", f"events are limited to {MAX_EVENT_BYTES} bytes")
    if event.get("schema_version") != SCHEMA_VERSION:
        raise EventError("unsupported_schema", f"schema_version must be {SCHEMA_VERSION}")
    for key in ("event_id", "profile_id", "device_id"):
        if not isinstance(event.get(key), str) or not _UUID_RE.match(event[key]):
            raise EventError("bad_id", f"{key} must be a lowercase UUID")
    if event.get("event_type") not in EVENT_TYPES:
        raise EventError("unknown_type", f"unknown event_type {event.get('event_type')!r}")
    if event["event_type"] in RESERVED_TYPES:
        raise EventError("reserved_type", f"{event['event_type']} has no payload schema yet")
    for key in ("occurred_at", "recorded_at"):
        if not isinstance(event.get(key), str) or not _TS_RE.match(event[key]):
            raise EventError("bad_timestamp", f"{key} must be UTC ISO 8601 ending in Z")
    game_id = event.get("game_id")
    if game_id is not None and not is_slug(game_id):
        raise EventError("bad_game_id", "game_id must be a lowercase slug")
    achievement_id = event.get("achievement_id")
    if achievement_id is not None and not is_slug(achievement_id):
        raise EventError("bad_achievement_id", "achievement_id must be 'pack-id:achievement-id'")
    required = _REQUIRED_FIELDS.get(event["event_type"])
    if required and event.get(required) is None:
        raise EventError("missing_field", f"{event['event_type']} requires {required}")
    source = event.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("adapter"), str):
        raise EventError("bad_source", "source.adapter is required")
    if not isinstance(event.get("payload"), dict):
        raise EventError("bad_payload", "payload must be an object")
    integrity = event.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("event_hash") != compute_hash(event):
        raise EventError("bad_hash", "integrity.event_hash does not match the event")
    _validate_payload(event["event_type"], event["payload"])
    if profile_id is not None and event["profile_id"] != profile_id:
        raise EventError("wrong_profile", "event belongs to a different profile")
    return event


# ---- payloads -----------------------------------------------------------------
# A correct hash only proves an event was not altered after it was written.
# Whoever wrote it could still have put anything in the payload, and every
# device and server reduces every event, so one malformed payload must be
# refused here rather than crash a library somewhere else.

_TEXT_LIMIT = 2000


def _opt_text(payload: dict, *names: str, limit: int = _TEXT_LIMIT) -> None:
    for name in names:
        value = payload.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise EventError("bad_payload", f"payload.{name} must be text of at most {limit} characters")


def _number(payload: dict, name: str, *, required: bool) -> None:
    value = payload.get(name)
    if value is None and not required:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) > 1e12:
        raise EventError("bad_payload", f"payload.{name} must be a finite number")


def _opt_slug(payload: dict, name: str) -> None:
    value = payload.get(name)
    if value is not None and not is_slug(value):
        raise EventError("bad_payload", f"payload.{name} must be a lowercase id")


def _opt_dict(payload: dict, *names: str) -> None:
    for name in names:
        if payload.get(name) is not None and not isinstance(payload[name], dict):
            raise EventError("bad_payload", f"payload.{name} must be an object")


def _validate_payload(kind: str, p: dict) -> None:
    if kind in ("profile.created", "profile.updated"):
        _opt_text(p, "name", limit=120)
        _opt_dict(p, "privacy", "settings")
    elif kind == "device.registered":
        _opt_text(p, "name", "platform", "client_version", limit=200)
    elif kind in ("game.registered", "game.metadata_updated"):
        _opt_text(p, "title", "platform", "icon_url", limit=500)
        last = p.get("last_played")
        if last is not None and (not isinstance(last, str) or not _TS_RE.match(last)):
            raise EventError("bad_payload", "payload.last_played must be UTC ISO 8601 ending in Z")
        _number(p, "playtime_minutes", required=False)
        _opt_slug(p, "linked_to")
        _number(p, "release_year", required=False)
        ext = p.get("external_ids")
        if ext is not None and (not isinstance(ext, dict) or not all(
                isinstance(k, str) and isinstance(v, (str, int)) and not isinstance(v, bool) for k, v in ext.items())):
            raise EventError("bad_payload", "payload.external_ids must map names to ids")
    elif kind == "game.status_changed":
        if p.get("status") not in GAME_STATUSES:
            raise EventError("bad_payload", f"payload.status must be one of {', '.join(GAME_STATUSES)}")
        _opt_text(p, "note", limit=500)
    elif kind in ("game.installation_registered", "game.installation_updated"):
        if not isinstance(p.get("installation_id"), str) or not _UUID_RE.match(p["installation_id"]):
            raise EventError("bad_payload", "payload.installation_id must be a UUID")
        _opt_text(p, "kind", "source", "executable_name", "sha256", limit=300)
        _number(p, "size", required=False)
    elif kind in ("pack.installed", "pack.updated"):
        from .packs import PackError, validate_definitions
        part = p.get("part")
        if part is not None and not (
                isinstance(part, dict) and isinstance(part.get("set"), str) and re.fullmatch(r"[0-9a-f]{32}", part["set"])
                and all(isinstance(part.get(k), int) and not isinstance(part.get(k), bool) for k in ("index", "count"))
                and 1 <= part["count"] <= 1000 and 0 <= part["index"] < part["count"]):
            raise EventError("bad_payload", "payload.part must be {set, index, count}")
        try:
            validate_definitions({**p, "id": p.get("pack_id")}, p.get("achievements"))
        except PackError as exc:
            raise EventError("bad_payload", f"invalid pack: {exc}") from None
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            # The pack validator is written for files people author; a payload
            # can hold any JSON shape, so every failure is a refusal, never a crash.
            raise EventError("bad_payload", f"invalid pack: {type(exc).__name__}: {exc}") from None
    elif kind == "pack.removed":
        if not is_slug(p.get("pack_id")):
            raise EventError("bad_payload", "payload.pack_id is required")
    elif kind == "progress.incremented":
        _number(p, "amount", required=False)
    elif kind == "progress.set":
        _number(p, "value", required=True)
    elif kind == "achievement.unlocked":
        _opt_text(p, "provenance", "mode", "evidence", "note", "reason", "installation_id", limit=500)
    elif kind == "achievement.revoked":
        _opt_text(p, "reason", limit=500)
        revokes = p.get("revokes")
        if revokes is not None and (not isinstance(revokes, list) or len(revokes) > 1000
                                    or not all(isinstance(r, str) and _UUID_RE.match(r) for r in revokes)):
            raise EventError("bad_payload", "payload.revokes must be a list of event ids")
    elif kind in ("session.started", "session.ended"):
        _opt_text(p, "installation_id", "started_event_id", limit=64)
        _number(p, "seconds", required=False)
    elif kind in ("platform.account_linked", "platform.import_completed"):
        _opt_text(p, "adapter", "account", limit=200)
        _opt_dict(p, "summary")


def sort_key(event: dict) -> tuple[str, str, str]:
    """Deterministic order for reduction: when it happened, then when it was
    recorded, then the id as a tiebreak every device agrees on."""
    return (event["occurred_at"], event["recorded_at"], event["event_id"])
