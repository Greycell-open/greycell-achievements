"""How rare an achievement is: the share of players who have it.

Steam publishes every achievement's global unlock percentage in its keyless
achievement schema (IPlayerService/GetGameAchievements, the same answer the
catalogue reads). Rarity is not part of a player's history (it changes as
more people play), so it is kept in a cache on this computer, one small file
per Steam app, refreshed weekly, and never written to events or synced.

RetroAchievements games (ra-<id>) get theirs from the RetroAchievements sync,
which already receives each achievement's award count and the game's player
count (store()): no extra request.

Two ways a Steam game's rarity arrives:
  - RarityCrawler, part of the watcher: one Steam game of the library at a
    time, recently played first, at most one request every PACE seconds;
  - for_unlock(): when an achievement unlocks in a game with no rarity yet,
    that one game is fetched there and then, so a rare unlock is known in time
    for its popup.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

from .fsutil import write_json_atomic

REFRESH = 7 * 86400.0               # a week: percentages move slowly
PACE = 5.0                          # the crawler asks Steam at most once every 5 s
MISS_FOR = 86400.0                  # a game Steam had no answer for: asked again tomorrow


def cache_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "OpenAchievements" / "rarity"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "openachievements" / "rarity"


def _appid(game_id: str) -> str | None:
    if not str(game_id).startswith("steam-"):
        return None
    appid = str(game_id).split("-", 1)[1]
    return appid if appid.isdigit() else None


def _key(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def _file(game_id: str, folder: Path | None) -> Path:
    safe = "".join(ch for ch in str(game_id) if ch.isalnum() or ch in "-_.")
    return Path(folder or cache_dir()) / f"{safe}.json"


def _load(game_id: str, folder: Path | None) -> dict | None:
    try:
        return json.loads(_file(game_id, folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def store(game_id: str, rows: list[tuple[str, str, float]], folder: Path | None = None,
          now: float | None = None) -> None:
    """Keep percentages a platform's sync already received (RetroAchievements):
    rows of (stable id, name, percent)."""
    data = _tables(rows)
    data["fetched"] = time.time() if now is None else now
    write_json_atomic(_file(game_id, folder), data)


def _default_fetch(url: str) -> tuple[int, str]:
    from .catalog import steam as cat
    return cat.PacedFetcher(pace=0.0, retries=0)(url)


def fetch(appid: str, http: Callable[[str], tuple[int, str]] | None = None, folder: Path | None = None,
          now: float | None = None) -> dict | None:
    """Ask Steam for one game's percentages and keep them. Only Steam's own
    answer is cached (a list, or "no achievements"); a failed request is not,
    so it is simply asked again later. None when there are no percentages."""
    from .catalog import steam as cat
    now = time.time() if now is None else now
    try:
        status, body = (http or _default_fetch)(cat.SCHEMA_URL.format(appid=int(appid)))
    except Exception:  # noqa: BLE001 - offline, slow, refused: no rarity this time, asked again later
        return None
    if status != 200:                            # 403/404 may be passing refusals: asked again later
        return None
    try:
        response = json.loads(body)["response"]
        items = response.get("achievements", [])
        if not isinstance(items, list):
            raise ValueError("achievements is not a list")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None                              # garbled: not an answer, nothing cached
    rows = [(str(a.get("internal_name") or ""), str(a.get("localized_name") or ""), a.get("player_percent_unlocked"))
            for a in items if isinstance(a, dict)]
    data = _tables(rows)
    data.update(fetched=now, answered=True)
    write_json_atomic(_file(f"steam-{appid}", folder), data)
    return data if data["by_id"] else None


def _tables(rows) -> dict:
    """{"by_id": {stable id: pct}, "by_name": {name: pct}} where a name shared
    by two achievements is left out (it could be either)."""
    by_id, by_name, seen = {}, {}, {}
    for stable, name, value in rows:
        try:
            pct = round(float(value), 2)
        except (TypeError, ValueError):
            continue
        if stable:
            by_id[stable] = pct
        key = _key(name)
        if key:
            seen[key] = seen.get(key, 0) + 1
            by_name[key] = pct
    return {"by_id": by_id, "by_name": {k: v for k, v in by_name.items() if seen[k] == 1}}


def fresh(appid: str, folder: Path | None = None, now: float | None = None) -> bool:
    data = _load(f"steam-{appid}", folder)
    if not data:
        return False
    now = time.time() if now is None else now
    age = now - float(data.get("fetched") or 0)
    return age < (REFRESH if data.get("by_name") else MISS_FOR)


def percent(game_id: str, name: str, folder: Path | None = None, stable_id: str | None = None) -> float | None:
    """The share of players with this achievement, from the cache only: by the
    platform's own id when known, else by a name no other achievement shares."""
    data = _load(game_id, folder)
    if not data:
        return None
    value = (data.get("by_id") or {}).get(str(stable_id)) if stable_id not in (None, "") else None
    if value is None:
        value = (data.get("by_name") or {}).get(_key(name))
    return float(value) if value is not None else None


def for_unlock(game_id: str, name: str, http=None, folder: Path | None = None,
               stable_id: str | None = None) -> float | None:
    """The percentage for an achievement that just unlocked: from the cache,
    or fetched now if this game has none yet (one request)."""
    appid = _appid(game_id)
    if not appid:
        return percent(game_id, name, folder, stable_id)  # RetroAchievements: from its sync, if any
    if not fresh(appid, folder):
        fetch(appid, http, folder)
    return percent(game_id, name, folder, stable_id)


def fresh_after(fetcher, appid: str, http, folder) -> bool:
    fetcher(appid, http, folder)
    return fresh(appid, folder)


class RarityCrawler:
    """One Steam game of the library per look, recently played first, never
    more often than PACE seconds; games already fresh are skipped."""

    def __init__(self, profile, http=None, folder: Path | None = None, clock=time.monotonic):
        self.profile, self.http, self.folder, self.clock = profile, http, folder, clock
        self._last = None
        self._tried: dict[str, float] = {}         # no answer (failure, refusal): not again for a day, this session

    def _next(self) -> str | None:
        state = self.profile.state()
        packs, games = state["packs"], state["games"]
        order = sorted((gid for gid in games if _appid(gid) and packs.get(gid)),
                       key=lambda gid: games[gid].get("last_played") or "", reverse=True)
        now = self.clock()
        for gid in order:
            appid = _appid(gid)
            if not fresh(appid, self.folder) and now - self._tried.get(appid, -MISS_FOR) >= MISS_FOR:
                return appid
        return None

    def poll(self) -> str | None:
        """Fetch one game's rarity if it is time; returns the app id asked for."""
        now = self.clock()
        if self._last is not None and now - self._last < PACE:
            return None
        appid = self._next()
        if not appid:
            return None
        self._last = now
        if not fresh_after(fetch, appid, self.http, self.folder):
            self._tried[appid] = now                 # move on to the next game instead of asking again
        return appid
