"""Standalone executables.

A game that no storefront tracks (DRM-free, physical media, a source port, an
old install) is registered by pointing at its executable. From then on:

- the executable is **fingerprinted** (name, size, SHA-256) so an unlock can
  say which build it came from, and a moved or updated install is noticed;
- the adapter **watches the process list** with the operating system's own
  tools (tasklist, /proc, ps) and records `session.started` / `session.ended`;
- pack rules that only need those signals unlock achievements with
  `local-executable` provenance: first launch, total playtime, session count.

Read-only observation, always. No injection, no memory access, no DRM or
anti-cheat interaction, no ownership check, and the executable itself is never
uploaded. The path lives in this machine's config, not in synced events:
another device has no use for it and it can name the user's folders.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import events as ev
from ..profile import Profile, ProfileError
from .base import AdapterMetadata

ADAPTER_VERSION = "0.1.0"
FULL_HASH_LIMIT = 512 * 1024 * 1024
SIGNALS = ("launch", "playtime", "sessions")

METADATA = AdapterMetadata(
    adapter_id="executable",
    adapter_version=ADAPTER_VERSION,
    support_level="official",
    authentication_method="none (local)",
    data_access_method="executable fingerprint and the OS process list, read-only",
    capabilities=("register-installation", "fingerprint", "detect-launch-and-exit", "playtime", "rule-unlocks"),
    rate_limits="polls the process list every few seconds while watching",
    known_limitations=(
        "only notices a game while `openachievements watch` (or the UI) is running",
        "launchers that start the real game under another name need the real executable registered",
        "in-game events need a save-file, log or plugin adapter; this one only sees the process",
    ),
    terms_or_policy_notes="Observation only. Never modifies, injects into, or uploads the game.",
)


def fingerprint(path: Path) -> dict:
    """SHA-256 of the whole file, or of the first and last 64 MiB plus the size
    for anything over 512 MiB (marked `partial`), so huge files stay quick."""
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        if size <= FULL_HASH_LIMIT:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
            partial = False
        else:
            digest.update(fh.read(64 * 1024 * 1024))
            fh.seek(-64 * 1024 * 1024, os.SEEK_END)
            digest.update(fh.read())
            digest.update(str(size).encode())
            partial = True
    return {"executable_name": path.name, "size": size, "sha256": digest.hexdigest(), "partial": partial}


def register(profile: Profile, game_id: str, executable: Path, *, title: str | None = None,
             source: str = "standalone") -> dict:
    """Register an installation of `game_id` (creating the game if needed)."""
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise ProfileError(f"no file at {executable}")
    state = profile.state()
    if game_id not in state["games"]:
        profile.register_game(game_id, title or executable.stem)
    fp = fingerprint(executable)
    for inst in state["games"].get(game_id, {}).get("installations", {}).values():
        if inst.get("sha256") == fp["sha256"] and inst.get("device_id") == profile.device_id:
            _remember_path(profile, inst["installation_id"], executable)
            return {"installation_id": inst["installation_id"], "already_registered": True}
    installation_id = ev.new_id()
    profile.record("game.installation_registered", {
        "installation_id": installation_id, "kind": "local-executable", "source": source, **fp,
    }, game_id=game_id, adapter="executable", adapter_version=ADAPTER_VERSION)
    _remember_path(profile, installation_id, executable)
    return {"installation_id": installation_id, **fp}


def _remember_path(profile: Profile, installation_id: str, executable: Path) -> None:
    with profile.config.editing() as config:
        config.setdefault("installations", {})[installation_id] = {
            "profile_id": profile.profile_id, "path": str(executable)}


def local_installations(profile: Profile) -> list[dict]:
    """This machine's registered executables that still exist."""
    config = profile.config.load().get("installations", {})
    out = []
    for game_id, game in profile.state()["games"].items():
        for inst in game["installations"].values():
            local = config.get(inst["installation_id"])
            if local and local.get("profile_id") == profile.profile_id:
                out.append({"game_id": game_id, **inst, "path": local["path"]})
    return out


def _windows_process_paths() -> set[str]:
    """Every running program's full path, asked of Windows directly.

    No child process: the watcher runs every few seconds, and from the windowed
    app each PowerShell it started flashed a console window on screen (and cost
    a PowerShell start-up each time). Processes this user may not inspect
    (system, other users, some elevated ones) are skipped, as Get-Process did."""
    import ctypes
    from ctypes import wintypes
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    psapi.EnumProcesses.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    size = 1024
    while True:
        pids = (wintypes.DWORD * size)()
        used = wintypes.DWORD()
        if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(used)):
            return set()
        count = used.value // ctypes.sizeof(wintypes.DWORD)
        if count < size:
            break
        size *= 2
    found: set[str] = set()
    buffer = ctypes.create_unicode_buffer(32768)
    for pid in pids[:count]:
        handle = kernel32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            length = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                found.add(buffer.value.lower())
        finally:
            kernel32.CloseHandle(handle)
    return found


def running_executables() -> set[str]:
    """Lowercased full paths (or bare names, where the OS will not say more)
    of every running process. Uses only what the OS ships with."""
    found: set[str] = set()
    try:
        if sys.platform == "win32":
            found.update(_windows_process_paths())
        elif Path("/proc").is_dir():
            for pid in os.listdir("/proc"):
                if pid.isdigit():
                    try:
                        found.add(os.readlink(f"/proc/{pid}/exe").lower())
                    except OSError:
                        continue
        else:
            out = subprocess.run(("ps", "-axo", "comm="), capture_output=True, text=True, timeout=20).stdout
            found.update(line.strip().lower() for line in out.splitlines() if line.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return found


def _is_running(path: str, running: set[str]) -> bool:
    p = path.lower()
    return p in running or os.path.basename(p) in {os.path.basename(r) for r in running if "/" not in r and "\\" not in r}


@dataclass
class Watcher:
    """Turns process-list snapshots into session events and rule unlocks.
    `poll()` is pure given a snapshot, so tests drive it directly."""
    profile: Profile
    open_sessions: dict = field(default_factory=dict)     # installation_id -> (event, monotonic start)

    def poll(self, running: set[str] | None = None, now: float | None = None) -> list[dict]:
        running = running_executables() if running is None else running
        now = time.monotonic() if now is None else now
        written: list[dict] = []
        for inst in local_installations(self.profile):
            inst_id, active = inst["installation_id"], _is_running(inst["path"], running)
            if active and inst_id not in self.open_sessions:
                event = self.profile.record("session.started", {"installation_id": inst_id},
                                            game_id=inst["game_id"], adapter="executable",
                                            adapter_version=ADAPTER_VERSION)
                self.open_sessions[inst_id] = (event, now)
                written.append(event)
            elif not active and inst_id in self.open_sessions:
                started, t0 = self.open_sessions.pop(inst_id)
                written.append(self.profile.record("session.ended", {
                    "installation_id": inst_id, "started_event_id": started["event_id"],
                    "seconds": max(0, round(now - t0))}, game_id=inst["game_id"], adapter="executable",
                    adapter_version=ADAPTER_VERSION))
                written.extend(evaluate_rules(self.profile, inst["game_id"], inst_id))
            elif active and inst_id in self.open_sessions:
                pass
        for inst_id, (started, _t0) in list(self.open_sessions.items()):
            if not any(i["installation_id"] == inst_id for i in local_installations(self.profile)):
                self.open_sessions.pop(inst_id)
        for event in [w for w in written if w["event_type"] == "session.started"]:
            written.extend(evaluate_rules(self.profile, event["game_id"], event["payload"]["installation_id"]))
        return written

    def run(self, interval: float = 5.0, on_event=None, on_error=None) -> None:
        while True:
            try:
                for event in self.poll():
                    if on_event:
                        on_event(event)
            except Exception as exc:  # noqa: BLE001 - keep watching; report, do not die
                if on_error:
                    on_error(exc)
            time.sleep(interval)


def evaluate_rules(profile: Profile, game_id: str, installation_id: str) -> list[dict]:
    """Unlock any achievement whose rules are all satisfied by this game's
    sessions. Rules this adapter does not understand are left for others."""
    from ..reducer import is_unlocked
    state = profile.state()
    played = state.get("sessions", {}).get(game_id, {"count": 0, "seconds": 0, "started": 0})
    written = []
    for pack_id, pack in state["packs"].items():
        if pack.get("removed") or game_id not in (pack.get("game_ids") or []):
            continue
        for ach_id, definition in pack["achievements"].items():
            rules = definition.get("rules") or []
            if not rules or not all(isinstance(r, dict) and r.get("signal") in SIGNALS for r in rules):
                continue
            key = f"{pack_id}:{ach_id}"
            if is_unlocked(state, key):
                continue
            try:
                ok = all(
                    (r["signal"] == "launch" and played["started"] >= 1)
                    or (r["signal"] == "playtime" and played["seconds"] >= float(r["minutes"]) * 60)
                    or (r["signal"] == "sessions" and played["count"] >= int(r["count"]))
                    for r in rules)
            except (KeyError, TypeError, ValueError, OverflowError):
                # Packs are validated on install, but events can come from other
                # clients: one bad rule skips its achievement, never the watcher.
                continue
            if ok:
                written.append(profile.record("achievement.unlocked", {
                    "provenance": "local-executable", "installation_id": installation_id,
                    "rules": rules}, achievement_id=key, game_id=game_id,
                    adapter="executable", adapter_version=ADAPTER_VERSION))
                state = profile.state()
    return written
