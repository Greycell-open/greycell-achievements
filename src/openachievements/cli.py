"""`openachievements`: every core action from a terminal.

Secrets are never taken as plain arguments where avoidable: passwords are
prompted for (or read from OA_PASSWORD), the RetroAchievements key from
OA_RA_KEY or a prompt, so they do not land in shell history. Steam takes no
key at all: sign in through Steam, then this PC's Steam files are followed.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from . import __version__
from .profile import MachineConfig, Profile, ProfileError, default_home


def _out(args, data, text: str | None = None) -> None:
    if args.json or text is None:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(text)


def _secret(env: str, prompt: str) -> str:
    return os.environ.get(env) or getpass.getpass(prompt)


def _profile(args) -> Profile:
    return Profile.open(args.home, args.profile, MachineConfig(args.config) if args.config else None)


def _progress_bar(done: int, total: int, width: int = 20) -> str:
    filled = round(width * done / total) if total else 0
    return "#" * filled + "." * (width - filled)


def cmd_profile(args) -> None:
    config = MachineConfig(args.config) if args.config else MachineConfig()
    if args.action == "create":
        p = Profile.create(args.name, home=args.home, config=config)
        _out(args, {"profile_id": p.profile_id, "folder": str(p.folder)},
             f"Created profile '{args.name}' at {p.folder}")
    elif args.action == "show":
        p = _profile(args)
        lib = p.library()
        _out(args, {"profile_id": p.profile_id, "device_id": p.device_id, "folder": str(p.folder),
                    "totals": lib["totals"]},
             f"{lib['profile'].get('name')}  ({p.profile_id})\nfolder  {p.folder}\ndevice  {p.device_id}\n"
             f"{lib['totals']['unlocked']}/{lib['totals']['achievements']} achievements, "
             f"{lib['totals']['points']} points, {lib['totals']['games']} games")
    elif args.action == "restore":
        from .sync import restore
        p = restore(args.server, args.username, _secret("OA_PASSWORD", "Password: "), Path(args.home or default_home()),
                    config)
        _out(args, {"profile_id": p.profile_id, "folder": str(p.folder)}, f"Restored {p.profile_id} to {p.folder}")


def ev_statuses() -> tuple[str, ...]:
    from .events import GAME_STATUSES
    return GAME_STATUSES


def cmd_game(args) -> None:
    p = _profile(args)
    if args.action == "list":
        lib = p.library()
        games = [g for g in lib["games"] if not args.status or g.get("status") == args.status]
        lines = [f"{g['game_id']:<28} {_progress_bar(g['unlocked'], g['total'])} {g['unlocked']:>4}/{g['total']:<4} "
                 f"{(g.get('status') or ''):<10} {(g.get('title') or '')[:40]}" for g in games]
        _out(args, games, "\n".join(lines) or "No games yet.")
    elif args.action == "status":
        p.set_status(args.game_id, args.status, note=args.note)
        _out(args, {"game_id": args.game_id, "status": args.status},
             f"{args.game_id}: {'no status' if args.status == 'none' else args.status}")
    elif args.action == "register":
        p.register_game(args.game_id, args.title, platform=args.platform)
        _out(args, {"game_id": args.game_id}, f"Registered {args.game_id}")
    elif args.action == "register-executable":
        from .adapters import executable
        info = executable.register(p, args.game_id, Path(args.path), title=args.title)
        _out(args, info, f"Registered {Path(args.path).name} for {args.game_id} ({info['installation_id']})")
    elif args.action == "link":
        p.link_games(args.game_id, None if args.into == "none" else args.into)
        _out(args, {"ok": True}, f"{args.game_id} now shows inside {args.into}" if args.into != "none"
             else f"{args.game_id} unlinked")


def cmd_achievement(args) -> None:
    p = _profile(args)
    if args.action == "list":
        games = [g for g in p.library()["games"] if not args.game or g["game_id"] == args.game]
        lines = []
        for g in games:
            lines.append(f"\n{g.get('title') or g['game_id']}  {g['unlocked']}/{g['total']}")
            for a in g["achievements"]:
                mark = "x" if a["unlocked"] else " "
                prog = f" ({a['progress']['value']}/{a['progress']['target']})" if a["progress"] else ""
                src = f"  [{', '.join(a['provenance'])}]" if a["unlocked"] else ""
                lines.append(f"  [{mark}] {a['key']:<40} {a['name']}{prog}{src}")
        _out(args, games, "\n".join(lines).strip() or "No achievements yet.")
    elif args.action == "revoke":
        p.revoke(args.key, args.reason)
        _out(args, {"ok": True}, f"Revoked {args.key} (the unlock stays in history)")




def cmd_pack(args) -> None:
    p = _profile(args)
    e = p.install_pack(Path(args.path))
    _out(args, {"pack_id": e["payload"]["pack_id"], "event": e["event_type"]},
         f"{'Updated' if e['event_type'] == 'pack.updated' else 'Installed'} {e['payload']['pack_id']} "
         f"({len(e['payload']['achievements'])} achievements)")


def cmd_index(args) -> None:
    p = _profile(args)
    problems = p.index.rebuild()
    _out(args, {"problems": problems}, "Index rebuilt." + ("" if not problems else
                                                         "\nProblems (quarantined):\n  " + "\n  ".join(problems)))


def cmd_export(args) -> None:
    from .bundle import csv_summary, export_bundle
    p = _profile(args)
    if args.format == "csv":
        text = csv_summary(p)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"Wrote {args.out}")
        else:
            print(text, end="")
        return
    out = Path(args.out or p.folder / "exports" / f"open-achievements-{p.profile_id[:8]}.zip")
    export_bundle(p, out)
    _out(args, {"bundle": str(out)}, f"Wrote {out}")


def cmd_import_bundle(args) -> None:
    from .bundle import restore_bundle
    p = restore_bundle(Path(args.path), Path(args.home or default_home()),
                       MachineConfig(args.config) if args.config else None)
    _out(args, {"profile_id": p.profile_id}, f"Restored {p.profile_id} to {p.folder}")


def cmd_server(args) -> None:
    from .sync import SyncClient, connect
    p = _profile(args)
    if args.action == "connect":
        password = _secret("OA_PASSWORD", "Password: ")
        s = connect(p, args.url, args.username, password, register=args.register)
        _out(args, {"username": s["username"]}, f"This device is signed in to {args.url} as {s['username']}")
    elif args.action == "disconnect":
        p.config.set_sync(p.profile_id, {})
        _out(args, {"ok": True}, "Disconnected. The local profile is unchanged.")
    elif args.action == "status":
        client = SyncClient(p)
        pending = client.pending()
        settings = p.config.sync_settings(p.profile_id)
        _out(args, {"server": settings.get("server"), "pending": len(pending)},
             f"{settings.get('server')}  {len(pending)} event(s) waiting to sync")


def cmd_sync(args) -> None:
    from .sync import SyncClient
    result = SyncClient(_profile(args)).run()
    _out(args, result.__dict__, f"Sent {result.uploaded}, received {result.downloaded}"
         + (f", {len(result.rejected)} rejected" if result.rejected else ""))


def cmd_import(args) -> None:
    p = _profile(args)
    from .adapters import retroachievements as ra
    summary = ra.import_account(p, args.account, _secret("OA_RA_KEY", "RetroAchievements Web API key: "),
                                only_changed=not args.full)
    s = summary.as_dict()
    _out(args, s, f"{args.source}: {s['games_seen']} games seen, {s['games_added']} new, "
         f"{s['unlocks_added']} unlocks imported ({s['unlocks_already_known']} already here)"
         + ("".join(f"\n  ! {x}" for x in s["problems"])))


def cmd_watch(args) -> None:
    from .adapters import executable, savefile
    from .watching import watch_forever
    p = _profile(args)
    count = len(executable.local_installations(p))
    allowed = sum(1 for pid, pk in p.state()["packs"].items() if not pk.get("removed")
                  for s in pk.get("saves") or [] if savefile.granted_folder(p, pid, s) is not None)
    print(f"Watching {count} registered executable(s) and {allowed} allowed save location(s). Ctrl+C to stop.")
    try:
        watch_forever(p, args.interval, say=print)
    except KeyboardInterrupt:
        print("Stopped.")


def cmd_update(args) -> None:
    from . import update
    p = _profile(args)
    if args.action == "check":
        if args.value in ("on", "off"):
            with p.config.editing() as config:
                config.setdefault("update", {})["check"] = args.value == "on"
            _out(args, {"check": args.value}, f"Daily update check {args.value}.")
            return
    if args.action == "repo":
        if not args.value:
            raise SystemExit("error: say `update repo owner/name`")
        with p.config.editing() as config:
            config.setdefault("update", {})["repo"] = args.value
    status = update.check(p.config, force=args.action in ("check", "repo", "install"))
    if not status["repo"]:
        _out(args, status, "No update source set (`openachievements update repo owner/name`).")
        return
    found = status["available"]
    if args.action != "install" or not found:
        _out(args, status, f"Version {found['version']} is available (you have {status['current']})." if found
             else f"Up to date ({status['current']}).")
        return
    if update.frozen():
        _out(args, {"download": update.DOWNLOAD_PAGE}, f"Version {found['version']} is out: download it from "
             f"{update.DOWNLOAD_PAGE}. Your achievements stay where they are.")
        return
    if update.running_from_source():
        _out(args, {"skipped": "source checkout"}, f"Version {found['version']} is out, but this copy runs from a "
             "git checkout: pull it instead of installing over it.")
        return
    print(f"Installing {found['version']} from {found['url']} ...")
    code = update.install(found)
    if code:
        raise SystemExit(f"error: the update did not install (pip exited {code}); this version keeps working")
    _out(args, {"installed": found["version"]}, f"Installed {found['version']}. Start the app again to use it.")


def cmd_notify(args) -> None:
    from . import notify
    p = _profile(args)
    if args.action == "test":
        notify._launch([{"name": "This is how an unlock looks", "game": "", "points": 0}],
                       notify.settings(p.config.load())["sound"])
        _out(args, {"ok": True}, "Showing a test popup at the bottom of the screen.")
        return
    if args.action in ("on", "off"):
        notify.change(p, enabled=args.action == "on")
    elif args.action in ("steam", "sound"):
        if args.value not in ("on", "off"):
            raise SystemExit(f"error: say `notify {args.action} on` or `notify {args.action} off`")
        notify.change(p, **{args.action: args.value == "on"})
    s = notify.settings(p.config.load())
    _out(args, s, f"Popups {'on' if s['enabled'] else 'off'}, sound {'on' if s['sound'] else 'off'}, "
                  f"Steam unlocks {'shown' if s['steam'] else 'left to Steam'}.")


def cmd_emulators(args) -> None:
    from .adapters import emulator
    emulator.set_enabled(_profile(args), args.state == "on")
    _out(args, {"emulators": args.state}, "Achievements Steam emulators record will be read." if args.state == "on"
         else "Steam emulator achievement files will not be read.")


def cmd_autodetect(args) -> None:
    from .adapters import autodetect
    autodetect.set_enabled(_profile(args), args.state == "on")
    _out(args, {"autodetect": args.state}, "Games will be recognised as they run." if args.state == "on"
         else "Running programs will not be matched to games.")


def cmd_steam(args) -> None:
    from .adapters import steam_local
    p = _profile(args)
    if args.action == "link":
        info = steam_local.link(p, Path(args.steam_dir) if args.steam_dir else None, args.steam_id)
        _out(args, info, f"Linked Steam account {info['steam_id']} on this computer ({info['games']} games with "
             "achievement files). Importing now; 'watch' keeps it live.")
        args.action = "sync"
    if args.action == "unlink":
        steam_local.unlink(p)
        _out(args, {"ok": True}, "Steam on this computer is no longer followed. Imported achievements stay.")
    elif args.action == "sync":
        if not steam_local.linked(p):
            raise SystemExit("error: link Steam first: openachievements steam link")
        w = steam_local.SteamLocalWatcher(p)
        written = w.poll()
        games = len({e["game_id"] for e in written})
        _out(args, {"unlocks": len(written), "games": games, "problems": list(w.problems.values())},
             f"{len(written)} new achievement(s) across {games} game(s)"
             + "".join(f"\n  ! {m}" for m in w.problems.values()))


def cmd_save(args) -> None:
    from . import cli_saves
    from .adapters import savefile
    p = _profile(args)
    if args.action in cli_saves.ACTIONS:
        cli_saves.run(args, p, _out)
        return
    if args.action in ("allow", "revoke"):
        try:
            if args.action == "allow":
                folder = savefile.allow(p, args.pack_id, args.save_id)
                _out(args, {"folder": str(folder)}, f"Allowed: {args.pack_id}/{args.save_id} may read {folder} "
                     "on this machine (read only; nothing is copied or uploaded).")
            else:
                savefile.revoke(p, args.pack_id, args.save_id)
                _out(args, {"ok": True}, f"{args.pack_id}/{args.save_id} will no longer be read.")
        except ProfileError as exc:
            raise SystemExit(f"error: {exc}") from None
        return
    if args.action == "locate":
        if not args.reset and not args.folder:
            raise SystemExit("error: give the folder, or --reset")
        savefile.locate(p, args.pack_id, args.save_id, None if args.reset else Path(args.folder))
        _out(args, {"ok": True}, "Back to the pack's default location." if args.reset
             else f"{args.pack_id}/{args.save_id} is read from {Path(args.folder).resolve()} on this machine.")
        return
    report = savefile.check(p, args.pack_id)
    lines = []
    for r in report:
        if not r["allowed"]:
            lines.append(f"{r['pack_id']} / {r['save']}: not allowed yet. It would read "
                         f"{r['folder'] or '(no folder on this OS)'}; to allow: "
                         f"openachievements save allow {r['pack_id']} {r['save']}")
            continue
        lines.append(f"{r['pack_id']} / {r['save']}: {r['folder'] or '(no such folder on this OS)'}"
                     + ("" if r["exists"] else "  (not found)"))
        for f in r["files"]:
            lines.append(f"    {f['file']}: " + (f"! {f['error']}" if "error" in f
                                                  else (", ".join(f["satisfies"]) or "no achievements yet")))
    _out(args, report, "\n".join(lines) or "No installed pack declares save files.")


def cmd_serve(args) -> None:
    import uvicorn
    from .local_app import create_local_app
    p = _profile(args)
    if not args.no_watch:
        from .watching import start_in_background
        start_in_background(p)
        print("Watching in the background: running games, saves and Steam stay up to date while this runs.")
    print(f"Greycell Achievements on http://127.0.0.1:{args.port}  (this computer only)")
    uvicorn.run(create_local_app(p), host="127.0.0.1", port=args.port, log_level="warning")


def cmd_catalog(args) -> None:
    from .catalog import steam as cat
    folder = Path(args.dir) if args.dir else cat.default_dir()
    if args.action == "crawl":
        apps = cat.read_app_list(args.apps)
        print(f"Crawling {len(apps)} Steam apps into {folder} (one request per {args.pace}s per host; "
              f"stop any time, it resumes)")
        try:
            s = cat.crawl(apps, folder, cat.PacedFetcher(pace=args.pace, notify=print), limit=args.limit, refresh=args.refresh,
                          report=print)
        except KeyboardInterrupt:
            print("Stopped. Run the same command to carry on.")
            return
        _out(args, s.__dict__, f"{s.checked} checked, {s.packs} games with achievements ({s.achievements} "
             f"achievements), {s.without_achievements} without, {s.skipped_done} already done"
             + (f", {len(s.problems)} problems" if s.problems else ""))
        return
    index = cat.CatalogIndex(folder)
    if args.action == "list":
        rows = index.search(args.search or "", limit=args.limit)
        _out(args, rows, "\n".join(f"{r['appid']:>8}  {r['title']}  ({r['achievements']})" for r in rows)
             or ("Nothing catalogued yet." if not len(index) else "No match."))
    elif args.action == "install":
        try:
            added = cat.add_to_profile(_profile(args), index, args.appid, status=args.status,
                                      fetch=cat.PacedFetcher(pace=1.5))
        except cat.CatalogError as exc:
            raise SystemExit(f"error: {exc} (at {folder})") from None
        _out(args, added, f"Added {added['title']} ({added['achievements']} achievements)"
             + (f", {added['status']}" if added["status"] else ""))


def cmd_testgame(args) -> None:
    from . import testgame
    p = _profile(args)
    if args.action == "reset":
        n = testgame.reset(p)
        _out(args, {"taken_back": n}, f"Test game reset: {n} unlocks taken back, save deleted. Run testgame to play again.")
        return
    if args.action == "remove":
        testgame.remove(p)
        _out(args, {"ok": True}, "Test game removed from the library.")
        return
    level = testgame.play(p)
    name = testgame.ACHIEVEMENTS[level - 1][1]
    extra = " That completes it: Platinum is next." if level == testgame.LEVELS else ""
    _out(args, {"level": level}, f"Finished level {level} of {testgame.LEVELS}. With the app running, "
         f"'{name}' unlocks within a few seconds.{extra}")


def cmd_doctor(args) -> None:
    p = _profile(args)
    report = p.log.read(quarantine=False)
    from .adapters import executable
    missing = [i for i in executable.local_installations(p) if not Path(i["path"]).exists()]
    data = {"events": len(report.events), "problems": report.problems, "duplicates": report.duplicates,
            "missing_executables": [i["path"] for i in missing], "folder": str(p.folder)}
    lines = [f"{len(report.events)} events, {len(report.problems)} problem(s), {report.duplicates} duplicate line(s)"]
    lines += [f"  ! {x}" for x in report.problems] + [f"  ! executable moved or deleted: {m}" for m in data["missing_executables"]]
    _out(args, data, "\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="openachievements", description="Your achievements, on your own terms.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--home", help="folder holding profiles (default ~/OpenAchievements or $OPENACHIEVEMENTS_HOME)")
    ap.add_argument("--profile", help="profile id (default: this machine's active profile)")
    ap.add_argument("--config", help="machine config folder (default: platform config dir)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("profile", help="create, show or restore a profile")
    ps = p.add_subparsers(dest="action", required=True)
    c = ps.add_parser("create"); c.add_argument("name")
    ps.add_parser("show")
    r = ps.add_parser("restore", help="download a synced profile onto this device")
    r.add_argument("server"); r.add_argument("username")
    p.set_defaults(func=cmd_profile)

    g = sub.add_parser("game", help="games and installations")
    gs = g.add_subparsers(dest="action", required=True)
    gl = gs.add_parser("list"); gl.add_argument("--status", choices=[s for s in ev_statuses() if s != "none"])
    st = gs.add_parser("status", help="backlog: wishlist, backlog, playing, completed, dropped, or none")
    st.add_argument("game_id"); st.add_argument("status", choices=ev_statuses()); st.add_argument("--note")
    reg = gs.add_parser("register"); reg.add_argument("game_id"); reg.add_argument("title")
    reg.add_argument("--platform")
    rx = gs.add_parser("register-executable"); rx.add_argument("game_id"); rx.add_argument("path")
    rx.add_argument("--title")
    ln = gs.add_parser("link", help="show one game's achievements inside another ('none' to undo)")
    ln.add_argument("game_id"); ln.add_argument("into")
    g.set_defaults(func=cmd_game)

    # Achievements unlock from what a game records (imports, saves, play), never by hand.
    a = sub.add_parser("achievement", help="list or revoke")
    as_ = a.add_subparsers(dest="action", required=True)
    al = as_.add_parser("list"); al.add_argument("game", nargs="?")
    ar = as_.add_parser("revoke"); ar.add_argument("key"); ar.add_argument("--reason")
    a.set_defaults(func=cmd_achievement)


    pk = sub.add_parser("pack", help="install an achievement pack folder or .zip")
    pk.add_argument("action", choices=["install"]); pk.add_argument("path")
    pk.set_defaults(func=cmd_pack)

    ix = sub.add_parser("index", help="rebuild the SQLite index from the event files")
    ix.add_argument("action", choices=["rebuild"]); ix.set_defaults(func=cmd_index)

    ex = sub.add_parser("export", help="portable bundle (.zip) or CSV summary")
    ex.add_argument("--format", choices=["bundle", "csv"], default="bundle"); ex.add_argument("--out")
    ex.set_defaults(func=cmd_export)

    ib = sub.add_parser("restore-bundle", help="restore a profile from an exported bundle")
    ib.add_argument("path"); ib.set_defaults(func=cmd_import_bundle)

    sv = sub.add_parser("server", help="connect this device to a sync server")
    ss = sv.add_subparsers(dest="action", required=True)
    sc = ss.add_parser("connect"); sc.add_argument("url"); sc.add_argument("username")
    sc.add_argument("--register", action="store_true", help="create the account first")
    ss.add_parser("disconnect"); ss.add_parser("status")
    sv.set_defaults(func=cmd_server)

    sub.add_parser("sync", help="exchange events with the connected server").set_defaults(func=cmd_sync)

    im = sub.add_parser("import", help="import achievements from a platform (read-only; Steam: see `steam link`)")
    im.add_argument("source", choices=["retroachievements"])
    im.add_argument("account", help="RetroAchievements username")
    im.add_argument("--full", action="store_true", help="refetch every game, not only changed ones")
    im.set_defaults(func=cmd_import)

    w = sub.add_parser("watch", help="detect running games and save progress; record sessions and unlocks")
    w.add_argument("--interval", type=float, default=5.0); w.set_defaults(func=cmd_watch)

    up = sub.add_parser("update", help="new versions from the project's releases")
    up.add_argument("action", nargs="?", default="status", choices=["status", "check", "install", "repo"])
    up.add_argument("value", nargs="?")
    up.set_defaults(func=cmd_update)

    nt = sub.add_parser("notify", help="the popup when you unlock something (on by default)")
    nt.add_argument("action", choices=["on", "off", "status", "test", "steam", "sound"])
    nt.add_argument("value", nargs="?", choices=["on", "off"])
    nt.set_defaults(func=cmd_notify)

    au = sub.add_parser("autodetect", help="recognise games from running programs (on by default)")
    au.add_argument("state", choices=["on", "off"]); au.set_defaults(func=cmd_autodetect)
    em = sub.add_parser("emulators", help="read achievements Steam emulators record (on by default)")
    em.add_argument("state", choices=["on", "off"]); em.set_defaults(func=cmd_emulators)

    sm = sub.add_parser("steam", help="follow Steam on this computer: games and achievements as they happen")
    sms = sm.add_subparsers(dest="action", required=True)
    sk = sms.add_parser("link", help="follow the Steam account signed in here (no password, no key)")
    sk.add_argument("--steam-id", help="which account, if several have signed in here")
    sk.add_argument("--steam-dir", help="Steam's folder, if it is not found")
    sms.add_parser("unlink"); sms.add_parser("sync", help="import now instead of waiting for 'watch'")
    sm.set_defaults(func=cmd_steam)

    sa = sub.add_parser("save", help="save-file achievements, the save keeper (kept copies, restore) and rule learning")
    sas = sa.add_subparsers(dest="action", required=True)
    sc2 = sas.add_parser("check", help="list save files and which achievements each satisfies")
    sc2.add_argument("pack_id", nargs="?")
    for name, text in (("allow", "let this machine read a pack's save folder (shows the folder)"),
                       ("revoke", "stop reading a pack's save folder")):
        sp = sas.add_parser(name, help=text); sp.add_argument("pack_id"); sp.add_argument("save_id")
    sl = sas.add_parser("locate", help="this machine keeps a game's saves in another folder (allows it too)")
    sl.add_argument("pack_id"); sl.add_argument("save_id"); sl.add_argument("folder", nargs="?")
    sl.add_argument("--reset", action="store_true", help="use the pack's default location again")
    from . import cli_saves
    cli_saves.register(sas)
    sa.set_defaults(func=cmd_save)

    se = sub.add_parser("serve", help="open the library in your browser, on this computer only")
    se.add_argument("--port", type=int, default=8788)
    se.add_argument("--no-watch", action="store_true", help="only the page; do not watch in the background")
    se.set_defaults(func=cmd_serve)

    ct = sub.add_parser("catalog", help="the public Steam achievement catalogue (no account needed)")
    ct.add_argument("--dir", help="catalogue folder (default: platform data dir or $OA_CATALOG_DIR)")
    cs = ct.add_subparsers(dest="action", required=True)
    cc = cs.add_parser("crawl", help="fetch games and their achievements from Steam's public pages")
    cc.add_argument("source", choices=["steam"])
    cc.add_argument("--apps", required=True, help="file with one appid (optionally TAB name) per line")
    cc.add_argument("--limit", type=int, help="stop after this many apps")
    cc.add_argument("--pace", type=float, default=1.5, help="seconds between requests to one host")
    cc.add_argument("--refresh", action="store_true", help="crawl apps again even if done")
    cl = cs.add_parser("list"); cl.add_argument("search", nargs="?")
    cl.add_argument("--limit", type=int, default=200)
    ci = cs.add_parser("install", help="add a Steam game and its achievements to your library (fetched now if not catalogued)"); ci.add_argument("appid", type=int)
    ci.add_argument("--status", choices=[s for s in ev_statuses() if s != "none"])
    ct.set_defaults(func=cmd_catalog)

    tg = sub.add_parser("testgame", help="a five-level test game to try unlocks, popups and Platinum")
    tg.add_argument("action", nargs="?", default="play", choices=["play", "reset", "remove"])
    tg.set_defaults(func=cmd_testgame)

    sub.add_parser("doctor", help="check the profile folder").set_defaults(func=cmd_doctor)
    return ap


def _safe_streams() -> None:
    """Game titles and names are in every script. A Windows console or a
    redirected log may use a legacy code page; print what it can rather than
    crash (a multi-day crawl died on its first title with a macron)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_streams()
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ProfileError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - adapters and sync raise their own typed errors
        code = getattr(exc, "code", None)
        if code:
            print(f"error ({code}): {exc}", file=sys.stderr)
            return 1
        raise
    return 0
