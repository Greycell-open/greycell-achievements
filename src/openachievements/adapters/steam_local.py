"""Steam on this computer: achievements as they happen, from the Steam client.

`openachievements steam link` (or "Link Steam" in the library) chooses the
Steam account signed in on this computer. From then on the watcher reads that
account's achievement files in Steam's own folder (see steamfiles.py): every
game it has played gets its pack from Steam's schema, with the real hidden
flags and descriptions, and every unlock is imported with Steam's own time.
When Steam rewrites a game's file, the new unlocks appear within one poll.

No password, no API key, nothing inside the game, and nothing is written to
Steam. Unlocks carry the same external id as the Steam Web API import
(`steamid:appid:apiname`), so both routes can never count one unlock twice;
achievement ids reuse the public catalogue's where it has the game.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import events as ev
from .. import steamfiles as sf
from ..catalog import steam as cat
from ..profile import Profile, ProfileError
from .base import AdapterMetadata, utc_iso

ADAPTER_VERSION = "0.1.0"
ICON_URL = "https://shared.akamai.steamstatic.com/community_assets/images/apps/{appid}/{icon}"
MAX_PROBLEMS = 200
BATCH = 50

METADATA = AdapterMetadata(
    adapter_id="steam",
    adapter_version=ADAPTER_VERSION,
    support_level="official",
    authentication_method="none: the Steam account signed in on this computer, chosen with `steam link`",
    data_access_method="the Steam client's own achievement files on this computer, read-only",
    capabilities=("discover-games", "import-definitions", "import-unlocks", "timestamps", "live"),
    rate_limits="reads a game's file only when Steam has rewritten it",
    known_limitations=(
        "only games this computer's Steam has run appear",
        "Steam writes a game's file when the game records progress, usually within seconds",
    ),
    terms_or_policy_notes="Reads the player's own local files. Nothing is sent to or written to Steam.",
)


# ---- linking ------------------------------------------------------------------------

def link(profile: Profile, root: Path | None = None, steam_id: str | None = None) -> dict:
    """Follow one Steam account on this computer (the most recently signed-in
    one unless `steam_id` says which)."""
    root = Path(root) if root else sf.steam_dir()
    if root is None or not (root / "appcache" / "stats").is_dir():
        raise ProfileError("Steam is not installed here, or has not run yet (set OA_STEAM_DIR to its folder)")
    accounts = sf.signed_in_accounts(root)
    if steam_id:
        accounts = [a for a in accounts if a["steam_id"] == str(steam_id)]
    if not accounts:
        raise ProfileError("no Steam account has signed in on this computer" if not steam_id
                           else f"Steam account {steam_id} has not signed in on this computer")
    chosen = accounts[0]
    with profile.config.editing() as config:
        config.setdefault("steam_local", {})[profile.profile_id] = {
            "root": str(root.resolve()), "steam_id": chosen["steam_id"], "account_id": chosen["account_id"]}
    if profile.state()["accounts"].get("steam", {}).get("account") != chosen["steam_id"]:
        profile.record("platform.account_linked", {"adapter": "steam", "account": chosen["steam_id"]},
                       adapter="steam", adapter_version=ADAPTER_VERSION)
    return {"steam_id": chosen["steam_id"], "games": len(sf.stats_files(root / "appcache" / "stats",
                                                                           chosen["account_id"]))}


def unlink(profile: Profile) -> None:
    with profile.config.editing() as config:
        config.get("steam_local", {}).pop(profile.profile_id, None)


def linked(profile: Profile, config: dict | None = None) -> dict | None:
    config = profile.config.load() if config is None else config
    return config.get("steam_local", {}).get(profile.profile_id)


# ---- one game -------------------------------------------------------------------------

def _catalogue_entry(index: cat.CatalogIndex | None, appid: int) -> dict | None:
    try:
        return index.get(appid) if index is not None else None
    except OSError:
        return None


def plan_game(profile: Profile, state: dict, known: set, stats_dir: Path, link_info: dict, appid: int,
              index: cat.CatalogIndex | None = None) -> tuple[list[dict], list[dict]]:
    """What bringing one game up to date would write: (pack and game events,
    unlock events). Nothing is written here, so many games can be recorded in
    one go (a profile rebuilds its state once per write)."""
    from ..packs import split_for_events, validate_definitions
    schema = sf.read_schema(stats_dir, appid)
    if not schema["achievements"]:
        return [], []
    game_id = f"steam-{appid}"
    installed = state["packs"].get(game_id)
    catalogue = _catalogue_entry(index, appid)
    title = schema["title"] or (catalogue and catalogue["pack"]["games"][0]["title"]) or \
        (state["games"].get(game_id, {}).get("title")) or f"Steam app {appid}"
    rows = [{"name": a["name"], "description": a["description"],
             "icon": ICON_URL.format(appid=appid, icon=a["icon"]) if a["icon"] else None}
            for a in schema["achievements"]]
    if installed and not installed.get("removed"):
        previous = {"achievements": list(installed["achievements"].values())}
    else:
        previous = catalogue
    entry = cat.build_pack(appid, title, rows, crawled_at=ev.now(), previous=previous)
    for built, real in zip(entry["achievements"], schema["achievements"]):
        built["hidden"] = real["hidden"]                     # Steam's own flag, not the guess from a blank page
        built["external_id"] = real["api_name"]
    entry["pack"]["source"] = "steam-local"
    ids = {a["api_name"]: b["id"] for a, b in zip(schema["achievements"], entry["achievements"])}
    wanted = {a["id"]: a for a in entry["achievements"]}
    fields = ("name", "description", "hidden", "icon")
    current = {k: {f: v.get(f) for f in fields} for k, v in (installed or {}).get("achievements", {}).items()}
    make = lambda kind, payload, **kw: ev.make_event(kind, profile_id=profile.profile_id, device_id=profile.device_id,
                                                     payload=payload, adapter="steam",
                                                     adapter_version=ADAPTER_VERSION, **kw)
    packs = []
    if (not installed or installed.get("removed") or installed.get("source") != "steam-local"
            or current != {k: {f: v.get(f) for f in fields} for k, v in wanted.items()}):
        if game_id not in state["games"]:
            game = entry["pack"]["games"][0]
            packs.append(make("game.registered", {"title": game["title"], "platform": game["platform"],
                                                  "external_ids": game["external_ids"]}, game_id=game_id))
        description = validate_definitions(entry["pack"], cat.without_rarity(entry["achievements"]))
        kind = "pack.updated" if installed and not installed.get("removed") else "pack.installed"
        packs.extend(make(kind, payload) for payload in split_for_events(description))
    unlocks = []
    for api_name, when in sf.read_unlocks(stats_dir, link_info["account_id"], appid, schema).items():
        external = f"{link_info['steam_id']}:{appid}:{api_name}"
        if external in known or api_name not in ids:
            continue
        unlocks.append(make("achievement.unlocked", {"provenance": "imported", "mode": "steam-client"},
                            achievement_id=f"{game_id}:{ids[api_name]}", game_id=game_id,
                            occurred_at=utc_iso(when) or ev.now(), external_account_id=link_info["steam_id"],
                            external_event_id=external))
    return packs, unlocks


def sync_game(profile: Profile, stats_dir: Path, link_info: dict, appid: int,
              index: cat.CatalogIndex | None = None) -> list[dict]:
    """Bring one game up to date now. Returns the unlock events written."""
    packs, unlocks = plan_game(profile, profile.state(), profile.index.external_ids("steam"), stats_dir,
                               link_info, appid, index)
    if packs or unlocks:
        profile.commit(packs + unlocks)
    return unlocks


# ---- every game this account has played ----------------------------------------------------

RECENT_DAYS = 14
PACKS_PER_POLL = 25
STORE_URL = "https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic"


def known_names(root: Path | None, index: cat.CatalogIndex | None, catalog_dir: Path | None) -> dict[int, str]:
    """Titles from what is on this computer: the catalogue, the crawl's app
    list, and Steam's install manifests."""
    names: dict[int, str] = {}
    if catalog_dir is not None:
        apps = Path(catalog_dir) / "steam-apps.txt"
        if apps.exists():
            for line in apps.read_text(encoding="utf-8", errors="replace").splitlines():
                head, _, name = line.partition("\t")
                if head.isdigit() and name.strip():
                    names[int(head)] = name.strip()
    if index is not None:
        names.update({a: e[3] for a, e in index.entries.items()})
    if root is not None:
        from .autodetect import steam_install_dirs
        try:
            for entry in os.scandir(root / "steamapps"):
                if entry.name.startswith("appmanifest_") and entry.name.endswith(".acf"):
                    app = sf.parse_text_vdf(Path(entry.path).read_text(encoding="utf-8", errors="replace")) \
                        .get("AppState", {})
                    if str(app.get("appid", "")).isdigit() and app.get("name"):
                        names[int(app["appid"])] = app["name"]
        except (OSError, sf.SteamFileError):
            pass
    return names


def sync_played(profile: Profile, root: Path, link_info: dict, index: cat.CatalogIndex | None,
                names: dict[int, str], now: float | None = None) -> dict:
    """Every game Steam says this account has played: in the library, with
    Steam's last-played time and play time. Games played in the last two weeks
    that have no status go on the Playing shelf. Returns what changed."""
    import time as _time
    now = _time.time() if now is None else now
    played = sf.read_played(root, link_info["account_id"])
    state = profile.state()
    games = state["games"]
    pending, added, unnamed, playing = [], 0, [], 0
    for appid, seen in played.items():
        game_id = f"steam-{appid}"
        last = utc_iso(seen["last_played"]) if seen["last_played"] else None
        fields = {k: v for k, v in (("last_played", last), ("playtime_minutes", seen["playtime_minutes"])) if v}
        game = games.get(game_id)
        if game is None:
            title = names.get(appid)
            if not title:
                unnamed.append(appid)
            pending.append(ev.make_event("game.registered", profile_id=profile.profile_id,
                                         device_id=profile.device_id, game_id=game_id, adapter="steam",
                                         payload={"title": title or f"Steam app {appid}", "platform": "PC (Steam)",
                                                  "external_ids": {"steam": appid}, **fields}))
            added += 1
            changed = True
        else:
            changed = bool(last and last > (game.get("last_played") or "")) or \
                seen["playtime_minutes"] > (game.get("playtime_minutes") or 0)
            if changed:
                pending.append(ev.make_event("game.metadata_updated", profile_id=profile.profile_id,
                                             device_id=profile.device_id, game_id=game_id, adapter="steam",
                                             payload=fields))
        recent = seen["last_played"] and now - seen["last_played"] < RECENT_DAYS * 86400
        if changed and recent and not (game or {}).get("status"):
            pending.append(ev.make_event("game.status_changed", profile_id=profile.profile_id,
                                         device_id=profile.device_id, game_id=game_id, adapter="steam",
                                         payload={"status": "playing"}))
            playing += 1
    if pending:
        profile.commit(pending)
    return {"played": len(played), "added": added, "updated": len(pending) - added - playing,
            "playing": playing, "unnamed": unnamed}


def add_catalogue_packs(profile: Profile, index: cat.CatalogIndex | None, limit: int = 200) -> int:
    """Achievement lists for Steam games already in the library that have none
    yet, from the catalogue, recorded in one write per call (a large library
    would otherwise rebuild its state once per game)."""
    from ..packs import PackError, split_for_events, validate_definitions
    if index is None:
        return 0
    state = profile.state()
    pending, done = [], 0
    for game_id in state["games"]:
        if done >= limit:
            break
        if not game_id.startswith("steam-") or game_id in state["packs"]:
            continue
        appid = int(game_id[6:])
        if appid not in index.entries:
            continue
        try:
            entry = index.get(appid)
            description = validate_definitions(entry["pack"], cat.without_rarity(entry["achievements"])) if entry else None
        except (PackError, OSError, ValueError, KeyError, TypeError):
            continue
        if not description:
            continue
        pending.extend(ev.make_event("pack.installed", profile_id=profile.profile_id, device_id=profile.device_id,
                                     payload=payload) for payload in split_for_events(description))
        done += 1
    if pending:
        profile.commit(pending)
    return done


def fill_names(profile: Profile, appids: list[int], fetch=None, pause: float = 1.5, limit: int = 40) -> int:
    """Look up missing titles on Steam's public store API, slowly."""
    import json as _json
    import time as _time
    from .base import http_json
    fetch = fetch or (lambda url: http_json(url, retries=1))
    named = 0
    for appid in appids[:limit]:
        try:
            data = (fetch(STORE_URL.format(appid=appid)) or {}).get(str(appid)) or {}
        except Exception:  # noqa: BLE001 - a name is a nicety; never stop for it
            continue
        name = (data.get("data") or {}).get("name") if data.get("success") else None
        if name:
            profile.record("game.metadata_updated", {"title": name[:500]}, game_id=f"steam-{appid}", adapter="steam")
            named += 1
        if pause:
            _time.sleep(pause)
    return named


# ---- watching -------------------------------------------------------------------------------

@dataclass
class SteamLocalWatcher:
    """Re-reads a game only when Steam has rewritten its file. The first poll
    imports what is already there; later new unlocks in a game with no status
    put it on the Playing shelf."""
    profile: Profile
    catalog_dir: Path | None = None
    seen: dict = field(default_factory=dict)           # appid -> (size, mtime_ns)
    problems: dict = field(default_factory=dict)
    _reported: set = field(default_factory=set)
    imported_once: set = field(default_factory=set)   # appids whose history is already in
    name_lookups: bool = True                          # ask Steam's store for titles we lack
    played_seen: tuple | None = None
    last_played_sync: dict | None = None
    _unnamed: list = field(default_factory=list)
    _namer: object = None

    def _sync_played(self, root: Path, info: dict, index, catalog_dir: Path) -> None:
        path = sf.localconfig_path(root, info["account_id"])
        try:
            st = path.stat()
        except OSError:
            return
        stamp = (st.st_size, st.st_mtime_ns)
        if self.played_seen != stamp:
            try:
                result = sync_played(self.profile, root, info, index, known_names(root, index, catalog_dir))
                self.played_seen = stamp
                self.last_played_sync = result
                self._unnamed.extend(a for a in result["unnamed"] if a not in self._unnamed)
                self.problems.pop("played", None)
            except Exception as exc:  # noqa: BLE001 - reported, retried next poll
                self.problems["played"] = f"Steam play history: {exc} (will retry)"
        add_catalogue_packs(self.profile, index)
        if self._unnamed and self.name_lookups and not (self._namer and self._namer.is_alive()):
            batch, self._unnamed = self._unnamed[:40], self._unnamed[40:]
            import threading
            self._namer = threading.Thread(target=fill_names, args=(self.profile, batch), daemon=True)
            self._namer.start()

    def new_problems(self) -> list[str]:
        fresh = [m for k, m in self.problems.items() if (k, m) not in self._reported]
        self._reported.update(self.problems.items())
        return fresh

    def poll(self) -> list[dict]:
        info = linked(self.profile)
        if not info:
            return []
        stats_dir = Path(info["root"]) / "appcache" / "stats"
        catalog_dir = Path(self.catalog_dir) if self.catalog_dir else cat.default_dir()
        index = cat.CatalogIndex(catalog_dir) if (catalog_dir / cat.PACKS_FILE).exists() else None
        self._sync_played(Path(info["root"]), info, index, catalog_dir)
        written = []
        changed = []
        for appid, path in sf.stats_files(stats_dir, info["account_id"]).items():
            try:
                st = path.stat()
            except OSError:
                continue
            stamp = (st.st_size, st.st_mtime_ns)
            if self.seen.get(appid) == stamp:
                continue
            changed.append((appid, stamp))
        # Games are planned one by one and written together, a batch at a time:
        # a profile rebuilds its state once per write, not once per game.
        for start in range(0, len(changed), BATCH):
            written.extend(self._write_batch(stats_dir, info, index, changed[start:start + BATCH]))
        return written

    def _write_batch(self, stats_dir: Path, info: dict, index, batch: list) -> list[dict]:
        state = self.profile.state()
        known = self.profile.index.external_ids("steam")
        planned = []
        for appid, stamp in batch:
            # A game counts as seen only once it is written, or once its files
            # are known to be unreadable. A busy profile or a missing schema is
            # tried again next poll.
            try:
                packs, unlocks = plan_game(self.profile, state, known, stats_dir, info, appid, index)
            except FileNotFoundError:
                continue                           # a game with progress but no schema cached yet
            except sf.SteamFileError as exc:
                self.seen[appid] = stamp           # unreadable until Steam rewrites it
                if len(self.problems) < MAX_PROBLEMS or appid in self.problems:
                    self.problems[appid] = f"Steam app {appid}: {exc}"
                continue
            except Exception as exc:  # noqa: BLE001 - one game's failure never stops the others
                if len(self.problems) < MAX_PROBLEMS or appid in self.problems:
                    self.problems[appid] = f"Steam app {appid}: {exc} (will retry)"
                continue
            planned.append((appid, stamp, packs, unlocks))
        try:
            self.profile.commit([e for _a, _s, packs, unlocks in planned for e in packs + unlocks])
        except Exception as exc:  # noqa: BLE001 - nothing marked seen: the whole batch is retried
            for appid, *_ in planned:
                if len(self.problems) < MAX_PROBLEMS or appid in self.problems:
                    self.problems[appid] = f"Steam app {appid}: {exc} (will retry)"
            return []
        written, now_playing = [], []
        for appid, stamp, _packs, unlocks in planned:
            self.seen[appid] = stamp
            self.problems.pop(appid, None)
            written.extend(unlocks)
            # The first successful read of a game is its history, however late
            # it comes; only unlocks after that are play happening now.
            if unlocks and appid in self.imported_once:
                now_playing.append(appid)
            self.imported_once.add(appid)
        state = self.profile.state()
        for appid in now_playing:
            if not state["games"].get(f"steam-{appid}", {}).get("status"):
                self.profile.set_status(f"steam-{appid}", "playing")
        return written
