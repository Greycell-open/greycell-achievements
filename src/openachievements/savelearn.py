"""Learning save rules from the saves the keeper holds.

The keeper (savekeeper.py) holds every version of a game's saves. Read as
flat values (savevalues.py), two versions side by side say exactly what the
game wrote in between: "levels.KIT_Main: Entered -> Complete".

  - `changes()` lists those differences, oldest first, for writing a rule by
    hand: play to the moment an achievement describes, see what changed.
  - `suggest()` learns rules by itself where the game has another source of
    unlocks (Steam, a Steam emulator, GOG): for each unlock, the values that
    changed in the save around that moment, were never like that before, and
    do not change all the time (timers, positions). A rule learned on one
    copy then works on copies with no other source, a DRM-free one included.
  - `add_rule()` writes a rule into this computer's own rules folder, applied
    like the rules that ship with the app (adapters/saverules.py).

Suggestions are never applied by themselves: a wrong unlock is worse than a
missing one, so a person accepts each.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import savevalues
from .fsutil import write_json_atomic
from .savekeeper import Keeper, KeeperError

NOISY = 0.5                     # a value that changes in more than half the versions is not progress
_WORD = re.compile(r"[a-z]+|\d+")
_FROM = ("save-file", "manual")  # unlocks that cannot teach a save rule (they came from one)


def rules_dir(profile) -> Path:
    return Path(profile.config.dir) / "saverules"


def _values(keeper: Keeper, game_id: str, rel: str, sha: str) -> tuple[str, dict]:
    cache = keeper._game_dir(game_id) / "values" / f"{sha}.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        return cached["format"], cached["values"]
    except (OSError, ValueError, KeyError):
        pass
    fmt, values = savevalues.values(rel.rsplit("/", 1)[-1], keeper.read(game_id, sha))
    write_json_atomic(cache, {"format": fmt, "values": values})
    return fmt, values


def _timeline(keeper: Keeper, game_id: str) -> dict[tuple[str, str], list[dict]]:
    """(folder, file) -> its versions oldest first: {at, sha, format, values}."""
    out: dict[tuple[str, str], list[dict]] = {}
    for snap in reversed(keeper.snapshots(game_id)):
        if snap.get("reason") == "before-restore":
            continue
        for rel, f in snap["files"].items():
            versions = out.setdefault((snap["folder"], rel), [])
            if versions and versions[-1]["sha"] == f["sha256"]:
                continue
            versions.append({"at": snap["taken_at"], "sha": f["sha256"]})
    for (folder, rel), versions in out.items():
        for v in versions:
            v["format"], v["values"] = _values(keeper, game_id, rel, v["sha"])
    return out


def _diff(before: dict, after: dict) -> dict:
    keys = set(before) | set(after)
    return {k: (before.get(k), after.get(k)) for k in keys if before.get(k) != after.get(k)}


def changes(profile, game_id: str, file: str | None = None, limit: int = 200) -> list[dict]:
    """Every value that changed between kept versions, oldest first."""
    out = []
    for (folder, rel), versions in _timeline(Keeper(profile), game_id).items():
        if file and file.lower() not in rel.lower():
            continue
        for a, b in zip(versions, versions[1:]):
            for fld, (old, new) in sorted(_diff(a["values"], b["values"]).items()):
                out.append({"at": b["at"], "folder": folder, "file": rel, "format": b["format"],
                            "field": fld, "old": old, "new": new})
    out.sort(key=lambda c: c["at"])
    return out[-limit:]


def _words(text) -> set[str]:
    return {w for w in _WORD.findall(re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text or "")).lower()) if len(w) > 2}


def _condition(old, new) -> tuple[str, object] | None:
    if isinstance(new, bool) or isinstance(new, str):
        return "==", (str(new).lower() if isinstance(new, bool) else new)
    if isinstance(new, (int, float)) and isinstance(old, (int, float)) and new > old:
        return ">=", new
    return None


def _holds(op: str, want, value) -> bool:
    if value is None:
        return False
    if op == "==":
        return str(value).lower() == str(want).lower()
    try:
        return float(value) >= float(want)
    except (TypeError, ValueError):
        return False


def suggest(profile, game_id: str) -> list[dict]:
    """Rules learned from this game's unlocks from other sources, best first."""
    state = profile.state()
    pack = next((p for p in state["packs"].values() if game_id in (p.get("game_ids") or []) and not p.get("removed")),
                None)
    if not pack:
        return []
    timeline = _timeline(Keeper(profile), game_id)
    noisy = {}
    for key, versions in timeline.items():
        counts: dict[str, int] = {}
        for a, b in zip(versions, versions[1:]):
            for fld in _diff(a["values"], b["values"]):
                counts[fld] = counts.get(fld, 0) + 1
        steps = max(1, len(versions) - 1)
        noisy[key] = {f for f, n in counts.items() if steps >= 3 and n / steps > NOISY}
    unlocks = [e for e in profile.log.read().events if e["event_type"] == "achievement.unlocked"
               and e.get("game_id") == game_id and e["source"].get("adapter") not in _FROM]
    out = []
    for e in unlocks:
        ach_id = e["achievement_id"].split(":", 1)[1]
        definition = pack["achievements"].get(ach_id)
        if not definition or definition.get("rules"):
            continue
        t, about = e["occurred_at"], _words(definition.get("name")) | _words(definition.get("description")) \
            | _words(definition.get("external_id"))
        found = []
        for (folder, rel), versions in timeline.items():
            before = [v for v in versions if v["at"] <= t]
            after = [v for v in versions if v["at"] > t]
            if not before or not after:
                continue
            for fld, (old, new) in _diff(before[-1]["values"], after[0]["values"]).items():
                cond = _condition(old, new)
                if fld in noisy[(folder, rel)] or cond is None:
                    continue
                if any(_holds(*cond, v["values"].get(fld)) for v in before):
                    continue                              # it was already like that: not this moment
                score = len(about & _words(fld))
                found.append({"folder": folder, "file": rel, "format": after[0]["format"], "field": fld,
                              "op": cond[0], "value": cond[1], "score": score})
        if not found:
            continue
        found.sort(key=lambda c: -c["score"])
        best = found[0]
        clear = len(found) == 1 or best["score"] > (found[1]["score"] if len(found) > 1 else 0)
        out.append({"achievement": definition.get("name"), "achievement_id": ach_id, "unlocked_at": t,
                    "confidence": "high" if clear and (best["score"] or len(found) == 1) else "low",
                    "candidates": len(found), **{k: best[k] for k in ("folder", "file", "format", "field", "op",
                                                                       "value")}})
    return sorted(out, key=lambda s: (s["confidence"] != "high", s["unlocked_at"]))


# ---- this computer's own rules ------------------------------------------------------------

def _save_entry(folder: str, rel: str, fmt: str) -> dict:
    """The rule-file form of a save: a known root, a path under it, a file name."""
    from .adapters import savefile
    path = Path(folder).joinpath(*rel.split("/")[:-1])
    name = rel.split("/")[-1]
    for root in savefile.SAVE_ROOT_NAMES:
        base = savefile.root_folder(root)
        if base is None:
            continue
        try:
            under = path.resolve().relative_to(base.resolve())
        except (ValueError, OSError):
            continue
        sid = re.sub(r"[^a-z0-9]+", "-", Path(name).stem.lower()).strip("-")[:40] or "save"
        return {"id": sid, "root": root, "path": under.as_posix(), "pattern": name, "format": fmt}
    raise KeeperError(f"{path} is not under a folder save rules can name (AppData, Documents, Saved Games)")


def add_rule(profile, game_id: str, achievement: str, folder: str, file: str, field: str, op: str, value,
             fmt: str | None = None) -> Path:
    """Write or extend this computer's rules file for the game; returns it."""
    from .adapters import saverules
    pack = profile.state()["packs"].get(game_id)
    if not pack or pack.get("removed"):
        raise KeeperError(f"{game_id} has no achievement list to add a rule to")
    wanted = achievement.strip().lower()
    hit = [a for aid, a in pack["achievements"].items()
           if aid == wanted or " ".join(str(a.get("name", "")).split()).lower() == " ".join(wanted.split())]
    if len(hit) != 1:
        raise KeeperError(f"no single achievement called {achievement!r} in {game_id}")
    if fmt is None:
        kept = [s for s in Keeper(profile).snapshots(game_id) if s["folder"] == folder and file in s["files"]]
        if not kept:
            raise KeeperError(f"no kept copy of {file} to tell its format from")
        fmt = _values(Keeper(profile), game_id, file, kept[0]["files"][file]["sha256"])[0]
    if fmt not in ("json", "ini", "gvas", "fireproof", "nrbf", "es3"):
        raise KeeperError(f"{file} has no reader ({fmt}): a rule cannot read it")
    save = _save_entry(folder, file, fmt)
    path = rules_dir(profile) / f"{game_id}.json"
    try:
        rules = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rules = {"game_id": game_id, "title": pack.get("name"), "verified": "rules written on this computer",
                 "saves": [], "achievements": {}}
    same = next((s for s in rules["saves"] if s["root"] == save["root"] and s["path"] == save["path"]
                 and s["pattern"] == save["pattern"]), None)
    if same is None:
        while any(s["id"] == save["id"] for s in rules["saves"]):
            save["id"] += "-2"
        rules["saves"].append(save)
        same = save
    name = hit[0]["name"]
    rule = {"signal": "save", "save": same["id"], "field": field, "op": op, "value": value}
    rules["achievements"][name] = {"why": f"{field} {op} {value} in {file}", "rules": [rule]}
    write_json_atomic(path, rules)
    saverules.apply(profile)
    return path
