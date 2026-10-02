"""Filesystem primitives the profile folder depends on.

Atomic writes for mutable JSON, an exclusive lock for appends, and path checks
for anything that came from outside (packs, archives). Standard library only,
and the same code on Windows, macOS and Linux.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Bytes to a temp file beside the target, fsync, then rename over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_json_atomic(path: Path, data: Any) -> None:
    """Write to a temp file in the same directory, fsync, then rename over the
    target, so a crash leaves either the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"   # whole, before the file opens
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


class LockTimeout(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    """Whether a process with this id is running on this machine."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259                        # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_owner(path: Path) -> dict | None:
    for attempt in range(50):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(0.02)          # Windows: someone has it open this instant
        except (ValueError, OSError):
            break
    return {}          # half-written or foreign: treat as owned, judge by age only


def _is_stale(path: Path, owner: dict, stale_after: float) -> bool:
    """A lock is broken only when its owner is known to be gone: a process on
    this machine that no longer runs. A lock from another machine (a synced
    folder) cannot be checked, so it is broken only after `stale_after`
    seconds; a live local owner is never broken, however long it takes."""
    if owner.get("host") == socket.gethostname() and isinstance(owner.get("pid"), int):
        return not _pid_alive(owner["pid"])
    try:
        return time.time() - path.stat().st_mtime > stale_after
    except FileNotFoundError:
        return False


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(Path(path).resolve()))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _unlink_retrying(path: Path) -> None:
    """Windows refuses to delete a file another thread or process has open for
    a moment (sharing violation); try again briefly instead of failing a write
    that has already succeeded."""
    for attempt in range(100):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.02)
    path.unlink(missing_ok=True)


@contextmanager
def file_lock(path: Path, timeout: float = 30.0, stale_after: float = 600.0) -> Iterator[None]:
    """Threads of one process take turns on a lock first (the watcher, the
    page and background fetches all write), then the file lock below keeps
    separate processes apart."""
    with _thread_lock(path):
        with _process_lock(path, timeout, stale_after):
            yield


@contextmanager
def _process_lock(path: Path, timeout: float, stale_after: float) -> Iterator[None]:
    """Exclusive lock by creating `path` with O_EXCL. Works on every OS and on
    network and synced folders where fcntl/msvcrt locks are unreliable.

    The file records who holds it (host, pid, a random token). Only that holder
    removes it, so a lock broken as stale can never be deleted later by the
    process it was taken from."""
    path.parent.mkdir(parents=True, exist_ok=True)
    me = {"host": socket.gethostname(), "pid": os.getpid(), "token": uuid.uuid4().hex}
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(me, fh)
            break
        except PermissionError:
            # Windows: the holder is deleting the file this instant ("delete
            # pending" answers access denied, not exists). Wait like any turn.
            if time.monotonic() > deadline:
                raise LockTimeout(f"could not lock {path}") from None
            time.sleep(0.01)
            continue
        except FileExistsError:
            owner = _read_owner(path)
            if owner is None:
                continue
            if _is_stale(path, owner, stale_after):
                # Break it only if it is still the same stale lock we judged.
                if _read_owner(path) == owner:
                    _unlink_retrying(path)
                continue
            if time.monotonic() > deadline:
                raise LockTimeout(f"could not lock {path} (held by {owner.get('host')} pid {owner.get('pid')})") from None
            time.sleep(0.02)
    try:
        yield
    finally:
        if _read_owner(path) == me:
            _unlink_retrying(path)


def safe_child(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, refusing absolute paths, traversal and
    drive letters. For any path that a pack or archive supplies."""
    if not relative or relative.startswith(("/", "\\")) or ":" in relative:
        raise ValueError(f"unsafe path {relative!r}")
    parts = relative.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"unsafe path {relative!r}")
    target = (root / Path(*parts)).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise ValueError(f"path escapes its folder: {relative!r}")
    return target
