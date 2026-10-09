"""Games other stores installed, named from each store's own records, and the
save folders those stores keep.

Recognising a game by its folder name works for anything, but guesses. Every
store also writes down what it installed and where, and those records name
the game exactly:

- Epic Games Store: `%PROGRAMDATA%/Epic/EpicGamesLauncher/Data/Manifests/
  *.item`, one JSON file per install (DisplayName, InstallLocation, AppName;
  add-ons and engine plugins skipped as Playnite's Epic library does). On
  Linux, Heroic's `legendaryConfig/legendary/installed.json`.
- Ubisoft Connect: `HKLM\\SOFTWARE\\WOW6432Node\\Ubisoft\\Launcher\\Installs\\
  <app id>` InstallDir (the name is the folder's, as Playnite reads it).
- Amazon Games: `%LOCALAPPDATA%/Amazon Games/Data/Games/Sql/
  GameInstallInfo.sqlite`, table DbSet (Id, InstallDirectory, ProductTitle,
  Installed), opened read-only.
- Battle.net: the Windows uninstall entries it writes, whose UninstallString
  carries `--uid=<product>`.
- EA app: its uninstall entries (publisher Electronic Arts) whose folder holds
  `__Installer/installerdata.xml`, the content id from that file.
- Xbox app (PC Game Pass): `MicrosoftGame.config` in the game's Content
  folder: Identity Name, TitleId (8 hex digits), ShellVisuals
  DefaultDisplayName.

Saves those stores keep in their own place: Ubisoft Connect's
`savegames/<user>/<app id>`, the Xbox app's `Packages/<package>_*/
SystemAppData/wgs`, and Steam Cloud's `userdata/<user>/<app id>/remote`.
Each of those is synced by its store, so the save keeper copies them but
never puts one back by itself: the store's own cloud would fight it.

Read-only. Nothing here goes over the network.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

MAX_RECORD_BYTES = 1024 * 1024
# Stores whose games have no achievements of their own here: a catalogue title
# with the same name gives them a list, else the achievement finder looks.
FINDABLE = ("local-", "epic-", "ea-", "battlenet-", "amazon-")
PLATFORMS = {"epic": "PC (Epic)", "ea": "PC (EA)", "ubisoft": "PC (Ubisoft)", "battlenet": "PC (Battle.net)",
             "amazon": "PC (Amazon)", "xbox": "PC (Xbox)"}
UNINSTALL_KEYS = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                  r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
_LAUNCHERS = ("ea app", "ea desktop", "origin", "battle.net", "ubisoft connect", "epic games launcher")


@dataclass(frozen=True)
class Install:
    store: str          # epic, ea, ubisoft, battlenet, amazon, xbox
    ref: str            # the store's own id for the game
    title: str
    folder: str
    package: str = ""   # Xbox only: the package name its saves are kept under


def game_id(inst: Install) -> str:
    """The library id: the one the store's own link uses where there is one
    (Ubisoft, Xbox), else `<store>-<id>`."""
    if inst.store == "xbox":
        return f"xbox-{int(inst.ref, 16)}"
    ref = re.sub(r"[^a-z0-9._-]+", "-", inst.ref.lower()).strip("-.")[:100] or "game"
    return f"{inst.store}-{ref}"


def has_own_id(inst: Install) -> bool:
    """Ubisoft and Xbox games keep their store id even when a catalogue
    title matches: their own links (LumaPlay, Xbox) add the achievements."""
    return inst.store == "ubisoft" or inst.store == "xbox"


def _read(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_RECORD_BYTES).decode("utf-8-sig", errors="replace")
    except OSError:
        return None


def _clean(title) -> str:
    text = re.sub(r"[\u2122\u00ae\u00a9]", "", str(title or ""))     # trademarks
    return " ".join(text.split())[:200]


# ---- Epic ------------------------------------------------------------------------------

def epic_installs(manifests: Path | None = None, heroic: Path | None = None) -> list[Install]:
    if manifests is None and sys.platform == "win32":
        manifests = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    if heroic is None and sys.platform.startswith("linux"):
        heroic = Path.home() / ".config" / "heroic" / "legendaryConfig" / "legendary" / "installed.json"
    out = []
    try:
        items = sorted(manifests.glob("*.item")) if manifests is not None else []
    except OSError:
        items = []
    for item in items:
        try:
            data = json.loads(_read(item) or "")
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        cats = [str(c) for c in data.get("AppCategories") or []]
        if ("addons" in cats and "addons/launchable" not in cats) or any(c in ("plugins", "plugins/engine", "engines")
                                                                          for c in cats):
            continue
        if any(str(a).startswith("UE_") for a in data.get("CompatibleApps") or []):
            continue
        folder, name = data.get("InstallLocation"), str(data.get("AppName") or "")
        if folder and name and not name.startswith("UE_") and not data.get("bIsIncompleteInstall"):
            out.append(Install("epic", name, _clean(data.get("DisplayName")) or Path(folder).name, str(folder)))
    text = _read(heroic) if heroic is not None else None
    try:
        installed = json.loads(text) if text else {}
    except ValueError:
        installed = {}
    for name, app in (installed.items() if isinstance(installed, dict) else []):
        if isinstance(app, dict) and app.get("install_path") and not app.get("is_dlc"):
            out.append(Install("epic", str(app.get("app_name") or name), _clean(app.get("title")) or str(name),
                               str(app["install_path"])))
    return out


# ---- Amazon ----------------------------------------------------------------------------

def amazon_installs(db: Path | None = None) -> list[Install]:
    if db is None:
        if sys.platform != "win32":
            return []
        db = Path(os.environ.get("LOCALAPPDATA", "")) / "Amazon Games" / "Data" / "Games" / "Sql" / "GameInstallInfo.sqlite"
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute("SELECT Id, InstallDirectory, ProductTitle FROM DbSet WHERE Installed = 1").fetchall()
        finally:
            con.close()
    except (sqlite3.Error, ValueError):
        return []
    return [Install("amazon", str(i), _clean(t) or Path(d).name, str(d)) for i, d, t in rows if i and d]


# ---- the registry: Ubisoft's installs, and uninstall entries (Battle.net, EA) -------------

def _registry():
    from . import ubisoft                       # its small winreg helpers
    return ubisoft


def uninstall_entries() -> list[dict]:
    """Every program's uninstall entry, machine-wide and for this user."""
    if sys.platform != "win32":
        return []
    reg = _registry()
    winreg = reg._winreg()
    out = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in UNINSTALL_KEYS:
            for sub in reg._subkeys(root, key):
                values = reg._values(root, f"{key}\\{sub}")
                if values:
                    out.append({**values, "_key": sub})
    return out


def ubisoft_folders() -> dict[str, str]:
    """App id -> install folder, from Ubisoft Connect's registry."""
    if sys.platform != "win32":
        return {}
    reg = _registry()
    root = reg._winreg().HKEY_LOCAL_MACHINE
    out = {}
    for app in reg._subkeys(root, f"{reg.LAUNCHER_KEY}\\Installs"):
        folder = reg._values(root, f"{reg.LAUNCHER_KEY}\\Installs\\{app}").get("InstallDir")
        if app.isdigit() and folder:
            out[app] = str(folder).replace("/", "\\" if sys.platform == "win32" else "/").rstrip("\\/")
    return out


def ubisoft_installs(folders: dict[str, str] | None = None) -> list[Install]:
    folders = ubisoft_folders() if folders is None else folders
    return [Install("ubisoft", app, _clean(Path(f.replace("\\", "/")).name), f) for app, f in folders.items()]


def registry_installs(entries: list[dict] | None = None) -> list[Install]:
    """Battle.net and EA app games from their uninstall entries."""
    entries = uninstall_entries() if entries is None else entries
    out = []
    for e in entries:
        name, folder = _clean(e.get("DisplayName")), str(e.get("InstallLocation") or "").strip().strip('"')
        if not name or not folder or name.lower() in _LAUNCHERS:
            continue
        publisher, uninstall = str(e.get("Publisher") or ""), str(e.get("UninstallString") or "")
        uid = re.search(r"--uid=([^\s\"]+)", uninstall)
        if uid and "battle.net" in uninstall.lower():
            out.append(Install("battlenet", uid.group(1), name, folder))
        elif publisher.lower().startswith("electronic arts"):
            # EA's own services (its anti-cheat, the app) carry the same
            # publisher: only a folder with the EA installer's record is a game.
            content = ea_content_id(folder)
            if content:
                out.append(Install("ea", content, name, folder))
    return out


# ---- markers inside a game's own folder ------------------------------------------------

def ea_content_id(folder: str) -> str | None:
    """The first content id in the EA installer record inside the game folder."""
    text = _read(Path(folder) / "__Installer" / "installerdata.xml")
    if not text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.lower().endswith("contentid") and (el.text or "").strip():
            return el.text.strip()
    return None


def xbox_config(folder: Path) -> Install | None:
    """The Xbox app's MicrosoftGame.config in this folder, if there is one."""
    text = _read(folder / "MicrosoftGame.config")
    if not text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    identity, title_id, shell = root.find("Identity"), (root.findtext("TitleId") or "").strip(), root.find("ShellVisuals")
    package = identity.get("Name", "") if identity is not None else ""
    if not re.fullmatch(r"[0-9A-Fa-f]{8}", title_id) or title_id.upper() == "FFFFFFFF" or not package:
        return None
    name = shell.get("DefaultDisplayName", "") if shell is not None else ""
    if not name or name.startswith("ms-resource:"):
        name = folder.parent.name if folder.name.lower() == "content" else folder.name
    return Install("xbox", title_id.upper(), _clean(name), str(folder), package=package)


def ea_marker(folder: Path) -> Install | None:
    content = ea_content_id(str(folder))
    return Install("ea", content, _clean(folder.name), str(folder)) if content else None


# ---- one index of everything installed --------------------------------------------------

def _key(folder: str) -> str:
    return os.path.normcase(os.path.normpath(folder.replace("\\", os.sep) if os.sep == "/" else folder))


class StoreIndex:
    """Install folders the stores recorded. `find(program)` names the game a
    running program belongs to, or None."""

    def __init__(self, installs: list[Install] | None = None):
        self.by_folder: dict[str, Install] = {}
        for inst in installs if installs is not None else everything():
            self.by_folder.setdefault(_key(inst.folder), inst)

    def find(self, path: str) -> Install | None:
        here = Path(path).parent
        for folder in [here, *list(here.parents)[:4]]:
            if folder == folder.parent:
                break
            inst = self.by_folder.get(_key(str(folder)))
            if inst is not None:
                return inst
            # Markers the store left inside the game folder itself.
            inst = xbox_config(folder) or (ea_marker(folder) if (folder / "__Installer").is_dir() else None)
            if inst is not None:
                return inst
        return None


def everything() -> list[Install]:
    out = []
    for reader in (epic_installs, amazon_installs, ubisoft_installs, registry_installs):
        try:
            out += reader()
        except Exception:  # noqa: BLE001 - one store's records being odd never stops the others
            continue
    return out


# ---- the stores' own save folders -------------------------------------------------------

def _has_files(folder: Path) -> bool:
    try:
        for _root, _dirs, files in os.walk(folder):
            if files:
                return True
    except OSError:
        return False
    return False


def ubisoft_saves(connect: Path | None, app: str) -> list[str]:
    """Ubisoft Connect's `savegames/<user>/<app id>` folders for one game."""
    base = connect / "savegames" if connect is not None else None
    try:
        users = [u for u in base.iterdir() if u.is_dir()] if base is not None else []
    except OSError:
        return []
    return [str(u / app) for u in users if (u / app).is_dir()]


def ubisoft_saved_games(connect: Path | None) -> dict[str, list[str]]:
    """App id -> its save folders holding files, for every game Ubisoft Connect saved."""
    out: dict[str, list[str]] = {}
    base = connect / "savegames" if connect is not None else None
    try:
        users = [u for u in base.iterdir() if u.is_dir()] if base is not None else []
    except OSError:
        return {}
    for user in users:
        try:
            apps = [a for a in user.iterdir() if a.is_dir() and a.name.isdigit()]
        except OSError:
            continue
        for app in apps:
            if _has_files(app):
                out.setdefault(app.name, []).append(str(app))
    return out


def xbox_saves(local_appdata: Path | None, package: str) -> list[str]:
    """The Xbox app's synced save containers for one package."""
    base = local_appdata / "Packages" if local_appdata is not None else None
    if base is None or not package:
        return []
    try:
        found = [p for p in base.iterdir() if p.name.lower().startswith(package.lower() + "_")]
    except OSError:
        return []
    return [str(p / "SystemAppData" / "wgs") for p in found if (p / "SystemAppData" / "wgs").is_dir()]


def steam_cloud(steam_root: Path | None, appids: set[int]) -> dict[int, list[str]]:
    """App id -> `userdata/<user>/<app id>/remote` folders holding files."""
    base = steam_root / "userdata" if steam_root is not None else None
    try:
        users = [u for u in base.iterdir() if u.is_dir() and u.name.isdigit()] if base is not None else []
    except OSError:
        return {}
    out: dict[int, list[str]] = {}
    for user in users:
        for appid in appids:
            remote = user / str(appid) / "remote"
            if remote.is_dir() and _has_files(remote):
                out.setdefault(appid, []).append(str(remote))
    return out


def store_synced(folder: str) -> bool:
    """A folder a store's own cloud keeps in step: never put back automatically."""
    low = str(folder).replace("\\", "/").lower().rstrip("/")
    parts = low.split("/")
    if "systemappdata" in parts and parts[-1] == "wgs":
        return True
    if "savegames" in parts and any("ubisoft" in p for p in parts):
        return True
    return parts[-1] == "remote" and "userdata" in parts


def save_folders(inst: Install, connect: Path | None = None, local_appdata: Path | None = None) -> list[str]:
    """Where this store keeps the game's saves, when it keeps them itself."""
    if inst.store == "ubisoft":
        from . import ubisoft
        return ubisoft_saves(connect if connect is not None else ubisoft.connect_dir(), inst.ref)
    if inst.store == "xbox":
        if local_appdata is None and os.environ.get("LOCALAPPDATA"):
            local_appdata = Path(os.environ["LOCALAPPDATA"])
        return xbox_saves(local_appdata, inst.package)
    return []


def unreal_saves(path: str, local_appdata: Path | None) -> list[str]:
    """An Unreal game's own save folder, from its project name: the folder
    above `Binaries` (`<Project>/Binaries/Win64/<Project>-Win64-Shipping.exe`).
    Saves go to `%LOCALAPPDATA%/<Project>/Saved/SaveGames`, or beside the game."""
    try:
        p = Path(os.path.realpath(path))           # the process list gives lower case; the disk has the real name
    except OSError:
        p = Path(path)
    binaries = next((a for a in list(p.parents)[:3] if a.name.lower() == "binaries"), None)
    if binaries is None:
        return []
    project = binaries.parent
    places = [project / "Saved" / "SaveGames"]
    if local_appdata is not None:
        places.insert(0, local_appdata / project.name / "Saved" / "SaveGames")
    return [str(f) for f in places if f.is_dir()]
