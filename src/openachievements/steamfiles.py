"""Reading the files the Steam client keeps on this computer.

Only reading. These are the player's own files: which account signed in
(`config/loginusers.vdf`), each played game's achievement schema
(`appcache/stats/UserGameStatsSchema_<appid>.bin`) and the player's unlocks
with their times (`appcache/stats/UserGameStats_<account>_<appid>.bin`). Steam
rewrites a game's stats file when the game records progress, which is what
makes live capture possible without an account link, an API key, or anything
running inside the game.
"""
from __future__ import annotations

import html
import os
import re
import struct
import sys
from pathlib import Path

STEAMID64_BASE = 76561197960265728
MAX_FILE_BYTES = 16 * 1024 * 1024


class SteamFileError(ValueError):
    pass


def steam_dir() -> Path | None:
    """Where Steam is installed: $OA_STEAM_DIR, the registry on Windows, or the
    usual folders elsewhere."""
    if os.environ.get("OA_STEAM_DIR"):
        return Path(os.environ["OA_STEAM_DIR"])
    candidates = []
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                candidates.append(Path(winreg.QueryValueEx(key, "SteamPath")[0]))
        except OSError:
            pass
        candidates.append(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "Steam")
    else:
        candidates += linux_steam_dirs()
    return next((c for c in candidates if (c / "appcache" / "stats").is_dir()), None)


def linux_steam_dirs(home: Path | None = None) -> list[Path]:
    """Where Steam lives on Linux: the native client (and the Steam Deck), the
    Flatpak and the Snap."""
    home = Path(home or Path.home())
    return [home / ".steam" / "steam", home / ".steam" / "root", home / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
            home / "snap" / "steam" / "common" / ".local" / "share" / "Steam"]


def library_paths(root: Path | None) -> list[Path]:
    """Every Steam library folder: Steam's own and the ones in libraryfolders.vdf
    (a second drive, an SD card on the Steam Deck)."""
    if root is None:
        return []
    paths = [Path(root)]
    try:
        libraries = parse_text_vdf((Path(root) / "steamapps" / "libraryfolders.vdf").read_text(
            encoding="utf-8", errors="replace")).get("libraryfolders", {})
    except (OSError, SteamFileError):
        libraries = {}
    for value in libraries.values():
        if isinstance(value, dict) and value.get("path") and Path(value["path"]) not in paths:
            paths.append(Path(value["path"]))
    return paths


# A Windows game played through Proton keeps its saves in a Windows user folder
# inside its own prefix, one per game: steamapps/compatdata/<appid>/pfx.
PROTON_USER = ("pfx", "drive_c", "users", "steamuser")


def proton_users(root: Path | None, appid: str | int | None = None) -> dict[str, Path]:
    """appid -> the steamuser folder of its Proton prefix, in every library;
    only the one game's when `appid` is given."""
    found: dict[str, Path] = {}
    for lib in library_paths(root):
        compat = lib / "steamapps" / "compatdata"
        try:
            names = [str(appid)] if appid is not None else [e.name for e in os.scandir(compat) if e.name.isdigit()]
        except OSError:
            continue
        for name in names:
            user = compat.joinpath(name, *PROTON_USER)
            if name not in found and user.is_dir():
                found[name] = user
    return found


# ---- binary KeyValues (appcache/stats) ------------------------------------------------

def parse_binary_kv(data: bytes) -> dict:
    """Steam's binary KeyValues. Bounded: raises SteamFileError on anything it
    does not understand rather than guessing."""
    def node(i: int, depth: int) -> tuple[dict, int]:
        if depth > 32:
            raise SteamFileError("KeyValues nested too deeply")
        out = {}
        while True:
            if i >= len(data):
                raise SteamFileError("KeyValues ended early")
            t = data[i]
            i += 1
            if t in (8, 11):
                return out, i
            j = data.index(0, i)
            key = data[i:j].decode("utf-8", "replace")
            i = j + 1
            if t == 0:
                value, i = node(i, depth + 1)
            elif t == 1:
                j = data.index(0, i)
                value, i = data[i:j].decode("utf-8", "replace"), j + 1
            elif t in (2, 4, 6):
                value, i = struct.unpack_from("<i" if t == 2 else "<I", data, i)[0], i + 4
            elif t == 3:
                value, i = struct.unpack_from("<f", data, i)[0], i + 4
            elif t in (7, 10):
                value, i = struct.unpack_from("<Q" if t == 7 else "<q", data, i)[0], i + 8
            else:
                raise SteamFileError(f"unknown KeyValues type {t}")
            out[key] = value
    try:
        return node(0, 0)[0]
    except (ValueError, struct.error, IndexError) as exc:
        raise SteamFileError(f"unreadable KeyValues: {exc}") from None


def _read(path: Path) -> dict:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SteamFileError(f"{path.name} is unexpectedly large")
    return parse_binary_kv(path.read_bytes())


def _text(value) -> str:
    """Steam's binary files store a value as a number when it looks like one:
    Rusty's Retirement has an achievement named 666, kept as the integer 666.
    A name is text whatever its type; anything else is no text at all."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def read_schema(stats_dir: Path, appid: int) -> dict:
    """{"title": str | None, "achievements": [...]} in Steam's own order. Each
    achievement: api_name, name, description, hidden, icon (file name), stat, bit."""
    data = _read(stats_dir / f"UserGameStatsSchema_{appid}.bin").get(str(appid)) or {}
    found = []
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    for stat_id in sorted(stats, key=lambda k: int(k) if k.isdigit() else 0):
        bits = stats[stat_id].get("bits") if isinstance(stats[stat_id], dict) else None
        for bit_id in sorted(bits or {}, key=lambda k: int(k) if k.isdigit() else 0):
            bit = bits[bit_id]
            if not isinstance(bit, dict) or not bit.get("name"):
                continue
            d = bit.get("display") if isinstance(bit.get("display"), dict) else {}
            english = lambda f: " ".join(html.unescape(_text((d.get(f) or {}).get("english"))).split()) \
                if isinstance(d.get(f), dict) else ""
            found.append({"api_name": bit["name"], "name": english("name") or bit["name"],
                          "description": english("desc"), "hidden": str(d.get("hidden")) == "1",
                          "icon": (d.get("icon") or "").lower() or None, "stat": stat_id, "bit": int(bit_id)})
    # Steam stores some names HTML-escaped ("CAT &amp; ONION"); the page escapes on its own.
    title = html.unescape(_text(data.get("gamename"))) or None
    return {"title": title, "achievements": found}


def read_unlocks(stats_dir: Path, account_id: int, appid: int, schema: dict) -> dict[str, int | None]:
    """api_name -> unlock time (Unix seconds, or None when Steam kept no time)
    for every achievement whose bit is set."""
    cache = _read(stats_dir / f"UserGameStats_{account_id}_{appid}.bin").get("cache") or {}
    unlocked = {}
    for a in schema["achievements"]:
        stat = cache.get(a["stat"])
        if not isinstance(stat, dict) or not isinstance(stat.get("data"), int):
            continue
        if (stat["data"] & 0xFFFFFFFF) >> a["bit"] & 1:
            when = (stat.get("AchievementTimes") or {}).get(str(a["bit"]))
            unlocked[a["api_name"]] = when if isinstance(when, int) and when > 0 else None
    return unlocked


def stats_files(stats_dir: Path, account_id: int) -> dict[int, Path]:
    """appid -> this account's stats file."""
    pattern = re.compile(rf"^UserGameStats_{account_id}_(\d+)\.bin$")
    found = {}
    try:
        for entry in os.scandir(stats_dir):
            m = pattern.match(entry.name)
            if m and entry.is_file():
                found[int(m.group(1))] = Path(entry.path)
    except OSError:
        pass
    return found


# ---- text VDF (config/loginusers.vdf) ---------------------------------------------------

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_text_vdf(text: str) -> dict:
    stack, current, key = [], {}, None
    for m in _TOKEN.finditer(text):
        word, brace = m.group(1), m.group(2)
        if brace == "{":
            child = {}
            current[key] = child
            stack.append(current)
            current, key = child, None
        elif brace == "}":
            if not stack:
                raise SteamFileError("unbalanced VDF")
            current, key = stack.pop(), None
        elif key is None:
            key = word
        else:
            current[key] = word
            key = None
    return current


def signed_in_accounts(root: Path) -> list[dict]:
    """Accounts that have signed in to Steam on this computer, most recent
    first: steam_id (SteamID64), account_id (for file names), most_recent."""
    try:
        users = parse_text_vdf((root / "config" / "loginusers.vdf").read_text(encoding="utf-8", errors="replace"))
    except (OSError, SteamFileError):
        return []
    found = []
    for sid, info in (users.get("users") or {}).items():
        if sid.isdigit() and int(sid) > STEAMID64_BASE and isinstance(info, dict):
            found.append({"steam_id": sid, "account_id": int(sid) - STEAMID64_BASE,
                          "most_recent": info.get("MostRecent") == "1",
                          "timestamp": int(info["Timestamp"]) if str(info.get("Timestamp", "")).isdigit() else 0})
    found.sort(key=lambda a: (not a["most_recent"], -a["timestamp"]))
    return found


# ---- what this account has played (userdata/<account>/config/localconfig.vdf) -----------

def read_played(root: Path, account_id: int) -> dict[int, dict]:
    """appid -> {"last_played": Unix seconds, "playtime_minutes": int} for every
    app this account has run, as Steam records it on this computer. Only apps
    with some play time are returned."""
    path = root / "userdata" / str(account_id) / "config" / "localconfig.vdf"
    if path.stat().st_size > MAX_FILE_BYTES * 4:
        raise SteamFileError("localconfig.vdf is unexpectedly large")
    data = parse_text_vdf(path.read_text(encoding="utf-8", errors="replace"))
    store = data.get("UserLocalConfigStore") or data
    apps = ((((store.get("Software") or {}).get("Valve") or store.get("Software", {}).get("valve") or {})
             .get("Steam") or {}).get("apps")) or {}
    played = {}
    for appid, info in apps.items():
        if not (appid.isdigit() and isinstance(info, dict)):
            continue
        minutes = int(info["Playtime"]) if str(info.get("Playtime", "")).isdigit() else 0
        last = int(info["LastPlayed"]) if str(info.get("LastPlayed", "")).isdigit() else 0
        if minutes > 0 or last > 0:
            played[int(appid)] = {"last_played": last, "playtime_minutes": minutes}
    return played


def localconfig_path(root: Path, account_id: int) -> Path:
    return root / "userdata" / str(account_id) / "config" / "localconfig.vdf"
