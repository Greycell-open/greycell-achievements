"""The Steam app id a game's own folder declares.

A game is often named differently on disk than on Steam: "Hellraiser" in its
folders, "Clive Barker's Hellraiser: Revival" on the store. Matching by name
misses those, but many installs carry the app id itself: the Steamworks SDK's
`steam_appid.txt`, or a Steam emulator's settings (`steam_emu.ini` and the
others in emulator_formats.SETTINGS_FILES). That number is the game's identity
and needs no guess, so autodetect asks for it before trying names.

Only an unambiguous answer counts: one id across the whole folder. 480
(Spacewar, Valve's test app) is a placeholder emulators and samples use, never
the game.
"""
from __future__ import annotations

import os
from collections import deque
from pathlib import Path

from .emulator_formats import SETTINGS_FILES, _get, _settings

PLACEHOLDER_IDS = {480}
MAX_DEPTH = 6                 # Unreal: <game>/Engine/Binaries/ThirdParty/Steamworks/Steamv157/Win64
MAX_ENTRIES = 6000
_ID_FILES = {"steam_appid.txt"}
_INI_FILES = {n.lower() for n in (*SETTINGS_FILES, "steam_emu.ini", "CODEX.ini", "RUNE.ini", "EMPRESS.ini")
              if n.lower().endswith(".ini")}
_INI_KEYS = (("Settings", "AppId"), ("GameSettings", "AppId"), ("TENOKE", "id"), ("Settings", "AppID"))
# Big asset folders that never hold the id; skipping them keeps the walk short.
_SKIP = {"content", "paks", "movies", "_redist", "redist", "__installer", "directx", "streamingassets",
         "resources", "localization", "shadercache"}
_CONTAINERS = {"games", "game", "program files", "program files (x86)", "steamapps", "common", "steamlibrary",
               "desktop", "downloads", "documents", "epic games", "gog games", "gog galaxy"}


def game_root(path: str) -> Path:
    """The install folder of a running program: above `<Project>/Binaries` for
    Unreal games, else the program's own folder."""
    p = Path(path)
    for parent in list(p.parents)[:4]:
        if parent.name.lower() == "binaries":
            return parent.parent.parent
    return p.parent


def _from_file(path: Path) -> int | None:
    name = path.name.lower()
    if name in _ID_FILES:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:64].split()
        except OSError:
            return None
        value = text[0] if text else ""
    else:
        sections = _settings(path)
        value = next((v for s, k in _INI_KEYS if (v := _get(sections, s, k))), "")
    return int(value) if value.isdigit() and 0 < int(value) < 2**32 else None


def declared_appids(root: Path) -> set[int]:
    """Every Steam app id the install's own files name."""
    found: set[int] = set()
    seen = 0
    queue = deque([(root, 0)])
    while queue:
        folder, depth = queue.popleft()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen > MAX_ENTRIES:
                return found
            try:
                if e.is_file():
                    low = e.name.lower()
                    if low in _ID_FILES or low in _INI_FILES:
                        appid = _from_file(Path(e.path))
                        if appid is not None:
                            found.add(appid)
                elif e.is_dir(follow_symlinks=False) and depth < MAX_DEPTH and e.name.lower() not in _SKIP:
                    queue.append((Path(e.path), depth + 1))
            except OSError:
                continue
    return found - PLACEHOLDER_IDS


def declared_appid(path: str) -> int | None:
    """The one Steam app id a running program's install declares, or None."""
    root = game_root(path)
    # A program sitting straight in a drive or a library folder ("C:\\Games")
    # would make the walk read every game in it, and take a neighbour's id.
    if len(root.parts) < 3 or root.name.lower() in _CONTAINERS:
        return None
    ids = declared_appids(root)
    return next(iter(ids)) if len(ids) == 1 else None
