"""RetroAchievements import.

Uses the public RetroAchievements Web API with the player's own Web API key
(from their RetroAchievements control panel). Never a password.

- API_GetUserCompletionProgress lists every game the player has touched.
- API_GetGameInfoAndUserProgress gives a game's achievement set with the
  player's DateEarned / DateEarnedHardcore.

Mapping:

  RA game 1234          -> game `ra-1234`, external_ids.retroachievements = 1234
  its achievement set   -> pack `ra-1234`, source "retroachievements",
                           achievement ids `a<RA id>`, RA points preserved
  an earned achievement -> achievement.unlocked, one per mode:
                           external_event_id "<user>:<achId>:softcore|hardcore"

Softcore and hardcore stay distinct, so a hardcore unlock that follows a
softcore one is a second provenance record, not a replacement. Badge images
are referenced by their RetroAchievements URL and not copied.

Live (the dashboard's Link accounts, ADR 0009): the player gives a username
and their Web API key once. The key is kept only in the operating system's
credential store (credentials.py), never in the profile, an event, a log or a
sync. The watcher asks for the completion list every two minutes and fetches
only the games whose last award changed, one request per round and never two
within five seconds. Requests use the account's ULID, which survives a
rename; external ids keep the username recorded at link time, so a rename
never counts anything twice. Unlocks sync quietly like Steam's: the emulator
already shows its own popup.
"""
from __future__ import annotations

import json
import time

from .. import credentials
from .. import events as ev
from ..fsutil import write_json_atomic
from ..packs import split_for_events
from ..profile import Profile, ProfileError
from .base import AdapterError, AdapterMetadata, ImportSummary, build_url, http_json, utc_iso

API = "https://retroachievements.org/API/"
MEDIA = "https://media.retroachievements.org"
ADAPTER_VERSION = "0.2.0"
SECRET = "retroachievements-key"
KEY_PAGE = "https://retroachievements.org/settings"
MIN_GAP = 5.0                       # seconds between two requests to RetroAchievements
LIST_EVERY = 120.0                  # look at the completion list every two minutes
STATUS: dict = {}                   # profile id -> what the page shows while a sync runs

METADATA = AdapterMetadata(
    adapter_id="retroachievements",
    adapter_version=ADAPTER_VERSION,
    support_level="official",
    authentication_method="RetroAchievements username and personal Web API key",
    data_access_method="RetroAchievements public Web API (read-only)",
    capabilities=("discover-games", "import-definitions", "import-unlocks", "hardcore-distinction",
                  "timestamps", "badge-references"),
    rate_limits="unpublished; requests are paced and back off on 429",
    known_limitations=(
        "only games the player has earned at least one achievement in, or started, are listed",
        "subsets and event sets appear as their own games",
        "badges are linked, not copied",
    ),
    terms_or_policy_notes="Read-only use of the documented Web API with the player's own key. "
                          "Nothing is ever written to RetroAchievements.",
)


def _fetch_all_games(fetch, username: str, key: str) -> list[dict]:
    games, offset = [], 0
    while True:
        page = fetch(build_url(API + "API_GetUserCompletionProgress.php", y=key, u=username, c=500, o=offset))
        if not isinstance(page, dict):
            raise AdapterError("bad_response", "unexpected completion progress response")
        results = page.get("Results") or page.get("results") or []
        games.extend(results)
        total = page.get("Total") or page.get("total") or 0
        offset += len(results)
        if not results or offset >= total:
            return games


def _get(d: dict, *names):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return None


def _keep_rarity(game_id: str, info: dict) -> None:
    """RetroAchievements sends each achievement's award count and the game's
    player count with the game: their ratio is its rarity, kept like Steam's."""
    players = _get(info, "NumDistinctPlayers", "NumDistinctPlayersCasual", "numDistinctPlayers")
    try:
        players = int(players or 0)
    except (TypeError, ValueError):
        return
    if players <= 0:
        return
    by_name = {}
    for a in (_get(info, "Achievements", "achievements") or {}).values():
        awarded = _get(a, "NumAwarded", "numAwarded")
        if _get(a, "Title", "title") and isinstance(awarded, (int, float)):
            by_name[_get(a, "Title", "title")] = 100.0 * awarded / players
    if by_name:
        from .. import rarity
        try:
            rarity.store(game_id, by_name)
        except OSError:
            pass                                          # rarity is a nicety; the sync goes on


def plan_game(profile: Profile, state: dict, known: set, username: str, listing: dict,
              info: dict) -> tuple[list[dict], int]:
    """Events for one RA game (register it, install or update its set, the
    unlocks not seen before) and how many unlocks were already here."""
    ra_id = _get(listing, "GameID", "gameId")

    def event(kind, payload, **fields):
        return ev.make_event(kind, profile_id=profile.profile_id, device_id=profile.device_id, payload=payload,
                             adapter="retroachievements", adapter_version=ADAPTER_VERSION,
                             external_account_id=username, **fields)

    out, already = [], 0
    _keep_rarity(f"ra-{int(ra_id)}", info)
    stamp = str(_get(listing, "MostRecentAwardedDate", "mostRecentAwardedDate") or "")
    last = utc_iso(stamp)
    game_id = f"ra-{int(ra_id)}"
    title = _get(info, "Title", "title") or _get(listing, "Title", "title") or game_id
    console = _get(info, "ConsoleName", "consoleName") or _get(listing, "ConsoleName", "consoleName")
    icon = _get(info, "ImageIcon", "imageIcon")
    if game_id not in state["games"]:
        out.append(event("game.registered", {
            "title": title, "platform": console, "icon_url": f"{MEDIA}{icon}" if icon else None,
            "external_ids": {"retroachievements": int(ra_id)}, **({"last_played": last} if last else {})},
            game_id=game_id))
    elif last and last > (state["games"][game_id].get("last_played") or ""):
        out.append(event("game.metadata_updated", {"last_played": last}, game_id=game_id))

    raw = _get(info, "Achievements", "achievements") or {}
    achievements = sorted(raw.values(), key=lambda a: (_get(a, "DisplayOrder", "displayOrder") or 0,
                                                       _get(a, "ID", "id") or 0))
    definitions = []
    for a in achievements:
        ach_id = _get(a, "ID", "id")
        if ach_id is None:
            continue
        badge = _get(a, "BadgeName", "badgeName")
        definitions.append({
            "id": f"a{int(ach_id)}",
            "name": (_get(a, "Title", "title") or f"Achievement {ach_id}").strip()[:120] or f"Achievement {ach_id}",
            "description": (_get(a, "Description", "description") or "")[:500],
            "points": int(_get(a, "Points", "points") or 0),
            "hidden": False,
            "icon": f"{MEDIA}/Badge/{badge}.png" if badge else None,
            "category": _get(a, "type") or None,
            "external_id": int(ach_id),
        })
    pack_id = f"ra-{int(ra_id)}"
    if definitions:
        description = {
            "pack_id": pack_id, "name": f"{title} (RetroAchievements)", "version": stamp or "imported",
            "schema_version": "1.0", "game_ids": [game_id], "source": "retroachievements",
            "authors": ["RetroAchievements community"], "license": None,
            "source_repository": f"https://retroachievements.org/game/{int(ra_id)}",
            "supported_adapters": ["retroachievements"], "achievements": definitions,
        }
        existing = state["packs"].get(pack_id)
        if not existing or existing.get("achievements") != {d["id"]: d for d in definitions}:
            out.extend(event("pack.updated" if existing else "pack.installed", payload)
                       for payload in split_for_events(description))

    for a in achievements:
        ach_id = _get(a, "ID", "id")
        if ach_id is None:
            continue
        for mode, field_names in (("softcore", ("DateEarned", "dateEarned")),
                                  ("hardcore", ("DateEarnedHardcore", "dateEarnedHardcore"))):
            when = utc_iso(_get(a, *field_names))
            if not when:
                continue
            external = f"{username.lower()}:{int(ach_id)}:{mode}"
            if external in known:
                already += 1
                continue
            out.append(event("achievement.unlocked", {"provenance": "imported", "mode": mode},
                             achievement_id=f"{pack_id}:a{int(ach_id)}", game_id=game_id,
                             occurred_at=when, external_event_id=external))
            known.add(external)
    return out, already


def import_account(profile: Profile, username: str, api_key: str, *, fetch=None,
                   only_changed: bool = True, pause: float = 0.25) -> ImportSummary:
    """Import everything new. `only_changed` skips games whose most recent
    award date matches the last import, which makes a re-run cheap."""
    if not username or not api_key:
        raise AdapterError("missing_credentials", "a RetroAchievements username and Web API key are required")
    fetch = fetch or (lambda url: http_json(url, pause=pause))
    summary = ImportSummary(adapter="retroachievements")
    marker_key = f"retroachievements:{username.lower()}"
    seen_dates: dict = dict(profile.config.load().get("import_markers", {}).get(marker_key, {}))

    games = _fetch_all_games(fetch, username, api_key)
    summary.games_seen = len(games)
    known_unlocks = profile.index.external_ids("retroachievements")
    state = profile.state()
    pending: list[dict] = []
    if "retroachievements" not in state["accounts"]:
        pending.append(ev.make_event("platform.account_linked", profile_id=profile.profile_id,
                                     device_id=profile.device_id,
                                     payload={"adapter": "retroachievements", "account": username},
                                     adapter="retroachievements", adapter_version=ADAPTER_VERSION,
                                     external_account_id=username))

    for listing in games:
        ra_id = _get(listing, "GameID", "gameId")
        if ra_id is None:
            continue
        stamp = str(_get(listing, "MostRecentAwardedDate", "mostRecentAwardedDate") or "")
        if only_changed and stamp and seen_dates.get(str(ra_id)) == stamp:
            continue
        try:
            info = fetch(build_url(API + "API_GetGameInfoAndUserProgress.php", y=api_key, g=ra_id, u=username, a=1))
        except AdapterError as exc:
            summary.problems.append(f"game {ra_id}: {exc}")
            continue
        if not isinstance(info, dict):
            summary.problems.append(f"game {ra_id}: unexpected response")
            continue
        events, already = plan_game(profile, state, known_unlocks, username, listing, info)
        pending.extend(events)
        summary.games_added += sum(e["event_type"] == "game.registered" for e in events)
        summary.packs_written += any(e["event_type"] in ("pack.installed", "pack.updated") for e in events)
        summary.unlocks_added += sum(e["event_type"] == "achievement.unlocked" for e in events)
        summary.unlocks_already_known += already
        if stamp:
            seen_dates[str(ra_id)] = stamp

    pending.append(ev.make_event("platform.import_completed", profile_id=profile.profile_id,
                                 device_id=profile.device_id, payload={"adapter": "retroachievements", "summary": {
                                     k: v for k, v in summary.as_dict().items() if k != "problems"}},
                                 adapter="retroachievements", adapter_version=ADAPTER_VERSION,
                                 external_account_id=username))
    profile.commit(pending)
    with profile.config.editing() as config:
        config.setdefault("import_markers", {})[marker_key] = seen_dates
    return summary


# ---- the live link ------------------------------------------------------------------------

class RaError(ProfileError):
    pass


def _config(profile: Profile) -> dict:
    return profile.config.load().get("retroachievements", {}).get(profile.profile_id) or {}


def _save_config(profile: Profile, link: dict | None) -> None:
    with profile.config.editing() as config:
        mine = config.setdefault("retroachievements", {})
        if link is None:
            mine.pop(profile.profile_id, None)
        else:
            mine[profile.profile_id] = link


def linked(profile: Profile) -> dict | None:
    link = _config(profile)
    return link if link.get("ulid") else None


def _sync_file(profile: Profile):
    return profile.config.dir / f"ra-sync-{profile.profile_id}.json"


def _ask(fetch, url: str):
    """One request, no waiting inside the watcher; RA's answers turned into
    sentences. The key is part of the URL, so no error ever repeats the URL."""
    try:
        return fetch(url)
    except AdapterError as exc:
        if exc.code == "unauthorized":
            raise RaError("RetroAchievements refused the Web API key. Copy it again from "
                          f"{KEY_PAGE} and reconnect.") from None
        if "429" in str(exc):
            raise RaError("RetroAchievements asked to slow down; the sync waits and tries again.") from None
        if exc.code == "unreachable":
            raise RaError("Could not reach RetroAchievements; the sync tries again in a few minutes.") from None
        raise RaError(f"RetroAchievements: {exc}") from None


def _fetch_once(url: str):
    return http_json(url, retries=0)


def connect(profile: Profile, username: str, api_key: str, fetch=None) -> dict:
    """Check the key and the account, keep the key in the credential store, remember the account."""
    fetch = fetch or _fetch_once
    username, api_key = (username or "").strip(), (api_key or "").strip()
    if not username or len(username) > 64 or not username.replace("_", "").replace(".", "").replace("-", "").isalnum():
        raise RaError("That is not a RetroAchievements username.")
    if not api_key.isalnum() or not 16 <= len(api_key) <= 64:
        raise RaError(f"That is not a Web API key: it is letters and digits, shown on {KEY_PAGE}.")
    found = _ask(fetch, build_url(API + "API_GetUserProfile.php", u=username, y=api_key))
    if not isinstance(found, dict) or not found.get("ULID"):
        raise RaError(f"No RetroAchievements player called {username!r}.")
    credentials.set_secret(SECRET, profile.profile_id, api_key)
    link = {"username": found.get("User") or username, "ulid": found["ULID"]}
    _save_config(profile, link)
    _sync_file(profile).unlink(missing_ok=True)
    if profile.state()["accounts"].get("retroachievements", {}).get("account") != link["username"]:
        profile.record("platform.account_linked", {"adapter": "retroachievements", "account": link["username"]},
                       adapter="retroachievements")
    STATUS.pop(profile.profile_id, None)
    return link


def disconnect(profile: Profile) -> None:
    credentials.delete_secret(SECRET, profile.profile_id)
    _save_config(profile, None)
    _sync_file(profile).unlink(missing_ok=True)
    STATUS.pop(profile.profile_id, None)


class RaWatcher:
    """One RetroAchievements request per poll, at most one every MIN_GAP seconds (see the module)."""

    def __init__(self, profile: Profile, fetch=None, clock=time.monotonic):
        self.profile, self.clock = profile, clock
        self.fetch = fetch or (lambda url: _fetch_once(url))      # looked up at call time (tests replace it)
        self.queue: list = []
        self._listed_at = None
        self._last_request = None
        self._problems: list = []
        self._seen = self._load_seen()

    def _load_seen(self) -> dict:
        try:
            return json.loads(_sync_file(self.profile).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _status(self, **values) -> None:
        STATUS[self.profile.profile_id] = {**STATUS.get(self.profile.profile_id, {}), **values}

    def new_problems(self) -> list:
        out, self._problems = self._problems, []
        return out

    def poll(self) -> list[dict]:
        link = linked(self.profile)
        if not link:
            return []
        now = self.clock()
        if self._last_request is not None and now - self._last_request < MIN_GAP:
            return []
        if not self.queue:
            if self._listed_at is not None and now - self._listed_at < LIST_EVERY:
                return []
            self.queue.append(("list", 0, []))
        key = credentials.get_secret(SECRET, self.profile.profile_id)
        if not key:
            self._status(error="The saved Web API key is missing. Disconnect and connect again.", running=False)
            self.queue.clear()
            self._listed_at = now
            return []
        self._last_request = now
        try:
            written = self._step(link, key, self.queue[0])
        except RaError as exc:
            self._status(error=str(exc), running=False)
            self._problems.append(str(exc))
            self.queue.clear()
            self._listed_at = self.clock()                      # try again at the next look, not every round
            return []
        self.queue.pop(0)
        if not self.queue:
            self._listed_at = self.clock()
            self._status(running=False, last_sync=ev.now(), error=None)
        return written

    def _step(self, link: dict, key: str, task: tuple) -> list[dict]:
        if task[0] == "list":
            offset, found = task[1], task[2]
            page = _ask(self.fetch, build_url(API + "API_GetUserCompletionProgress.php", y=key, u=link["ulid"],
                                              c=500, o=offset))
            if not isinstance(page, dict):
                raise RaError("RetroAchievements sent a completion list this version cannot read.")
            results = page.get("Results") or []
            found += [r for r in results if _get(r, "GameID", "gameId") is not None]
            total = int(page.get("Total") or 0)
            if results and offset + len(results) < total:
                self.queue.append(("list", offset + len(results), found))
                return []
            packs = self.profile.state()["packs"]
            todo = [r for r in found
                    if self._seen.get(str(r["GameID"])) != str(r.get("MostRecentAwardedDate") or "")
                    or f"ra-{int(r['GameID'])}" not in packs]
            todo.sort(key=lambda r: str(r.get("MostRecentAwardedDate") or ""), reverse=True)   # latest play first
            self.queue.extend(("game", r) for r in todo)
            self._status(running=bool(todo), done=0, total=len(todo), titles=len(found), error=None)
            return []
        listing = task[1]
        info = _ask(self.fetch, build_url(API + "API_GetGameInfoAndUserProgress.php", y=key, g=listing["GameID"],
                                          u=link["ulid"], a=1))
        if not isinstance(info, dict):
            raise RaError("RetroAchievements sent a game this version cannot read.")
        events, _already = plan_game(self.profile, self.profile.state(),
                                     self.profile.index.external_ids("retroachievements"), link["username"],
                                     listing, info)
        if events:
            self.profile.commit(events)
        self._seen[str(listing["GameID"])] = str(listing.get("MostRecentAwardedDate") or "")
        write_json_atomic(_sync_file(self.profile), self._seen)
        st = STATUS.get(self.profile.profile_id, {})
        self._status(done=st.get("done", 0) + 1, current=_get(info, "Title", "title"))
        return [e for e in events if e["event_type"] == "achievement.unlocked"]


def status(profile: Profile) -> dict:
    link = linked(profile)
    return {"linked": bool(link), "username": link and link["username"],
            "has_key": bool(link and credentials.get_secret(SECRET, profile.profile_id)),
            "profile_url": link and f"https://retroachievements.org/user/{link['username']}",
            "sync": STATUS.get(profile.profile_id, {}), "key_page": KEY_PAGE}
