"""What every adapter declares and shares.

An adapter turns something outside the profile (a platform account, a local
executable, a save file) into ordinary events. It never writes back to the
platform: imported achievements are read-only copies, and nothing an adapter
does can grant a Steam, PSN or Xbox achievement.

Imports are idempotent by construction. Every imported unlock carries a
stable `external_event_id`; the importer skips ids already in the profile,
and the reducer collapses the same id arriving from two devices into one
provenance record.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .. import __version__

SUPPORT_LEVELS = ("official", "community", "experimental", "manual-import", "unavailable")


@dataclass(frozen=True)
class AdapterMetadata:
    adapter_id: str
    adapter_version: str
    support_level: str
    authentication_method: str
    data_access_method: str
    capabilities: tuple[str, ...]
    rate_limits: str
    known_limitations: tuple[str, ...]
    terms_or_policy_notes: str

    def as_dict(self) -> dict:
        return {k: list(v) if isinstance(v, tuple) else v for k, v in self.__dict__.items()}


@dataclass
class ImportSummary:
    adapter: str
    games_seen: int = 0
    games_added: int = 0
    packs_written: int = 0
    unlocks_added: int = 0
    unlocks_already_known: int = 0
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


Fetch = Callable[[str], object]


def http_json(url: str, *, retries: int = 4, pause: float = 0.0) -> object:
    """GET JSON with backoff on 429/5xx. The URL may carry an API key, so it is
    never included in an error message or log line."""
    safe = url.split("?")[0]
    for attempt in range(retries + 1):
        if pause:
            time.sleep(pause)
        req = urllib.request.Request(url, headers={"User-Agent": f"open-achievements/{__version__}",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(min(60, 2 ** attempt * 2) + random.random())
                continue
            if exc.code in (401, 403):
                raise AdapterError("unauthorized", f"{safe} refused the credentials ({exc.code})") from None
            raise AdapterError("http_error", f"{safe} answered {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < retries:
                time.sleep(min(60, 2 ** attempt * 2) + random.random())
                continue
            raise AdapterError("unreachable", f"could not reach {safe}") from None
    raise AdapterError("unreachable", "gave up")


def build_url(base: str, **params) -> str:
    return base + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def utc_iso(value: str | int | float | None) -> str | None:
    """Platform timestamps to the event format. Accepts 'YYYY-MM-DD HH:MM:SS'
    (treated as UTC), ISO strings, and Unix seconds."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, timezone.utc)
        else:
            text = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text if "T" in text or "+" in text else text.replace(" ", "T"))
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
