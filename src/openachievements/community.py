"""Community stats: anonymous counts for greycell.app's numbers.

On by default (owner, 2026-10-03), switched off in Settings, Privacy, and said
once on first run. The player's library stays on their computer: this sends
only what happened, never who. Each item is one of:

    install    the app ran for the first time on a computer
    unlock     an achievement unlocked just now
    platinum   a game's last achievement unlocked just now
    added      the player added a game to their library
    backlog    the player put a game on the Backlog shelf
    completed  the player put a game on the Completed shelf

with its platform and the minute it happened. For Steam games it also carries
the app id, the achievement's Steam name and, for a Platinum, the hours played:
public Steam data that lets greycell.app show names it looks up itself. Never
a player, an install id, an account, a path, or a game the player named.
Imports of old progress never count: only unlocks that happened in the last
hour, and only shelves the player chose in the app. Only unlocks a platform
recorded (Steam, PlayStation, Xbox, GOG, RetroAchievements) are named: an
unlock read from a save file or a play session cannot be verified, so it
counts as an unnamed "other PC" unlock, and a save that unlocks many at once
(notify.save_dumps) does not count at all (owner, 2026-10-03).

Items wait in a small file in the settings folder and go out once an hour,
in one request. Switched off, the waiting items are dropped and nothing is
sent.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from . import __version__, privacy
from .fsutil import file_lock, read_json, write_json_atomic

URL = "https://greycell.app/api/achievements/events"
SEND_EVERY = 3600.0                 # one request an hour at most
MAX_QUEUE = 1000                    # a computer offline for long keeps the newest
FRESH = 3600.0                      # an unlock older than this is an import, not a moment
KINDS = ("install", "unlock", "platinum", "added", "backlog", "completed")
PLATFORMS = {"steam-local": "steam", "steam": "steam", "psn": "playstation", "xbox": "xbox", "gog": "gog",
             "retroachievements": "retroachievements"}
_API = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
_lock = threading.Lock()


def _queue_path(config_store):
    return config_store.dir / "community-queue.json"


def _minute(when: datetime | None = None) -> str:
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:00Z")


def steam_ref(game_id: str | None) -> int | None:
    if game_id and game_id.startswith("steam-") and game_id[6:].isdigit():
        return int(game_id[6:])
    return None


def note(config_store, kind: str, platform: str = "pc", appid: int | None = None, api: str | None = None,
         minutes: int | None = None, at: datetime | None = None) -> bool:
    """Queue one item, when sharing is on. True when queued."""
    if kind not in KINDS or not privacy.allowed(config_store, "stats"):
        return False
    item = {"kind": kind, "platform": platform, "at": _minute(at)}
    if appid is not None:
        item["appid"] = int(appid)
        if api and _API.match(api):
            item["api"] = api
        if minutes is not None:
            item["minutes"] = max(0, min(int(minutes), 600000))
    with _lock, file_lock(config_store.dir / ".community.lock", timeout=10.0, stale_after=60.0):
        queue = read_json(_queue_path(config_store), {}) or {}
        items = (queue.get("items") or [])[-(MAX_QUEUE - 1):] + [item]
        write_json_atomic(_queue_path(config_store), {**queue, "items": items})
    return True


def first_run(config_store) -> bool:
    """The anonymous "a new install" item, once per computer."""
    if (config_store.load().get("community") or {}).get("install_sent"):
        return False
    config_store.edit(lambda c: c.setdefault("community", {}).__setitem__("install_sent", True))
    import sys
    return note(config_store, "install", "windows" if sys.platform == "win32" else "linux")


def platform_of(game_id: str | None) -> str:
    """A game's platform from its id, for the shelves the player sets."""
    for prefix, name in (("steam-", "steam"), ("ra-", "retroachievements"), ("psn-", "playstation"),
                         ("xbox-", "xbox"), ("gog-", "gog")):
        if (game_id or "").startswith(prefix):
            return name
    return "pc"


def _age(occurred: str, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(occurred.replace("Z", "+00:00"))).total_seconds()
    except (ValueError, AttributeError):
        return None


def from_round(profile, events: list[dict], now: datetime | None = None) -> int:
    """Queue a watcher round's fresh unlocks, and a Platinum for each game they
    completed. Old unlocks being imported are not counted."""
    now = now or datetime.now(timezone.utc)
    if not privacy.allowed(profile.config, "stats"):
        return 0
    from .notify import save_dumps
    state = profile.state()
    dumps = save_dumps(events)
    fresh_games: dict = {}                               # game -> platform, a platform-recorded one first
    queued = 0
    for e in events:
        if e.get("event_type") != "achievement.unlocked" or e.get("game_id") in dumps:
            continue
        age = _age(e.get("occurred_at", ""), now)
        if age is None or not -300 <= age <= FRESH:
            continue
        adapter = (e.get("source") or {}).get("adapter")
        game_id = e.get("game_id")
        platform = PLATFORMS.get(adapter, "pc")                # recorded by a platform, or not verifiable
        appid = steam_ref(game_id) if platform == "steam" else None
        pack_id, _, ach_id = (e.get("achievement_id") or "").partition(":")
        definition = ((state["packs"].get(pack_id) or {}).get("achievements") or {}).get(ach_id) or {}
        queued += note(profile.config, "unlock", platform, appid=appid,
                       api=definition.get("external_id") if appid else None,
                       at=now - timedelta(seconds=max(0.0, age)))
        if game_id and fresh_games.get(game_id) in (None, "pc"):
            fresh_games[game_id] = platform
    if fresh_games:
        games = {g["game_id"]: g for g in profile.library()["games"]}
        for game_id, platform in sorted(fresh_games.items()):
            g = games.get(game_id)
            if g and g["total"] and g["unlocked"] == g["total"]:
                appid = steam_ref(game_id) if platform == "steam" else None
                minutes = (g.get("playtime_seconds") or 0) // 60 if appid else None
                queued += note(profile.config, "platinum", platform, appid=appid, minutes=minutes or None)
    return queued


def _post(url: str, body: bytes) -> int:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"GreycellAchievements/{__version__}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status


def send(config_store, now: float | None = None, post=None, force: bool = False) -> str:
    """Send what is waiting, at most once an hour. What happened, for the log."""
    now = time.time() if now is None else now
    with _lock, file_lock(config_store.dir / ".community.lock", timeout=10.0, stale_after=60.0):
        queue = read_json(_queue_path(config_store), {}) or {}
        items = queue.get("items") or []
        if not privacy.allowed(config_store, "stats"):
            if items:
                write_json_atomic(_queue_path(config_store), {"items": [], "last_sent": queue.get("last_sent")})
            return "off"
        if not items:
            return "nothing"
        if not force and now - float(queue.get("last_sent") or 0) < SEND_EVERY:
            return "later"
        url = (config_store.load().get("community") or {}).get("url") or URL
        batch = items[:500]
        try:
            status = (post or _post)(url, json.dumps({"items": batch}).encode("utf-8"))
        except Exception:  # noqa: BLE001 - offline: they wait for the next hour
            write_json_atomic(_queue_path(config_store), {**queue, "last_sent": now})
            return "failed"
        rest = items[len(batch):] if status in (200, 202, 204, 400, 413) else items   # a refused batch is dropped
        write_json_atomic(_queue_path(config_store), {"items": rest, "last_sent": now})
        return f"sent {len(batch)}" if status in (200, 202, 204) else f"refused ({status})"
