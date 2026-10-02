"""A profile folder and the actions on it.

Two places on disk, deliberately separate:

- The **profile folder** (`<home>/profiles/<profile-id>/`) is the portable,
  user-owned copy of the achievement history. It can be backed up, moved, put
  in a synced folder or copied to another machine.
- The **machine config** (`<config>/`) holds what belongs to this computer
  only: its device id for each profile, sync server settings, the sync token
  and cursor. If it were inside the profile folder, two machines sharing that
  folder would share one device identity, and a token would travel with every
  export.
"""
from __future__ import annotations

import contextlib
import os
import threading
import platform as _platform
import shutil
import sys
from pathlib import Path

from . import __version__
from . import events as ev
from . import packs as packs_mod
from .eventlog import EventLog
from .fsutil import file_lock, read_json, write_json_atomic
from .index import Index


def default_home() -> Path:
    return Path(os.environ.get("OPENACHIEVEMENTS_HOME") or Path.home() / "OpenAchievements")


def default_config_dir() -> Path:
    if os.environ.get("OPENACHIEVEMENTS_CONFIG"):
        return Path(os.environ["OPENACHIEVEMENTS_CONFIG"])
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "OpenAchievements"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "openachievements"


class ProfileError(RuntimeError):
    pass


_HELD = threading.local()                    # machine.json edits this thread is inside


class MachineConfig:
    """Per-computer settings, written owner-only. Secrets are not kept here."""

    def __init__(self, config_dir: Path | None = None):
        self.dir = Path(config_dir or default_config_dir())
        self.path = self.dir / "machine.json"

    def load(self) -> dict:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        for key, empty in (("devices", {}), ("sync", {}), ("active_profile", None)):
            if not isinstance(data.get(key), type(empty)) if empty is not None else key not in data:
                data[key] = empty                    # a partial or hand-edited file gets what is missing
        return data

    @contextlib.contextmanager
    def editing(self):
        """Every change to machine.json goes through here: load, change, save
        as one step under one lock, so two threads (the watcher, the page, the
        updater) never write back stale copies over each other. Nothing is
        saved if the change raises."""
        key = str(self.dir.resolve())
        held = _HELD.__dict__.setdefault("dirs", {})
        if held.get(key) is not None:                # already inside an edit on this thread: same
            yield held[key]                          # transaction, same data, the outer edit saves it
            return
        # Threads of this process take turns, and a lock file next to
        # machine.json keeps the tray app and a command typed meanwhile
        # (GreycellAchievements.exe update check off) from overwriting each other.
        with file_lock(self.dir / ".machine.lock", timeout=30.0, stale_after=60.0):
            data = held[key] = self.load()
            try:
                yield data
                self.save(data)
            finally:
                held[key] = None

    def edit(self, change) -> dict:
        with self.editing() as data:
            change(data)
        return data

    def save(self, data: dict) -> None:
        write_json_atomic(self.path, data)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def device_id(self, profile_id: str) -> str | None:
        return self.load()["devices"].get(profile_id)

    def set_device(self, profile_id: str, device_id: str) -> None:
        with self.editing() as data:
            data["devices"][profile_id] = device_id
            data["active_profile"] = data.get("active_profile") or profile_id

    # The sync server's session token is a reusable secret: it lives in the
    # OS credential store (credentials.py), never in machine.json. The scope
    # includes this config folder, so two configs on one computer (tests, a
    # second Windows user's portable copy) never share a token.
    SYNC_SECRET = "sync-token"

    def _sync_scope(self, profile_id: str) -> str:
        import hashlib
        return f"{profile_id}@{hashlib.sha1(str(self.dir.resolve()).encode()).hexdigest()[:10]}"

    def sync_settings(self, profile_id: str) -> dict:
        from . import credentials
        settings = dict(self.load()["sync"].get(profile_id, {}))
        if settings.get("token"):                    # written by an older version: move it out of the file
            self.set_sync(profile_id, settings)
            settings = dict(self.load()["sync"].get(profile_id, {}))
        if settings.get("server"):
            token = credentials.get_secret(self.SYNC_SECRET, self._sync_scope(profile_id))
            if token:
                settings["token"] = token
        return settings

    def set_sync(self, profile_id: str, settings: dict) -> None:
        from . import credentials
        settings = dict(settings)
        token = settings.pop("token", None)
        scope = self._sync_scope(profile_id)
        if token:
            credentials.set_secret(self.SYNC_SECRET, scope, token)
        elif not settings.get("server"):
            credentials.delete_secret(self.SYNC_SECRET, scope)       # disconnected: the token goes too
        self.edit(lambda data: data["sync"].__setitem__(profile_id, settings))


class Profile:
    def __init__(self, folder: Path, config: MachineConfig | None = None):
        self.folder = Path(folder)
        meta = read_json(self.folder / "profile.json")
        if not meta or "profile_id" not in meta:
            raise ProfileError(f"no profile at {self.folder}")
        self.meta = meta
        self.profile_id: str = meta["profile_id"]
        self.config = config or MachineConfig()
        self.log = EventLog(self.folder, self.profile_id)
        self.index = Index(self.folder, self.log)
        self.device_id = self.config.device_id(self.profile_id) or self._register_this_device()

    # ---- creation and opening ------------------------------------------------

    @classmethod
    def create(cls, name: str, home: Path | None = None, config: MachineConfig | None = None) -> "Profile":
        home = Path(home or default_home())
        profile_id = ev.new_id()
        folder = home / "profiles" / profile_id
        for sub in ("devices", "games", "packs", "events", "evidence", "exports", "backups", "cache", "logs"):
            (folder / sub).mkdir(parents=True, exist_ok=True)
        write_json_atomic(folder / "profile.json", {
            "profile_id": profile_id, "name": name, "created_at": ev.now(),
            "format": "open-achievements-profile", "format_version": "1.0",
        })
        config = config or MachineConfig()
        device_id = ev.new_id()
        config.set_device(profile_id, device_id)
        profile = cls(folder, config)
        profile.record("profile.created", {"name": name, "privacy": {"public": False}})
        profile.record("device.registered", profile._device_payload())
        return profile

    @classmethod
    def open(cls, home: Path | None = None, profile_id: str | None = None,
             config: MachineConfig | None = None) -> "Profile":
        home = Path(home or default_home())
        config = config or MachineConfig()
        profile_id = profile_id or config.load().get("active_profile")
        root = home / "profiles"
        if profile_id:
            return cls(root / profile_id, config)
        found = sorted(p for p in root.glob("*") if (p / "profile.json").exists()) if root.exists() else []
        if len(found) == 1:
            return cls(found[0], config)
        raise ProfileError("no profile selected: create one, or pass --profile")

    def _register_this_device(self) -> str:
        """A profile folder copied to a new machine gets a new device id here,
        and says so in its history."""
        device_id = ev.new_id()
        self.config.set_device(self.profile_id, device_id)
        self.device_id = device_id
        self.record("device.registered", self._device_payload())
        return device_id

    def _device_payload(self) -> dict:
        return {"name": _platform.node() or "this computer", "platform": f"{sys.platform}",
                "client_version": __version__}

    # ---- writing events -------------------------------------------------------

    def record(self, event_type: str, payload: dict | None = None, **fields) -> dict:
        event = ev.make_event(event_type, profile_id=self.profile_id, device_id=self.device_id,
                              payload=payload, **fields)
        self.commit([event])
        return event

    def commit(self, new_events: list[dict]) -> int:
        # Checked before appending: if the index already lagged the log (a
        # crash last time between these two lines), it is rebuilt, not patched.
        # All three steps hold the writer lock, so another process cannot
        # append in between and have its events counted as indexed.
        with self.log.locked():
            was_current = self.index.is_current()
            written = self.log.append(new_events, lock_held=True)
            if written or not was_current:
                self.index.add(new_events, was_current=was_current)
        return written

    # ---- games, packs, progress -------------------------------------------------

    def library(self) -> dict:
        return self.index.library()

    def state(self) -> dict:
        return self.index.state()

    def register_game(self, game_id: str, title: str, *, platform: str | None = None,
                      external_ids: dict | None = None) -> dict:
        if not ev.is_slug(game_id) or ":" in game_id:
            raise ProfileError("game id must be a lowercase slug, for example 'hollow-knight'")
        if game_id in self.state()["games"]:
            raise ProfileError(f"game {game_id!r} is already registered")
        return self.record("game.registered", {"title": title, "platform": platform,
                                               "external_ids": external_ids or {}}, game_id=game_id)

    def set_status(self, game_id: str, status: str, note: str | None = None) -> dict:
        """Put a game in the backlog, mark it playing, completed, and so on."""
        if game_id not in self.state()["games"]:
            raise ProfileError(f"no game {game_id!r}")
        if status not in ev.GAME_STATUSES:
            raise ProfileError(f"status must be one of {', '.join(ev.GAME_STATUSES)}")
        return self.record("game.status_changed", {"status": status, "note": note}, game_id=game_id)

    def link_games(self, game_id: str, into: str | None) -> dict:
        """Show `game_id`'s achievements inside `into` (None undoes it)."""
        games = self.state()["games"]
        for g in (game_id, into):
            if g is not None and g not in games:
                raise ProfileError(f"no game {g!r}")
        if into == game_id:
            raise ProfileError("a game cannot be linked to itself")
        step, seen = into, set()
        while step is not None and step not in seen:
            if step == game_id:
                raise ProfileError(f"{into!r} is already shown inside {game_id!r}; unlink it first")
            seen.add(step)
            step = games.get(step, {}).get("linked_to")
        return self.record("game.metadata_updated", {"linked_to": into}, game_id=game_id)

    def install_pack(self, source: Path) -> dict:
        source = Path(source)
        staging = self.folder / "cache" / f"pack-{ev.new_id()}"
        try:
            folder = packs_mod.extract_zip(source, staging) if source.suffix.lower() == ".zip" else source
            description = packs_mod.load_folder(folder)
            state = self.state()
            for game in description.get("games", []):
                if game["id"] not in state["games"]:
                    self.record("game.registered", {"title": game.get("title", game["id"]),
                                                    "platform": game.get("platform"),
                                                    "external_ids": game.get("external_ids") or {}},
                                game_id=game["id"])
            state = self.state()
            missing = [g for g in description["game_ids"] if g not in state["games"]]
            if missing:
                raise ProfileError(f"register these games first, or list them under 'games' in pack.json: {missing}")
            target = self.folder / "packs" / description["pack_id"]
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(folder, target)
            existing = state["packs"].get(description["pack_id"])
            kind = "pack.updated" if existing and not existing.get("removed") else "pack.installed"
            now = ev.now()
            events = [ev.make_event(kind, profile_id=self.profile_id, device_id=self.device_id, payload=payload,
                                    occurred_at=now) for payload in packs_mod.split_for_events(description)]
            self.commit(events)
            return events[0]
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _definition(self, key: str) -> dict:
        pack_id, _, ach_id = key.partition(":")
        pack = self.state()["packs"].get(pack_id)
        if not pack or pack.get("removed") or ach_id not in pack["achievements"]:
            raise ProfileError(f"no installed achievement {key!r}")
        return pack["achievements"][ach_id]

    def _game_for(self, key: str) -> str | None:
        pack = self.state()["packs"].get(key.partition(":")[0]) or {}
        ids = pack.get("game_ids") or []
        return ids[0] if ids else None

    def record_unlock(self, key: str, *, adapter: str, provenance: str = "imported",
                      evidence: str | None = None, note: str | None = None) -> dict | None:
        """An unlock an adapter saw happen. Returns None if it was already
        unlocked. There is no manual unlock (owner decision 2026-10-01): a
        hand-made one is refused here and never counted by the reducer."""
        if adapter == "manual" or provenance == "manual":
            raise ProfileError("achievements unlock from what a game recorded, not by hand")
        definition = self._definition(key)
        state = self.state()
        from .reducer import is_unlocked
        if is_unlocked(state, key) and not definition.get("repeatable"):
            return None
        payload = {"provenance": provenance}
        if evidence:
            payload["evidence"] = evidence
        if note:
            payload["note"] = note
        return self.record("achievement.unlocked", payload, achievement_id=key, game_id=self._game_for(key),
                           adapter=adapter)

    def revoke(self, key: str, reason: str | None = None) -> dict:
        """Revoke the unlocks this device can see, by event id. Naming them
        makes the revocation hold whatever order the events sort in, so a
        device with a slow clock cannot lose it behind the unlock it meant."""
        self._definition(key)
        entry = self.state()["unlocks"].get(key) or {"records": {}, "revoked": set()}
        targets = sorted({i for k, r in entry["records"].items() if k not in entry["revoked"]
                          for i in r["event_ids"]})
        if not targets:
            raise ProfileError(f"{key!r} is not unlocked")
        return self.record("achievement.revoked", {"reason": reason, "revokes": targets},
                           achievement_id=key, game_id=self._game_for(key))

    def record_progress(self, key: str, amount: float = 1, *, adapter: str) -> list[dict]:
        """Progress an adapter saw; unlocks automatically at the target. Never by hand."""
        if adapter == "manual":
            raise ProfileError("progress comes from what a game recorded, not by hand")
        definition = self._definition(key)
        if "progress" not in definition:
            raise ProfileError(f"{key!r} has no progress target")
        written = [self.record("progress.incremented", {"amount": amount}, achievement_id=key,
                               game_id=self._game_for(key), adapter=adapter)]
        return written + self.reconcile_progress()

    def reconcile_progress(self) -> list[dict]:
        """Unlock every progress achievement whose merged total has reached its
        target. Runs after local progress and after sync, because the total can
        cross the line only once another device's increments arrive.

        The unlock carries a deterministic external id, so two devices that
        both notice the same threshold produce one provenance record, not two."""
        from .reducer import is_unlocked
        state = self.state()
        written = []
        for pack_id, pack in state["packs"].items():
            if pack.get("removed"):
                continue
            for ach_id, definition in pack["achievements"].items():
                target = (definition.get("progress") or {}).get("target")
                key = f"{pack_id}:{ach_id}"
                prog = state["progress"].get(key)
                if not target or not prog or prog["value"] < target or is_unlocked(state, key):
                    continue
                # Already crossed once and then revoked: a person decided, so
                # do not keep re-unlocking it (or writing no-op events forever).
                if f"manual:progress-target:{key}" in state["unlocks"].get(key, {}).get("records", {}):
                    continue
                written.append(self.record(
                    "achievement.unlocked", {"provenance": "adapter-verified", "reason": "progress"},
                    achievement_id=key, game_id=(pack.get("game_ids") or [None])[0],
                    external_event_id=f"progress-target:{key}"))
                state = self.state()
        return written
