"""Save rules found by name, for games that write their own achievements.

Many games keep a copy of what they award in their own save, labelled with
Steam's achievement id: Mini Airways writes `Ach_Map_LondonPass = true` when it
awards Map_LondonPass, whether or not Steam (or a Steam emulator) is there to
hear it. A DRM-free copy then unlocks nothing, although the save says exactly
what was earned. This looks for that, by itself, while a game with a Steam
achievement list is played:

- every readable file in the game's save folders is read as flat values
  (savevalues.py);
- a field whose name is a Steam achievement id, alone or after a common prefix
  (`Ach_`, `Achievement_`, `unlocked_` ...), holding true or 1, is that
  achievement. An id shared by two achievements once punctuation is ignored is
  never used;
- when two or more such fields follow one pattern (same place, same prefix),
  the game's other achievements get the same rule before they are earned, so
  the next unlock is seen as it lands.

Unlike rules learned from timing (savelearn.suggest), which wait for a person
to accept them, these are written at once (owner, 2026-10-07: "creates
achievement rules when I boot a DRM-free game"): the game itself names the
field after the achievement, so a wrong unlock would need a wrong label in the
game. They go to this computer's rules folder, like accepted rules, and never
replace a rule already there for that achievement.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import savevalues
from .fsutil import write_json_atomic

PREFIXES = ("", "ach", "achievement", "achievements", "achv", "achieve", "unlocked", "achunlocked",
            "achievementunlocked", "steamach", "steamachievement", "trophy")
MAX_FILE = 8 * 1024 * 1024
READABLE = ("json", "ini", "gvas", "fireproof", "nrbf", "es3")
_SKIP_TAIL = ("date", "time", "timestamp", "progress", "count", "percent", "stat")
_NAMES: dict[str, dict[str, str]] = {}      # game id -> Steam names, once per session


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def steam_names(profile, game_id: str, fetch=None) -> dict[str, str]:
    """Steam achievement id -> display name for a steam-<appid> game: from its
    list, else the rarity cache, else Steam's keyless schema."""
    pack = profile.state()["packs"].get(game_id) or {}
    out = {a["external_id"]: a["name"] for a in pack.get("achievements", {}).values()
           if a.get("external_id") and a.get("name")}
    if out or not (game_id.startswith("steam-") and game_id[6:].isdigit()):
        return out
    if game_id in _NAMES:
        return dict(_NAMES[game_id])
    from . import rarity
    cached = rarity.names(game_id[6:])
    if cached:
        _NAMES[game_id] = dict(cached)
        return dict(cached)
    from .catalog import steam as cat
    rows = cat.fetch_schema(int(game_id[6:]), fetch or cat.PacedFetcher(pace=1.0, retries=1)) or []
    found = {r["api_name"]: r["name"] for r in rows if r.get("api_name")}
    if found:
        _NAMES[game_id] = found
    return found


def save_files(folders: list[str], limit: int = 200) -> list[tuple[str, str]]:
    """(folder, relative file) for files in the folders and one level below."""
    found = []
    for folder in folders:
        base = Path(folder)
        if not base.is_dir():
            continue
        for path in list(base.iterdir()) + [p for d in base.iterdir() if d.is_dir() for p in d.iterdir()]:
            try:
                if path.is_file() and 0 < path.stat().st_size <= MAX_FILE:
                    found.append((folder, path.relative_to(base).as_posix()))
            except OSError:
                continue
            if len(found) >= limit:
                return found
    return found


def matches(values: dict, names: dict[str, str]) -> dict[str, tuple[str, str, str]]:
    """Display name -> (field, container, prefix) for fields named after an
    achievement that hold true or 1."""
    ids = {}
    clash = set()
    for api in names:
        n = _norm(api)
        if len(n) < 4:
            continue
        if n in ids:
            clash.add(n)
        ids[n] = api
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    for field, value in values.items():
        if not (value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1)):
            continue
        container, _, last = field.rpartition(".")
        low = _norm(last)
        if low.endswith(_SKIP_TAIL):
            continue
        for prefix in PREFIXES:
            if low.startswith(prefix) and low[len(prefix):] in ids and low[len(prefix):] not in clash:
                api = ids[low[len(prefix):]]
                raw_prefix = last[: len(last) - len(api)] if last.lower().endswith(api.lower()) else None
                by_name.setdefault(names[api], []).append((field, container, raw_prefix))
                break
    return {name: hits[0] for name, hits in by_name.items() if len(hits) == 1}


def rules_for(values: dict, names: dict[str, str]) -> dict[str, dict]:
    """Display name -> {"field", "op", "value"} for what the save proves, plus
    the same pattern for every other achievement when two or more agree."""
    found = matches(values, names)
    out = {}
    for name, (field, _c, _p) in found.items():
        is_bool = values[field] is True
        out[name] = {"field": field, "op": "==" if is_bool else ">=", "value": "true" if is_bool else "1"}
    patterns = {(c, p) for _f, c, p in found.values() if p is not None}
    if len(found) >= 2 and len(patterns) == 1:
        container, prefix = next(iter(patterns))
        kinds = {values[f] is True for f, _c, _p in found.values()}
        if len(kinds) == 1:
            is_bool = kinds.pop()
            for api, name in names.items():
                if name in out or len(_norm(api)) < 4:
                    continue
                field = f"{container}.{prefix}{api}" if container else f"{prefix}{api}"
                out[name] = {"field": field, "op": "==" if is_bool else ">=", "value": "true" if is_bool else "1"}
    return out


def seek(profile, game_id: str, folders: list[str], fetch=None) -> dict:
    """Look through the game's saves and write what was found into this
    computer's rules for `game_id` (a steam-<appid> game with a list).
    {"file": rel or None, "rules": count added, "seen": count proved}"""
    from .adapters import saverules
    from .savelearn import KeeperError, _save_entry, rules_dir
    pack = profile.state()["packs"].get(game_id)
    if not pack or pack.get("removed") or not pack.get("achievements"):
        return {"file": None, "rules": 0, "seen": 0}
    names = steam_names(profile, game_id, fetch)
    in_pack = {" ".join(str(a.get("name", "")).split()).lower() for a in pack["achievements"].values()}
    names = {api: n for api, n in names.items() if " ".join(n.split()).lower() in in_pack}
    if not names:
        return {"file": None, "rules": 0, "seen": 0}
    best = None
    for folder, rel in save_files(folders):
        try:
            data = (Path(folder) / rel).read_bytes()
            fmt, values = savevalues.values(rel, data)
        except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError):
            continue
        if fmt not in READABLE or not values:
            continue
        proved = matches(values, names)
        if proved and (best is None or len(proved) > best[3]):
            best = (folder, rel, fmt, len(proved), values)
    if best is None:
        return {"file": None, "rules": 0, "seen": 0}
    folder, rel, fmt, seen, values = best
    try:
        save = _save_entry(folder, rel, fmt)
    except KeeperError:
        return {"file": rel, "rules": 0, "seen": seen}
    path = rules_dir(profile) / f"{game_id}.json"
    try:
        rules = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rules = {"game_id": game_id, "title": pack.get("name"),
                 "verified": "found by name in the game's own save on this computer (ruleseek)",
                 "saves": [], "achievements": {}}
    same = next((s for s in rules["saves"] if s["root"] == save["root"] and s["path"] == save["path"]
                 and s["pattern"] == save["pattern"]), None)
    if same is None:
        while any(s["id"] == save["id"] for s in rules["saves"]):
            save["id"] += "-2"
        rules["saves"].append(save)
        same = save
    shipped = saverules.bundled().get(game_id, {}).get("achievements", {})
    added = 0
    for name, rule in rules_for(values, names).items():
        if name in rules["achievements"] or name in shipped:
            continue                                   # a rule already there, shipped or written, wins
        rules["achievements"][name] = {"why": f"{rule['field']} in {rel} is named after the achievement",
                                       "rules": [{"signal": "save", "save": same["id"], **rule}]}
        added += 1
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, rules)
        saverules.apply(profile)
    return {"file": rel, "rules": added, "seen": seen}


def seek_playing(profile, playing, looked: dict, now: float, every: float = 120.0) -> list[dict]:
    """For games being played that have a Steam list, look at most every
    `every` seconds each. Returns what each look found."""
    from .adapters import autodetect
    from .finder import shown_game
    state = profile.state()
    games = state["games"]
    out = []
    for game_id in sorted(set(playing)):
        if game_id not in games:
            continue
        shown = shown_game(games, game_id)
        if not shown.startswith("steam-") or now - looked.get(shown, -1e18) < every:
            continue
        looked[shown] = now
        members = [g for g in games if shown_game(games, g) == shown]
        folders = list(dict.fromkeys(f for g in members for f in autodetect.save_candidates(profile, g)))
        if not folders:
            continue
        found = seek(profile, shown, folders)
        if found["rules"]:
            out.append({"game_id": shown, **found})
    return out
