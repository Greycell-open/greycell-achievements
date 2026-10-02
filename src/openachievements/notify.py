"""The unlock popup: which unlocks show one, and handing them to the window.

When the watcher records an unlock that just happened, a small card slides up
at the bottom centre of the screen with a short chime, the way a console does
it, then fades. The window itself is `toast.py`, run as its own process so the
watcher never hosts a GUI loop; this module only decides and hands over.

What pops:
- an `achievement.unlocked` that happened in the last two minutes, so importing
  years of history never pops;
- not one from Steam unless asked (`notify steam on`): Steam shows its own.

Settings are this machine's (machine config, `notify`): on/off, steam, sound.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Callable

RECENT_SECONDS = 120
MAX_PER_ROUND = 10
DEFAULTS = {"enabled": True, "steam": False, "sound": True, "unlock_sound": "pop", "platinum_sound": "burst"}
SOUND_KEYS = {"unlock_sound": "unlock", "platinum_sound": "platinum"}


def settings(config: dict) -> dict:
    from . import sounds
    current = {**DEFAULTS, **(config.get("notify") or {})}
    for key, kind in SOUND_KEYS.items():                  # a choice of a sound since replaced: the default
        if current[key] != sounds.CUSTOM:
            current[key] = sounds.known(kind, current[key])
    return current


def change(profile, **values) -> dict:
    with profile.config.editing() as config:
        current = settings(config)
        from . import sounds
        for k, v in values.items():
            if k in SOUND_KEYS:
                table = sounds.PLATINUM if SOUND_KEYS[k] == "platinum" else sounds.UNLOCK
                if v not in table and v != sounds.CUSTOM:
                    raise ValueError(f"{k} must be one of {', '.join(table)}")
                current[k] = v
            elif k in DEFAULTS:
                current[k] = bool(v)
        config["notify"] = current
    return current


def _age(iso: str, now: datetime) -> float | None:
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (now - when).total_seconds()


def wanted(event: dict, prefs: dict, now: datetime | None = None) -> bool:
    if not prefs["enabled"] or event.get("event_type") != "achievement.unlocked":
        return False
    # Steam, PlayStation and Xbox show their own popups: their unlocks sync quietly unless asked.
    if event.get("source", {}).get("adapter") in ("steam", "psn", "xbox", "gog", "retroachievements") and not prefs["steam"]:
        return False
    age = _age(event.get("occurred_at", ""), now or datetime.now(timezone.utc))
    return age is not None and -60 <= age <= RECENT_SECONDS


def custom_file(profile, kind: str):
    """Where the player's own sound for `kind` is kept, on this computer only."""
    return profile.config.dir / "sounds" / f"custom-{kind}.wav"


def platinum_cards(profile, events: list[dict], prefs: dict, now: datetime) -> list[dict]:
    """A Platinum card for each game this round's fresh unlocks completed.
    Every source counts, Steam included (Steam has no platinum; this is ours),
    but only fresh unlocks: old progress being imported is not a moment."""
    if not prefs["enabled"]:
        return []
    fresh = set()
    for e in events:
        age = _age(e.get("occurred_at", ""), now)
        if e.get("event_type") == "achievement.unlocked" and age is not None and -60 <= age <= RECENT_SECONDS:
            fresh.add(e.get("game_id"))
    fresh.discard(None)
    if not fresh:
        return []
    games = {g["game_id"]: g for g in profile.library()["games"]}
    return [{"name": "Platinum", "game": games[gid].get("title") or gid, "points": 0, "platinum": True}
            for gid in sorted(fresh)
            if gid in games and games[gid]["total"] and games[gid]["unlocked"] == games[gid]["total"]]


def card(profile, event: dict) -> dict:
    """What the popup says: the achievement and its game, never more."""
    state = profile.state()
    pack_id, _, ach_id = (event.get("achievement_id") or "").partition(":")
    definition = (state["packs"].get(pack_id) or {}).get("achievements", {}).get(ach_id, {})
    game = state["games"].get(event.get("game_id") or "", {})
    return {"name": definition.get("name") or ach_id or "Achievement",
            "game": game.get("title") or event.get("game_id") or "",
            "points": definition.get("points") or 0}


def _launch(cards: list[dict], sound) -> None:
    """Start the popup window on its own, without a console window."""
    if getattr(sys, "frozen", False):              # GreycellAchievements.exe has a popup mode
        command = [sys.executable, "--toast"]
    else:
        python = sys.executable
        windowed = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.name == "nt" and os.path.exists(windowed):
            python = windowed
        command = [python, "-m", "openachievements.toast"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    # sound: True/False, or {"on": bool, "unlock": name, "platinum": name} with the chosen sounds
    choice = sound if isinstance(sound, dict) else {"on": bool(sound)}
    proc.stdin.write(json.dumps({"cards": cards, "sound": bool(choice.get("on")),
                                 "sounds": {k: choice[k] for k in ("unlock", "platinum", "unlock_file", "platinum_file")
                                            if k in choice}}).encode("utf-8"))
    proc.stdin.close()


class Notifier:
    """Collects one watcher round's unlocks and shows them together, in order."""

    def __init__(self, profile, launch: Callable[[list[dict], bool], None] | None = None):
        self.profile = profile
        self._launch = launch or _launch
        self._pending: list[dict] = []

    def add(self, event: dict) -> None:
        self._pending.append(event)

    def flush(self) -> int:
        events, self._pending = self._pending, []
        if not events or sys.platform != "win32" and self._launch is _launch:
            return 0
        prefs = settings(self.profile.config.load())
        now = datetime.now(timezone.utc)
        cards = [card(self.profile, e) for e in events if wanted(e, prefs, now)][:MAX_PER_ROUND]
        cards += platinum_cards(self.profile, events, prefs, now)
        if cards:
            self._launch(cards, {"on": prefs["sound"], "unlock": prefs["unlock_sound"],
                                 "platinum": prefs["platinum_sound"],
                                 **{f"{kind}_file": str(custom_file(self.profile, kind)) for kind in ("unlock", "platinum")
                                    if prefs[f"{kind}_sound"] == "custom" and custom_file(self.profile, kind).exists()}})
        return len(cards)
