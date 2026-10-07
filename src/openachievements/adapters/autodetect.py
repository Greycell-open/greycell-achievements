"""Recognising a game while it runs, wherever it came from.

The watcher already sees running programs. This names them: a program inside
Steam's `steamapps/common/<folder>` is identified exactly from Steam's install
manifests; any other program is matched by its folder and file names against
the catalogue's titles, and only a single clear match counts. A recognised game
joins the library (Playing, unless it already has a status), its program is
registered so play time counts, and folders that look like its saves are
noted, by name only.

Folder discovery lists directory names under the known save roots and reads
no file. Reading a save still needs `save allow` (adapters/savefile.py).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import steamfiles as sf
from ..catalog import steam as cat
from ..profile import Profile, ProfileError
from . import executable, savefile

# Words that name an edition or a release, not the game.
_NOISE = re.compile(r"\b(definitive|deluxe|ultimate|complete|goty|game of the year|remastered|enhanced|"
                    r"anniversary|directors cut|director s cut|edition|repack|portable|x64|x86|win64|win32|"
                    r"shipping|launcher|dx11|dx12|v\d+(\.\d+)*|build \d+)\b")
_BRACKETS = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_SYSTEM = ("\\windows\\", "/windows/", "\\windowsapps\\", "\\microsoft\\", "\\nvidia", "\\amd\\",
           "\\common files\\", "\\appdata\\", "/usr/", "/system/", "/bin/", "/sbin/", "/library/")
# Files and folders game engines and game SDKs leave next to a game's program.
# Without one of these, a program is not treated as a game, whatever its name.
_ENGINE_FILES = ("unityplayer.dll", "gameassembly.dll", "monobleedingedge", "steam_api.dll", "steam_api64.dll",
                 "eossdk-win64-shipping.dll", "eossdk-win32-shipping.dll", "galaxy.dll", "galaxy64.dll",
                 "bink2w64.dll", "bink2w32.dll", "binkw32.dll", "binkw64.dll", "fmod.dll", "fmod64.dll", "fmodex.dll",
                 "fmodex64.dll", "physx3_x64.dll", "physx3_x86.dll", "renpy", "data.win", "nw_elf.dll")
_ENGINE_SUFFIXES = (".vpk", ".bsa", ".ba2", ".pck", ".rpa", ".forge", "_data")
# Programs that run beside a game, from the game's own folder, without being it:
# crash reporters, installers, redistributables. Recognised as the game, each
# became a second installation and the game's play time counted twice.
_HELPERS = ("unitycrashhandler", "crashreportclient", "crashpad_handler", "crashhandler", "crashreporter",
            "unins", "uninstall", "vc_redist", "vcredist", "dxsetup", "dxwebsetup", "dotnetfx", "oalinst",
            "ue4prereqsetup", "ueprereqsetup", "easyanticheat_setup", "physxsetup")
SIMILARITY = 0.95
SAVE_SCAN_EVERY = 5 * 60
FUZZY_MIN_LENGTH = 8
SAVE_SEARCH_DEPTH = 3


def normalise(name: str) -> str:
    text = _BRACKETS.sub(" ", name.lower().replace("_", " ").replace(".", " "))
    text = _NON_WORD.sub(" ", text.replace("'", ""))
    text = _NOISE.sub(" ", text)
    return " ".join(text.split())


# Folder names engines, launchers and tools use for their own purposes. A game
# with one of these as its whole title ("SAVED", "SYNC") is never matched by name.
_GENERIC = {"saved", "saves", "savegames", "save", "sync", "config", "configs", "logs", "log", "cache", "caches",
            "data", "profiles", "profile", "user", "users", "settings", "temp", "tmp", "crashes", "crash",
            "webcache", "shadercache", "local", "roaming", "game", "games", "unity", "unreal", "unrealengine",
            "godot", "renpy", "app userdata", "steam", "backup", "backups", "screenshots", "mods", "plugins",
            "packages", "microsoft", "google", "mozilla", "nvidia", "amd", "intel", "discord", "spotify"}


class TitleMatcher:
    """Catalogue titles by normalised name. A match must be unique.

    A title with a subtitle ("Silent Hill: Townfall") is also known by the
    subtitle alone when that is at least 6 characters and no other title shares
    it, because games often name their folders that way."""

    def __init__(self, index: cat.CatalogIndex | None, library: dict | None = None):
        self.by_name: dict[str, set] = {}
        subtitles: dict[str, set] = {}
        for appid, (_o, _l, _c, title) in (index.entries.items() if index else []):
            self.by_name.setdefault(normalise(title), set()).add(("steam", appid))
            for sep in (":", " - "):
                if sep in title:
                    sub = normalise(title.split(sep, 1)[1])
                    if len(sub) >= 6:
                        subtitles.setdefault(sub, set()).add(("steam", appid))
        for sub, ids in subtitles.items():
            if len(ids) == 1 and sub not in self.by_name:
                self.by_name[sub] = set(ids)
        for name in [n for n in self.by_name if n in _GENERIC]:
            del self.by_name[name]
        library = library or {}

        def identity(game_id: str) -> tuple:
            # A game shown inside another (game link) is that other game; a
            # Steam game is the same game as its catalogue entry. One identity
            # each, or a match would look ambiguous.
            seen = set()
            while library.get(game_id, {}).get("linked_to") and game_id not in seen:
                seen.add(game_id)
                game_id = library[game_id]["linked_to"]
            steam = re.fullmatch(r"steam-(\d+)", game_id)
            return ("steam", int(steam.group(1))) if steam else ("game", game_id)

        self.by_name = {name: {identity(f"steam-{i}") if kind == "steam" else identity(i) for kind, i in ids}
                        for name, ids in self.by_name.items()}
        for game_id, game in library.items():
            # A game tracked only by its folder's name is a stand-in until a
            # catalogue names it: it never competes with a catalogued title.
            if game.get("title") and not game_id.startswith("local-"):
                self.by_name.setdefault(normalise(game["title"]), set()).add(identity(game_id))
        self.names = [n for n in self.by_name if len(n) >= 4]

    def match(self, candidates: list[str]) -> tuple | None:
        for raw in candidates:
            name = normalise(raw)
            if len(name) < 4:
                continue
            exact = self.by_name.get(name)
            if exact and len(exact) == 1:
                return next(iter(exact))
            if len(name) < FUZZY_MIN_LENGTH:
                continue
            close = difflib.get_close_matches(name, self.names, n=2, cutoff=SIMILARITY)
            if len(close) == 1 and len(self.by_name[close[0]]) == 1:
                return next(iter(self.by_name[close[0]]))
        return None


def is_helper(path: str) -> bool:
    stem = Path(path.replace("\\", "/")).stem.lower()
    return stem.startswith(_HELPERS)


def looks_like_a_game(path: str) -> bool:
    """A game engine or game SDK file beside the program or one folder up, or
    an Unreal-style Binaries folder above it. Deliberately narrow: generic
    runtime files (Chromium .pak, SDL, OpenAL, lib folders) appear in ordinary
    apps too."""
    here = Path(path).parent
    if any(a.name.lower() == "binaries" for a in list(here.parents)[:2] + [here]):
        return True
    for folder in (here, here.parent):
        try:
            names = [e.name.lower() for e in os.scandir(folder)]
        except OSError:
            continue
        if any(n in _ENGINE_FILES or n.endswith(_ENGINE_SUFFIXES) for n in names):
            return True
    return False


def gog_info(path: str) -> tuple[int, str] | None:
    """(GOG product id, title) from the `goggame-<id>.info` every GOG install
    carries, in the program's folder or up to two above it."""
    here = Path(path).parent
    for folder in [here, *list(here.parents)[:2]]:
        try:
            infos = [e.path for e in os.scandir(folder) if e.name.lower().startswith("goggame-")
                     and e.name.lower().endswith(".info")]
        except OSError:
            continue
        for info in infos:
            try:
                data = json.loads(Path(info).read_text(encoding="utf-8-sig", errors="replace")[:65536])
                gid, name = int(data.get("gameId") or data.get("rootGameId")), str(data.get("name") or "").strip()
            except (OSError, ValueError, TypeError):
                continue
            if name and data.get("rootGameId", gid) in (gid, str(gid)):
                return gid, name[:200]
    return None


def unity_info(path: str) -> tuple[str, str] | None:
    """(company, product) from a Unity game's `<Name>_Data/app.info`: the
    names its saves live under (LocalLow/<company>/<product>)."""
    p = Path(path)
    try:
        lines = (p.parent / f"{p.stem}_Data" / "app.info").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
        return lines[0].strip()[:120], lines[1].strip()[:120]
    return None


def folder_title(path: str) -> str | None:
    """A readable title from the game's folder: repack tags and versions left out.
    The process list gives paths in lower case; the disk has the real case."""
    try:
        path = os.path.realpath(path)
    except OSError:
        pass
    for raw in candidates_for(path)[:-1] or candidates_for(path):
        text = _BRACKETS.sub(" ", raw.replace("_", " "))
        text = re.sub(r"\b(v\d+(\.\d+)*|build \d+|repack|portable|x64|x86|win64|win32)\b", " ", text, flags=re.I)
        text = " ".join(text.split()).strip(" -")
        if len(text) >= 2 and normalise(text) not in _GENERIC:
            return text[:120]
    return None


def local_game_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-")
    return f"local-{slug or 'game'}"


def candidates_for(path: str) -> list[str]:
    """Names to try, most specific first: the folders above the program (the
    game folder is usually one or two up), then the program's own name."""
    p = Path(path)
    parts = [a for a in p.parents][:3]
    names = [a.name for a in parts if a.name and a.name.lower() not in
             ("bin", "binaries", "win64", "win32", "x64", "game", "games", "program files", "program files (x86)")]
    return names + [p.stem]


def steam_install_dirs(root: Path | None) -> dict[str, int]:
    """Lowercased install folder name -> appid, from every Steam library's manifests."""
    if root is None:
        return {}
    found = {}
    for lib in sf.library_paths(root):
        try:
            for entry in os.scandir(lib / "steamapps"):
                if entry.name.startswith("appmanifest_") and entry.name.endswith(".acf"):
                    state = sf.parse_text_vdf(Path(entry.path).read_text(encoding="utf-8", errors="replace"))
                    app = state.get("AppState", {})
                    if str(app.get("appid", "")).isdigit() and app.get("installdir"):
                        found[app["installdir"].lower()] = int(app["appid"])
        except (OSError, sf.SteamFileError):
            continue
    return found


def find_save_folders(title: str, limit: int = 5) -> list[str]:
    """Folders under the known save roots named like the game. Names only."""
    target = normalise(title)
    if len(target) < 4:
        return []
    found = []
    for root in [r for r in savefile.search_roots() if r.is_dir()]:
        stack = [(root, 0)]
        while stack and len(found) < limit:
            folder, depth = stack.pop()
            try:
                entries = [e for e in os.scandir(folder) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
            except OSError:
                continue
            for e in entries:
                name = normalise(e.name)
                if name == target or (len(name) >= 6 and difflib.SequenceMatcher(None, name, target).ratio() >= SIMILARITY):
                    found.append(e.path)
                elif depth + 1 < SAVE_SEARCH_DEPTH and depth == 0:
                    stack.append((Path(e.path), depth + 1))   # publisher folder, then the game
    return found[:limit]


class _Titles:
    """Known Steam names in the shape TitleMatcher reads."""

    def __init__(self, names: dict[int, str]):
        self.entries = {appid: (0, 0, 0, title) for appid, title in names.items()}


# ---- games found by the saves they left --------------------------------------------------

# Extensions that are saves by themselves, and generic ones that count only
# when the file name also says so (plenty of ordinary apps keep .dat or .json).
_SAVE_EXT = (".sav", ".save", ".sl2", ".es3", ".ess", ".rpgsave", ".savegame", ".sav2", ".sgd", ".gsave")
_MAYBE_EXT = (".dat", ".bin", ".json", ".slot", ".profile")
_SAVE_WORDS = ("save", "slot", "profile", "progress", "checkpoint")


def _save_evidence(folder: Path, depth: int = 0) -> float | None:
    """Newest modified time of save-like files in the folder (or its
    SaveGames/Saves subfolders), or None when nothing looks like a save."""
    newest = None
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return None
    for e in entries:
        name = e.name.lower()
        try:
            if e.is_file(follow_symlinks=False):
                if name.endswith(_SAVE_EXT) or (name.endswith(_MAYBE_EXT) and any(w in name for w in _SAVE_WORDS)):
                    newest = max(newest or 0, e.stat(follow_symlinks=False).st_mtime)
            elif e.is_dir(follow_symlinks=False) and depth < 2 and (
                    any(w in name for w in ("save", "slot", "profile")) or name in ("saved", "savegames")):
                inner = _save_evidence(Path(e.path), depth + 1)
                if inner:
                    newest = max(newest or 0, inner)
        except OSError:
            continue
    return newest


def discover_saved_games(matcher: TitleMatcher, roots: list[Path] | None = None, depth: int = 3) -> list[dict]:
    """Folders under the save roots named exactly like one catalogued game and
    holding save-like files: games that have been played on this computer,
    whatever their source. Reads names and dates only."""
    if roots is None:
        roots = savefile.search_roots()
    found: dict[tuple, dict] = {}
    for root in [r for r in roots if r.is_dir()]:
        stack = [(root, 0)]
        while stack:
            folder, level = stack.pop()
            try:
                subdirs = [e for e in os.scandir(folder) if e.is_dir(follow_symlinks=False)
                           and not e.name.startswith((".", "$"))]
            except OSError:
                continue
            for e in subdirs:
                name = normalise(e.name)
                ids = matcher.by_name.get(name)
                if ids and len(ids) == 1 and len(name) >= 4:
                    when = _save_evidence(Path(e.path))
                    if when:
                        ident = next(iter(ids))
                        if ident not in found or when > found[ident]["when"]:
                            found[ident] = {"ident": ident, "folder": e.path, "when": when}
                        continue
                if level + 1 < depth:
                    stack.append((Path(e.path), level + 1))
    return list(found.values())


@dataclass
class AutoDetector:
    """One poll: which running programs are games, and what to do about them."""
    profile: Profile
    catalog_dir: Path | None = None
    known: dict = field(default_factory=dict)          # lowercased exe path -> game_id or None (not a game)
    _matcher: TitleMatcher | None = None
    _matcher_at: float = 0.0
    _steam_dirs: dict | None = None
    _saves_at: float = -1e18
    _local: dict = field(default_factory=dict)         # exe path -> local game id it was, before a refresh

    def enabled(self) -> bool:
        return self.profile.config.load().get("autodetect", {}).get(self.profile.profile_id, True)

    def _refresh(self) -> None:
        if self._matcher is None or time.monotonic() - self._matcher_at > 300:
            # New titles may have arrived (the crawl runs for days): programs
            # judged "not a game", or known only by folder name, get another look.
            self._local.update({k: v for k, v in self.known.items() if v and v.startswith("local-")})
            self.known = {k: v for k, v in self.known.items() if v is not None and not v.startswith("local-")}
            folder = Path(self.catalog_dir) if self.catalog_dir else cat.default_dir()
            index = cat.CatalogIndex(folder) if (folder / cat.PACKS_FILE).exists() else None
            self._index = index
            # Every Steam title known here, not only games the crawl has reached:
            # a game is recognised by name; its achievements follow when crawled.
            from .steam_local import known_names
            self._names = known_names(None, index, folder)
            self._matcher = TitleMatcher(_Titles(self._names), self.profile.state()["games"])
            self._matcher_at = time.monotonic()
            self._steam_dirs = steam_install_dirs(sf.steam_dir())

    def identify(self, path: str) -> tuple | None:
        low = path.lower().replace("/", "\\")
        if any(s in low for s in _SYSTEM) or is_helper(path):
            return None
        if "\\steamapps\\common\\" in low:
            folder = low.split("\\steamapps\\common\\", 1)[1].split("\\", 1)[0]
            appid = (self._steam_dirs or {}).get(folder)
            if appid:
                return ("steam", appid)
        gog = gog_info(path)
        if not looks_like_a_game(path) and gog is None:
            return None
        if gog is not None:
            return ("gog", gog[0], gog[1])
        unity = unity_info(path)
        match = self._matcher.match(candidates_for(path) + ([unity[1]] if unity else []))
        if match:
            return match
        # A game no catalogue names (never on Steam, or not yet): kept by its
        # folder's name for play time and saves, with no achievements.
        title = folder_title(path)
        return ("local", local_game_id(title), title) if title else None

    def discover_saves(self) -> list[dict]:
        """Add games found by their save folders; last played is the newest save."""
        from .base import utc_iso
        self._refresh()
        added = []
        state = self.profile.state()
        for hit in discover_saved_games(self._matcher):
            kind, ref = hit["ident"]
            game_id = f"steam-{ref}" if kind == "steam" else ref
            when = utc_iso(hit["when"])
            try:
                if game_id not in state["games"]:
                    if kind != "steam":
                        continue
                    if self._index is not None and self._index.get(ref):
                        cat.add_to_profile(self.profile, self._index, ref)
                    else:
                        self.profile.register_game(game_id, self._names.get(ref, f"Steam app {ref}"),
                                                   platform="PC", external_ids={"steam": ref})
                    added.append({"game_id": game_id, "folder": hit["folder"]})
                game = self.profile.state()["games"][game_id]
                if when and when > (game.get("last_played") or ""):
                    self.profile.record("game.metadata_updated", {"last_played": when}, game_id=game_id,
                                        adapter="save-file")
                    if time.time() - hit["when"] < 14 * 86400 and not game.get("status"):
                        self.profile.set_status(game_id, "playing")
                key = f"{self.profile.profile_id}:{game_id}"
                if hit["folder"] not in (self.profile.config.load().get("save_candidates") or {}).get(key, []):
                    with self.profile.config.editing() as config:   # only when there is something new
                        found = config.setdefault("save_candidates", {}).setdefault(key, [])
                        if hit["folder"] not in found:
                            found.insert(0, hit["folder"])
            except (ProfileError, cat.CatalogError, OSError, ValueError):
                continue
            state = self.profile.state()
        self._saves_at = time.monotonic()
        return added

    def poll(self, running: set[str] | None = None) -> list[dict]:
        if not self.enabled():
            return []
        running = executable.running_executables() if running is None else running
        self._refresh()
        if time.monotonic() - self._saves_at > SAVE_SCAN_EVERY:
            try:
                self.discover_saves()
            except Exception:  # noqa: BLE001 - discovery is best effort; recognition goes on
                self._saves_at = time.monotonic()
        # A program that stopped is judged afresh the next time it starts.
        self.known = {k: v for k, v in self.known.items() if k in running}
        found = []
        for path in running:
            if path in self.known or ("\\" not in path and "/" not in path):
                continue
            match = self.identify(path)
            self.known[path] = None
            if not match:
                continue
            if match[0] == "local" and self._local.get(path) == match[1]:
                self.known[path] = match[1]                 # still unnamed: nothing new to say
                continue
            try:
                found.append(self._adopt(path, match))
                self.known[path] = found[-1]["game_id"]
            except (ProfileError, cat.CatalogError, OSError, ValueError):
                continue
        return found

    def _adopt(self, path: str, match: tuple) -> dict:
        kind, ref = match[0], match[1]
        state = self.profile.state()
        if kind in ("gog", "local"):
            game_id = f"gog-{ref}" if kind == "gog" else ref
            if game_id not in state["games"]:
                self.profile.register_game(game_id, match[2], platform="PC (GOG)" if kind == "gog" else "PC",
                                           external_ids={"gog": ref} if kind == "gog" else {})
        elif kind == "steam":
            game_id = f"steam-{ref}"
            if game_id not in state["games"]:
                if self._index is not None and self._index.get(ref):
                    cat.add_to_profile(self.profile, self._index, ref)
                else:
                    self.profile.register_game(game_id, getattr(self, "_names", {}).get(ref) or Path(path).parent.name,
                                               platform="PC", external_ids={"steam": ref})
        else:
            game_id = ref
        if kind != "local":                         # tracked as a local game before it was named: link it
            title = folder_title(path)
            stand_in = local_game_id(title) if title else None
            games = self.profile.state()["games"]
            if stand_in in games and not games[stand_in].get("linked_to") and stand_in != game_id:
                self.profile.link_games(stand_in, game_id)
        game = self.profile.state()["games"][game_id]
        if game.get("status") in (None, "backlog", "wishlist"):
            self.profile.set_status(game_id, "playing")
        if Path(path).is_file():
            executable.register(self.profile, game_id, Path(path), source="detected")
        saves = find_save_folders(game.get("title") or game_id)
        unity = unity_info(path)
        if unity:                                   # the engine says exactly where: first
            exact = savefile.root_folder("LOCALLOW")
            exact = exact / unity[0] / unity[1] if exact is not None else None
            if exact is not None and exact.is_dir():
                saves = [str(exact)] + [f for f in saves if os.path.normcase(f) != os.path.normcase(str(exact))]
        with self.profile.config.editing() as config:
            config.setdefault("save_candidates", {})[f"{self.profile.profile_id}:{game_id}"] = saves
        return {"game_id": game_id, "title": game.get("title"), "path": path, "save_folders": saves}


def set_enabled(profile: Profile, on: bool) -> None:
    with profile.config.editing() as config:
        config.setdefault("autodetect", {})[profile.profile_id] = on


def save_candidates(profile: Profile, game_id: str) -> list[str]:
    return profile.config.load().get("save_candidates", {}).get(f"{profile.profile_id}:{game_id}", [])
