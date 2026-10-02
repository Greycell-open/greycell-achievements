"""Updates from the project's GitHub releases.

The app asks GitHub for the latest release at most once a day (one
unauthenticated request, nothing about the player in it), the page says when a
newer one exists, and `openachievements update` installs that release's source
archive into the app's own environment. run.bat runs the update when the app
starts, so closing and reopening it is all a player does.

Where releases come from is a setting, not a constant in code paths: the
machine config's `update.repo` ("owner/name"), else `OA_UPDATE_REPO`, else
`DEFAULT_REPO`. With none, nothing is checked. `update check off` stops the
daily look.

The Windows app (GreycellAchievements.exe) cannot pip-install over itself. It
reads a small manifest from greycell.app instead (`update.manifest`, else
`MANIFEST`): the version, the installer's address, its SHA-256 and size, and
release notes. self_update.py downloads that installer, checks it and runs it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import __version__

DEFAULT_REPO: str | None = "greycell-open/greycell-achievements"
CHECK_EVERY = timedelta(days=1)
API = "https://api.github.com/repos/{repo}/releases/latest"
ARCHIVE = "https://github.com/{repo}/archive/refs/tags/{tag}.zip"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION = re.compile(r"^v?(\d+(?:\.\d+){0,3})$")
MANIFEST = "https://greycell.app/downloads/greycell-achievements/latest.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"open-achievements/{__version__}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def version_tuple(text: str) -> tuple[int, ...] | None:
    m = _VERSION.match((text or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def newer(candidate: str, current: str = __version__) -> bool:
    a, b = version_tuple(candidate), version_tuple(current)
    return a is not None and b is not None and a > b


def safe_url(url: str) -> bool:
    """HTTPS anywhere, plain HTTP only on this computer (for testing a build)."""
    parts = urllib.parse.urlsplit(str(url or ""))
    return parts.scheme == "https" and bool(parts.hostname) or (
        parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"))


def settings(config: dict) -> dict:
    mine = config.get("update") or {}
    repo = mine.get("repo") or os.environ.get("OA_UPDATE_REPO") or DEFAULT_REPO
    manifest = mine.get("manifest") or MANIFEST
    return {"repo": repo if repo and _REPO.match(repo) else None, "check": mine.get("check", True),
            "manifest": manifest if safe_url(manifest) else None,
            "last_check": mine.get("last_check"), "latest": mine.get("latest")}


def latest(repo: str, get: Callable[[str], dict] | None = None) -> dict | None:
    """The newest release, or None when the repository has none."""
    try:
        data = (get or _get_json)(API.format(repo=repo))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:                          # GitHub's answer for a repository with no release yet
            return None
        raise
    tag = str(data.get("tag_name") or "")
    if not version_tuple(tag) or data.get("draft") or data.get("prerelease"):
        return None
    return {"version": tag.lstrip("v"), "tag": tag, "url": ARCHIVE.format(repo=repo, tag=tag),
            "page": data.get("html_url"), "notes": str(data.get("body") or "")[:2000]}


def latest_setup(manifest: str, get: Callable[[str], dict] | None = None) -> dict | None:
    """The newest Windows installer from the manifest, or None when it is not
    one this version can trust (no version, no checksum, no safe address)."""
    data = (get or _get_json)(manifest)
    version = str(data.get("version") or "")
    setup, digest = str(data.get("setup") or ""), str(data.get("sha256") or "").lower()
    size = data.get("size")
    if not version_tuple(version) or not safe_url(setup) or not _SHA256.match(digest):
        return None
    if not isinstance(size, int) or not 0 < size < 500 * 1024 * 1024:
        return None
    if urllib.parse.urlsplit(setup).hostname != urllib.parse.urlsplit(manifest).hostname:
        return None                                   # the installer comes from where the manifest is
    version = version.lstrip("v")
    return {"version": version, "tag": f"v{version}", "setup": setup, "sha256": digest, "size": size,
            "page": DOWNLOAD_PAGE, "notes": str(data.get("notes") or "")[:2000]}


def check(config_store, now: datetime | None = None, get: Callable[[str], dict] | None = None,
          force: bool = False, network: bool = True) -> dict:
    """{"current", "available": release or None}. Asks at most daily; a failed
    look is simply tried again tomorrow. The Windows app asks greycell.app's
    manifest, anything else GitHub's releases."""
    now = now or datetime.now(timezone.utc)
    config = config_store.load()
    s = settings(config)
    app = frozen()
    result = {"current": __version__, "available": None, "repo": s["repo"],
              "download": DOWNLOAD_PAGE if app else None, "installable": False, "reached": None}
    source = s["manifest"] if app else s["repo"]
    if not source or not (s["check"] or force):
        return result
    last = s["last_check"]
    due = network and (force or not last or now - datetime.fromisoformat(last) >= CHECK_EVERY)
    found = s["latest"]
    if due:
        try:
            found = latest_setup(source, get) if app else latest(source, get)
            result["reached"] = True
        except Exception:  # noqa: BLE001 - offline or rate-limited: no update today, not an error
            found = s["latest"]
            result["reached"] = False
        def remember(fresh: dict) -> None:                # into the file as it is now, not as it was
            mine = fresh.setdefault("update", {})
            mine["last_check"] = now.isoformat()
            mine["latest"] = found
        config_store.edit(remember)
    if found and app and not found.get("setup"):
        found = None                                  # remembered from GitHub before: not installable here
    if found and newer(found["version"]):
        result["available"] = found
        result["installable"] = bool(app and found.get("setup"))
    return result


DOWNLOAD_PAGE = "https://greycell.app/achievements/"


def frozen() -> bool:
    """True inside GreycellAchievements.exe: a new version is downloaded, not
    pip-installed over the running app."""
    return bool(getattr(sys, "frozen", False))


def running_from_source() -> bool:
    """True when this copy is a developer's checkout (a git working tree):
    installing a release would replace the code being worked on."""
    from pathlib import Path
    return any((p / ".git").exists() for p in Path(__file__).resolve().parents[:4])


def install(release: dict, run: Callable[[list[str]], int] | None = None) -> int:
    """Install a release into this Python environment (the app's own .venv)."""
    target = f"open-achievements[server] @ {release['url']}"
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", target]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return (run or (lambda c: subprocess.call(c, creationflags=flags)))(cmd)
