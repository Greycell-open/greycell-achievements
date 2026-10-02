"""The SQLite index: a derived cache, never a source of truth.

`index.sqlite` in the profile folder holds a copy of every event plus the
reduced library, so the CLI and UI do not re-read every JSONL file on each
call and imports can check "have I seen this external unlock" quickly.

Deleting it loses nothing. `rebuild()` recreates it from the event files,
keeping the old one as `index.sqlite.bak` first.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from . import reducer
from .eventlog import EventLog

INDEX_VERSION = 3          # 2: hidden achievements' `secret`; 3: hand-made unlocks and progress never count

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  game_id TEXT,
  achievement_id TEXT,
  occurred_at TEXT NOT NULL,
  adapter TEXT,
  external_event_id TEXT,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_external ON events(adapter, external_event_id);
CREATE INDEX IF NOT EXISTS events_game ON events(game_id);
"""


class Index:
    def __init__(self, profile_dir: Path, log: EventLog):
        self.path = profile_dir / "index.sqlite"
        self.log = log
        self._state = None

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.executescript(_SCHEMA)
        return db

    def _log_signature(self) -> str:
        """Every event file and its size. Appends only ever grow a file or add
        one, so if this differs from what was indexed, events were written that
        the index never saw (a crash between append and index, or another
        program writing to the folder)."""
        return json.dumps([[p.relative_to(self.log.root).as_posix(), p.stat().st_size] for p in self.log.files()])

    def _is_current(self, db: sqlite3.Connection) -> bool:
        row = db.execute("SELECT value FROM meta WHERE key = 'index_version'").fetchone()
        if not row or int(row[0]) != INDEX_VERSION:
            return False
        sig = db.execute("SELECT value FROM meta WHERE key = 'log_signature'").fetchone()
        return bool(sig) and sig[0] == self._log_signature()

    def _mark_covered(self, db: sqlite3.Connection) -> None:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('log_signature', ?)", (self._log_signature(),))

    def _current(self) -> bool:
        if not self.path.exists():
            return False
        db = self._connect()
        try:
            return self._is_current(db)
        finally:
            db.close()

    def is_current(self) -> bool:
        return self._current()

    def add(self, new_events: list[dict], *, was_current: bool = True) -> None:
        """Index events that were just appended, and refresh the library.
        `was_current` is whether the index covered the whole log before the
        append. The caller must hold `log.locked()` across that check, the
        append and this call, or another writer's events could be marked as
        covered without being indexed."""
        if not was_current or not self.path.exists():
            self.rebuild(lock_held=True)
            return
        db = self._connect()
        try:
            with db:
                self._insert(db, new_events)
                # The library view is built when it is next read, not on every
                # write: an import writes hundreds of times and reads once.
                db.execute("DELETE FROM meta WHERE key = 'library'")
                self._mark_covered(db)
        finally:
            db.close()
        self._state = None

    def _insert(self, db: sqlite3.Connection, new_events: list[dict]) -> None:
        db.executemany(
            "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(e["event_id"], e["event_type"], e.get("game_id"), e.get("achievement_id"), e["occurred_at"],
              e["source"].get("adapter"), e["source"].get("external_event_id"),
              json.dumps(e, sort_keys=True, ensure_ascii=False)) for e in new_events],
        )

    @staticmethod
    def _covered(db: sqlite3.Connection) -> str | None:
        row = db.execute("SELECT value FROM meta WHERE key = 'log_signature'").fetchone()
        return row[0] if row else None

    def rebuild(self, *, lock_held: bool = False) -> list[str]:
        """Recreate from the event files. Returns every problem found, which the
        caller reports; the source events are never modified.

        Runs under the log's writer lock, so no append lands between reading
        the files and recording what was covered, and no half-written line is
        mistaken for a corrupt one."""
        if not lock_held:
            with self.log.locked():
                return self._rebuild()
        return self._rebuild()

    def _rebuild(self) -> list[str]:
        """Built in a separate file and swapped in whole: another process
        reading the index at that moment sees the old one or the new one,
        never an empty one half filled."""
        self._state = None
        report = self.log.read()
        fresh = self.path.with_name("index.sqlite.new")
        fresh.unlink(missing_ok=True)
        db = sqlite3.connect(fresh)
        try:
            db.executescript(_SCHEMA)
            with db:
                self._insert(db, report.events)
                db.execute("INSERT OR REPLACE INTO meta VALUES ('index_version', ?)", (str(INDEX_VERSION),))
                self._mark_covered(db)
        finally:
            db.close()
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_name("index.sqlite.bak"))
        _replace_retrying(fresh, self.path)
        return report.problems

    def _ensure_current(self) -> None:
        """Rebuild only if the index is still behind once the writer lock is
        ours. A reader that caught a writer between its append and its index
        update waits for it, then finds nothing left to do."""
        if self._current():
            return
        with self.log.locked():
            if not self._current():
                self._rebuild()

    def library(self) -> dict:
        view = reducer.library(reducer.empty_state())
        for _attempt in range(5):
            self._ensure_current()
            db = self._connect()
            try:
                row = db.execute("SELECT value FROM meta WHERE key = 'library'").fetchone()
                if row is not None:
                    return json.loads(row[0])
                # Built outside any lock (it takes a while), then stored only
                # if no write landed in between and the index covers the log.
                covered = self._covered(db)
                events = [json.loads(r[0]) for r in db.execute("SELECT body FROM events")]
                view = reducer.library(reducer.reduce(events))
                db.execute("BEGIN IMMEDIATE")
                if self._covered(db) != covered or not self._is_current(db):
                    db.rollback()
                    continue
                db.execute("INSERT OR REPLACE INTO meta VALUES ('library', ?)", (json.dumps(view, ensure_ascii=False),))
                db.commit()
                return view
            finally:
                db.close()
        return view          # still changing after every try: the latest view, just not cached

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        self._ensure_current()
        db = self._connect()
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def events(self) -> list[dict]:
        return [json.loads(r[0]) for r in self._query("SELECT body FROM events ORDER BY occurred_at, event_id")]

    def state(self) -> dict:
        """The reduced state, cached until the log changes. It is shared: read
        it, never change it (the only way to change state is to record an event).
        Copying it per call cost most of a large import's time."""
        sig = self.log.signature()
        if getattr(self, "_state", None) is None or self._state[0] != sig or not self._current():
            self._state = (sig, reducer.reduce(self.events()))
        return self._state[1]

    def has_external(self, adapter: str, external_event_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM events WHERE adapter = ? AND external_event_id = ? LIMIT 1",
                                (adapter, external_event_id)))

    def external_ids(self, adapter: str) -> set[str]:
        return {r[0] for r in self._query(
            "SELECT external_event_id FROM events WHERE adapter = ? AND external_event_id IS NOT NULL", (adapter,))}

    def event_ids(self) -> set[str]:
        return {r[0] for r in self._query("SELECT event_id FROM events")}


def _replace_retrying(source: Path, target: Path) -> None:
    """Windows refuses to replace a file another process has open for a moment
    (a reader's short connection); try again briefly."""
    for _attempt in range(250):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            time.sleep(0.02)
    os.replace(source, target)
