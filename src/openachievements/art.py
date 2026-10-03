"""Pictures for the dashboard: game banners and achievement icons.

The page never loads pictures from another site itself: it asks this app,
which serves a copy kept on this computer and fetches it the first time only.
So a picture costs one request ever, works offline afterwards, and the page
makes no requests to platforms on its own. The copies are a private display
cache, like a browser's; nothing is shared or published.

Only pictures from the platforms' own image hosts are fetched (HOSTS), only
real images (content type image/*) and only up to MAX_BYTES, so the app is
never a general proxy. A picture that is not there is remembered as missing
for a day, so a broken link is not asked for on every page view.

Game banners for Steam games, first found wins:
  1. Steam's own copy on this computer (appcache/librarycache/<appid>/header.jpg),
  2. Steam's fixed public address for the app,
  3. the address Steam's store API names (newer games keep banners under a
     hashed path), asked at most once a second.
Other platforms' games use the picture their import recorded (icon_url).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__
from .fsutil import write_bytes_atomic

HOSTS = ("steamstatic.com", "steampowered.com", "steamcommunity.com", "media.retroachievements.org",
         "gog-statics.com", "images.gog.com", "playstation.net", "playstation.com", "xboxlive.com",
         "s-microsoft.com")
MAX_BYTES = 2 * 1024 * 1024
MISS_FOR = 86400.0
STEAM_HEADER = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"
STEAM_DETAILS = "https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic"
_store_lock = threading.Lock()
_store_last = [0.0]


def cache_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "OpenAchievements" / "art"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "openachievements" / "art"


def allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in HOSTS)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):          # redirects are followed by hand, checked first
        return None


class Refused(ValueError):
    """A definite no: not an allowed host, not an image, too large."""


def _get(url: str, read: Callable, open_url: Callable | None = None, limit: int = MAX_BYTES):
    """GET from an allowed host. Every redirect target is checked against HOSTS
    before it is requested, so an allowed host can never send this app
    somewhere else."""
    opener = open_url or urllib.request.build_opener(_NoRedirect).open
    for _ in range(4):
        if not allowed(url):
            raise Refused("not a platform image host")
        req = urllib.request.Request(url, headers={"User-Agent": f"GreycellAchievements/{__version__}"})
        try:
            with opener(req, timeout=15) as resp:
                return read(resp, limit)
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308) or not exc.headers.get("Location"):
                raise
            url = urllib.parse.urljoin(url, exc.headers["Location"])
    raise Refused("too many redirects")


def _fetch(url: str, open_url: Callable | None = None) -> tuple[str, bytes]:
    return _get(url, lambda resp, limit: (resp.headers.get("Content-Type") or "", resp.read(limit + 1)), open_url)


def _definite(exc: BaseException) -> bool:
    """Worth remembering as missing for a day: the answer will not change on
    a retry. A timeout, a dropped connection, 429 or 5xx is retried next time."""
    if isinstance(exc, Refused):
        return True
    return isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 404, 410)


def _name(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _missing_recently(marker: Path, now: float) -> bool:
    try:
        return now - marker.stat().st_mtime < MISS_FOR
    except OSError:
        return False


def cached(url: str, fetch: Callable[[str], tuple[str, bytes]] | None = None, folder: Path | None = None,
           now: float | None = None, online: bool = True) -> Path | None:
    """The local copy of an allowed image URL, fetched once; None when it is
    not allowed, not an image, too large, or not there (remembered a day).
    With `online` False (the player switched pictures off) only copies already
    on this computer are used."""
    if not allowed(url):
        return None
    folder = Path(folder or cache_dir())
    path, miss = folder / (_name(url) + ".img"), folder / (_name(url) + ".miss")
    if path.exists():
        return path
    if not online:
        return None
    now = time.time() if now is None else now
    if _missing_recently(miss, now):
        return None
    try:
        kind, body = (fetch or _fetch)(url)
        if not kind.lower().startswith("image/") or not body or len(body) > MAX_BYTES:
            raise Refused("not an image")
    except (OSError, ValueError) as exc:
        if _definite(exc):
            folder.mkdir(parents=True, exist_ok=True)
            miss.write_bytes(b"")
            os.utime(miss, (now, now))
        return None
    folder.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(path, body)
    return path


def _steam_store_header(appid: str, get_json: Callable[[str], dict]) -> tuple[str | None, bool]:
    """The banner address Steam's store names, and whether a None is definite
    (the store answered) rather than a failed request worth retrying."""
    with _store_lock:                                   # Steam's store API: at most one call a second
        wait = 1.0 - (time.monotonic() - _store_last[0])
        if wait > 0:
            time.sleep(wait)
        _store_last[0] = time.monotonic()
        try:
            data = get_json(STEAM_DETAILS.format(appid=appid)).get(str(appid)) or {}
        except (OSError, ValueError) as exc:
            return None, _definite(exc)
    url = ((data.get("data") or {}).get("header_image") or "") if data.get("success") else ""
    return (url, True) if allowed(url) else (None, True)


def _get_json(url: str) -> dict:
    return _get(url, lambda resp, limit: json.loads(resp.read(limit)), limit=512 * 1024)


def steam_banner(appid: str, steam_root: Path | None, fetch=None, get_json=None, folder: Path | None = None,
                 now: float | None = None, online: bool = True) -> Path | None:
    if not str(appid).isdigit():
        return None
    folder = Path(folder or cache_dir())
    by_app = folder / (_name(f"steam-banner:{appid}") + ".img")    # whichever address found it, kept by app id
    if steam_root:
        local = Path(steam_root) / "appcache" / "librarycache" / str(appid) / "header.jpg"
        if local.is_file():
            return local
    if by_app.exists():                                  # found before, also through the store: works offline
        return by_app
    if not online:
        return cached(STEAM_HEADER.format(appid=appid), fetch, folder, now, online=False)
    found = cached(STEAM_HEADER.format(appid=appid), fetch, folder, now)
    if found:
        return found
    key = f"steam-store-header:{appid}"
    miss = folder / (_name(key) + ".miss")
    if _missing_recently(miss, time.time() if now is None else now):
        return None
    url, definite = _steam_store_header(str(appid), get_json or _get_json)
    found = cached(url, fetch, folder, now) if url else None
    if not found:
        if definite and (not url or (folder / (_name(url) + ".miss")).exists()):
            folder.mkdir(parents=True, exist_ok=True)
            miss.write_bytes(b"")                       # the store has no banner: ask again tomorrow
        return None
    write_bytes_atomic(by_app, found.read_bytes())
    return by_app


def game_picture(game: dict, steam_root: Path | None, **kw) -> Path | None:
    """The picture for a library game, or None (the page shows its letter tile)."""
    game_id = str(game.get("game_id") or "")
    ids = [game_id] + [str(g.get("game_id") or "") for g in game.get("linked_games") or []]
    for gid in ids:                                     # a game linked across sources: its Steam banner first
        if gid.startswith("steam-"):
            found = steam_banner(gid.split("-", 1)[1], steam_root, **kw)
            if found:
                return found
    if game.get("icon_url"):
        return cached(game["icon_url"], kw.get("fetch"), kw.get("folder"), kw.get("now"), kw.get("online", True))
    return None
