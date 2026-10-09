"""The watcher: everything that happens on its own while the app runs.

One round every few seconds: recognise running games and count their play
time, read allowed save folders, follow Steam on this computer (unlocks and
play history), read the achievements a Steam emulator recorded, find games by
their save folders, keep a copy of every game's saves (and put them back when a
reinstalled game has none), and fetch achievement lists
for library games that have none. `openachievements watch` runs it in the
foreground; `openachievements serve` runs it in the background, so the library
page alone keeps everything up to date.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from .profile import Profile


# What the watcher saw on its last round, for the page's live dashboard:
# the games running now and when it last looked. Written by the watcher thread,
# read by the local app; a whole new dict is assigned each round, never mutated.
LIVE: dict = {"playing": [], "at": None}


def _quiet(_message: str) -> None:
    pass


def watch_forever(profile: Profile, interval: float = 5.0, say: Callable[[str], None] = _quiet,
                  stop: threading.Event | None = None) -> None:
    from .adapters import (autodetect, emulator, executable, gog, psn, retroachievements, rpcs3, savefile, saverules,
                           shadps4, steam_local, stores, ubisoft, vita3k, xbox, xenia)
    from .catalog import steam as cat
    from .notify import Notifier

    popup = Notifier(profile)
    from . import community
    round_events: list[dict] = []

    def show(e: dict) -> None:
        round_events.append(e)
        if e["event_type"] == "achievement.unlocked":
            say(f"  unlocked {e['achievement_id']}  ({e['payload'].get('provenance')})")
            popup.add(e)
        else:
            say(f"  {e['event_type']} {e.get('game_id')}")

    def complain(exc: Exception | str) -> None:
        say(f"  ! {exc}")

    procs = executable.Watcher(profile)
    detector = autodetect.AutoDetector(profile)
    watchers = [savefile.SaveWatcher(profile), emulator.EmulatorWatcher(profile), steam_local.SteamLocalWatcher(profile),
                psn.PsnWatcher(profile), rpcs3.Rpcs3Watcher(profile), xenia.XeniaWatcher(profile),
                ubisoft.UbisoftWatcher(profile), vita3k.Vita3kWatcher(profile), shadps4.ShadPs4Watcher(profile),
                xbox.XboxWatcher(profile), gog.GogWatcher(profile),
                retroachievements.RaWatcher(profile)]
    from .savekeeper import KeeperWatcher
    keeper = KeeperWatcher(profile)                   # every known save folder, kept; put back on reinstall
    lists = cat.PackFetcher(profile)
    last_lists = 0.0
    from .finder import AchievementFinder
    finder = AchievementFinder(profile)               # achievements for games recognised without any
    asked_for: dict = {}                              # running game -> when its list was last asked for
    rule_looks: dict = {}                             # game -> when its saves were last read for rules by name
    from . import rarity
    rare = rarity.RarityCrawler(profile)              # Steam's unlock percentages, one game at a time
    if steam_local.linked(profile):
        say("Following Steam on this computer: new achievements appear as Steam records them.")
    if detector.enabled():
        say("Recognising games as they run (openachievements autodetect off to stop).")
    while not (stop and stop.is_set()):
        running = executable.running_executables()          # one look at the process list per round
        if time.monotonic() - last_lists > 60:                # achievement lists for games the crawl lacks
            last_lists = time.monotonic()
            folder = cat.default_dir()
            try:
                lists.want(cat.CatalogIndex(folder) if (folder / cat.PACKS_FILE).exists() else None)
            except Exception as exc:  # noqa: BLE001 - keep watching; report, do not die
                complain(exc)
            try:
                finder.want()
                while finder.found:
                    local_id, steam_id = finder.found.pop(0)
                    say(f"  achievements found for {local_id}: linked into {steam_id}")
            except Exception as exc:  # noqa: BLE001 - keep watching; report, do not die
                complain(exc)
        try:
            for found in detector.poll(running):
                say(f"  recognised {found['title']} ({found['game_id']})"
                    + (f"; saves may be in {found['save_folders'][0]}" if found["save_folders"] else ""))
                if found["game_id"].startswith(stores.FINDABLE):
                    finder.want()                         # being played now: look its achievements up now
            for event in procs.poll(running):
                show(event)
        except Exception as exc:  # noqa: BLE001 - keep watching; report, do not die
            complain(exc)
        try:
            for pack_id in saverules.apply(profile):      # shipped save rules for games in the library
                say(f"  save rules added to {pack_id}")
        except Exception as exc:  # noqa: BLE001 - keep watching; report, do not die
            complain(exc)
        for w in watchers:
            try:
                for event in w.poll():
                    show(event)
            except Exception as exc:  # noqa: BLE001 - keep watching; report, do not die
                complain(exc)
            for problem in getattr(w, "new_problems", lambda: [])():
                complain(problem)
        try:
            rare.poll()
        except Exception as exc:  # noqa: BLE001 - rarity is a nicety; keep watching
            complain(exc)
        try:
            playing = {gid for gid in detector.known.values() if gid}
            playing |= {i["game_id"] for i in executable.local_installations(profile)
                        if executable._is_running(i["path"], running)}
            global LIVE
            LIVE = {"playing": sorted(playing), "at": time.time()}
        except Exception as exc:  # noqa: BLE001 - the dashboard is a nicety; keep watching
            complain(exc)
        try:                                                  # being played with no achievements: fetch first
            from .finder import ask_for_playing
            for game_id in ask_for_playing(profile, LIVE["playing"], lists, finder, asked_for, time.monotonic()):
                say(f"  {game_id} is being played without achievements: asking Steam first")
        except Exception as exc:  # noqa: BLE001 - keep watching; report, do not die
            complain(exc)
        try:                                                  # saves that name their achievements: rules by name
            from .ruleseek import seek_playing
            for found in seek_playing(profile, LIVE["playing"], rule_looks, time.monotonic()):
                say(f"  {found['rules']} save rules for {found['game_id']} from {found['file']} "
                    f"({found['seen']} achievements named in it)")
        except Exception as exc:  # noqa: BLE001 - keep watching; report, do not die
            complain(exc)
        try:                                                  # the save keeper: copies, and saves put back
            for message in keeper.poll(set(LIVE["playing"])):
                say(f"  {message}")
        except Exception as exc:  # noqa: BLE001 - keep watching; report, do not die
            complain(exc)
        try:
            popup.flush()                                     # the round's fresh unlocks, one popup each
        except Exception as exc:  # noqa: BLE001 - a popup is a nicety; keep watching
            complain(exc)
        try:                                                  # anonymous counts for greycell.app, if shared
            community.from_round(profile, round_events)
            sent = community.send(profile.config)
            if sent.startswith(("sent", "refused")):
                say(f"  community stats: {sent}")
        except Exception as exc:  # noqa: BLE001 - stats are a nicety; keep watching
            complain(exc)
        round_events.clear()
        if stop:
            stop.wait(interval)
        else:
            time.sleep(interval)


def start_in_background(profile: Profile, interval: float = 5.0) -> threading.Event:
    """Run the watcher on a daemon thread; set the returned event to stop it."""
    stop = threading.Event()
    threading.Thread(target=watch_forever, args=(profile, interval, lambda m: print(m, file=sys.stderr), stop),
                     daemon=True, name="openachievements-watch").start()
    return stop
