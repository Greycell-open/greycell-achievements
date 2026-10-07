"""The Steam catalogue: every Steam game's achievements, with no account.

A clean slate for anyone starting up. Nothing here uses a Steam account, an
API key, or anyone's personal data; it reads what Steam shows to anyone:

- `ISteamUserStats/GetGlobalAchievementPercentagesForApp` (documented Web
  API, no key) answers 403 for a game without achievements, so it is the cheap
  filter over the whole store;
- the public community page `steamcommunity.com/stats/<appid>/achievements`
  lists every achievement with its name, description, icon and unlock rate.

Descriptions of hidden achievements are blank on that page, and it carries no
hidden flag, so a blank description marks an entry hidden (see build_pack).

Each game becomes an ordinary achievement pack, `steam-<appid>`, the same id
the Steam importer uses. Achievement ids come from the display name, and a
re-crawl keeps the ids of the previous crawl (see build_pack). Steam's
internal names are not on the public page, and pairing the two public lists by
unlock rate was measured at 75% right (ties at one decimal), so it is not
attempted: the icon hash plus the name is the key that links an entry to
Steam's own schema (unique for 7,701 of 7,715 achievements measured).

Pacing is deliberate: one request per `pace` seconds per host, and a 429 or
5xx backs off. A full pass over ~170,000 apps takes about two days, resumes
where it stopped, and never runs faster because the store is large.
"""
from __future__ import annotations

import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .. import __version__
from .. import events as ev
from ..fsutil import write_json_atomic
from ..packs import PackError, validate_definitions

PERCENT_URL = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/?gameid={appid}"
COMMUNITY_URL = "https://steamcommunity.com/stats/{appid}/achievements/?l=english"
# Keyless, and unlike the public page it gives hidden achievements' real
# descriptions, Steam's own hidden flags and the internal (API) names.
SCHEMA_URL = "https://api.steampowered.com/IPlayerService/GetGameAchievements/v1/?appid={appid}&language=english"
SCHEMA_ICON_URL = "https://shared.fastly.steamstatic.com/community_assets/images/apps/{appid}/{icon}"
USER_AGENT = f"OpenAchievementsCatalog/{__version__} (+https://greycell.app)"

PACKS_FILE = "steam-packs.jsonl"
STATE_FILE = "steam-state.json"
INDEX_FILE = "steam-index.tsv"      # appid, byte offset, length, achievements, title: one line per crawl

_ROW = re.compile(r'<div class="achieveRow[^"]*">(.*?)<div style="clear: both;"></div>', re.S)
_IMG = re.compile(r'<img src="([^"]+)"')
_PCT = re.compile(r'<div class="achievePercent">\s*([^<%]*?)\s*%?\s*</div>')
_NAME = re.compile(r"<h3>(.*?)</h3>", re.S)
_DESC = re.compile(r"<h5>(.*?)</h5>", re.S)
_TITLE = re.compile(r"<title>Steam Community :: (.*?) :: Achievements</title>", re.S)
_TAG = re.compile(r"<[^>]+>")
_SLUG = re.compile(r"[^a-z0-9]+")


def default_dir() -> Path:
    if os.environ.get("OA_CATALOG_DIR"):
        return Path(os.environ["OA_CATALOG_DIR"])
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "OpenAchievements" / "catalog"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "openachievements" / "catalog"


class CatalogError(RuntimeError):
    pass


class StatsUnavailable(CatalogError):
    """Steam has achievements for the app but publishes no stats page for it
    (demos, some unreleased apps). An answer, not a failure: not retried."""


# ---- fetching ---------------------------------------------------------------------

Fetch = Callable[[str], "tuple[int, str]"]


class PacedFetcher:
    """GET with a minimum gap per host and backoff on 429/5xx. Returns
    (status, body); 403 and 404 are answers, not failures."""

    def __init__(self, pace: float = 1.5, retries: int = 5, sleep=time.sleep, clock=time.monotonic,
                 notify: Callable[[str], None] = lambda _m: None):
        self.pace, self.retries, self.sleep, self.clock, self.notify = pace, retries, sleep, clock, notify
        self.backoffs = 0
        self._last: dict[str, float] = {}

    def _wait(self, host: str) -> None:
        gap = self.pace - (self.clock() - self._last.get(host, -1e9))
        if gap > 0:
            self.sleep(gap)
        self._last[host] = self.clock()

    def __call__(self, url: str) -> tuple[int, str]:
        host = url.split("/")[2]
        for attempt in range(self.retries + 1):
            self._wait(host)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404):
                    return exc.code, ""
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    after = exc.headers.get("Retry-After") if exc.headers else None
                    delay = float(after) if after and after.isdigit() else min(900, 30 * 2 ** attempt)
                    self.backoffs += 1
                    self.notify(f"  {host} answered {exc.code}; waiting {delay:.0f}s")
                    self.sleep(delay + random.random())
                    continue
                raise CatalogError(f"{url.split('?')[0]} answered {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt < self.retries:
                    self.backoffs += 1
                    self.notify(f"  {host} unreachable; waiting {min(300, 10 * 2 ** attempt)}s")
                    self.sleep(min(300, 10 * 2 ** attempt))
                    continue
                raise CatalogError(f"could not reach {host}") from None
        raise CatalogError(f"{host} kept refusing; stopped")


# ---- parsing and packs ------------------------------------------------------------

def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub("", fragment)).split())


def parse_community_page(page: str) -> tuple[str | None, list[dict]]:
    """The game's title and every achievement row, in page order."""
    title = _TITLE.search(page)
    rows = []
    for block in _ROW.findall(page):
        name = _NAME.search(block)
        if not name or not _text(name.group(1)):
            continue
        img, pct, desc = _IMG.search(block), _PCT.search(block), _DESC.search(block)
        rows.append({
            "name": _text(name.group(1)),
            "description": _text(desc.group(1)) if desc else "",
            "icon": html.unescape(img.group(1)) if img else None,
            "percent": pct.group(1).strip() if pct else None,
        })
    return (_text(title.group(1)) if title else None), rows


def _slug(name: str) -> str:
    return (_SLUG.sub("-", name.lower()).strip("-") or "achievement")[:90]


def _icon_key(icon: str | None) -> str | None:
    """The icon's file name, which Steam derives from the image's hash."""
    return icon.rsplit("/", 1)[-1].lower() if icon else None


def _previous_ids(previous: dict | None) -> tuple[dict, dict, dict, set]:
    """Ways to recognise an achievement from the last crawl: icon and name,
    else icon alone, else name alone (each only where it was unambiguous)."""
    if not previous:
        return {}, {}, {}, set()
    both, by_icon, by_name = {}, {}, {}
    icons, names = {}, {}
    for a in previous.get("achievements", []):
        icon = _icon_key(a.get("icon"))
        both.setdefault((icon, a["name"]), a["id"])
        icons.setdefault(icon, []).append(a["id"])
        names.setdefault(a["name"], []).append(a["id"])
    by_icon = {k: v[0] for k, v in icons.items() if k and len(v) == 1}
    by_name = {k: v[0] for k, v in names.items() if len(v) == 1}
    return both, by_icon, by_name, {a["id"] for a in previous.get("achievements", [])}


def build_pack(appid: int, title: str, rows: list[dict], *, crawled_at: str, previous: dict | None = None) -> dict:
    """An ordinary pack (validated exactly as a hand-written one would be).

    Ids are what unlocks point at, so they must survive a re-crawl: an
    achievement keeps its id from `previous` (the last crawl of this game) when
    its icon and name, or its icon alone, or its name alone still identify it.
    Only a genuinely new achievement gets a new id, and never one an earlier
    achievement had.

    Steam blanks the description of every hidden achievement on the public
    page, so a blank description marks it hidden. Measured against Steam's
    schemas: all 910 hidden ones caught, 22 visible ones without a description
    also hidden (the safe direction: they show once unlocked)."""
    both, by_icon, by_name, reserved = _previous_ids(previous)
    ids: set[str] = set()
    achievements = []
    for row in rows:
        icon = row.get("icon") or ""
        aid = None
        for candidate in (both.get((_icon_key(icon), row["name"])), by_icon.get(_icon_key(icon)),
                          by_name.get(row["name"])):
            if candidate and candidate not in ids:
                aid = candidate
                break
        if aid is None:
            base = aid = _slug(row["name"])
            n = 2
            while aid in ids or aid in reserved:
                aid, n = f"{base}-{n}", n + 1
        ids.add(aid)
        hidden = row["hidden"] if "hidden" in row else not row["description"].strip()
        item = {"id": aid, "name": row["name"][:120], "description": row["description"][:500], "points": 0,
                "hidden": bool(hidden)}
        if icon.startswith("https://"):
            item["icon"] = icon[:300]
        if row.get("api_name"):
            item["external_id"] = str(row["api_name"])[:128]    # Steam's stable id: rarity looks it up by this
        achievements.append(item)
    game_id = f"steam-{appid}"
    meta = {
        "id": game_id, "name": f"{title} (Steam)"[:120], "version": crawled_at[:10],
        "game_ids": [game_id], "authors": [], "license": None, "source": "steam-catalog",
        "source_repository": f"https://store.steampowered.com/app/{appid}",
        "supported_adapters": ["steam"],
        "games": [{"id": game_id, "title": title[:200], "platform": "PC (Steam)", "external_ids": {"steam": appid}}],
    }
    validate_definitions(meta, achievements)
    return {"appid": appid, "crawled_at": crawled_at, "pack": meta, "achievements": achievements}


def without_rarity(achievements: list[dict]) -> list[dict]:
    """Achievements as they go into a pack event: without Steam's unlock
    percentage, which changes over time and lives in the rarity cache on this
    computer (rarity.py), never in history. Catalogue files crawled before
    this still carry it, so it is dropped here, where packs become events."""
    return [{k: v for k, v in a.items() if k != "rarity"} for a in achievements]


def fetch_schema(appid: int, fetch: Fetch) -> list[dict] | None:
    """Rows from Steam's keyless achievement schema, or None when it has no
    answer (it says nothing, rather than "none", for games without any)."""
    try:
        status, body = fetch(SCHEMA_URL.format(appid=appid))
        if status != 200:
            return None
        items = json.loads(body)["response"].get("achievements") or []
        rows = [{"name": a["localized_name"], "description": a.get("localized_desc") or "",
                 "icon": SCHEMA_ICON_URL.format(appid=appid, icon=a["icon"]) if a.get("icon") else "",
                 "percent": a.get("player_percent_unlocked") or "", "hidden": bool(a.get("hidden")),
                 "api_name": a.get("internal_name") or ""}
                for a in items if a.get("localized_name")]
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        return None
    return rows or None


def fetch_game(appid: int, fetch: Fetch, *, known_title: str | None = None, previous: dict | None = None) -> dict | None:
    """One game's pack, or None when Steam says it has no achievements. Anything
    that is not a clear answer raises CatalogError, so the app is retried."""
    rows = fetch_schema(appid, fetch)
    if rows:
        title = known_title
        if not title:
            status, page = fetch(COMMUNITY_URL.format(appid=appid))
            title = parse_community_page(page)[0] if status == 200 else None
        return build_pack(appid, title or f"Steam app {appid}", rows, crawled_at=ev.now(), previous=previous)
    status, body = fetch(PERCENT_URL.format(appid=appid))
    if status in (403, 404):
        return None
    if status != 200:
        raise CatalogError(f"app {appid}: unlock rates answered {status}")
    try:
        listed = len(json.loads(body)["achievementpercentages"]["achievements"])
    except (ValueError, KeyError, TypeError):
        raise CatalogError(f"app {appid}: unlock rates were not the expected JSON") from None
    if not listed:
        return None
    status, page = fetch(COMMUNITY_URL.format(appid=appid))
    title, rows = parse_community_page(page) if status == 200 else (None, [])
    if not rows and "No stats are available" in page:
        raise StatsUnavailable(f"app {appid}: Steam publishes no achievement stats for it")
    if not rows:
        raise CatalogError(f"app {appid}: Steam lists {listed} achievements but the community page showed none")
    entry = build_pack(appid, title or known_title or f"Steam app {appid}", rows, crawled_at=ev.now(),
                       previous=previous)
    if listed != len(rows):
        entry["note"] = f"Steam lists {listed} achievements, the page showed {len(rows)}"
        entry["partial"] = True
    return entry


# ---- the crawl ----------------------------------------------------------------------

@dataclass
class CrawlSummary:
    checked: int = 0
    packs: int = 0
    achievements: int = 0
    without_achievements: int = 0
    skipped_done: int = 0
    unavailable: int = 0
    problems: list[str] = field(default_factory=list)


def read_app_list(path: Path) -> list[tuple[int, str | None]]:
    """`appid` or `appid<TAB>name` per line; anything else is ignored."""
    apps = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        head, _, name = line.partition("\t")
        if head.strip().isdigit():
            apps.append((int(head), name.strip() or None))
    return apps


def _repair_tail(path: Path) -> None:
    """Cut a line left unfinished by a crash, so the next append starts clean.
    Scans back from the end in blocks; the file can be large."""
    if not path.exists():
        return
    with open(path, "rb+") as fh:
        end = fh.seek(0, os.SEEK_END)
        if end == 0:
            return
        fh.seek(end - 1)
        if fh.read(1) == b"\n":
            return
        pos = end
        while pos > 0:
            start = max(0, pos - 65536)
            fh.seek(start)
            cut = fh.read(pos - start).rfind(b"\n")
            if cut != -1:
                fh.truncate(start + cut + 1)
                return
            pos = start
        fh.truncate(0)


DONE = ("pack", "none", "unavailable")      # "partial" and "error" are tried again


def crawl(apps: Iterable[tuple[int, str | None]], out_dir: Path, fetch: Fetch, *, limit: int | None = None,
          refresh: bool = False, report: Callable[[str], None] = lambda _m: None,
          progress_every: int = 100, clock=time.monotonic) -> CrawlSummary:
    """Resumable: finished apps are recorded in the state file and skipped next
    time unless `refresh`. Packs are appended to one JSONL file; when an app is
    crawled again its newer line wins and keeps the older line's ids."""
    apps = list(apps)
    out_dir = Path(out_dir)
    outer_report = report

    def report(message: str) -> None:
        try:
            outer_report(message)
        except Exception:  # noqa: BLE001 - progress output must never stop the crawl
            pass
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path, packs_path = out_dir / STATE_FILE, out_dir / PACKS_FILE
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    _repair_tail(packs_path)
    index = CatalogIndex(out_dir)
    summary = CrawlSummary()
    started, last = clock(), None
    try:
        with open(packs_path, "ab") as packs, open(out_dir / INDEX_FILE, "a", encoding="utf-8", newline="\n") as idx:
            for appid, known_title in apps:
                if limit is not None and summary.checked >= limit:
                    break
                if not refresh and state.get(str(appid), {}).get("status") in DONE:
                    summary.skipped_done += 1
                    continue
                summary.checked += 1
                last = appid
                try:
                    entry = fetch_game(appid, fetch, known_title=known_title, previous=index.get(appid))
                except StatsUnavailable:
                    summary.unavailable += 1
                    state[str(appid)] = {"status": "unavailable", "at": ev.now()}
                    entry = False
                except Exception as exc:  # noqa: BLE001 - one app never stops or blocks the crawl
                    message = str(exc) if isinstance(exc, (CatalogError, PackError)) \
                        else f"app {appid}: {type(exc).__name__}: {exc}"
                    summary.problems.append(message)
                    state[str(appid)] = {"status": "error", "at": ev.now(), "error": message[:300]}
                    report(f"  ! {message}")
                    entry = False
                if entry is None:
                    summary.without_achievements += 1
                    state[str(appid)] = {"status": "none", "at": ev.now()}
                elif entry:
                    line = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                    offset = packs.tell()
                    packs.write(line)
                    packs.flush()
                    title = entry["pack"]["games"][0]["title"]
                    idx.write(f"{appid}\t{offset}\t{len(line)}\t{len(entry['achievements'])}\t{_tsv(title)}\n")
                    idx.flush()
                    index.note(appid, offset, len(line), len(entry["achievements"]), title)
                    summary.packs += 1
                    summary.achievements += len(entry["achievements"])
                    state[str(appid)] = {"status": "partial" if entry.get("partial") else "pack",
                                         "at": entry["crawled_at"], "achievements": len(entry["achievements"])}
                    title = entry["pack"]["games"][0]["title"]
                    report(f"  {appid} {title}: {len(entry['achievements'])} achievements"
                           + (f" ({entry['note']})" if entry.get("partial") else ""))
                if summary.checked % 25 == 0:
                    try:
                        write_json_atomic(state_path, state)
                    except (OSError, TypeError, ValueError) as exc:
                        # A missed progress save costs at most 25 apps on resume;
                        # stopping the crawl (as on 2026-10-01, 14 hours in) costs all.
                        report(f"  ! progress not saved this time ({type(exc).__name__}: {exc}); continuing")
                if progress_every and summary.checked % progress_every == 0:
                    minutes = max(clock() - started, 1e-9) / 60
                    report(f"== {summary.checked + summary.skipped_done}/{len(apps)} apps, last {last}, "
                           f"{summary.checked / minutes:.0f}/min, {summary.packs} packs, {summary.unavailable} "
                           f"unavailable, {len(summary.problems)} problems, "
                           f"{getattr(fetch, 'backoffs', 0)} backoffs")
    finally:
        write_json_atomic(state_path, state)
    return summary


def _tsv(text: str) -> str:
    return " ".join(text.replace("\t", " ").split())


class CatalogIndex:
    """Titles and positions of every catalogued game, small enough to keep in
    memory; a game's pack is read from the large JSONL file only when needed.

    The index is rebuilt from the JSONL file when it is missing or does not
    reach the file's end (a crash between the two writes, or output from
    before the index existed)."""

    def __init__(self, out_dir: Path):
        self.dir = Path(out_dir)
        self.entries: dict[int, tuple[int, int, int, str]] = {}
        packs = self.dir / PACKS_FILE
        size = packs.stat().st_size if packs.exists() else 0
        path = self.dir / INDEX_FILE
        reach = 0
        if path.exists():
            _repair_tail(path)
            for line in path.read_text(encoding="utf-8").splitlines():
                parts = line.split("\t", 4)
                if len(parts) == 5 and parts[0].isdigit():
                    appid, offset, length, count = map(int, parts[:4])
                    self.entries[appid] = (offset, length, count, parts[4])
                    reach = max(reach, offset + length)
        if reach != size:
            self._rebuild(packs)

    def _rebuild(self, packs: Path) -> None:
        self.entries = {}
        lines = []
        if packs.exists():
            offset = 0
            with open(packs, "rb") as fh:
                for raw in fh:
                    try:
                        entry = json.loads(raw)
                        appid, title = int(entry["appid"]), entry["pack"]["games"][0]["title"]
                        count = len(entry["achievements"])
                    except (ValueError, KeyError, TypeError, IndexError):
                        offset += len(raw)
                        continue
                    self.entries[appid] = (offset, len(raw), count, title)
                    lines.append(f"{appid}\t{offset}\t{len(raw)}\t{count}\t{_tsv(title)}\n")
                    offset += len(raw)
        tmp = self.dir / (INDEX_FILE + ".tmp")
        tmp.write_text("".join(lines), encoding="utf-8", newline="\n")
        os.replace(tmp, self.dir / INDEX_FILE)

    def note(self, appid: int, offset: int, length: int, count: int, title: str) -> None:
        self.entries[appid] = (offset, length, count, title)

    def get(self, appid: int) -> dict | None:
        found = self.entries.get(appid)
        if not found:
            return None
        with open(self.dir / PACKS_FILE, "rb") as fh:
            fh.seek(found[0])
            try:
                return json.loads(fh.read(found[1]))
            except ValueError:
                return None

    def search(self, query: str, limit: int = 30) -> list[dict]:
        """Titles containing every word of the query; exact and prefix matches first."""
        words = query.lower().split()
        hits = []
        for appid, (_o, _l, count, title) in self.entries.items():
            low = title.lower()
            if all(w in low for w in words):
                rank = (0 if low == query.lower().strip() else 1 if low.startswith(query.lower().strip()) else 2,
                        len(low))
                hits.append((rank, {"appid": appid, "title": title, "achievements": count}))
        hits.sort(key=lambda h: h[0])
        return [h[1] for h in hits[:limit]]

    def __len__(self) -> int:
        return len(self.entries)


def load(out_dir: Path) -> dict[int, dict]:
    """Every catalogued game, newest crawl per app."""
    path = Path(out_dir) / PACKS_FILE
    games: dict[int, dict] = {}
    if not path.exists():
        return games
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
                games[int(entry["appid"])] = entry
            except (ValueError, KeyError, TypeError):
                continue            # a line cut short by a crash; the app is crawled again
    return games


def write_pack_folder(entry: dict, folder: Path) -> Path:
    """The entry as a normal pack folder, for `Profile.install_pack`."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pack.json").write_text(json.dumps(entry["pack"], ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "achievements.json").write_text(json.dumps(entry["achievements"], ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    return folder


def add_to_profile(profile, index: CatalogIndex, appid: int, status: str | None = None,
                   fetch: Fetch | None = None) -> dict:
    """Install a catalogued game's pack (registering the game) and optionally
    give it a backlog status. Re-adding an installed game only changes its
    status, unless the catalogue has a newer crawl of it. With `fetch`, a game
    the crawl has not reached yet is read from Steam's public pages now."""
    import tempfile
    entry = index.get(appid)
    if entry is None and fetch is not None:
        known = profile.state()["games"].get(f"steam-{appid}", {}).get("title")
        entry = fetch_game(appid, fetch, known_title=known)
        if entry is None:
            raise CatalogError(f"Steam lists no achievements for app {appid}")
    if entry is None:
        raise CatalogError(f"app {appid} is not in the catalogue")
    game_id = f"steam-{appid}"
    state = profile.state()
    installed = state["packs"].get(game_id)
    from_steam = installed and not installed.get("removed") and installed.get("source") == "steam-local"
    if not from_steam and (not installed or installed.get("removed")
                           or installed.get("version") != entry["pack"]["version"]):
        # A pack built from the player's own Steam files is better data than the
        # public page (real hidden flags and descriptions): never replace it.
        with tempfile.TemporaryDirectory() as tmp:
            clean = {**entry, "achievements": without_rarity(entry["achievements"])}
            profile.install_pack(write_pack_folder(clean, Path(tmp) / game_id))
    if status:
        profile.set_status(game_id, status)
    return {"game_id": game_id, "title": entry["pack"]["games"][0]["title"],
            "achievements": len(entry["achievements"]), "status": status}


# ---- fetching one game now ----------------------------------------------------------------

def install_entry(profile, entry: dict, kind: str = "pack.installed") -> None:
    """Record a catalogue entry as the game's pack (registering the game if
    needed), in one write."""
    from ..packs import split_for_events, validate_definitions
    state = profile.state()
    game = entry["pack"]["games"][0]
    events = []
    if game["id"] not in state["games"]:
        events.append(ev.make_event("game.registered", profile_id=profile.profile_id, device_id=profile.device_id,
                                    game_id=game["id"], payload={"title": game["title"], "platform": game["platform"],
                                                                 "external_ids": game["external_ids"]}))
    description = validate_definitions(entry["pack"], without_rarity(entry["achievements"]))
    events.extend(ev.make_event(kind, profile_id=profile.profile_id, device_id=profile.device_id, payload=payload)
                  for payload in split_for_events(description))
    profile.commit(events)


def _hidden_text_missing(pack: dict) -> bool:
    """A public-page list: hidden achievements there have blank descriptions.
    Lists from Steam's own files or schema already carry them."""
    return pack.get("source") == "steam-catalog" and any(
        a.get("hidden") and not a.get("description") for a in pack["achievements"].values())


INDEX_PER_LOOK = 25                     # library games installed from the catalogue per look


class PackFetcher:
    """Fetches achievement lists from Steam's public pages for library games the
    catalogue has not reached yet, most recently played first, one at a time
    in the background. A game being played should not wait days for the crawl."""

    def __init__(self, profile, fetch: Fetch | None = None):
        import threading
        self.profile = profile
        self._fetch = fetch or PacedFetcher(pace=1.5)
        self._queue: list[int] = []
        self._tried: set[int] = set()
        self._lock = threading.Lock()
        self._thread = None
        self.fetched: list[int] = []

    def want(self, index: CatalogIndex | None) -> None:
        """Queue every Steam game in the library that has no pack and is not in
        the catalogue, and every one whose list came from the public page with
        hidden achievements' descriptions blank, newest activity first."""
        state = self.profile.state()
        missing, from_index = [], []
        for game_id, game in state["games"].items():
            if not game_id.startswith("steam-") or not game_id[6:].isdigit():
                continue
            appid = int(game_id[6:])
            if appid in self._tried:
                continue
            pack = state["packs"].get(game_id)
            if pack and not pack.get("removed"):
                if not _hidden_text_missing(pack):
                    continue
            elif pack is None and index is not None and appid in index.entries:
                # Catalogued, yet no list: a game added before the crawl reached
                # it, or found by its saves or an emulator. Install it from here.
                from_index.append(appid)
                continue
            missing.append((game.get("last_played") or "", appid))
        for appid in from_index[:INDEX_PER_LOOK]:
            try:
                add_to_profile(self.profile, index, appid)
            except (CatalogError, OSError, ValueError):
                self._tried.add(appid)
        with self._lock:
            queued = set(self._queue)
            for _when, appid in sorted(missing, reverse=True):
                if appid not in queued:
                    self._queue.append(appid)
            self._queue.sort(key=lambda a: next((w for w, x in missing if x == a), ""), reverse=True)
        self._start()

    def _start(self) -> None:
        import threading
        if self._queue and not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                appid = self._queue.pop(0)
            self._tried.add(appid)
            self.fetch_one(appid)

    def fetch_one(self, appid: int) -> bool:
        """Fetch and install one game's list now, or fill in the hidden
        descriptions of one it has. False when there was nothing to add."""
        try:
            state = self.profile.state()
            title = state["games"].get(f"steam-{appid}", {}).get("title")
            installed = state["packs"].get(f"steam-{appid}")
            if installed and not installed.get("removed"):
                if not _hidden_text_missing(installed):
                    return False
                entry = fetch_game(appid, self._fetch, known_title=title,
                                   previous={"achievements": list(installed["achievements"].values())})
                if not entry or not any(a["hidden"] and a["description"] for a in entry["achievements"]):
                    return False
                now = self.profile.state()["packs"].get(f"steam-{appid}") or {}
                if now.get("checksum") != installed.get("checksum"):
                    return False                              # changed meanwhile: next round decides again
                entry["pack"]["name"] = installed.get("name") or entry["pack"]["name"]
                install_entry(self.profile, entry, kind="pack.updated")
                self.fetched.append(appid)
                return True
            entry = fetch_game(appid, self._fetch, known_title=title)
            if not entry:
                return False
            if f"steam-{appid}" in self.profile.state()["packs"]:
                return False
            install_entry(self.profile, entry)
            self.fetched.append(appid)
            return True
        except Exception:  # noqa: BLE001 - one game's list is a nicety; the next is tried
            return False
