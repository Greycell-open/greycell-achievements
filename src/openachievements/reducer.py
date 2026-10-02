"""Deterministic state from events. No I/O, no clock, no randomness.

Given the same set of events, in any delivery order, every device and every
server computes the same library. That is what lets devices sync by exchanging
events instead of overwriting each other's summaries:

- the same event twice is one event (deduplicated by event_id);
- unlocks are additive; the same external unlock imported on two devices is
  one achievement with one provenance record, keyed by adapter + external id;
- progress increments commute; a `progress.set` resets the base and only
  increments ordered after it count on top;
- two sets from different devices with different values at the same moment
  are surfaced as a conflict, not silently resolved;
- a revocation is its own event and never deletes the unlock it revokes.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from . import events as ev

# Provenance labels shown to people. Anything an adapter
# does not map explicitly is "unverified": honest by default.
PROVENANCE_BY_ADAPTER = {
    "manual": "manual",
    "executable": "local-executable",
    "save-file": "save-derived",
    "game-log": "game-log",
    "retroachievements": "imported",
    "steam": "imported",
    "psn": "imported",
    "xbox": "imported",
    "gog": "imported",
    "import": "imported",
}


def empty_state() -> dict:
    return {
        "profile": {},
        "devices": {},
        "games": {},
        "packs": {},
        "progress": {},
        "unlocks": {},
        "accounts": {},
        "imports": [],
        "sessions": {},
        "conflicts": [],
        "pack_parts": {},          # pack_id -> set id -> index -> payload, until a set is complete
        "pending_status": {},      # game_id -> status event payload that sorted before the game existed
        "event_count": 0,
    }


def reduce(events: Iterable[dict]) -> dict:
    unique = {e["event_id"]: e for e in events}
    ordered = sorted(unique.values(), key=ev.sort_key)
    state = empty_state()
    for event in ordered:
        apply(state, event)
    _finish_progress(state)
    return state


def _unlock_key(event: dict) -> str:
    source = event.get("source") or {}
    external = source.get("external_event_id")
    return f"{source.get('adapter')}:{external}" if external else event["event_id"]


def apply(state: dict, event: dict) -> None:
    kind = event["event_type"]
    p = event["payload"]
    state["event_count"] += 1
    game_id = event.get("game_id")
    key = event.get("achievement_id")

    if kind in ("profile.created", "profile.updated"):
        state["profile"].update({k: v for k, v in p.items() if k in ("name", "privacy", "settings")})
        state["profile"].setdefault("created_at", event["occurred_at"])
        state["profile"]["profile_id"] = event["profile_id"]

    elif kind == "device.registered":
        state["devices"][event["device_id"]] = {
            "device_id": event["device_id"], "name": p.get("name"), "platform": p.get("platform"),
            "client_version": p.get("client_version"), "first_seen": event["occurred_at"],
        }

    elif kind == "game.registered" and game_id:
        game = state["games"].setdefault(game_id, {"game_id": game_id, "installations": {}, "external_ids": {}})
        held = state.setdefault("pending_status", {}).pop(game_id, None)
        if held:
            _apply_status(game, *held)
        game.setdefault("registered_at", event["occurred_at"])
        for field in ("title", "platform", "release_year", "icon_url"):
            if p.get(field) is not None:
                game[field] = p[field]
        game["external_ids"].update(p.get("external_ids") or {})
        _played(game, p)

    elif kind == "game.metadata_updated" and game_id in state["games"]:
        game = state["games"][game_id]
        for field in ("title", "platform", "release_year", "icon_url"):
            if field in p:
                game[field] = p[field]
        game["external_ids"].update(p.get("external_ids") or {})
        _played(game, p)
        if "linked_to" in p:
            # One game, several sources: this entry is shown inside another.
            # Nothing is merged or rewritten; `linked_to: null` undoes it.
            if p["linked_to"]:
                game["linked_to"] = p["linked_to"]
            else:
                game.pop("linked_to", None)

    elif kind == "game.status_changed" and game_id:
        # The backlog: the latest status wins, like any setting a person changes.
        # A device with a slow clock can sort it before the game's registration;
        # it is held and applied when the game appears, never dropped.
        if game_id not in state["games"]:
            state.setdefault("pending_status", {})[game_id] = (p["status"], event["occurred_at"])
        else:
            _apply_status(state["games"][game_id], p["status"], event["occurred_at"])

    elif kind in ("game.installation_registered", "game.installation_updated") and game_id in state["games"]:
        inst_id = p.get("installation_id")
        if inst_id:
            inst = state["games"][game_id]["installations"].setdefault(inst_id, {"installation_id": inst_id})
            inst.update({k: v for k, v in p.items() if k != "installation_id"})
            inst["device_id"] = event["device_id"]

    elif kind in ("pack.installed", "pack.updated"):
        pack_id = p.get("pack_id")
        part = p.get("part")
        if pack_id and isinstance(part, dict):
            # One part of a pack too large for a single event: hold it until
            # every part of its set is here, then apply them as one pack. Parts
            # arrive from other devices, so the set must be consistent: every
            # part agrees on the count, the assembled pack hashes to the set id
            # it was split under, and it validates as a whole pack.
            sets = state.setdefault("pack_parts", {}).setdefault(pack_id, {})
            pending = sets.setdefault(part["set"], {"count": part["count"], "parts": {}, "bad": False})
            if part["count"] != pending["count"]:
                pending["bad"] = True
            pending["parts"][part["index"]] = p
            if pending["bad"] or len(pending["parts"]) < pending["count"]:
                return
            sets.pop(part["set"])
            parts = [pending["parts"][i] for i in range(pending["count"])]
            p = {**{k: v for k, v in parts[0].items() if k != "part"},
                 "achievements": [a for q in parts for a in q.get("achievements") or []]}
            if hashlib.sha256(ev.canonical(p)).hexdigest()[:32] != part["set"] or not _valid_set(part["set"], p):
                return
        if pack_id:
            # Events are loaded fresh for every reduction and state is read-only,
            # so the payload can be used as it is rather than copied.
            pack = state["packs"].setdefault(pack_id, {"pack_id": pack_id, "achievements": {}})
            pack.update({k: v for k, v in p.items() if k not in ("achievements", "part")})
            pack["removed"] = False
            pack["achievements"] = {a["id"]: a for a in p.get("achievements") or [] if isinstance(a, dict) and "id" in a}

    elif kind == "pack.removed":
        pack = state["packs"].get(p.get("pack_id"))
        if pack:
            pack["removed"] = True

    # No hand-made achievements (owner decision 2026-10-01): an unlock marked
    # manual, or progress typed in by hand, stays in the history, from any
    # device or older client, and counts for nothing.
    elif kind in ("progress.incremented", "progress.set", "achievement.unlocked") and _by_hand(event):
        return

    elif kind == "progress.incremented" and key:
        prog = state["progress"].setdefault(key, {"base": 0, "since_base": 0, "sets": []})
        prog["since_base"] += _number(p.get("amount", 1))

    elif kind == "progress.set" and key:
        prog = state["progress"].setdefault(key, {"base": 0, "since_base": 0, "sets": []})
        value = _number(p.get("value", 0))
        for earlier in prog["sets"]:
            if (earlier["device_id"] != event["device_id"] and earlier["value"] != value
                    and earlier["occurred_at"][:16] == event["occurred_at"][:16]):
                state["conflicts"].append({
                    "kind": "progress.set", "achievement_id": key,
                    "values": [earlier["value"], value],
                    "event_ids": [earlier["event_id"], event["event_id"]],
                })
        prog["sets"].append({"value": value, "device_id": event["device_id"],
                             "occurred_at": event["occurred_at"], "event_id": event["event_id"]})
        prog["base"], prog["since_base"] = value, 0

    elif kind == "achievement.unlocked" and key:
        entry = state["unlocks"].setdefault(key, {"records": {}, "revoked": set(), "pending_revokes": set()})
        record_key = _unlock_key(event)
        if event["event_id"] in entry.setdefault("pending_revokes", set()):
            # A revocation that named this unlock arrived first (clock skew).
            entry["revoked"].add(record_key)
        if record_key in entry["records"]:
            # Two devices can record the same unlock (same external id). Keep
            # every event id, so a revocation naming either one reaches it.
            entry["records"][record_key]["event_ids"].append(event["event_id"])
        else:
            source = event["source"]
            entry["records"][record_key] = {
                "event_id": event["event_id"],
                "event_ids": [event["event_id"]],
                "occurred_at": event["occurred_at"],
                "device_id": event["device_id"],
                "adapter": source.get("adapter"),
                "external_event_id": source.get("external_event_id"),
                "provenance": p.get("provenance") or PROVENANCE_BY_ADAPTER.get(source.get("adapter"), "unverified"),
                "mode": p.get("mode"),
                "evidence": p.get("evidence"),
            }

    elif kind == "achievement.revoked" and key:
        entry = state["unlocks"].setdefault(key, {"records": {}, "revoked": set(), "pending_revokes": set()})
        targets = p.get("revokes")
        for record_key, record in entry["records"].items():
            if targets is None or any(i in targets for i in record["event_ids"]):
                entry["revoked"].add(record_key)
        if isinstance(targets, list):
            # Targeted revocations hold regardless of order: a named unlock that
            # sorts after this event (another device's clock) is revoked on arrival.
            entry.setdefault("pending_revokes", set()).update(t for t in targets if isinstance(t, str))

    elif kind in ("session.started", "session.ended") and game_id:
        played = state["sessions"].setdefault(game_id, {"count": 0, "seconds": 0, "started": 0, "last_played": None})
        if kind == "session.started":
            played["started"] += 1
        else:
            played["count"] += 1
            played["seconds"] += max(0, int(_number(p.get("seconds", 0))))
        played["last_played"] = max(played["last_played"] or "", event["occurred_at"])

    elif kind == "platform.account_linked":
        adapter = p.get("adapter") or event["source"].get("adapter")
        state["accounts"][adapter] = {"adapter": adapter, "account": p.get("account"),
                                      "linked_at": event["occurred_at"]}

    elif kind == "platform.import_completed":
        state["imports"].append({"adapter": p.get("adapter") or event["source"].get("adapter"),
                                 "at": event["occurred_at"], "summary": p.get("summary")})
        state["imports"] = state["imports"][-50:]


def _played(game: dict, p: dict) -> None:
    """When a platform or the save folder last saw the game played. The latest
    wins, whatever order devices report it in."""
    if isinstance(p.get("last_played"), str):
        game["last_played"] = max(game.get("last_played") or "", p["last_played"])
    if isinstance(p.get("playtime_minutes"), (int, float)) and not isinstance(p["playtime_minutes"], bool):
        game["playtime_minutes"] = max(game.get("playtime_minutes") or 0, p["playtime_minutes"])


_VALID_SETS: dict[str, bool] = {}


def _valid_set(set_id: str, payload: dict) -> bool:
    """A reassembled pack's content is fixed by its set id (checked just
    before), so its validity is too: validate once per process."""
    if set_id not in _VALID_SETS:
        if len(_VALID_SETS) > 20000:
            _VALID_SETS.clear()
        _VALID_SETS[set_id] = _valid_pack(payload)
    return _VALID_SETS[set_id]


def _valid_pack(payload: dict) -> bool:
    from .packs import PackError, validate_definitions
    try:
        validate_definitions({**payload, "id": payload.get("pack_id")}, payload.get("achievements"))
    except (PackError, TypeError, ValueError, AttributeError, KeyError):
        return False
    return True


def _apply_status(game: dict, status: str, at: str) -> None:
    if status == "none":
        game.pop("status", None)
        game.pop("status_at", None)
    else:
        game["status"], game["status_at"] = status, at


def _number(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0
    return int(n) if n.is_integer() else n


def _finish_progress(state: dict) -> None:
    for prog in state["progress"].values():
        prog["value"] = prog["base"] + prog["since_base"]


def _by_hand(event: dict) -> bool:
    if event["event_type"] == "achievement.unlocked":
        return (event.get("payload") or {}).get("provenance") == "manual" or (
            not (event.get("payload") or {}).get("provenance") and event["source"].get("adapter") == "manual")
    return event["source"].get("adapter") == "manual"


def is_unlocked(state: dict, key: str) -> bool:
    entry = state["unlocks"].get(key)
    return bool(entry) and any(k not in entry["revoked"] for k in entry["records"])


def _link_target(games: dict, game_id: str) -> str:
    """Follow links to the game that is actually shown, refusing cycles."""
    path = [game_id]
    while True:
        nxt = games[path[-1]].get("linked_to")
        if nxt not in games:
            return path[-1]
        if nxt in path:
            # A cycle (only possible from events written elsewhere, since
            # Profile.link_games refuses them): every member shows inside the
            # lowest id, which every device picks the same way.
            return min(path[path.index(nxt):])
        path.append(nxt)


def _fold_linked(games: dict) -> None:
    for game_id in list(games):
        target = _link_target(games, game_id)
        if target == game_id:
            continue
        source, dest = games[game_id], games[target]
        dest["achievements"].extend(source["achievements"])
        for n in ("unlocked", "total", "points", "points_total", "playtime_seconds"):
            dest[n] += source[n]
        dest["external_ids"] = {**source.get("external_ids", {}), **dest.get("external_ids", {})}
        dest["installations"] = dest["installations"] + source["installations"]
        dest.setdefault("linked_games", []).append(
            {"game_id": game_id, "title": source.get("title"), "platform": source.get("platform")})
    for game_id in [g for g in games if _link_target(games, g) != g]:
        del games[game_id]


def library(state: dict) -> dict:
    """The view people look at: games, their achievements, what is unlocked,
    by which sources. JSON-serialisable, used by the CLI, the local web UI and
    the server alike."""
    games: dict[str, dict] = {}
    for game_id, game in state["games"].items():
        games[game_id] = {**{k: v for k, v in game.items() if k != "installations"},
                          "installations": list(game["installations"].values()),
                          "achievements": [], "unlocked": 0, "total": 0, "points": 0, "points_total": 0,
                          "playtime_seconds": max(state["sessions"].get(game_id, {}).get("seconds", 0),
                                                  int(game.get("playtime_minutes") or 0) * 60),
                          "last_played": max(filter(None, [state["sessions"].get(game_id, {}).get("last_played"),
                                                           game.get("last_played")]), default=None)}
    for pack_id, pack in state["packs"].items():
        if pack.get("removed"):
            continue
        for game_id in pack.get("game_ids") or []:
            game = games.get(game_id)
            if game is None:
                continue
            for ach_id, definition in pack["achievements"].items():
                key = f"{pack_id}:{ach_id}"
                entry = state["unlocks"].get(key) or {"records": {}, "revoked": set()}
                live = [r for k, r in entry["records"].items() if k not in entry["revoked"]]
                unlocked = bool(live)
                target = (definition.get("progress") or {}).get("target")
                prog = state["progress"].get(key)
                points = definition.get("points") or 0
                hide = definition.get("hidden") and not unlocked
                game["achievements"].append({
                    "key": key, "pack_id": pack_id, "id": ach_id,
                    "name": "Hidden achievement" if hide else definition.get("name", ach_id),
                    "description": "" if hide else definition.get("description", ""),
                    "points": points, "hidden": bool(definition.get("hidden")),
                    "icon": definition.get("icon"), "source": pack.get("source", "pack"),
                    "unlocked": unlocked,
                    # The real text of a still-hidden achievement, for its owner to
                    # reveal on purpose; public views drop it (`without_secrets`).
                    "secret": {"name": definition.get("name", ach_id),
                               "description": definition.get("description", "")} if hide else None,
                    "unlocked_at": min((r["occurred_at"] for r in live), default=None),
                    "provenance": sorted({r["provenance"] for r in live}),
                    "sources": sorted({r["adapter"] for r in live}),
                    "progress": {"value": prog["value"], "target": target} if prog and target else None,
                })
                game["total"] += 1
                game["points_total"] += points
                if unlocked:
                    game["unlocked"] += 1
                    game["points"] += points
    _fold_linked(games)
    for game in games.values():
        game["achievements"].sort(key=lambda a: (not a["unlocked"], a["unlocked_at"] or "", a["name"]))
        # The most recent sign of play from any source: sessions, the platform's
        # own record, a save folder, or an unlock.
        unlocks = [a["unlocked_at"] for a in game["achievements"] if a["unlocked_at"]]
        game["last_activity"] = max(filter(None, [game.get("last_played"), *unlocks]), default=None)
    ordered = sorted(games.values(), key=lambda g: (g.get("title") or g["game_id"]).lower())
    return {
        "profile": state["profile"],
        "games": ordered,
        "totals": {
            "games": len(ordered),
            "unlocked": sum(g["unlocked"] for g in ordered),
            "achievements": sum(g["total"] for g in ordered),
            "points": sum(g["points"] for g in ordered),
        },
        "accounts": list(state["accounts"].values()),
        "conflicts": state["conflicts"],
        "event_count": state["event_count"],
    }


def without_secrets(games: list[dict]) -> list[dict]:
    """Games as anyone else may see them: hidden achievements stay hidden."""
    return [{**g, "achievements": [{**a, "secret": None} for a in g.get("achievements", [])]} for g in games]
