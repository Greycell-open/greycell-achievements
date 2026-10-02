"""GOG achievements, read only, from the player's public GOG profile (ADR 0008).

GOG's achievement API (gameplay.gog.com) only accepts tokens issued to GOG
Galaxy's own client, and using Galaxy's client id would be impersonating it.
GOG profiles, though, are public pages: gog.com/u/<username> lists every game
with its playtime and achievement percentage as JSON, and each game's profile
page carries the full achievement list with unlock times. That is all this
reads. No sign-in, no password, no token, nothing secret is stored: linking is
the GOG username, and the profile has to be visible to everyone (GOG: Settings,
Privacy). GOG could change these pages without notice; the dialog says so.

Syncing: one request per watcher round and at most one every ten seconds, as a
small queue like PlayStation's: the games list, then the profile page of each
game whose achievement percentage or last session changed. Unlocks keep GOG's
unlock time and an external id, so nothing is counted twice. They sync quietly
like Steam's.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import events as ev
from ..fsutil import write_json_atomic
from ..packs import PackError, split_for_events, validate_definitions
from ..profile import Profile, ProfileError
from .base import utc_iso

ADAPTER_VERSION = "0.1.0"
SITE = "https://www.gog.com"
USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MIN_GAP = 10.0                      # seconds between two requests to GOG
LIST_EVERY = 300.0                  # look at the games list every five minutes
STATUS: dict = {}                   # profile id -> what the page shows while a sync runs
_DATA = re.compile(r"window\.profilesData\.(\w+)\s*=\s*")


class GogError(ProfileError):
    pass


def _http(method: str, url: str, headers: dict, data: bytes | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def page_data(html: str, name: str):
    """One `window.profilesData.<name> = ...;` value from a GOG profile page, or None."""
    for m in _DATA.finditer(html):
        if m.group(1) == name:
            try:
                return json.JSONDecoder().raw_decode(html, m.end())[0]
            except ValueError:
                return None
    return None


# ---- the account link -------------------------------------------------------------------

def _config(profile: Profile) -> dict:
    return profile.config.load().get("gog", {}).get(profile.profile_id) or {}


def _save_config(profile: Profile, link: dict | None) -> None:
    with profile.config.editing() as config:
        mine = config.setdefault("gog", {})
        if link is None:
            mine.pop(profile.profile_id, None)
        else:
            mine[profile.profile_id] = link


def linked(profile: Profile) -> dict | None:
    link = _config(profile)
    return link if link.get("user_id") else None


def _sync_file(profile: Profile):
    return profile.config.dir / f"gog-sync-{profile.profile_id}.json"


class Client:
    """Plain requests to public gog.com pages, never two within MIN_GAP."""

    def __init__(self, http=None, clock=time.monotonic):
        self.http = http or (lambda *a, **k: _http(*a, **k))       # looked up at call time (tests replace it)
        self.clock = clock

    def get(self, url: str, json_wanted: bool) -> tuple[int, bytes]:
        status, _h, body = self.http("GET", url, {
            "Accept": "application/json" if json_wanted else "text/html",
            "Accept-Language": "en-US", "User-Agent": f"GreycellAchievements/{ADAPTER_VERSION}"})
        if status == 429:
            raise GogError("GOG asked to slow down; the sync waits and tries again.")
        return status, body

    def games(self, username: str, page: int) -> dict:
        status, body = self.get(f"{SITE}/u/{urllib.parse.quote(username)}/games/stats?"
                                f"sort=recent_playtime&order=desc&page={page}", True)
        if status == 404:
            raise GogError(f"No GOG profile called {username!r}.")
        if status == 403:
            raise GogError("GOG would not show this profile's games. On gog.com open Settings, Privacy, and "
                           "set your profile to visible to everyone.")
        if status != 200:
            raise GogError(f"GOG answered {status}.")
        try:
            return json.loads(body)
        except ValueError:
            raise GogError("GOG sent a games list this version cannot read.") from None

    def game_page(self, username: str, product_id: str) -> str:
        status, body = self.get(f"{SITE}/u/{urllib.parse.quote(username)}/game/{product_id}", False)
        if status != 200:
            raise GogError(f"GOG answered {status} for a game page.")
        return body.decode("utf-8", "replace")


def connect(profile: Profile, username: str, http=None) -> dict:
    """Check the profile exists and its games are visible, then remember it."""
    username = (username or "").strip().rstrip("/").rsplit("/u/", 1)[-1].split("/")[0]   # a pasted link works too
    if not USERNAME.match(username):
        raise GogError("That is not a GOG username: letters, digits, dots, dashes and underscores.")
    client = Client(http)
    status, body = client.get(f"{SITE}/u/{urllib.parse.quote(username)}", False)
    user = page_data(body.decode("utf-8", "replace"), "profileUser") if status == 200 else None
    if not user or not user.get("userId"):
        raise GogError(f"No GOG profile called {username!r}.")
    client.games(username, 1)                                   # refuses a private profile with the reason
    link = {"username": user.get("username") or username, "user_id": str(user["userId"])}
    _save_config(profile, link)
    _sync_file(profile).unlink(missing_ok=True)
    if profile.state()["accounts"].get("gog", {}).get("account") != link["username"]:
        profile.record("platform.account_linked", {"adapter": "gog", "account": link["username"]}, adapter="gog")
    STATUS.pop(profile.profile_id, None)
    return link


def disconnect(profile: Profile) -> None:
    _save_config(profile, None)
    _sync_file(profile).unlink(missing_ok=True)
    STATUS.pop(profile.profile_id, None)


# ---- turning a profile into the library ---------------------------------------------------

def game_id_for(product_id: str) -> str:
    return f"gog-{product_id}"


def _mine(stats, user_id: str) -> dict:
    return (stats or {}).get(user_id) or {} if isinstance(stats, dict) else {}


def plan_game(profile: Profile, state: dict, known: set, link: dict, item: dict, achievements: list) -> list[dict]:
    """Events for one GOG game: register it, install or update its achievement
    list, and the unlocks not seen before."""
    game, product_id = item["game"], str(item["game"]["id"])
    game_id = game_id_for(product_id)
    make = lambda kind, payload, **kw: ev.make_event(kind, profile_id=profile.profile_id, device_id=profile.device_id,
                                                     payload=payload, adapter="gog",
                                                     adapter_version=ADAPTER_VERSION, **kw)
    out = []
    mine = _mine(item.get("stats"), link["user_id"])
    last = utc_iso(mine.get("lastSession"))
    title = game.get("title") or product_id
    if game_id not in state["games"]:
        out.append(make("game.registered", {"title": title, "platform": "GOG", "external_ids": {"gog": product_id},
                                            **({"last_played": last} if last else {})}, game_id=game_id))
    elif last and last > (state["games"][game_id].get("last_played") or ""):
        out.append(make("game.metadata_updated", {"last_played": last}, game_id=game_id))
    rows = []
    for a in achievements:
        info = a.get("achievement") or {}
        if not info.get("id"):
            continue
        rows.append({"id": f"a{info['id']}", "name": (info.get("name") or "Achievement")[:120],
                     "description": (info.get("description") or "")[:500], "hidden": not info.get("visible", True),
                     "points": 0, "icon": info.get("imageUrlUnlocked") or info.get("imageUrlLocked") or None})
    installed = state["packs"].get(game_id)
    fields = ("name", "description", "hidden", "points", "icon")
    current = {k: {f: v.get(f) for f in fields} for k, v in (installed or {}).get("achievements", {}).items()}
    wanted = {r["id"]: {f: r.get(f) for f in fields} for r in rows}
    if rows and (not installed or installed.get("removed") or current != wanted):
        meta = {"id": game_id, "name": title, "game_ids": [game_id], "version": "1.0.0", "source": "gog",
                "supported_adapters": ["gog"]}
        try:
            description = validate_definitions(meta, rows)
        except PackError:
            return out
        kind = "pack.updated" if installed and not installed.get("removed") else "pack.installed"
        out.extend(make(kind, payload) for payload in split_for_events(description))
    ids = {r["id"] for r in rows}
    for a in achievements:
        info, got = a.get("achievement") or {}, _mine(a.get("stats"), link["user_id"])
        if not got.get("isUnlocked") or f"a{info.get('id')}" not in ids:
            continue
        external = f"{link['user_id']}:{product_id}:{info['id']}"
        if external in known:
            continue
        out.append(make("achievement.unlocked", {"provenance": "imported", "mode": "gog"},
                        achievement_id=f"{game_id}:a{info['id']}", game_id=game_id,
                        occurred_at=utc_iso(got.get("unlockDate")) or ev.now(),
                        external_account_id=link["user_id"], external_event_id=external))
    return out


class GogWatcher:
    """One GOG request per poll, at most one every MIN_GAP seconds (see the module)."""

    def __init__(self, profile: Profile, client: Client | None = None, clock=time.monotonic):
        self.profile, self.clock = profile, clock
        self.client = client or Client()
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
            self.queue.append(("list", 1, []))
        self._last_request = now
        try:
            written = self._step(link, self.queue[0])
        except GogError as exc:
            self._status(error=str(exc), running=False)
            self._problems.append(f"GOG: {exc}")
            self.queue.clear()
            self._listed_at = self.clock()                      # try again at the next look, not every round
            return []
        self.queue.pop(0)
        if not self.queue:
            self._listed_at = self.clock()
            self._status(running=False, last_sync=ev.now(), error=None)
        return written

    @staticmethod
    def _stamp(item: dict, user_id: str) -> str:
        mine = _mine(item.get("stats"), user_id)
        return f"{mine.get('achievementsPercentage')}|{mine.get('lastSession')}"

    def _step(self, link: dict, task: tuple) -> list[dict]:
        if task[0] == "list":
            page, found = task[1], task[2]
            data = self.client.games(link["username"], page)
            items = (data.get("_embedded") or {}).get("items") or []
            found += [i for i in items if (i.get("game") or {}).get("achievementSupport") and (i["game"].get("id"))]
            if page < int(data.get("pages") or 1) and items:
                self.queue.append(("list", page + 1, found))
                return []
            packs = self.profile.state()["packs"]
            todo = [i for i in found if self._seen.get(str(i["game"]["id"])) != self._stamp(i, link["user_id"])
                    or game_id_for(str(i["game"]["id"])) not in packs]
            # games with progress first, the rest after (a long library still shows what matters soon)
            todo.sort(key=lambda i: not _mine(i.get("stats"), link["user_id"]).get("achievementsPercentage"))
            self.queue.extend(("game", i) for i in todo)
            self._status(running=bool(todo), done=0, total=len(todo), titles=len(found), error=None)
            return []
        item = task[1]
        product_id = str(item["game"]["id"])
        html = self.client.game_page(link["username"], product_id)
        achievements = page_data(html, "achievements")
        if not isinstance(achievements, list):
            raise GogError("GOG changed its profile pages; this version cannot read them. Update the app.")
        events = plan_game(self.profile, self.profile.state(), self.profile.index.external_ids("gog"), link,
                           item, achievements)
        if events:
            self.profile.commit(events)
        self._seen[product_id] = self._stamp(item, link["user_id"])
        write_json_atomic(_sync_file(self.profile), self._seen)
        st = STATUS.get(self.profile.profile_id, {})
        self._status(done=st.get("done", 0) + 1, current=item["game"].get("title"))
        return [e for e in events if e["event_type"] == "achievement.unlocked"]


def status(profile: Profile) -> dict:
    link = linked(profile)
    return {"linked": bool(link), "username": link and link["username"],
            "profile_url": link and f"{SITE}/u/{urllib.parse.quote(link['username'])}",
            "sync": STATUS.get(profile.profile_id, {})}
