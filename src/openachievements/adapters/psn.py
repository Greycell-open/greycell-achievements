"""PlayStation trophies, read only (ADR 0006).

Sony offers no public trophy API and no sign-in for other apps. This uses the
same undocumented web API as Sony's own trophy pages and the community
libraries built on it, so it can change or stop without notice; the app says
so where you connect.

Connecting: the player signs in on playstation.com, opens
https://ca.account.sony.com/api/v1/ssocookie and pastes the "npsso" code it
shows. That code is exchanged once for an access token (an hour) and a refresh
token (about two months). Only the refresh token is kept, in the operating
system's credential store (credentials.py): never in the profile, an event, a
log or a sync. No password is ever asked for. A spare PSN account can read a
main account's trophies if those are visible to others; the dialog suggests it.

Syncing: one request per watcher round (about one every five seconds, under
Sony's rate limit), as a small queue: the trophy title list, then for each new
or changed title its trophy list and what was earned. Titles that did not
change since the last look are skipped. Unlocks keep Sony's earned time and an
external id, so nothing is ever counted twice. They sync quietly like Steam's.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import credentials
from .. import events as ev
from ..fsutil import write_json_atomic
from ..packs import PackError, split_for_events, validate_definitions
from ..profile import Profile, ProfileError
from .base import utc_iso

ADAPTER_VERSION = "0.1.0"
AUTH = "https://ca.account.sony.com/api/authz/v3/oauth"
CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
BASIC = "MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="
REDIRECT = "com.scee.psxandroid.scecompcall://redirect"
SCOPE = "psn:mobile.v2.core psn:clientapp"
TROPHY = "https://m.np.playstation.com/api/trophy/v1"
PROFILE = "https://us-prof.np.community.playstation.net/userProfile/v1/users/{}/profile2?fields=accountId,onlineId"
NPSSO_PAGE = "https://ca.account.sony.com/api/v1/ssocookie"
SECRET = "psn-refresh-token"
POINTS = {"bronze": 15, "silver": 30, "gold": 90, "platinum": 300}     # PSN's own trophy point values
LIST_EVERY = 300.0                  # look at the title list every five minutes
STATUS: dict = {}                   # profile id -> what the page shows while a sync runs


class PsnError(ProfileError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):          # the authorize step answers with a redirect to read
        return None


def _http(method: str, url: str, headers: dict, data: bytes | None = None) -> tuple[int, dict, bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""


# ---- the account link -------------------------------------------------------------------

def _config(profile: Profile) -> dict:
    return profile.config.load().get("psn", {}).get(profile.profile_id) or {}


def _save_config(profile: Profile, link: dict | None) -> None:
    with profile.config.editing() as config:
        mine = config.setdefault("psn", {})
        if link is None:
            mine.pop(profile.profile_id, None)
        else:
            mine[profile.profile_id] = link


def linked(profile: Profile) -> dict | None:
    link = _config(profile)
    return link if link.get("account_id") else None


def _sync_file(profile: Profile):
    return profile.config.dir / f"psn-sync-{profile.profile_id}.json"


class Session:
    """Tokens for one profile: the refresh token from the credential store, the
    access token in memory only."""

    def __init__(self, profile: Profile, http=None, clock=time.time):
        self.profile, self.clock = profile, clock
        self.http = http or (lambda *a, **k: _http(*a, **k))       # looked up at call time (tests replace it)
        self._access, self._expires = None, 0.0

    def _token_request(self, fields: dict) -> dict:
        status, _h, body = self.http("POST", f"{AUTH}/token", {
            "Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {BASIC}"},
            urllib.parse.urlencode(fields).encode())
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {}
        if status != 200 or not data.get("access_token"):
            raise PsnError("PlayStation did not accept the sign-in. Get a fresh code and connect again.")
        self._access, self._expires = data["access_token"], self.clock() + int(data.get("expires_in", 3600)) - 60
        if data.get("refresh_token"):
            credentials.set_secret(SECRET, self.profile.profile_id, data["refresh_token"])
        return data

    def sign_in(self, npsso: str) -> None:
        npsso = (npsso or "").strip().strip('"')
        if npsso.startswith("{"):                              # the whole page was pasted
            try:
                npsso = json.loads(npsso).get("npsso", "")
            except ValueError:
                npsso = ""
        if len(npsso) != 64 or not npsso.isalnum():
            raise PsnError("That is not an NPSSO code: it is 64 letters and digits, from " + NPSSO_PAGE)
        query = urllib.parse.urlencode({"access_type": "offline", "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
                                        "response_type": "code", "scope": SCOPE})
        status, headers, _b = self.http("GET", f"{AUTH}/authorize?{query}", {"Cookie": f"npsso={npsso}"})
        location = next((v for k, v in headers.items() if k.lower() == "location"), "")
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).get("code", [None])[0]
        if not code:
            raise PsnError("PlayStation did not accept that code. Sign in on playstation.com, open "
                           f"{NPSSO_PAGE} again and copy the new code.")
        self._token_request({"code": code, "redirect_uri": REDIRECT, "grant_type": "authorization_code",
                             "token_format": "jwt"})

    def access(self) -> str:
        if self._access and self.clock() < self._expires:
            return self._access
        refresh = credentials.get_secret(SECRET, self.profile.profile_id)
        if not refresh:
            raise PsnError("Not connected to PlayStation.")
        try:
            self._token_request({"refresh_token": refresh, "grant_type": "refresh_token",
                                 "token_format": "jwt", "scope": SCOPE})
        except PsnError:
            raise PsnError("The PlayStation connection expired (it lasts about two months). "
                           "Connect again with a fresh code.") from None
        return self._access

    def get(self, url: str) -> dict:
        for attempt in (1, 2):
            status, _h, body = self.http("GET", url, {"Authorization": f"Bearer {self.access()}",
                                                      "Accept-Language": "en-US"})
            if status == 401 and attempt == 1:
                self._access = None                              # expired early: refresh once
                continue
            break
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {}
        if status == 429:
            raise PsnError("PlayStation asked to slow down; the sync waits and tries again.")
        if status in (403, 404) or (isinstance(data, dict) and data.get("error")):
            raise PsnError("PlayStation would not show these trophies. If you connected with another account, "
                           "set your trophies to visible to anyone in your PlayStation privacy settings.")
        if status != 200:
            raise PsnError(f"PlayStation answered {status}.")
        return data


def connect(profile: Profile, npsso: str, online_id: str | None = None, http=None) -> dict:
    """Sign in with an NPSSO code, find whose trophies to read, and remember it."""
    session = Session(profile, http)
    session.sign_in(npsso)
    who = (online_id or "").strip() or "me"
    data = session.get(PROFILE.format(urllib.parse.quote(who)))
    found = data.get("profile") or {}
    if not found.get("accountId"):
        raise PsnError(f"No PlayStation player called {who!r}.")
    link = {"account_id": str(found["accountId"]), "online_id": found.get("onlineId") or who,
            "own_account": who == "me"}
    _save_config(profile, link)
    _sync_file(profile).unlink(missing_ok=True)
    if profile.state()["accounts"].get("psn", {}).get("account") != link["online_id"]:
        profile.record("platform.account_linked", {"adapter": "psn", "account": link["online_id"]}, adapter="psn")
    STATUS.pop(profile.profile_id, None)
    return link


def disconnect(profile: Profile) -> None:
    credentials.delete_secret(SECRET, profile.profile_id)
    _save_config(profile, None)
    _sync_file(profile).unlink(missing_ok=True)
    STATUS.pop(profile.profile_id, None)


# ---- turning trophies into the library ----------------------------------------------------

def game_id_for(np_id: str) -> str:
    return "psn-" + np_id.lower()


def plan_title(profile: Profile, state: dict, known: set, link: dict, title: dict,
               trophies: list, earned: list) -> list[dict]:
    """Events for one trophy title: register the game, install or update its
    trophy list, and the unlocks not seen before."""
    np_id = title["npCommunicationId"]
    game_id = game_id_for(np_id)
    make = lambda kind, payload, **kw: ev.make_event(kind, profile_id=profile.profile_id, device_id=profile.device_id,
                                                     payload=payload, adapter="psn",
                                                     adapter_version=ADAPTER_VERSION, **kw)
    out = []
    last = utc_iso(title.get("lastUpdatedDateTime"))
    platform = title.get("trophyTitlePlatform") or "PlayStation"
    if game_id not in state["games"]:
        out.append(make("game.registered", {"title": title.get("trophyTitleName") or np_id, "platform": platform,
                                            "external_ids": {"psn": np_id}, **({"last_played": last} if last else {})},
                        game_id=game_id))
    elif last and last > (state["games"][game_id].get("last_played") or ""):
        out.append(make("game.metadata_updated", {"last_played": last}, game_id=game_id))
    rows = [{"id": f"t{t['trophyId']}", "name": t.get("trophyName") or f"Trophy {t['trophyId']}",
             "description": t.get("trophyDetail") or "", "hidden": bool(t.get("trophyHidden")),
             "points": POINTS.get(t.get("trophyType"), 0), "category": t.get("trophyType") or None,
             "icon": t.get("trophyIconUrl") or None} for t in trophies if "trophyId" in t]
    installed = state["packs"].get(game_id)
    fields = ("name", "description", "hidden", "points", "icon")
    current = {k: {f: v.get(f) for f in fields} for k, v in (installed or {}).get("achievements", {}).items()}
    wanted = {r["id"]: {f: r.get(f) for f in fields} for r in rows}
    if rows and (not installed or installed.get("removed") or current != wanted):
        meta = {"id": game_id, "name": title.get("trophyTitleName") or np_id, "game_ids": [game_id],
                "version": str(title.get("trophySetVersion") or "1.0.0"), "source": "psn",
                "supported_adapters": ["psn"]}
        try:
            description = validate_definitions(meta, rows)
        except PackError:
            return out
        kind = "pack.updated" if installed and not installed.get("removed") else "pack.installed"
        out.extend(make(kind, payload) for payload in split_for_events(description))
    ids = {r["id"] for r in rows}
    for t in earned:
        if not t.get("earned") or f"t{t.get('trophyId')}" not in ids:
            continue
        external = f"{link['account_id']}:{np_id}:{t['trophyId']}"
        if external in known:
            continue
        out.append(make("achievement.unlocked", {"provenance": "imported", "mode": "psn"},
                        achievement_id=f"{game_id}:t{t['trophyId']}", game_id=game_id,
                        occurred_at=utc_iso(t.get("earnedDateTime")) or ev.now(),
                        external_account_id=link["account_id"], external_event_id=external))
    return out


class PsnWatcher:
    """One PlayStation request per poll, from a small queue (see the module)."""

    def __init__(self, profile: Profile, session: Session | None = None, clock=time.monotonic):
        self.profile, self.clock = profile, clock
        self.session = session or Session(profile)
        self.queue: list = []
        self.pending: dict = {}
        self._listed_at = None
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

    def _url(self, title: dict, who: str | None) -> str:
        np_id = title["npCommunicationId"]
        base = f"{TROPHY}/users/{who}/npCommunicationIds/{np_id}" if who else f"{TROPHY}/npCommunicationIds/{np_id}"
        legacy = title.get("npServiceName") == "trophy"           # PS3, PS4, Vita; PS5 needs none
        return f"{base}/trophyGroups/all/trophies" + ("?npServiceName=trophy" if legacy else "")

    def poll(self) -> list[dict]:
        link = linked(self.profile)
        if not link:
            return []
        if not self.queue:
            if self._listed_at is not None and self.clock() - self._listed_at < LIST_EVERY:
                return []
            self.queue.append(("list", 0))
        task = self.queue[0]
        try:
            written = self._step(link, task)
        except PsnError as exc:
            self._status(error=str(exc), running=False)
            self._problems.append(f"PlayStation: {exc}")
            self.queue.clear()
            self._listed_at = self.clock()                      # try again at the next look, not every round
            return []
        self.queue.pop(0)
        if not self.queue:
            self._listed_at = self.clock()
            self._status(running=False, last_sync=ev.now(), error=None)
        return written

    def _step(self, link: dict, task: tuple) -> list[dict]:
        kind = task[0]
        if kind == "list":
            offset = task[1]
            data = self.session.get(f"{TROPHY}/users/{link['account_id']}/trophyTitles?limit=800&offset={offset}")
            titles = data.get("trophyTitles") or []
            packs = self.profile.state()["packs"]
            for t in titles:
                np_id = t.get("npCommunicationId")
                if not np_id:
                    continue
                gid = game_id_for(np_id)
                if self._seen.get(np_id) != t.get("lastUpdatedDateTime") or gid not in packs:
                    self.queue.append(("defs", t))
                    self.queue.append(("earned", t))
            total = int(data.get("totalItemCount") or len(titles))
            if offset + len(titles) < total and titles:
                self.queue.append(("list", offset + len(titles)))
            todo = sum(1 for q in self.queue if q[0] == "earned")
            self._status(running=bool(todo), done=0, total=todo, titles=total, error=None)
            return []
        title = task[1]
        np_id = title["npCommunicationId"]
        if kind == "defs":
            self.pending[np_id] = self.session.get(self._url(title, None)).get("trophies") or []
            return []
        earned = self.session.get(self._url(title, link["account_id"])).get("trophies") or []
        events = plan_title(self.profile, self.profile.state(), self.profile.index.external_ids("psn"), link,
                            title, self.pending.pop(np_id, []), earned)
        if events:
            self.profile.commit(events)
        self._seen[np_id] = title.get("lastUpdatedDateTime")
        write_json_atomic(_sync_file(self.profile), self._seen)
        st = STATUS.get(self.profile.profile_id, {})
        self._status(done=st.get("done", 0) + 1, current=title.get("trophyTitleName"))
        return [e for e in events if e["event_type"] == "achievement.unlocked"]


def status(profile: Profile) -> dict:
    link = linked(profile)
    return {"linked": bool(link), "online_id": link and link["online_id"],
            "own_account": bool(link and link.get("own_account")),
            "has_session": bool(link and credentials.get_secret(SECRET, profile.profile_id)),
            "sync": STATUS.get(profile.profile_id, {}), "npsso_page": NPSSO_PAGE}
