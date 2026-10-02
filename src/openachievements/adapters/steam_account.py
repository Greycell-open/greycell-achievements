"""Linking a Steam account: Sign in through Steam, and nothing else.

"Sign in through Steam" (OpenID 2.0, Steam's documented sign-in for other
sites): the player logs in on steamcommunity.com and Steam tells this app the
account's SteamID64. The app never sees a password, and never uses or asks for
a Steam Web API key (owner decision 2026-10-01). The answer is checked with
Steam directly (`check_authentication`) and must match a single-use state this
app issued, so another site cannot link an account.

What comes in afterwards is what Steam on this PC knows about that account
(adapters/steam_local.py): every game it has played here, with its unlocks, as
they happen. Steam shows a player's whole library, including games never
played, only to an API key or a signed-in Steam session, so games owned but
never played on this PC do not come in.
"""
from __future__ import annotations

import re
import secrets
import time
import urllib.parse
import urllib.request

from ..profile import Profile, ProfileError

OPENID = "https://steamcommunity.com/openid/login"
_CLAIMED = re.compile(r"^https://steamcommunity\.com/openid/id/(\d{17})$")
STATE_TTL = 600


class SteamSignIn:
    """Issues single-use states and verifies Steam's answer."""

    def __init__(self, post=None, clock=time.monotonic):
        self._states: dict[str, float] = {}
        self._post = post or _post_form
        self._clock = clock

    def start(self, base_url: str) -> str:
        now = self._clock()
        self._states = {s: t for s, t in self._states.items() if now - t < STATE_TTL}
        state = secrets.token_urlsafe(24)
        self._states[state] = now
        return_to = f"{base_url.rstrip('/')}/v1/local/steam/return?state={state}"
        return OPENID + "?" + urllib.parse.urlencode({
            "openid.ns": "http://specs.openid.net/auth/2.0",
            "openid.mode": "checkid_setup",
            "openid.return_to": return_to,
            "openid.realm": base_url.rstrip("/") + "/",
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        })

    def finish(self, base_url: str, params: dict) -> str:
        """The SteamID64 Steam vouched for, or ProfileError."""
        state = params.get("state", "")
        issued = self._states.pop(state, None)
        if issued is None or self._clock() - issued >= STATE_TTL:
            raise ProfileError("this sign-in was not started here, or took too long; try again")
        if params.get("openid.mode") != "id_res":
            raise ProfileError("Steam sign-in was cancelled")
        expected = f"{base_url.rstrip('/')}/v1/local/steam/return?state={state}"
        if params.get("openid.return_to") != expected:
            raise ProfileError("Steam's answer was meant for another address")
        claimed = _CLAIMED.match(params.get("openid.claimed_id", ""))
        if not claimed or params.get("openid.identity") != params.get("openid.claimed_id"):
            raise ProfileError("Steam's answer did not name an account")
        check = {k: v for k, v in params.items() if k.startswith("openid.")}
        check["openid.mode"] = "check_authentication"
        if "is_valid:true" not in self._post(OPENID, check):
            raise ProfileError("Steam did not confirm this sign-in")
        return claimed.group(1)


def _post_form(url: str, fields: dict) -> str:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


# ---- the account ---------------------------------------------------------------

def remember(profile: Profile, steam_id: str) -> None:
    with profile.config.editing() as config:
        config.setdefault("steam_account", {})[profile.profile_id] = steam_id
    if profile.state()["accounts"].get("steam", {}).get("account") != steam_id:
        profile.record("platform.account_linked", {"adapter": "steam", "account": steam_id}, adapter="steam")


def account(profile: Profile) -> str | None:
    return profile.config.load().get("steam_account", {}).get(profile.profile_id)


def forget(profile: Profile) -> None:
    with profile.config.editing() as config:
        config.get("steam_account", {}).pop(profile.profile_id, None)
