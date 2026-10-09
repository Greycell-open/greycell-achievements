"""Save folders a game engine chooses by itself, worked out from the game.

Matching a save folder by the game's title misses engines that name it
something else or put it deeper than the usual places:

- Unreal: `%LOCALAPPDATA%/<Project>/Saved/SaveGames` (or `<Project>/Saved/
  SaveGames` beside the game), the project being the folder above `Binaries`
  (`<Project>/Binaries/Win64/<Project>-Win64-Shipping.exe`).
- Godot: `user://` is `<data>/Godot/app_userdata/<project name>`, where data
  is %APPDATA% on Windows, ~/.local/share/godot on Linux and
  ~/Library/Application Support/Godot on macOS (Godot's OS.get_user_data_dir).
  The project name is the game's own setting, usually its title.
- Ren'Py: `config.save_directory`, kept under %APPDATA%/RenPy on Windows,
  ~/.renpy on Linux and ~/Library/RenPy on macOS, plus `game/saves` inside
  the game. The folder name is the game's `config.save_directory`, read from
  `game/options.rpy` when it ships, else matched by name with the trailing
  number Ren'Py's template adds (`MyGame-1712345678`) left off.

Names only: folders are listed, no save is read here.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

MAX_OPTIONS_BYTES = 512 * 1024
_SAVE_DIRECTORY = re.compile(r"""config\.save_directory\s*=\s*(?:u|r)?["']([^"'\\/]{1,200})["']""")


def _real(path: str) -> Path:
    try:
        return Path(os.path.realpath(path))        # the process list gives lower case; the disk has the real name
    except OSError:
        return Path(path)


def compact(name: str) -> str:
    """Letters and digits only, lower case: "A Short Hike" and "AShortHike" agree."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _named(folder: Path | None, names: set[str], strip_number: bool = False) -> list[str]:
    try:
        entries = [e for e in os.scandir(folder) if e.is_dir(follow_symlinks=False)] if folder else []
    except OSError:
        return []
    found = []
    for e in entries:
        name = re.sub(r"-\d{6,}$", "", e.name) if strip_number else e.name
        if len(compact(name)) >= 3 and compact(name) in names:
            found.append(e.path)
    return sorted(found)


def unreal_saves(path: str, local_appdata: Path | None) -> list[str]:
    p = _real(path)
    binaries = next((a for a in list(p.parents)[:3] if a.name.lower() == "binaries"), None)
    if binaries is None:
        return []
    project = binaries.parent
    places = [project / "Saved" / "SaveGames"]
    if local_appdata is not None:
        places.insert(0, local_appdata / project.name / "Saved" / "SaveGames")
    return [str(f) for f in places if f.is_dir()]


def godot_root() -> Path | None:
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Godot" / "app_userdata" if os.environ.get("APPDATA") else None
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Godot" / "app_userdata"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "godot" / "app_userdata"


def renpy_root() -> Path | None:
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "RenPy" if os.environ.get("APPDATA") else None
    if sys.platform == "darwin":
        return home / "Library" / "RenPy"
    return home / ".renpy"


def is_godot(path: str) -> bool:
    """A .pck beside the program, or packed into it (Godot's embedded pack ends with "GDPC")."""
    p = _real(path)
    if p.with_suffix(".pck").is_file():
        return True
    try:
        with open(p, "rb") as fh:
            fh.seek(-4, os.SEEK_END)
            return fh.read(4) == b"GDPC"
    except OSError:
        return False


def renpy_game_dir(path: str) -> Path | None:
    """The `game` folder of a Ren'Py game: beside the program (Windows and
    Linux builds), or inside the Mac app's Resources/autorun."""
    p = _real(path)
    for base in [p.parent, *list(p.parents)[:3]]:
        game = base / "game"
        if game.is_dir() and (base / "renpy").is_dir():
            return game
        auto = base / "Resources" / "autorun" / "game"
        if auto.is_dir():
            return auto
    return None


def renpy_save_directory(game: Path) -> str | None:
    try:
        with open(game / "options.rpy", "rb") as fh:
            text = fh.read(MAX_OPTIONS_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    m = _SAVE_DIRECTORY.search(text)
    return m.group(1).strip() if m else None


def godot_saves(path: str, names: list[str], root: Path | None = None) -> list[str]:
    if not is_godot(path):
        return []
    wanted = {compact(n) for n in names + [_real(path).stem] if n}
    return _named(godot_root() if root is None else root, wanted)


def renpy_saves(path: str, names: list[str], root: Path | None = None) -> list[str]:
    game = renpy_game_dir(path)
    if game is None:
        return []
    root = renpy_root() if root is None else root
    found = []
    exact = renpy_save_directory(game)
    if exact and root is not None and (root / exact).is_dir():
        found.append(str(root / exact))
    if not found:
        wanted = {compact(n) for n in names + [_real(path).stem, game.parent.name] if n}
        found = _named(root, wanted, strip_number=True)
    if (game / "saves").is_dir():
        found.append(str(game / "saves"))
    return found


def exact_saves(path: str, names: list[str], local_appdata: Path | None) -> list[str]:
    """Every engine's own save folder for this program, most certain first."""
    out = unreal_saves(path, local_appdata) + godot_saves(path, names) + renpy_saves(path, names)
    return list(dict.fromkeys(out))
