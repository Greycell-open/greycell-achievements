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
import time
from datetime import datetime, timezone
from typing import Callable

RECENT_SECONDS = 120
PLATFORMS = ("steam", "psn", "xbox", "gog", "retroachievements")   # they show their own popups
MAX_PER_ROUND = 10
DEFAULTS = {"enabled": True, "steam": False, "sound": True, "unlock_sound": "echo", "platinum_sound": "burst",
            "rare_sound": "rare-echo", "rare_below": 1.0}
SOUND_KEYS = {"unlock_sound": "unlock", "platinum_sound": "platinum", "rare_sound": "rare"}
RARE_CHOICES = (1.0, 5.0, 10.0)     # "rare" means fewer than this share of Steam players have it


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
                table = sounds.TABLES[SOUND_KEYS[k]][0]()
                if v not in table and v != sounds.CUSTOM:
                    raise ValueError(f"{k} must be one of {', '.join(table)}")
                current[k] = v
            elif k == "rare_below":
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    v = None
                if v not in RARE_CHOICES:
                    raise ValueError(f"rare_below must be one of {', '.join(f'{c:g}' for c in RARE_CHOICES)}")
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


def skipped_because(event: dict, prefs: dict, now: datetime | None = None) -> str | None:
    """Why this unlock gets no popup, or None when it gets one. Written to
    app.log for every unlock, so a missing popup can be traced afterwards."""
    if event.get("event_type") != "achievement.unlocked":
        return "not an unlock"
    if not prefs["enabled"]:
        return "popups are turned off"
    # Steam, PlayStation and Xbox show their own popups: their unlocks sync quietly unless asked.
    adapter = event.get("source", {}).get("adapter")
    if adapter == "psn":
        return "PlayStation trophies are earned on the console"     # a popup here would arrive late, on the wrong screen
    if adapter in PLATFORMS and not prefs["steam"]:
        return f"{adapter} shows its own popup"
    age = _age(event.get("occurred_at", ""), now or datetime.now(timezone.utc))
    if age is None:
        return "no unlock time"
    if not -60 <= age <= RECENT_SECONDS:
        return f"unlocked {int(age)} s ago, an old unlock being imported"
    return None


def wanted(event: dict, prefs: dict, now: datetime | None = None) -> bool:
    return skipped_because(event, prefs, now) is None


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
        if e.get("source", {}).get("adapter") == "psn":
            continue                                      # earned on the console: no PC popup, Platinum included
        if e.get("event_type") == "achievement.unlocked" and age is not None and -60 <= age <= RECENT_SECONDS:
            fresh.add(e.get("game_id"))
    fresh.discard(None)
    if not fresh:
        return []
    games = {g["game_id"]: g for g in profile.library()["games"]}
    return [{"name": "Platinum", "game": games[gid].get("title") or gid, "points": 0, "platinum": True}
            for gid in sorted(fresh)
            if gid in games and games[gid]["total"] and games[gid]["unlocked"] == games[gid]["total"]]


PICTURE_WAIT = 3.0                  # the longest pictures may hold a popup, for all its cards together


def add_pictures(cards: list[dict], urls: list[str | None], wait: float = PICTURE_WAIT, cached=None) -> None:
    """Give each card its achievement's picture as a local file: from the
    picture cache, fetched together if needed, all within one `wait`. A card
    whose picture is not there in time shows the trophy instead."""
    import threading
    from . import art
    get = cached or art.cached
    found: dict[int, str] = {}

    def one(i: int, url: str) -> None:
        try:
            path = get(url)
        except Exception:  # noqa: BLE001 - no picture is never a reason to lose the popup
            path = None
        if path:
            found[i] = str(path)
    workers = [threading.Thread(target=one, args=(i, u), daemon=True) for i, u in enumerate(urls) if u]
    for w in workers:
        w.start()
    deadline = time.monotonic() + wait
    for w in workers:
        w.join(max(0.0, deadline - time.monotonic()))
    for i, path in list(found.items()):
        cards[i]["icon"] = path


def card(profile, event: dict, prefs: dict | None = None, rarity_of=None) -> dict:
    """What the popup says: the achievement and its game, and that it is rare
    when fewer than prefs["rare_below"] percent of Steam players have it."""
    state = profile.state()
    pack_id, _, ach_id = (event.get("achievement_id") or "").partition(":")
    definition = (state["packs"].get(pack_id) or {}).get("achievements", {}).get(ach_id, {})
    game = state["games"].get(event.get("game_id") or "", {})
    name = definition.get("name") or ach_id or "Achievement"
    out = {"name": name, "game": game.get("title") or event.get("game_id") or "", "points": definition.get("points") or 0}
    if prefs is not None and definition.get("icon"):
        out["_picture"] = definition["icon"]        # fetched for all cards at once, in flush
    if prefs is not None:
        from . import rarity
        try:
            stable = definition.get("external_id")           # Steam's api name, RetroAchievements' id
            from . import privacy
            pct = (rarity_of(event.get("game_id") or "", name) if rarity_of
                   else rarity.for_unlock(event.get("game_id") or "", name, stable_id=stable,
                                          online=privacy.allowed(profile.config, "rarity")))
        except Exception:  # noqa: BLE001 - no rarity is never a reason to lose the popup
            pct = None
        if pct is not None and pct < float(prefs.get("rare_below", DEFAULTS["rare_below"])):
            out["rare"] = pct
    return out


def popups_here() -> bool:
    """Windows always; Linux when there is a desktop to show them on."""
    if sys.platform == "win32":
        return True
    if sys.platform.startswith("linux"):
        from .linux_desktop import has_display
        return has_display()
    return False


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
    extra = {}
    if os.name != "nt":                            # its own session; a built app starts afresh, not as this one
        from .self_update import clean_environment
        extra = {"start_new_session": True, "env": clean_environment() if getattr(sys, "frozen", False) else None}
    proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, **extra)
    # sound: True/False, or {"on": bool, "unlock": name, "platinum": name} with the chosen sounds
    choice = sound if isinstance(sound, dict) else {"on": bool(sound)}
    proc.stdin.write(json.dumps({"cards": cards, "sound": bool(choice.get("on")),
                                 "sounds": {k: choice[k] for k in ("unlock", "platinum", "rare", "unlock_file",
                                                                   "platinum_file", "rare_file")
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
        if not events or self._launch is _launch and not popups_here():
            return 0
        prefs = settings(self.profile.config.load())
        now = datetime.now(timezone.utc)
        cards = []
        for e in events:
            if e.get("event_type") != "achievement.unlocked":
                continue
            reason = skipped_because(e, prefs, now)
            if reason is None and len(cards) >= MAX_PER_ROUND:
                reason = f"more than {MAX_PER_ROUND} in one round"
            if reason is None:
                cards.append(card(self.profile, e, prefs))
            elif e.get("source", {}).get("adapter") not in PLATFORMS:
                # Local signals (saves, play sessions) always say why; platform
                # imports bring thousands of old unlocks and would flood the log.
                print(f"popup: no popup for {e.get('achievement_id')}: {reason}")
        from . import art, privacy
        offline = None if privacy.allowed(self.profile.config, "pictures") else \
            (lambda url: art.cached(url, online=False))     # pictures switched off: only what is already here
        add_pictures(cards, [c.pop("_picture", None) for c in cards], cached=offline)
        cards += platinum_cards(self.profile, events, prefs, now)
        if cards:
            names = ", ".join(f"{c['name']} ({c['game']})" + (f" rare, {c['rare']:g}%" if "rare" in c else "") for c in cards)
            try:
                self._launch(cards, {"on": prefs["sound"], "unlock": prefs["unlock_sound"],
                                     "platinum": prefs["platinum_sound"], "rare": prefs["rare_sound"],
                                     **{f"{kind}_file": str(custom_file(self.profile, kind))
                                        for kind in ("unlock", "platinum", "rare")
                                        if prefs[f"{kind}_sound"] == "custom" and custom_file(self.profile, kind).exists()}})
            except Exception as exc:
                print(f"popup: could not start the popup for {names}: {exc!r}")
                raise
            print(f"popup: started for {names}")
        return len(cards)
