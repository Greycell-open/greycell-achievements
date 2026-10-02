"""Greycell Test Game: a five-level game that exists to try the app out.

There is no unlocking by hand, so the test game works like a real one: it has
a save file, and its achievements unlock from that save through the ordinary
save-file watcher, popup and Platinum included.

    testgame          play one more level (writes the save); the watcher does the rest
    testgame reset    take back its unlocks and delete the save, to test again
    testgame remove   reset, then take the test game's pack out of the library

The save is Documents/Greycell Test Game/progress.json, {"level": n}. Its unlocks
record save-derived provenance like any other game's.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .fsutil import write_json_atomic
from .profile import Profile

PACK_ID = GAME_ID = "greycell-test-game"
TITLE = "Greycell Test Game"
LEVELS = 5
ACHIEVEMENTS = [
    ("first-steps", "First Steps", "Finish level 1.", 1),
    ("getting-warm", "Getting Warm", "Finish level 2.", 2),
    ("halfway-there", "Halfway There", "Finish level 3.", 3),
    ("almost-there", "Almost There", "Finish level 4.", 4),
    ("the-end", "The End", "Finish the test game.", 5),
]


def _pack_files(folder: Path) -> Path:
    (folder / "pack.json").write_text(json.dumps({
        "schema_version": "1.0", "id": PACK_ID, "name": TITLE, "version": "1.0.0",
        "game_ids": [GAME_ID], "games": [{"id": GAME_ID, "title": TITLE, "platform": "Test"}],
        "authors": ["Greycell Achievements"], "license": "CC0-1.0", "supported_adapters": ["save-file"],
        "saves": [{"id": "main", "root": "DOCUMENTS", "path": TITLE, "pattern": "progress.json", "format": "json"}],
    }), encoding="utf-8")
    (folder / "achievements.json").write_text(json.dumps([
        {"id": aid, "name": name, "description": desc, "points": 10,
         "rules": [{"signal": "save", "save": "main", "field": "level", "op": ">=", "value": level}]}
        for aid, name, desc, level in ACHIEVEMENTS]), encoding="utf-8")
    return folder


def ensure(profile: Profile) -> Path:
    """Install the test game if it is not in the library, allow its save
    folder, and return that folder."""
    from .adapters import savefile
    pack = profile.state()["packs"].get(PACK_ID)
    if not pack or pack.get("removed"):
        with tempfile.TemporaryDirectory() as tmp:
            profile.install_pack(_pack_files(Path(tmp)))
    return savefile.allow(profile, PACK_ID, "main")


def level(folder: Path) -> int:
    try:
        return int(json.loads((folder / "progress.json").read_text(encoding="utf-8")).get("level", 0))
    except (OSError, ValueError, AttributeError):
        return 0


def play(profile: Profile) -> int:
    """Finish one more level: the save changes, and the watcher unlocks."""
    folder = ensure(profile)
    folder.mkdir(parents=True, exist_ok=True)
    reached = min(level(folder) + 1, LEVELS)
    write_json_atomic(folder / "progress.json", {"level": reached})
    return reached


def reset(profile: Profile) -> int:
    """Take back the test game's unlocks and delete its save. Returns how
    many unlocks were taken back."""
    from .adapters import savefile
    state = profile.state()
    taken = 0
    for aid, *_ in ACHIEVEMENTS:
        key = f"{PACK_ID}:{aid}"
        entry = state["unlocks"].get(key)
        if entry and set(entry.get("records", {})) - set(entry.get("revoked", set())):
            profile.revoke(key, reason="test game reset")
            taken += 1
    pack = state["packs"].get(PACK_ID)
    if pack and not pack.get("removed"):
        folder = savefile.save_folder(profile, PACK_ID, pack["saves"][0])
        if folder is not None:
            (folder / "progress.json").unlink(missing_ok=True)
    return taken


def remove(profile: Profile) -> None:
    reset(profile)
    pack = profile.state()["packs"].get(PACK_ID)
    if pack and not pack.get("removed"):
        profile.record("pack.removed", {"pack_id": PACK_ID}, game_id=GAME_ID)
