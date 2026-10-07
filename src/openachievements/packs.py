"""Achievement packs: validation and loading.

A pack is a folder (or a ZIP of one) with `pack.json` and `achievements.json`.
Installing a pack copies it into the profile's `packs/` folder and records a
`pack.installed` event carrying the full definitions, so the event log alone is
enough to interpret every unlock even if the pack folder is lost.

Nothing a pack says is trusted as a path: icon references are resolved with
`safe_child`, archives are checked member by member, sizes are bounded.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

from .events import canonical, is_slug
from .fsutil import safe_child

PACK_SCHEMA_VERSION = "1.0"
MAX_ACHIEVEMENTS = 10000          # Steam's largest sets are 5,000
# One event stays under the event size limit (events.MAX_EVENT_BYTES); a larger
# pack is recorded as several parts that the reducer applies together.
PACK_PART_BYTES = 48 * 1024
MAX_PACK_BYTES = 50 * 1024 * 1024
MAX_TEXT = 500


class PackError(ValueError):
    pass


def _text(value, field, *, required=True, limit=MAX_TEXT):
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PackError(f"{field} must be text of 1 to {limit} characters")
    return value.strip()


# Signals this project's adapters evaluate, with the fields each needs. Other
# signals are allowed (future adapters) as long as they are a named object.
KNOWN_SIGNALS = {
    "launch": {},
    "playtime": {"minutes": (0, 1_000_000)},
    "sessions": {"count": (1, 1_000_000)},
}


# Save-file rules (adapters/savefile.py). A pack names where a game keeps its
# saves relative to a known folder, never as a free path, and how to read them.
# No HOME: a pack names a game's folder, never a user's dotfiles or keys.
SAVE_ROOTS = ("LOCALAPPDATA", "LOCALLOW", "APPDATA", "DOCUMENTS", "SAVED_GAMES")
SAVE_FORMATS = ("json", "ini", "text", "gvas", "fireproof", "nrbf", "es3")
SAVE_OPS = ("==", "!=", ">=", "<=", ">", "<", "exists", "contains")


def _relative(value, field) -> str:
    text = _text(value, field, limit=300).replace("\\", "/")
    parts = text.split("/")
    if text.startswith("/") or ":" in text or any(part in ("", ".", "..") for part in parts):
        raise PackError(f"{field} must be a relative path without '..'")
    if any(part.startswith(".") for part in parts):
        raise PackError(f"{field} may not enter hidden folders")
    return text


def validate_saves(saves) -> list[dict]:
    if not isinstance(saves, list) or not 1 <= len(saves) <= 10:
        raise PackError("saves must be a list of 1 to 10 locations")
    clean, seen = [], set()
    for i, save in enumerate(saves):
        where = f"saves[{i}]"
        if not isinstance(save, dict) or not is_slug(save.get("id")) or ":" in save["id"] or save["id"] in seen:
            raise PackError(f"{where}.id must be a unique lowercase slug")
        seen.add(save["id"])
        if save.get("root") not in SAVE_ROOTS:
            raise PackError(f"{where}.root must be one of {', '.join(SAVE_ROOTS)}")
        if save.get("format") not in SAVE_FORMATS:
            raise PackError(f"{where}.format must be one of {', '.join(SAVE_FORMATS)}")
        pattern = _text(save.get("pattern", "*"), f"{where}.pattern", limit=100)
        if "/" in pattern or "\\" in pattern or ".." in pattern:
            raise PackError(f"{where}.pattern is a file name pattern, not a path")
        clean.append({"id": save["id"], "root": save["root"], "path": _relative(save.get("path"), f"{where}.path"),
                      "pattern": pattern, "format": save["format"]})
    return clean


def _validate_save_rule(rule: dict, where: str) -> dict:
    if not is_slug(rule.get("save")):
        raise PackError(f"{where}.save must name one of the pack's saves")
    if "contains" in rule and "field" not in rule:
        return {"signal": "save", "save": rule["save"], "contains": _text(rule["contains"], f"{where}.contains", limit=200)}
    op = rule.get("op", "exists")
    if op not in SAVE_OPS:
        raise PackError(f"{where}.op must be one of {' '.join(SAVE_OPS)}")
    clean = {"signal": "save", "save": rule["save"], "field": _text(rule.get("field"), f"{where}.field", limit=200),
             "op": op}
    if op != "exists":
        value = rule.get("value")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or value != value:
            raise PackError(f"{where}.value must be text or a number")
        if isinstance(value, str) and len(value) > 200:
            raise PackError(f"{where}.value is too long")
        clean["value"] = value
    return clean


def validate_rules(rules, where: str) -> list[dict]:
    if not isinstance(rules, list) or len(rules) > 20:
        raise PackError(f"{where} must be a list of at most 20 rules")
    clean = []
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict) or not isinstance(rule.get("signal"), str) or not rule["signal"].strip():
            raise PackError(f"{where}[{i}] must be an object with a 'signal' name")
        if rule["signal"] == "save":
            clean.append(_validate_save_rule(rule, f"{where}[{i}]"))
            continue
        for field, (low, high) in KNOWN_SIGNALS.get(rule["signal"], {}).items():
            value = rule.get(field)
            number = isinstance(value, (int, float)) and not isinstance(value, bool) and value == value
            if not number or not (low < value <= high if low == 0 else low <= value <= high):
                raise PackError(f"{where}[{i}].{field} must be a number above {low} and at most {high}"
                                if low == 0 else f"{where}[{i}].{field} must be a number from {low} to {high}")
        clean.append(dict(rule))
    return clean


def validate_definitions(meta: dict, achievements: list) -> dict:
    """Return a normalised pack description or raise PackError."""
    if not isinstance(meta, dict):
        raise PackError("pack.json must be an object")
    if meta.get("schema_version", PACK_SCHEMA_VERSION) != PACK_SCHEMA_VERSION:
        raise PackError(f"unsupported pack schema_version {meta.get('schema_version')!r}")
    pack_id = meta.get("id") or meta.get("pack_id")
    if not is_slug(pack_id) or ":" in pack_id:
        raise PackError("pack id must be a lowercase slug without ':'")
    game_ids = meta.get("game_ids")
    if not isinstance(game_ids, list) or not game_ids or not all(is_slug(g) for g in game_ids):
        raise PackError("game_ids must be a non-empty list of lowercase slugs")
    if not isinstance(achievements, list) or not achievements:
        raise PackError("achievements.json must be a non-empty list")
    if len(achievements) > MAX_ACHIEVEMENTS:
        raise PackError(f"a pack is limited to {MAX_ACHIEVEMENTS} achievements")
    seen, clean = set(), []
    for i, a in enumerate(achievements):
        where = f"achievements[{i}]"
        if not isinstance(a, dict):
            raise PackError(f"{where} must be an object")
        aid = a.get("id")
        if not is_slug(aid) or ":" in aid:
            raise PackError(f"{where}.id must be a lowercase slug without ':'")
        if aid in seen:
            raise PackError(f"duplicate achievement id {aid!r}")
        seen.add(aid)
        points = a.get("points", 0)
        if not isinstance(points, int) or not 0 <= points <= 10000:
            raise PackError(f"{where}.points must be a whole number from 0 to 10000")
        item = {
            "id": aid,
            "name": _text(a.get("name"), f"{where}.name", limit=120),
            "description": _text(a.get("description"), f"{where}.description", required=False) or "",
            "points": points,
            "hidden": bool(a.get("hidden", False)),
            "repeatable": bool(a.get("repeatable", False)),
        }
        for optional in ("category", "rarity", "icon"):
            if a.get(optional) is not None:
                item[optional] = _text(a[optional], f"{where}.{optional}", limit=300)
        if a.get("external_id") not in (None, ""):          # the platform's own id (Steam's internal name)
            item["external_id"] = _text(str(a["external_id"]), f"{where}.external_id", limit=128)
        progress = a.get("progress")
        if progress is not None:
            target = progress.get("target") if isinstance(progress, dict) else None
            if not isinstance(target, (int, float)) or target <= 0:
                raise PackError(f"{where}.progress.target must be a positive number")
            item["progress"] = {"type": progress.get("type", "counter"), "target": target,
                                "unit": progress.get("unit")}
        if a.get("rules") is not None:
            item["rules"] = validate_rules(a["rules"], f"{where}.rules")
        clean.append(item)
    description = {
        "pack_id": pack_id,
        "name": _text(meta.get("name", pack_id), "name", limit=120),
        "version": _text(str(meta.get("version", "1.0.0")), "version", limit=40),
        "schema_version": PACK_SCHEMA_VERSION,
        "game_ids": game_ids,
        "authors": [a for a in meta.get("authors", []) if isinstance(a, str)][:20],
        "license": meta.get("license"),
        "source": meta.get("source", "pack"),
        "source_repository": meta.get("source_repository"),
        "supported_adapters": meta.get("supported_adapters", ["manual"]),
        "achievements": clean,
    }
    if meta.get("saves") is not None:
        description["saves"] = validate_saves(meta["saves"])
        known = {s["id"] for s in description["saves"]}
        for a in clean:
            for r in a.get("rules", []):
                if r["signal"] == "save" and r["save"] not in known:
                    raise PackError(f"achievement {a['id']!r} reads save {r['save']!r}, which the pack does not declare")
    elif any(r["signal"] == "save" for a in clean for r in a.get("rules", [])):
        raise PackError("save rules need a 'saves' list in pack.json")
    if isinstance(meta.get("games"), list):
        description["games"] = [g for g in meta["games"] if isinstance(g, dict) and is_slug(g.get("id"))]
    description["checksum"] = "sha256:" + hashlib.sha256(canonical(clean)).hexdigest()
    return description


def split_for_events(description: dict) -> list[dict]:
    """The payloads that record `description`: itself if it fits one event,
    otherwise parts sharing a random set id, each with a slice of the
    achievements and every other field. The reducer applies the pack only when
    all parts of a set have arrived, so a half-synced pack never shows."""
    if len(canonical(description)) <= PACK_PART_BYTES:
        return [description]
    meta = {k: v for k, v in description.items() if k != "achievements"}
    room = PACK_PART_BYTES - len(canonical(meta)) - 200
    if room < 1024:
        raise PackError("pack metadata is too large to record")
    chunks, current, size = [], [], 0
    for a in description["achievements"]:
        n = len(canonical(a)) + 1
        if n > room:
            raise PackError(f"achievement {a.get('id')!r} is too large to record")
        if current and size + n > room:
            chunks.append(current)
            current, size = [], 0
        current.append(a)
        size += n
    chunks.append(current)
    set_id = hashlib.sha256(canonical(description)).hexdigest()[:32]
    return [{**meta, "achievements": chunk, "part": {"set": set_id, "index": i, "count": len(chunks)}}
            for i, chunk in enumerate(chunks)]


def load_folder(folder: Path) -> dict:
    folder = Path(folder)
    try:
        meta = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
        achievements = json.loads((folder / "achievements.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackError(f"missing {Path(exc.filename).name}") from None
    except json.JSONDecodeError as exc:
        raise PackError(f"invalid JSON: {exc}") from None
    description = validate_definitions(meta, achievements)
    for a in description["achievements"]:
        if a.get("icon") and not a["icon"].startswith(("http://", "https://")):
            safe_child(folder, a["icon"])
    return description


def extract_zip(archive: Path, destination: Path) -> Path:
    """Unpack a pack ZIP safely and return the folder holding pack.json."""
    with zipfile.ZipFile(archive) as zf:
        total = 0
        for info in zf.infolist():
            total += info.file_size
            if total > MAX_PACK_BYTES:
                raise PackError("pack archive is too large")
            if info.is_dir():
                continue
            target = safe_child(destination, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    candidates = [p.parent for p in destination.rglob("pack.json")]
    if len(candidates) != 1:
        raise PackError("the archive must contain exactly one pack.json")
    return candidates[0]
