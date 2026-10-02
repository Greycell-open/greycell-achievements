"""Save rules that ship with the app, one file per game in `saverules/`.

A game's achievement list usually comes from Steam (the catalogue or a fetch),
and Steam knows nothing about save files. These files say, for one game, where
its saves are and which values in them mean which achievement, by achievement
name (Steam's display names, which outlive our ids). The watcher merges them
into the installed pack as a `pack.updated`, and again whenever a later update
of that pack dropped them.

Only what a real save was seen to prove goes in a file: an achievement nobody
has tied to a value in the save stays unmapped rather than guessed, because a
wrong unlock is worse than a missing one.

Because these rules come with the app rather than from another person, their
save folder is allowed once, automatically, when it exists on this machine.
`save revoke` withdraws that, and it is not granted again.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..packs import PackError, split_for_events, validate_definitions
from ..profile import Profile
from . import savefile

RULES_DIR = Path(__file__).resolve().parent.parent / "saverules"
_META_SKIP = ("achievements", "removed", "pack_id", "checksum", "schema_version", "part")


@lru_cache(maxsize=1)
def bundled() -> dict[str, dict]:
    """game id -> rules file, for every file shipped."""
    found = {}
    for path in sorted(RULES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        found[data["game_id"]] = data
    return found


def _merged(pack: dict, rules: dict) -> tuple[dict, list[dict]] | None:
    """The pack with this file's saves and rules, or None if it has them."""
    names = {a.get("name"): a for a in pack["achievements"].values()}
    achievements = []
    for aid, definition in pack["achievements"].items():
        item = dict(definition)
        wanted = rules["achievements"].get(definition.get("name"))
        if wanted is not None:
            item["rules"] = wanted["rules"]
        achievements.append(item)
    if not any(n in names for n in rules["achievements"]):
        return None                                   # none of its achievements are in this pack
    if pack.get("saves") == rules["saves"] and all(
            (names[n].get("rules") or []) == r["rules"] for n, r in rules["achievements"].items() if n in names):
        return None
    meta = {k: v for k, v in pack.items() if k not in _META_SKIP}
    meta["id"] = pack["pack_id"]
    meta["saves"] = rules["saves"]
    return meta, achievements


def apply(profile: Profile) -> list[str]:
    """Bring every installed pack that has shipped rules up to date, and allow
    its save folders once. Returns the pack ids it changed."""
    state = profile.state()
    changed = []
    for game_id, rules in bundled().items():
        pack = state["packs"].get(game_id)
        if not pack or pack.get("removed"):
            continue
        merged = _merged(pack, rules)
        if merged is not None:
            try:
                description = validate_definitions(*merged)
            except PackError:
                continue                              # a pack shape these rules do not fit: leave it alone
            profile.commit([_event(profile, "pack.updated", payload)
                            for payload in split_for_events(description)])
            changed.append(game_id)
        _allow_once(profile, game_id, rules["saves"])
    return changed


def _event(profile: Profile, kind: str, payload: dict) -> dict:
    from .. import events as ev
    return ev.make_event(kind, profile_id=profile.profile_id, device_id=profile.device_id, payload=payload,
                         adapter="save-file", adapter_version=savefile.ADAPTER_VERSION)


def _allow_once(profile: Profile, pack_id: str, saves: list[dict]) -> None:
    for save in saves:
        key = f"{profile.profile_id}:{pack_id}:{save['id']}"
        if key in (profile.config.load().get("save_auto_allowed") or {}):
            continue
        folder = savefile.save_folder(profile, pack_id, save)
        if folder is None or not folder.is_dir():
            continue
        with profile.config.editing() as config:
            config.setdefault("save_grants", {})[key] = str(folder.resolve())
            config.setdefault("save_auto_allowed", {})[key] = str(folder.resolve())
