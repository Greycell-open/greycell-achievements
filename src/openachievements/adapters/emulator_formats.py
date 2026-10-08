"""Where each Steam emulator keeps the achievements it recorded, and how to read them.

Locations and formats follow what the community already documents for these
emulators (the open source Achievement Watcher project and its maintained
fork), so a copy of a game made by any of these groups is read the same way:

  fixed folders, one sub-folder per Steam app id
    CODEX          Public Documents/Steam/CODEX, Roaming/Steam/CODEX
    RUNE           Public Documents/Steam/RUNE
    OnlineFix      Public Documents/OnlineFix
    EMPRESS        Roaming/EMPRESS, Public Documents/EMPRESS (then remote/<appid>)
    Goldberg, GSE  Roaming/Goldberg SteamEmu Saves, Roaming/GSE Saves
    SmartSteamEmu  Roaming/SmartSteamEmu (stats.bin, binary)
    CreamAPI       Roaming/CreamAPI
    Reloaded, 3DM  ProgramData/Steam/<any folder>
    SKIDROW        Local/SKIDROW, Documents/SkidRow

  beside the game, found from its emulator settings file (only for programs
  the library already knows)
    TENOKE         tenoke.ini -> SteamData/user_stats.ini
    ALI213         ALI213.ini, valve.ini, SteamConfig.ini -> Profile/<player>/Stats
                   (or Documents/VALVE/<appid>/<player>/Stats)
    Hoodlum, DARKSiDERS, SKIDROW
                   hlm.ini, ds.ini, steam_api.ini -> SteamEmu/UserStats
                   (or Documents/<user>/<appid>/SteamEmu)
    UniverseLAN    UniverseLAN.ini -> UniverseLANData
    Goldberg, GSE  portable saves: steam_settings/configs.user.ini
                   ([user::saves] local_save_path, relative to the emulator's
                   dll, or saves_folder_name under Roaming), or the older
                   local_save.txt beside the dll; then <folder>/<appid>.
                   steam_settings/achievements.json is the game's list of
                   achievements, not unlocks, and is never read as unlocks.

Every format answers the same question, which achievements are unlocked and
when, and only an explicit "achieved" counts: a wrong guess at a format can
miss an unlock, never invent one.
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
import zlib
from pathlib import Path

# (emulator, root, folder holding one folder per app id, sub-path inside it or "" for the app folder)
LOCATIONS = (
    ("CODEX", "PUBLIC_DOCUMENTS", "Steam/CODEX", ""),
    ("CODEX", "APPDATA", "Steam/CODEX", ""),
    ("RUNE", "PUBLIC_DOCUMENTS", "Steam/RUNE", ""),
    ("OnlineFix", "PUBLIC_DOCUMENTS", "OnlineFix", ""),
    ("Goldberg", "APPDATA", "Goldberg SteamEmu Saves", ""),
    ("GSE", "APPDATA", "GSE Saves", ""),
    ("EMPRESS", "APPDATA", "EMPRESS", "remote/{appid}"),
    ("EMPRESS", "PUBLIC_DOCUMENTS", "EMPRESS", "remote/{appid}"),
    ("SmartSteamEmu", "APPDATA", "SmartSteamEmu", ""),
    ("CreamAPI", "APPDATA", "CreamAPI", ""),
    ("SKIDROW", "LOCALAPPDATA", "SKIDROW", ""),
    ("SKIDROW", "DOCUMENTS", "SkidRow", ""),
    ("Reloaded/3DM", "PROGRAMDATA", "Steam/*", ""),
)
# Tried in this order inside an app's folder; the first that exists is the one.
FILES = ("achievements.ini", "achievements.json", "achiev.ini", "stats.ini", "Achievements.Bin", "achieve.dat",
         "Achievements.ini", "stats/achievements.ini", "Stats/Achievements.ini", "stats.bin",
         "stats/CreamAPI.Achievements.cfg", "user_stats.ini", "UserStats/achiev.ini")
SETTINGS_FILES = ("tenoke.ini", "ALI213.ini", "valve.ini", "SteamConfig.ini", "hlm.ini", "ds.ini",
                  "steam_api.ini", "UniverseLAN.ini", "configs.user.ini", "local_save.txt")
_TRUE = ("1", "true", "yes")
_SKIP_SECTIONS = {"steamachievements", "steam64", "steam"}
_ACHIEVED_KEYS = ("achieved", "haveachieved", "unlocked", "earned")
_TIME_KEYS = ("unlocktime", "haveachievedtime", "unlock_time", "timestamp", "earned_time", "time")


def file_format(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".json"):
        return "json"
    if name == "stats.bin":
        return "sse"
    return "ini"


def first_file(folder: Path) -> Path | None:
    for rel in FILES:
        path = folder.joinpath(*rel.split("/"))
        if path.is_file():
            return path
    return None


# ---- reading -------------------------------------------------------------------------------

def text_of(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8", "replace").lstrip("\ufeff")


def when(value) -> float | None:
    try:
        t = float(str(value).strip().strip('"'))
    except (TypeError, ValueError):
        return None
    return t if t > 0 else None


def _hex_le(value) -> int | None:
    """Reloaded and 3DM write numbers as little-endian hex ("01000000" is 1)."""
    text = str(value).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}", text):
        return None
    return struct.unpack("<I", bytes.fromhex(text))[0]


def ini_sections(text: str) -> dict[str, dict[str, str]]:
    """A forgiving INI reader: sections, then key = value lines (keys may be
    quoted, as TENOKE writes them). Raises ValueError when nothing in it
    looks like INI at all."""
    sections: dict[str, dict[str, str]] = {}
    current = None
    seen_any = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            current = line[1:-1].strip().strip('"')
            sections.setdefault(current, {})
            seen_any = True
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().strip('"')
        if not key:
            continue
        sections.setdefault(current or "", {})[key] = value.strip()
        seen_any = True
    if not seen_any or not any(k for k in sections if k):
        if not any(sections.get("", {})):
            raise ValueError("not INI-style")
    return sections


def _from_ini(text: str) -> dict[str, float | None]:
    sections = ini_sections(text)
    lower = {k.lower(): k for k in sections}
    unlocked: dict[str, float | None] = {}
    if "achievements" in lower and "achievementsunlocktimes" in lower:            # Hoodlum, DARKSiDERS
        flags, times = sections[lower["achievements"]], sections[lower["achievementsunlocktimes"]]
        for name, value in flags.items():
            if str(value).strip().lower() in _TRUE:
                unlocked[name] = when(times.get(name))
        return unlocked
    if "state" in lower and "time" in lower:                                       # 3DM
        states, times = sections[lower["state"]], sections[lower["time"]]
        for name, value in states.items():
            if str(value).strip() == "0101":
                unlocked[name] = when(_hex_le(times.get(name)) if times.get(name) else None)
        return unlocked
    if "achievements" in lower:                                                    # TENOKE
        for name, value in sections[lower["achievements"]].items():
            if re.search(r"unlocked\s*=\s*true", value, re.I):
                found = re.search(r"time\s*=\s*(\d+)", value, re.I)
                unlocked[name] = when(found.group(1)) if found else None
        return unlocked
    data_section = lower.get("achieve_data")
    if data_section is not None:                                                   # flat name = 1 lists
        for name, value in sections[data_section].items():
            if str(value).strip().lower() in _TRUE:
                unlocked[name] = None
    for section, values in sections.items():
        if not section or section.lower() in _SKIP_SECTIONS or section == data_section:
            continue
        keys = {k.lower(): v for k, v in values.items()}
        achieved = any(str(keys.get(k, "")).strip().lower() in _TRUE for k in _ACHIEVED_KEYS)
        state = keys.get("state")
        if not achieved and state is not None:                                     # Reloaded
            achieved = _hex_le(state) == 1 or str(state).strip() == "1"
        if not achieved:
            continue
        stamp = None
        for k in _TIME_KEYS:
            if keys.get(k):
                raw = keys[k]
                stamp = when(_hex_le(raw)) if (state is not None and _hex_le(raw) is not None) else when(raw)
                if k == "unlocktime" and len(str(raw).strip()) == 7:
                    stamp = None                                                   # CreamAPI's cut-off times
                break
        unlocked[section] = stamp
    return unlocked


def _from_json(text: str) -> dict[str, float | None]:
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"not JSON ({exc})") from None
    items = doc.items() if isinstance(doc, dict) else (
        (i.get("name"), i) for i in doc if isinstance(i, dict)) if isinstance(doc, list) else ()
    unlocked = {}
    for name, item in items:
        if not name or not isinstance(item, dict):
            continue
        if any(item.get(k) in (True, 1, "1", "true") for k in ("earned", "achieved", "unlocked")):
            unlocked[str(name)] = when(item.get("earned_time") or item.get("unlock_time")
                                       or item.get("UnlockTime") or item.get("time"))
    return unlocked


def _from_sse(data: bytes) -> dict[str, float | None]:
    """SmartSteamEmu's stats.bin: a count, then 24-byte entries keyed by the
    CRC32 of the achievement's Steam name; value 1 is unlocked. Names come
    back as "crc:<8 hex>" for the caller to match."""
    if len(data) < 4:
        raise ValueError("stats.bin is too short")
    count = struct.unpack("<i", data[:4])[0]
    body = data[4:]
    if count < 0 or len(body) != count * 24:
        raise ValueError("stats.bin does not have the entries it announces")
    unlocked = {}
    for i in range(count):
        entry = body[i * 24:(i + 1) * 24]
        crc = struct.unpack("<I", entry[0:4])[0]
        unlock_time = struct.unpack("<i", entry[8:12])[0]
        value = struct.unpack("<i", entry[20:24])[0]
        if value == 1:
            unlocked[f"crc:{crc:08x}"] = when(unlock_time)
    return unlocked


def parse(data: bytes, fmt: str) -> dict[str, float | None]:
    """Steam internal name -> unlock time (Unix seconds, or None when the file
    kept none), for the achievements the file marks as unlocked."""
    if fmt == "sse":
        return _from_sse(data)
    text = text_of(data)
    if fmt == "json":
        return _from_json(text)
    return _from_ini(text)


def crc_name(api_name: str) -> str:
    return f"crc:{zlib.crc32(api_name.encode('utf-8')) & 0xffffffff:08x}"


# ---- finding the files -----------------------------------------------------------------------

def documents() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents"


def windows_roots() -> dict[str, Path]:
    public = os.environ.get("PUBLIC") or str(Path(os.environ.get("SystemDrive", "C:") + "\\") / "Users" / "Public")
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    program = os.environ.get("PROGRAMDATA") or "C:\\ProgramData"
    return {"PUBLIC_DOCUMENTS": Path(public) / "Documents", "APPDATA": Path(appdata), "LOCALAPPDATA": Path(local),
            "PROGRAMDATA": Path(program), "DOCUMENTS": documents()}


def fixed_files(roots: dict[str, Path]) -> list[tuple[str, int, Path]]:
    """(emulator, app id, file) for every achievement file in the fixed folders."""
    out = []
    for emulator, root, folder, inner in LOCATIONS:
        base = roots.get(root)
        if base is None:
            continue
        parts = folder.split("/")
        bases = [base.joinpath(*parts)]
        if parts[-1] == "*":
            try:
                bases = [Path(e.path) for e in os.scandir(base.joinpath(*parts[:-1])) if e.is_dir()]
            except OSError:
                continue
        for b in bases:
            try:
                entries = [e for e in os.scandir(b) if e.name.isdigit() and e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for e in entries:
                folder_path = Path(e.path).joinpath(*inner.format(appid=e.name).split("/")) if inner else Path(e.path)
                found = first_file(folder_path)
                if found is not None:
                    out.append((emulator, int(e.name), found))
    return out


def _settings(path: Path) -> dict[str, dict[str, str]]:
    try:
        return ini_sections(text_of(path.read_bytes()[:256 * 1024]))
    except (OSError, ValueError):
        return {}


def _get(sections: dict, section: str, key: str) -> str | None:
    for name, values in sections.items():
        if name.lower() == section.lower():
            for k, v in values.items():
                if k.lower() == key.lower():
                    return v.split("#")[0].split(";")[0].strip().strip('"')
    return None


def _up(start: Path, rel: str, levels: int = 4) -> Path | None:
    here = start
    for _ in range(levels + 1):
        candidate = here.joinpath(*rel.split("/"))
        if candidate.is_dir():
            return candidate
        if here.parent == here:
            break
        here = here.parent
    return None


def from_settings(cfg: Path) -> list[tuple[str, int, Path]]:
    """(emulator, app id, folder holding its achievement file) from one
    emulator settings file beside a game."""
    s = _settings(cfg)
    if not s:
        return []
    name = cfg.name.lower()
    out = []

    def add(emulator, appid, folder):
        if appid and str(appid).isdigit() and folder is not None:
            out.append((emulator, int(appid), folder))

    if name == "tenoke.ini":
        appid = _get(s, "TENOKE", "id")
        add("TENOKE", appid, _up(cfg.parent, "SteamData"))
    elif name in ("ali213.ini", "valve.ini", "steamconfig.ini"):
        appid, player, save_type = (_get(s, "Settings", k) for k in ("AppID", "PlayerName", "SaveType"))
        if player and save_type == "1":
            add("ALI213", appid, documents() / "VALVE" / str(appid) / player / "Stats")
        elif player:
            add("ALI213", appid, _up(cfg.parent, f"Profile/{player}/Stats"))
        else:
            add("ALI213", appid, _up(cfg.parent, "Profile/Stats"))
    elif name in ("hlm.ini", "ds.ini", "steam_api.ini"):
        emulator = {"hlm.ini": "Hoodlum", "ds.ini": "DARKSiDERS"}.get(name, "SKIDROW")
        appid = _get(s, "GameSettings", "AppId")
        where = (_get(s, "GameSettings", "UserDataFolder") or "").lower()
        if where == "mydocs":
            user = _get(s, "GameSettings", "UserName")
            if user:
                base = documents() / user / str(appid) / "SteamEmu"
                add(emulator, appid, base / "UserStats" if (base / "UserStats").is_dir() else base)
        elif appid:
            add(emulator, appid, _up(cfg.parent, "SteamEmu/UserStats") or _up(cfg.parent, "SteamEmu")
                or (_up(cfg.parent, "Profile/VALVE/Stats") if name == "hlm.ini" else None))
        elif name == "steam_api.ini":
            appid, steam_id = _get(s, "Settings", "AppId"), _get(s, "Settings", "SteamID")
            if steam_id:
                add("SKIDROW", appid, _up(cfg.parent, f"SteamProfile/{steam_id}"))
    elif name == "universelan.ini":
        add("UniverseLAN", _get(s, "GameSettings", "AppID"), _up(cfg.parent, "UniverseLANData"))
    return out


def _appid_near(folder: Path) -> str | None:
    """steam_appid.txt beside the emulator's dll or in its steam_settings."""
    for candidate in (folder / "steam_appid.txt", folder / "steam_settings" / "steam_appid.txt"):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip().split()[0]
        except (OSError, IndexError):
            continue
        if text.isdigit():
            return text
    return None


def _save_folders(base: Path, dll_dir: Path, emulator: str) -> list[tuple[str, int, Path]]:
    """Goldberg and GSE keep one folder per app id under their save folder."""
    out = []
    try:
        for e in os.scandir(base):
            if e.name.isdigit() and e.is_dir(follow_symlinks=False):
                out.append((emulator, int(e.name), Path(e.path)))
    except OSError:
        return out
    appid = _appid_near(dll_dir)
    if not out and appid and (base / "achievements.json").is_file():
        out.append((emulator, int(appid), base))
    return out


def portable_saves(cfg: Path) -> list[tuple[str, int, Path]]:
    """Goldberg and GSE saves moved out of their default folder by the game's
    own settings: configs.user.ini in steam_settings, or local_save.txt."""
    name = cfg.name.lower()
    if name == "configs.user.ini" and cfg.parent.name.lower() == "steam_settings":
        dll_dir = cfg.parent.parent
        local = _get(_settings(cfg), "user::saves", "local_save_path")
        folder_name = _get(_settings(cfg), "user::saves", "saves_folder_name")
        if local:
            base = Path(local) if Path(local).is_absolute() else dll_dir / local
            return _save_folders(base, dll_dir, "GSE")
        if folder_name and folder_name.lower() != "gse saves":
            return _save_folders(windows_roots()["APPDATA"] / folder_name, dll_dir, "GSE")
        return []
    if name == "local_save.txt":
        try:
            folder = cfg.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0].strip()
        except (OSError, IndexError):
            return []
        if folder:
            base = Path(folder) if Path(folder).is_absolute() else cfg.parent / folder
            return _save_folders(base, cfg.parent, "Goldberg")
    return []


def settings_files(game_dir: Path, limit: int = 4000) -> list[Path]:
    """Emulator settings files in a game's folder, at most four levels down
    (Unity keeps them in <Game>_Data/Plugins/x86_64)."""
    wanted = {n.lower() for n in SETTINGS_FILES}
    found, seen = [], 0
    stack = [(game_dir, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen > limit:
                return found
            try:
                if e.is_file() and e.name.lower() in wanted:
                    found.append(Path(e.path))
                elif e.is_dir(follow_symlinks=False) and depth < 4:
                    stack.append((Path(e.path), depth + 1))
            except OSError:
                continue
    return found


def game_files(game_dirs: list[Path]) -> list[tuple[str, int, Path]]:
    """(emulator, app id, file) for the achievement files beside these games."""
    out = []
    for game_dir in game_dirs:
        for cfg in settings_files(game_dir):
            found_here = portable_saves(cfg) if cfg.name.lower() in ("configs.user.ini", "local_save.txt") \
                else from_settings(cfg)
            for emulator, appid, folder in found_here:
                found = first_file(folder) if folder.is_dir() else None
                if found is not None:
                    out.append((emulator, appid, found))
    return out


def on_windows() -> bool:
    return sys.platform == "win32"
