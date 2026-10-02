"""Portable profile bundles.

A bundle is a ZIP of the profile folder's durable parts: profile.json, the
event files, installed packs, games, devices and evidence. The index, cache,
logs and backups are left out: they are derived or local. Restoring checks
every path, refuses to overwrite an existing profile, and rebuilds the index.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

from .fsutil import safe_child
from .profile import MachineConfig, Profile, ProfileError

DURABLE = ("profile.json", "events", "packs", "games", "devices", "evidence")


def export_bundle(profile: Profile, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in DURABLE:
            path = profile.folder / name
            if path.is_file():
                zf.write(path, f"{profile.profile_id}/{name}")
            elif path.is_dir():
                for f in sorted(path.rglob("*")):
                    if f.is_file() and not f.name.endswith(".lock"):
                        zf.write(f, f"{profile.profile_id}/{f.relative_to(profile.folder).as_posix()}")
        zf.writestr(f"{profile.profile_id}/library.json", json.dumps(profile.library(), indent=2, ensure_ascii=False))
    return out


def restore_bundle(archive: Path, home: Path, config: MachineConfig | None = None) -> Profile:
    with zipfile.ZipFile(archive) as zf:
        roots = {n.split("/", 1)[0] for n in zf.namelist()}
        if len(roots) != 1:
            raise ProfileError("a bundle holds exactly one profile folder")
        profile_id = roots.pop()
        target = Path(home) / "profiles" / profile_id
        if target.exists():
            raise ProfileError(f"a profile {profile_id} already exists here; move it aside first")
        for info in zf.infolist():
            if info.is_dir():
                continue
            dest = safe_child(Path(home) / "profiles", info.filename)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))
    profile = Profile(target, config)
    profile.index.rebuild()
    return profile


def csv_summary(profile: Profile) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["game", "platform", "achievement", "points", "unlocked", "unlocked_at", "sources", "provenance"])
    for game in profile.library()["games"]:
        for a in game["achievements"]:
            writer.writerow([game.get("title") or game["game_id"], game.get("platform") or "", a["name"], a["points"],
                             "yes" if a["unlocked"] else "no", a["unlocked_at"] or "", " ".join(a["sources"]),
                             " ".join(a["provenance"])])
    return buf.getvalue()
