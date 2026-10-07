"""Finding achievements for games that arrived without any.

A program the matcher could not name joins the library as `local-<folder>`
with no achievements (autodetect). Often it is a Steam game whose store title
differs from its folder: "Mini Airways" on disk is "Mini Airways - ATC
simulator" on Steam. The finder looks each such game up, most recently played
first, and when it finds the Steam game it installs that game's achievement
list and links the local entry into it, so play time, saves and anything
already recorded stay together.

Where it looks, in order:

1. the catalogue on this computer: a title equal to the game's name, or whose
   name before or after a subtitle is (only when exactly one catalogued game
   matches);
2. Steam's public store search (keyless, no account): only results whose
   title is exactly the game's name, or exactly it before or after a
   subtitle. When several share the name (a game's DLC and demo are listed
   beside it), the one with an achievement list of its own is the game; if
   that is not exactly one, nothing is linked. The request carries the game's
   name and nothing about the player.

Each game is looked up at most once a day (once a week after "not on Steam"),
one request per few seconds, and what was decided is kept in the machine
settings (never in events): a game nobody plays is never looked up again
until it is played.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from .adapters.autodetect import normalise, title_parts
from .catalog import steam as cat

STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/?term={term}&l=english&cc=US"
RETRY_AFTER = timedelta(days=1)          # Steam could not be reached, or it said nothing clear
NOT_FOUND_AFTER = timedelta(days=7)      # no Steam game by that name, or one without achievements
SETTINGS_KEY = "achievement_finder"
MAX_CANDIDATES = 4                        # store hits sharing the name, each asked for its list


def candidates(game: dict) -> list[str]:
    """Normalised names a game might be known by on Steam."""
    title = game.get("title") or ""
    names = [normalise(title)] + title_parts(title)
    return [n for n in dict.fromkeys(names) if len(n) >= 4]


def _names_of(title: str) -> set[str]:
    return {normalise(title), *title_parts(title)}


def catalogue_match(game: dict, index: cat.CatalogIndex | None) -> int | None:
    """The one catalogued Steam game with this name, or None."""
    if index is None:
        return None
    wanted = set(candidates(game))
    if not wanted:
        return None
    exact = {a for a, (_o, _l, _c, t) in index.entries.items() if normalise(t) in wanted}
    if len(exact) == 1:
        return next(iter(exact))
    if exact:
        return None
    by_part = {a for a, (_o, _l, _c, t) in index.entries.items() if wanted & set(title_parts(t))}
    return next(iter(by_part)) if len(by_part) == 1 else None


def store_match(game: dict, fetch) -> dict[int, str] | None | bool:
    """{appid: Steam's title} of the store results named like the game (a title
    equal to its name, or equal before or after a subtitle); None when there
    are none; False when the store gave no clear answer."""
    title = (game.get("title") or "").strip()
    wanted = set(candidates(game))
    if not title or not wanted:
        return None
    try:
        status, body = fetch(STORE_SEARCH_URL.format(term=urllib.parse.quote(title)))
        if status != 200:
            return False
        items = json.loads(body).get("items") or []
    except (OSError, ValueError, AttributeError, cat.CatalogError):
        return False
    exact, by_part = {}, {}
    for item in items:
        try:
            appid, name = int(item["id"]), str(item["name"])
        except (KeyError, TypeError, ValueError):
            continue
        if item.get("type", "app") != "app":
            continue
        if normalise(name) in wanted:
            exact[appid] = name
        elif wanted & set(title_parts(name)):
            by_part[appid] = name
    return exact or by_part or None


def pick(hits: dict[int, str], fetch) -> tuple[int, str] | None:
    """One game out of the store's hits. Several share a name when a game's
    DLC or demo is listed beside it ("Mini Airways - Tides of Barra"); those
    have no achievement list of their own, so the one hit that has one is the
    game. More than MAX_CANDIDATES is too vague to ask about."""
    if len(hits) == 1:
        return next(iter(hits.items()))
    if len(hits) > MAX_CANDIDATES:
        return None
    with_list = [(a, n) for a, n in hits.items() if cat.fetch_schema(a, fetch)]
    return with_list[0] if len(with_list) == 1 else None


class AchievementFinder:
    """Looks up local games in the background, newest activity first."""

    def __init__(self, profile, fetch=None, index_loader=None, now=None):
        self.profile = profile
        self._fetch = fetch or cat.PacedFetcher(pace=3.0)
        self._index_loader = index_loader or _default_index
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._queue: list[str] = []
        self._lock = threading.Lock()
        self._thread = None
        self.found: list[tuple[str, str]] = []           # (local game id, steam game id) this session

    # ---- what was decided before, on this machine -------------------------------------
    def _memory(self) -> dict:
        return (self.profile.config.load().get(SETTINGS_KEY) or {}).get(self.profile.profile_id) or {}

    def _remember(self, game_id: str, result: str, appid: int | None = None) -> None:
        stamp = self._now().isoformat()

        def apply(config: dict) -> None:
            mine = config.setdefault(SETTINGS_KEY, {}).setdefault(self.profile.profile_id, {})
            mine[game_id] = {"at": stamp, "result": result, **({"appid": appid} if appid else {})}
        self.profile.config.edit(apply)

    def due(self, game_id: str) -> bool:
        seen = self._memory().get(game_id)
        if not seen:
            return True
        try:
            at = datetime.fromisoformat(seen["at"])
        except (KeyError, TypeError, ValueError):
            return True
        wait = NOT_FOUND_AFTER if seen.get("result") in ("not-on-steam", "no-achievements") else RETRY_AFTER
        return self._now() - at >= wait

    # ---- which games ---------------------------------------------------------------------
    def waiting(self) -> list[str]:
        """Local games with no link and no achievements, newest activity first."""
        state = self.profile.state()
        games = []
        for game_id, game in state["games"].items():
            if not game_id.startswith("local-") or game.get("linked_to"):
                continue
            pack = state["packs"].get(game_id)
            if pack and not pack.get("removed") and pack.get("achievements"):
                continue
            games.append((game.get("last_activity") or game.get("last_played") or "", game_id))
        return [g for _when, g in sorted(games, reverse=True)]

    def want(self) -> None:
        """Queue every local game that is due for a look, and start looking."""
        with self._lock:
            queued = set(self._queue)
            for game_id in self.waiting():
                if game_id not in queued and self.due(game_id):
                    self._queue.append(game_id)
        if self._queue and not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, daemon=True, name="achievement-finder")
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                game_id = self._queue.pop(0)
            try:
                self.find_one(game_id)
            except Exception:  # noqa: BLE001 - one game's lookup is a nicety; the next is tried
                pass

    # ---- one game ------------------------------------------------------------------------
    def find_one(self, game_id: str) -> dict:
        """Look one local game up now. {"result": "linked" | "not-on-steam" |
        "no-achievements" | "unreachable" | "skipped", "steam_game": id?}"""
        state = self.profile.state()
        game = state["games"].get(game_id)
        if game is None or not game_id.startswith("local-") or game.get("linked_to"):
            return {"result": "skipped"}
        index = self._index_loader()
        appid = catalogue_match(game, index)
        steam_title = None
        if appid is None:
            hits = store_match(game, self._fetch)
            if hits is False:
                self._remember(game_id, "unreachable")
                return {"result": "unreachable"}
            found = pick(hits, self._fetch) if hits else None
            if found is None:
                self._remember(game_id, "not-on-steam")
                return {"result": "not-on-steam"}
            appid, steam_title = found
        steam_id = f"steam-{appid}"
        try:
            has_list = self._ensure_list(appid, steam_title, index)
        except (cat.CatalogError, OSError, ValueError):
            self._remember(game_id, "unreachable", appid)
            return {"result": "unreachable", "appid": appid}
        if not has_list:
            self._remember(game_id, "no-achievements", appid)
            return {"result": "no-achievements", "appid": appid}
        self._link(game_id, steam_id, game)
        self._remember(game_id, "linked", appid)
        self.found.append((game_id, steam_id))
        return {"result": "linked", "steam_game": steam_id, "appid": appid}

    def _ensure_list(self, appid: int, steam_title: str | None, index) -> bool:
        """The Steam game's achievement list in the library: True when it has one."""
        steam_id = f"steam-{appid}"
        pack = self.profile.state()["packs"].get(steam_id)
        if pack and not pack.get("removed") and pack.get("achievements"):
            return True
        if index is not None and index.get(appid):
            cat.add_to_profile(self.profile, index, appid)
            return True
        entry = cat.fetch_game(appid, self._fetch, known_title=steam_title)
        if not entry:
            return False
        if steam_id not in self.profile.state()["packs"]:
            cat.install_entry(self.profile, entry)
        return True

    def _link(self, game_id: str, steam_id: str, game: dict) -> None:
        self.profile.link_games(game_id, steam_id)
        steam = self.profile.state()["games"].get(steam_id) or {}
        if game.get("status") and steam.get("status") in (None, "backlog", "wishlist"):
            self.profile.set_status(steam_id, game["status"])


def _default_index():
    folder = cat.default_dir()
    return cat.CatalogIndex(folder) if (folder / cat.PACKS_FILE).exists() else None
