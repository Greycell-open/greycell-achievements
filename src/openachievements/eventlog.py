"""The append-only event log inside a profile folder.

Events live in `events/YYYY/MM/events-YYYY-MM-DD.jsonl`, one complete JSON
object per line, named by the date they were recorded on this device. Lines are
never edited. Reading validates every line; anything that fails is copied to
`events/quarantine/` with the reason, and reported, never silently dropped.

Events that arrive from a sync server are appended exactly like local ones, so
the folder alone is always enough to rebuild everything.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from . import events as ev
from .fsutil import file_lock


@dataclass
class ReadReport:
    events: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    duplicates: int = 0


class EventLog:
    def __init__(self, profile_dir: Path, profile_id: str):
        self.root = profile_dir / "events"
        self.profile_id = profile_id
        self._lock = self.root / ".append.lock"
        # Ids already in the log, valid while the files are exactly as this
        # process last saw them. Another process's append changes a size and
        # forces a re-read; this process's own appends keep it current.
        self._known: set[str] | None = None
        self._known_sig: list | None = None

    def _file_for(self, recorded_at: str) -> Path:
        year, month, day = recorded_at[:4], recorded_at[5:7], recorded_at[:10]
        return self.root / year / month / f"events-{day}.jsonl"

    @contextmanager
    def locked(self) -> Iterator[None]:
        """The folder's single writer lock. Held for every append, and by the
        index while it decides what the log contains, so the two never disagree
        about which events were covered."""
        with file_lock(self._lock):
            yield

    def append(self, new_events: Iterable[dict], *, lock_held: bool = False) -> int:
        """Validate, then append. Returns how many were written. Events already
        in the log (same event_id) are skipped, which makes re-delivery safe.
        Pass `lock_held` only from inside `locked()`."""
        batch = [ev.validate(e, profile_id=self.profile_id) for e in new_events]
        if not batch:
            return 0
        if lock_held:
            return self._write(batch)
        with self.locked():
            return self._write(batch)

    def signature(self) -> list:
        return [[str(p), p.stat().st_size] for p in self.files()]

    def _known_now(self) -> set[str]:
        sig = self.signature()
        if self._known is None or sig != self._known_sig:
            self._known, self._known_sig = self.known_ids(), sig
        return self._known

    def _write(self, batch: list[dict]) -> int:
        known = self._known_now()
        written = 0
        for event in batch:
            if event["event_id"] in known:
                continue
            path = self._file_for(event["recorded_at"])
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            known.add(event["event_id"])
            written += 1
        self._known_sig = self.signature()
        return written

    def files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.rglob("events-*.jsonl") if "quarantine" not in p.parts)

    def _lines(self) -> Iterator[tuple[Path, int, str]]:
        for path in self.files():
            with open(path, encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    if line.strip():
                        yield path, number, line

    def known_ids(self) -> set[str]:
        ids = set()
        for _path, _n, line in self._lines():
            try:
                ids.add(json.loads(line)["event_id"])
            except (ValueError, KeyError, TypeError):
                continue
        return ids

    def read(self, *, quarantine: bool = True) -> ReadReport:
        """Every valid event, in deterministic order. A truncated last line
        (a crash mid-write) is reported and quarantined like any other."""
        report = ReadReport()
        seen: set[str] = set()
        for path, number, line in self._lines():
            where = f"{path.relative_to(self.root)}:{number}"
            try:
                event = ev.validate(json.loads(line), profile_id=self.profile_id)
            except (ValueError, ev.EventError) as exc:
                reason = getattr(exc, "code", "invalid_json")
                report.problems.append(f"{where}: {reason}: {exc}")
                if quarantine:
                    self._quarantine(where, reason, line)
                continue
            if event["event_id"] in seen:
                report.duplicates += 1
                continue
            seen.add(event["event_id"])
            report.events.append(event)
        report.events.sort(key=ev.sort_key)
        return report

    def _quarantine(self, where: str, reason: str, line: str) -> None:
        qdir = self.root / "quarantine"
        qdir.mkdir(parents=True, exist_ok=True)
        record = {"found_at": where, "reason": reason, "line": line.rstrip("\n")}
        target = qdir / "quarantined.jsonl"
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        encoded = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if encoded not in existing:
            with open(target, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(encoded + "\n")
