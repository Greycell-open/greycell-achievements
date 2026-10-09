"""Xbox achievements, read only (ADR 0007).

Signing in is Microsoft's own: the player logs in on Microsoft's page with this
app's own registration (a public client, no secret, PKCE), granting
XboxLive.signin and offline_access. The app never sees the password. The
Microsoft token is exchanged for an Xbox Live user token and then an XSTS
token, which name the player (gamertag, XUID) and authorise reads from Xbox
Live. Only Microsoft's refresh token is kept, in the operating system's
credential store (credentials.py); the rest lives in memory.

The app uses its own registration (the player's Azure app id, set once), never
the client id of Microsoft's own apps: borrowing another client is
impersonation, which this project does not do.

Syncing mirrors PlayStation's: one request per poll, at most one poll every
ten seconds, backing off when Xbox asks (429 and Retry-After). The title
history comes first; each new or changed title then costs one request
(Xbox One, Series, PC) or two (Xbox 360, whose achievements live on the older
contract). Unlocks keep Xbox's time and an external id, and sync quietly.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
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
MS = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPE = "XboxLive.signin offline_access"
USER_AUTH = "https://user.auth.xboxlive.com/user/authenticate"
XSTS = "https://xsts.auth.xboxlive.com/xsts/authorize"
TITLES = "https://titlehub.xboxlive.com/users/xuid({xuid})/titles/titlehistory/decoration/achievement?maxItems=1000"
ACHIEVEMENTS = "https://achievements.xboxlive.com/users/xuid({xuid})/achievements?titleId={title}&maxItems=1000"
TITLE_ACHIEVEMENTS = "https://achievements.xboxlive.com/users/xuid({xuid})/titleachievements?titleId={title}&maxItems=1000"
SECRET = "xbox-refresh-token"
_CLIENT = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MIN_GAP = 10.0                      # seconds between two Xbox requests
LIST_EVERY = 300.0
STATE_TTL = 600
STATUS: dict = {}
XERR = {2148916233: "This Microsoft account has no Xbox profile yet. Sign in on xbox.com once, then try again.",
        2148916235: "Xbox Live is not available in this account's country.",
        2148916236: "This account needs adult verification on xbox.com first.",
        2148916238: "This is a child account: an adult has to add it to a Microsoft family first."}


class XboxError(ProfileError):
    pass


def _http(method: str, url: str, headers: dict, data: bytes | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


# ---- settings and the link ------------------------------------------------------------------

def _config(profile: Profile) -> dict:
    return profile.config.load().get("xbox", {}).get(profile.profile_id) or {}


def _save(profile: Profile, **values) -> dict:
    with profile.config.editing() as config:
        mine = config.setdefault("xbox", {}).setdefault(profile.profile_id, {})
        for k, v in values.items():
            if v is None:
                mine.pop(k, None)
            else:
                mine[k] = v
    return mine


def set_client(profile: Profile, client_id: str) -> str:
    client_id = (client_id or "").strip()
    if not _CLIENT.match(client_id):
        raise XboxError("That is not an Application (client) ID: it looks like 1a2b3c4d-1234-5678-9abc-0123456789ab.")
    _save(profile, client_id=client_id.lower())
    return client_id.lower()


def linked(profile: Profile) -> dict | None:
    c = _config(profile)
    return c if c.get("xuid") else None


def _sync_file(profile: Profile):
    return profile.config.dir / f"xbox-sync-{profile.profile_id}.json"


# ---- Microsoft sign-in (authorization code + PKCE) ------------------------------------------

class SignIn:
    """Single-use states with their PKCE verifiers, in memory."""

    def __init__(self, clock=time.monotonic):
        self.clock, self._states = clock, {}

    def start(self, profile: Profile, redirect_uri: str) -> str:
        client = _config(profile).get("client_id")
        if not client:
            raise XboxError("Paste the app's Application (client) ID first.")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        now = self.clock()
        self._states = {k: v for k, v in self._states.items() if now - v[1] < STATE_TTL}
        self._states[state] = (verifier, now, redirect_uri)
        return f"{MS}/authorize?" + urllib.parse.urlencode({
            "client_id": client, "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPE,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"})

    def finish(self, profile: Profile, params: dict, http=None) -> dict:
        if params.get("error"):
            raise XboxError("Microsoft sign-in was cancelled." if params.get("error") == "access_denied"
                            else f"Microsoft sign-in failed: {params.get('error_description') or params['error']}")
        entry = self._states.pop(params.get("state") or "", None)
        if not entry or self.clock() - entry[1] > STATE_TTL:
            raise XboxError("That sign-in was not started here, or took too long. Try again.")
        verifier, _t, redirect_uri = entry
        session = Session(profile, http)
        session.ms_token({"grant_type": "authorization_code", "code": params.get("code") or "",
                          "redirect_uri": redirect_uri, "code_verifier": verifier})
        who = session.xsts()
        link = _save(profile, xuid=who["xuid"], gamertag=who["gamertag"])
        _sync_file(profile).unlink(missing_ok=True)
        if profile.state()["accounts"].get("xbox", {}).get("account") != who["gamertag"]:
            profile.record("platform.account_linked", {"adapter": "xbox", "account": who["gamertag"]}, adapter="xbox")
        STATUS.pop(profile.profile_id, None)
        return link


class Session:
    def __init__(self, profile: Profile, http=None, clock=time.time):
        self.profile, self.clock = profile, clock
        self.http = http or (lambda *a, **k: _http(*a, **k))       # looked up at call time (tests replace it)
        self._auth, self._expires = None, 0.0

    def ms_token(self, fields: dict) -> str:
        client = _config(self.profile).get("client_id")
        if not client:
            raise XboxError("The app's Application (client) ID is missing.")
        status, _h, body = self.http("POST", f"{MS}/token", {"Content-Type": "application/x-www-form-urlencoded"},
                                     urllib.parse.urlencode({"client_id": client, "scope": SCOPE, **fields}).encode())
        data = _json(body)
        if status != 200 or not data.get("access_token"):
            hint = data.get("error_description", "")
            if "AADSTS7000218" in hint or "client_assertion" in hint:
                raise XboxError("In the app registration, turn on Authentication, 'Allow public client flows'.")
            if "redirect_uri" in hint:
                raise XboxError("The app registration's redirect URI must be http://localhost:8788/v1/local/xbox/return "
                                "under 'Mobile and desktop applications'.")
            raise XboxError("Microsoft did not accept the sign-in. Sign in with Microsoft again.")
        if data.get("refresh_token"):
            credentials.set_secret(SECRET, self.profile.profile_id, data["refresh_token"])
        self._ms = data["access_token"]
        return self._ms

    def xsts(self) -> dict:
        status, _h, body = self.http("POST", USER_AUTH, {"Content-Type": "application/json", "x-xbl-contract-version": "1"},
                                     json.dumps({"Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                                                                "RpsTicket": f"d={self._ms}"},
                                                 "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"}).encode())
        user = _json(body)
        if status != 200 or not user.get("Token"):
            raise XboxError("Xbox Live did not accept the Microsoft sign-in.")
        status, _h, body = self.http("POST", XSTS, {"Content-Type": "application/json", "x-xbl-contract-version": "1"},
                                     json.dumps({"Properties": {"SandboxId": "RETAIL", "UserTokens": [user["Token"]]},
                                                 "RelyingParty": "http://xboxlive.com", "TokenType": "JWT"}).encode())
        data = _json(body)
        if status != 200 or not data.get("Token"):
            raise XboxError(XERR.get(data.get("XErr"), "Xbox Live refused this account."))
        claims = (data.get("DisplayClaims", {}).get("xui") or [{}])[0]
        self._auth = f"XBL3.0 x={claims.get('uhs')};{data['Token']}"
        self._expires = self.clock() + 3000                       # XSTS lasts hours; renew well before
        return {"xuid": str(claims.get("xid") or ""), "gamertag": claims.get("gtg") or ""}

    def auth(self) -> str:
        if self._auth and self.clock() < self._expires:
            return self._auth
        refresh = credentials.get_secret(SECRET, self.profile.profile_id)
        if not refresh:
            raise XboxError("Not connected to Xbox.")
        try:
            self.ms_token({"grant_type": "refresh_token", "refresh_token": refresh})
        except XboxError:
            raise XboxError("The Xbox connection expired. Sign in with Microsoft again.") from None
        self.xsts()
        return self._auth

    def get(self, url: str, contract: str) -> dict:
        for attempt in (1, 2):
            status, headers, body = self.http("GET", url, {"Authorization": self.auth(), "x-xbl-contract-version": contract,
                                                           "Accept-Language": "en-US", "Accept": "application/json"})
            if status == 401 and attempt == 1:
                self._auth = None
                continue
            break
        if status == 429:
            retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), "60")
            raise RateLimited(float(retry) if str(retry).isdigit() else 60.0)
        if status in (403, 404):
            raise XboxError("Xbox Live would not show these achievements.")
        if status != 200:
            raise XboxError(f"Xbox Live answered {status}.")
        return _json(body)


class RateLimited(XboxError):
    def __init__(self, seconds: float):
        super().__init__(f"Xbox asked to slow down; waiting {int(seconds)} s.")
        self.seconds = seconds


def disconnect(profile: Profile) -> None:
    credentials.delete_secret(SECRET, profile.profile_id)
    _save(profile, xuid=None, gamertag=None)
    _sync_file(profile).unlink(missing_ok=True)
    STATUS.pop(profile.profile_id, None)


# ---- achievements into the library ------------------------------------------------------------

def game_id_for(title_id) -> str:
    return f"xbox-{int(title_id)}"


def _icon(a: dict) -> str | None:
    for m in a.get("mediaAssets") or []:
        if m.get("type") == "Icon" and m.get("url"):
            return m["url"]
    return None


def _gamerscore(a: dict) -> int:
    if isinstance(a.get("gamerscore"), int):
        return a["gamerscore"]
    for r in a.get("rewards") or []:
        if r.get("type") == "Gamerscore":
            try:
                return int(r.get("value") or 0)
            except ValueError:
                return 0
    return 0


def plan_title(profile: Profile, state: dict, known: set, link: dict, title: dict, achievements: list,
               unlocked_360: list | None = None) -> list[dict]:
    """Events for one title: the game, its achievement list, and new unlocks.
    Modern titles give every achievement with the player's progress; Xbox 360
    titles give the full list and the unlocked ones separately."""
    tid = int(title["titleId"])
    game_id = game_id_for(tid)
    make = lambda kind, payload, **kw: ev.make_event(kind, profile_id=profile.profile_id, device_id=profile.device_id,
                                                     payload=payload, adapter="xbox",
                                                     adapter_version=ADAPTER_VERSION, **kw)
    out = []
    devices = title.get("devices") or []
    platform = "Xbox 360" if devices == ["Xbox360"] else ("PC (Xbox)" if devices == ["PC"] else "Xbox")
    last = utc_iso((title.get("titleHistory") or {}).get("lastTimePlayed"))
    if game_id not in state["games"]:
        out.append(make("game.registered", {"title": title.get("name") or f"Xbox title {tid}", "platform": platform,
                                            "external_ids": {"xbox": tid}, **({"last_played": last} if last else {})},
                        game_id=game_id))
    elif last and last > (state["games"][game_id].get("last_played") or ""):
        out.append(make("game.metadata_updated", {"last_played": last}, game_id=game_id))
    rows = [{"id": f"a{a['id']}", "name": a.get("name") or f"Achievement {a['id']}",
             "description": a.get("description") or a.get("lockedDescription") or "",
             "hidden": bool(a.get("isSecret")), "points": _gamerscore(a), "icon": _icon(a)}
            for a in achievements if str(a.get("id", "")).isdigit()]
    from .. import rarity                                 # v2 answers carry rarity; Xbox 360 titles do not
    rarity.keep(game_id, [(f"a{a['id']}", a.get("name"), (a.get("rarity") or {}).get("currentPercentage"))
                          for a in achievements if str(a.get("id", "")).isdigit()])
    installed = state["packs"].get(game_id)
    fields = ("name", "description", "hidden", "points", "icon")
    current = {k: {f: v.get(f) for f in fields} for k, v in (installed or {}).get("achievements", {}).items()}
    wanted = {r["id"]: {f: r.get(f) for f in fields} for r in rows}
    if rows and (not installed or installed.get("removed") or current != wanted):
        meta = {"id": game_id, "name": title.get("name") or f"Xbox title {tid}", "game_ids": [game_id],
                "source": "xbox", "supported_adapters": ["xbox"]}
        try:
            description = validate_definitions(meta, rows)
        except PackError:
            return out
        kind = "pack.updated" if installed and not installed.get("removed") else "pack.installed"
        out.extend(make(kind, payload) for payload in split_for_events(description))
    ids = {r["id"] for r in rows}
    if unlocked_360 is not None:
        won = [(a, a.get("timeUnlocked")) for a in unlocked_360 if a.get("unlocked") or a.get("timeUnlocked")]
    else:
        won = [(a, (a.get("progression") or {}).get("timeUnlocked")) for a in achievements
               if a.get("progressState") == "Achieved"]
    for a, when in won:
        aid = f"a{a.get('id')}"
        external = f"{link['xuid']}:{tid}:{a.get('id')}"
        if aid not in ids or external in known:
            continue
        out.append(make("achievement.unlocked", {"provenance": "imported", "mode": "xbox"},
                        achievement_id=f"{game_id}:{aid}", game_id=game_id,
                        occurred_at=utc_iso(when) or ev.now(), external_account_id=link["xuid"],
                        external_event_id=external))
    return out


class XboxWatcher:
    def __init__(self, profile: Profile, session: Session | None = None, clock=time.monotonic):
        self.profile, self.clock = profile, clock
        self.session = session or Session(profile)
        self.queue: list = []
        self.pending: dict = {}
        self._listed_at = None
        self._next_at = 0.0
        self._problems: list = []
        try:
            self._seen = json.loads(_sync_file(profile).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._seen = {}

    def _status(self, **values) -> None:
        STATUS[self.profile.profile_id] = {**STATUS.get(self.profile.profile_id, {}), **values}

    def new_problems(self) -> list:
        out, self._problems = self._problems, []
        return out

    def poll(self) -> list[dict]:
        link = linked(self.profile)
        now = self.clock()
        if not link or now < self._next_at:
            return []
        if not self.queue:
            if self._listed_at is not None and now - self._listed_at < LIST_EVERY:
                return []
            self.queue.append(("list",))
        try:
            written = self._step(link, self.queue[0])
        except RateLimited as exc:
            self._next_at = now + exc.seconds                    # keep the queue; resume after the wait
            self._status(error=str(exc))
            return []
        except XboxError as exc:
            self._status(error=str(exc), running=False)
            self._problems.append(f"Xbox: {exc}")
            self.queue.clear()
            self._listed_at = now
            return []
        self._next_at = now + MIN_GAP
        self.queue.pop(0)
        if not self.queue:
            self._listed_at = now
            self._status(running=False, last_sync=ev.now(), error=None)
        return written

    def _step(self, link: dict, task: tuple) -> list[dict]:
        if task[0] == "list":
            data = self.session.get(TITLES.format(xuid=link["xuid"]), "2")
            titles = [t for t in data.get("titles") or [] if (t.get("achievement") or {}).get("totalAchievements")]
            packs = self.profile.state()["packs"]
            for t in titles:
                stamp = json.dumps(t.get("achievement") or {}, sort_keys=True)
                if self._seen.get(str(t["titleId"])) != stamp or game_id_for(t["titleId"]) not in packs:
                    if (t.get("devices") or []) == ["Xbox360"]:
                        self.queue.append(("all360", t))
                    self.queue.append(("title", t))
            todo = sum(1 for q in self.queue if q[0] == "title")
            self._status(running=bool(todo), done=0, total=todo, titles=len(titles), error=None)
            return []
        title = task[1]
        tid = str(title["titleId"])
        if task[0] == "all360":
            self.pending[tid] = self.session.get(TITLE_ACHIEVEMENTS.format(xuid=link["xuid"], title=tid), "1") \
                .get("achievements") or []
            return []
        legacy = tid in self.pending
        got = self.session.get(ACHIEVEMENTS.format(xuid=link["xuid"], title=tid), "1" if legacy else "2") \
            .get("achievements") or []
        events = plan_title(self.profile, self.profile.state(), self.profile.index.external_ids("xbox"), link, title,
                            self.pending.pop(tid) if legacy else got, got if legacy else None)
        if events:
            self.profile.commit(events)
        self._seen[tid] = json.dumps(title.get("achievement") or {}, sort_keys=True)
        write_json_atomic(_sync_file(self.profile), self._seen)
        st = STATUS.get(self.profile.profile_id, {})
        self._status(done=st.get("done", 0) + 1, current=title.get("name"))
        return [e for e in events if e["event_type"] == "achievement.unlocked"]


def status(profile: Profile) -> dict:
    c = _config(profile)
    return {"client_id": c.get("client_id"), "linked": bool(c.get("xuid")), "gamertag": c.get("gamertag"),
            "has_session": bool(c.get("xuid") and credentials.get_secret(SECRET, profile.profile_id)),
            "sync": STATUS.get(profile.profile_id, {})}
