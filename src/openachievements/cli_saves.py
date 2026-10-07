"""`openachievements save ...` commands for the save keeper and rule learning."""
from __future__ import annotations

import json
from pathlib import Path

ACTIONS = ("kept", "keep", "restore", "changes", "learn", "rule", "keeper")


def register(sas) -> None:
    sk = sas.add_parser("kept", help="games whose saves are kept, with their snapshots")
    sk.add_argument("game_id", nargs="?")
    sp = sas.add_parser("keep", help="keep a copy of a game's saves now")
    sp.add_argument("game_id")
    sr = sas.add_parser("restore", help="put kept saves back (what is there now is kept first)")
    sr.add_argument("game_id")
    sr.add_argument("--snapshot", help="snapshot id (default: the newest)")
    sr.add_argument("--to", help="another folder to restore into")
    sr.add_argument("--dry-run", action="store_true", help="only show what would be written")
    sc = sas.add_parser("changes", help="what changed in a game's saves between kept versions")
    sc.add_argument("game_id")
    sc.add_argument("--file", help="only files whose name contains this")
    sc.add_argument("--limit", type=int, default=60)
    sl = sas.add_parser("learn", help="rules learned from unlocks the game reported another way")
    sl.add_argument("game_id")
    sl.add_argument("--accept", type=int, action="append", default=[], help="add suggestion N as a rule")
    su = sas.add_parser("rule", help="add a save rule on this computer")
    su.add_argument("game_id")
    su.add_argument("achievement", help="its name or id")
    su.add_argument("--file", required=True, help="the save file, as `save changes` shows it")
    su.add_argument("--field", required=True)
    su.add_argument("--op", default="==", choices=["==", "!=", ">=", "<=", ">", "<", "contains", "exists"])
    su.add_argument("--value", default="true", help="text, or a number")
    su.add_argument("--folder", help="the save folder (default: the one the keeper holds it from)")
    so = sas.add_parser("keeper", help="keep copies of every game's saves (on by default)")
    so.add_argument("state", choices=["on", "off"])


def _running(profile, game_id: str) -> bool:
    from .adapters import executable
    running = executable.running_executables()
    return any(i["game_id"] == game_id and executable._is_running(i["path"], running)
               for i in executable.local_installations(profile))


def _number(text: str):
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def run(args, profile, out) -> None:
    from . import savekeeper, savelearn
    keeper = savekeeper.Keeper(profile)
    try:
        if args.action == "keeper":
            savekeeper.set_enabled(profile, args.state == "on")
            out(args, {"keeper": args.state}, "Saves will be kept." if args.state == "on" else
                "Saves will not be copied any more (copies already kept stay).")
        elif args.action == "kept":
            games = [args.game_id] if args.game_id else keeper.games()
            report = {g: keeper.snapshots(g) for g in games}
            lines = []
            for g, snaps in report.items():
                lines.append(f"{g}: {len(snaps)} snapshot(s)" + (f", newest {snaps[0]['taken_at']} from "
                                                                   f"{snaps[0]['folder']}" if snaps else ""))
                if args.game_id:
                    lines += [f"    {s['id']}  {s['taken_at']}  {len(s['files'])} files  {s['reason']}" for s in snaps]
            out(args, report, "\n".join(lines) or f"No saves kept yet. They are kept in {keeper.root}")
        elif args.action == "keep":
            folders = savekeeper.save_folders(profile).get(args.game_id) or []
            if not folders:
                raise SystemExit(f"error: no save folder known for {args.game_id}")
            taken = [s for s in (keeper.take(args.game_id, Path(f), reason="asked") for f in folders) if s]
            out(args, taken, f"Kept {len(taken)} new snapshot(s)." if taken else "Nothing new to keep.")
        elif args.action == "restore":
            if args.dry_run:
                plan = keeper.plan(args.game_id, args.snapshot, Path(args.to) if args.to else None)
            else:
                plan = keeper.restore(args.game_id, args.snapshot, Path(args.to) if args.to else None,
                                      running=_running(profile, args.game_id))
            verb = "Would write" if args.dry_run else "Wrote"
            lines = [f"{verb} {len(plan['actions'])} file(s) from {plan['snapshot']['taken_at']} "
                     f"into {plan['folder']}"] + [f"    {a['action']} {a['file']}" for a in plan["actions"]]
            if plan["replaces_existing"] and not args.dry_run:
                lines.append("What was there before is kept as a snapshot (reason before-restore).")
            out(args, {k: v for k, v in plan.items() if k != "snapshot"} | {"snapshot": plan["snapshot"]["id"]},
                "\n".join(lines))
        elif args.action == "changes":
            found = savelearn.changes(profile, args.game_id, args.file, args.limit)
            lines = [f"{c['at']}  {c['file']}  {c['field']}: {json.dumps(c['old'])} -> {json.dumps(c['new'])}"
                     for c in found]
            out(args, found, "\n".join(lines) or "No changes between kept versions yet: play, and look again.")
        elif args.action == "learn":
            found = savelearn.suggest(profile, args.game_id)
            for n in args.accept:
                if not 1 <= n <= len(found):
                    raise SystemExit(f"error: there is no suggestion {n}")
                s = found[n - 1]
                savelearn.add_rule(profile, args.game_id, s["achievement_id"], s["folder"], s["file"], s["field"],
                                   s["op"], s["value"], fmt=s["format"])
            lines = [f"{n}. {s['achievement']}: {s['field']} {s['op']} {json.dumps(s['value'])} in {s['file']}  "
                     f"({s['confidence']}, {s['candidates']} candidate(s))" for n, s in enumerate(found, 1)]
            if args.accept:
                lines.append(f"Added {len(args.accept)} rule(s) in {savelearn.rules_dir(profile)}.")
            out(args, found, "\n".join(lines) or "Nothing to learn yet: it needs kept saves from before and "
                                                 "after an unlock the game reported another way.")
        elif args.action == "rule":
            folder = args.folder
            if folder is None:
                held = [s for s in keeper.snapshots(args.game_id) if args.file in s["files"]]
                if not held:
                    raise SystemExit(f"error: no kept save named {args.file}; give --folder")
                folder = held[0]["folder"]
            path = savelearn.add_rule(profile, args.game_id, args.achievement, folder, args.file, args.field,
                                      args.op, _number(args.value))
            out(args, {"rules": str(path)}, f"Rule added in {path}. It applies from the next look at the save.")
    except savekeeper.KeeperError as exc:
        raise SystemExit(f"error: {exc}") from None
