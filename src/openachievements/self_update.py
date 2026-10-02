"""Updating the Windows app in place, the way VLC does.

Once a day the app reads greycell.app's manifest (update.check). When a newer
version exists and no game is running, a small Windows dialog says so with the
release notes and asks to download and install it now; "No" asks again the
next day. The dashboard offers the same through an "Install update" button.

Installing: the installer named in the manifest is downloaded to the user's
temp folder, its size and SHA-256 must match the manifest or it is deleted and
never run, then it runs with only its progress window (/SILENT), the app quits
so its files can be replaced, and the installer starts the new version in the
tray. Achievements are in the profile folder, which the installer never
touches.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from datetime import date
from pathlib import Path
from typing import Callable

from . import __version__, update

STATE: dict = {"phase": None, "error": None, "version": None}   # what the page shows while an update runs
QUIT: Callable[[], None] | None = None   # set by the Windows app: closes it from any thread
FIRST_LOOK = 60.0                   # seconds after start before the first check
LOOK_EVERY = 1800.0                 # then every half hour (update.check itself asks the network daily)
CHUNK = 256 * 1024
_lock = threading.Lock()


class UpdateError(Exception):
    pass


def _open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": f"GreycellAchievements/{__version__}"})
    return urllib.request.urlopen(req, timeout=60)


def download(release: dict, folder: Path | None = None, opener: Callable = _open) -> Path:
    """The installer, checked against the manifest's size and SHA-256. A file
    that does not match is deleted, never run."""
    if not update.safe_url(release.get("setup", "")):
        raise UpdateError("The update's address is not a secure one.")
    folder = Path(folder or Path(tempfile.gettempdir()) / "GreycellAchievements-update")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"GreycellAchievementsSetup-{release['version']}.exe"
    for old in folder.glob("GreycellAchievementsSetup-*"):  # earlier updates' installers: not kept forever
        if old != target:
            try:
                old.unlink()
            except OSError:
                pass
    partial = target.with_suffix(".part")
    digest, size = hashlib.sha256(), 0
    try:
        with opener(release["setup"]) as resp, open(partial, "wb") as out:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                size += len(block)
                if size > release["size"]:
                    raise UpdateError("The download is larger than announced; it was deleted.")
                digest.update(block)
                out.write(block)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"The download failed ({exc.__class__.__name__}). Try again later.") from None
    except UpdateError:
        partial.unlink(missing_ok=True)
        raise
    if size != release["size"] or digest.hexdigest() != release["sha256"]:
        partial.unlink(missing_ok=True)
        raise UpdateError("The download did not match the published checksum; it was deleted and not run.")
    partial.replace(target)
    return target


def clean_environment(env: dict | None = None) -> dict:
    """This app's environment without the one-file bootloader's own variables.
    The installer passes its environment to the new version it starts; with
    them, that new exe would load Python from this app's unpacked folder, which
    is deleted the moment this app quits ("Failed to load Python DLL")."""
    env = dict(os.environ if env is None else env)
    for key in list(env):
        if key.upper().startswith("_PYI_") or key.upper() == "_MEIPASS2":
            del env[key]
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def launch(setup: Path, popen: Callable = subprocess.Popen) -> None:
    """Run the installer with its progress window only. It closes this app's
    other processes, replaces the files and starts the new version."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    popen([str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], creationflags=flags, close_fds=True,
          env=clean_environment())


def install(release: dict, quit_app: Callable[[], None], opener: Callable = _open,
            popen: Callable = subprocess.Popen, folder: Path | None = None) -> bool:
    """Download, check, run the installer, quit. One at a time. False when it did not start."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        STATE.update(phase="downloading", error=None, version=release["version"])
        setup = download(release, folder, opener)
        STATE.update(phase="installing")
        try:
            launch(setup, popen)
        except OSError as exc:                       # quarantined by antivirus, blocked, deleted
            raise UpdateError(f"The installer could not start ({exc.__class__.__name__}). The app keeps "
                              "running; try again, or download it from greycell.app/achievements.") from None
    except UpdateError as exc:
        STATE.update(phase="error", error=str(exc))
        return False
    except Exception as exc:  # noqa: BLE001 - anything else: say so, keep the app, allow a retry
        STATE.update(phase="error", error=f"The update failed ({exc.__class__.__name__}). Try again later.")
        return False
    finally:
        _lock.release()
    quit_app()
    return True


def install_in_background(release: dict, quit_app: Callable[[], None]) -> bool:
    if STATE["phase"] in ("downloading", "installing"):
        return False
    STATE.update(phase="downloading", error=None, version=release["version"])
    threading.Thread(target=install, args=(release, quit_app), daemon=True, name="greycell-update").start()
    return True


# ---- the popup ------------------------------------------------------------------------

def _ask(title: str, text: str) -> bool:
    """A plain Windows Yes/No dialog. Only shown when no game is running, so it
    never takes focus from one."""
    import ctypes
    MB_YESNO, MB_ICONINFORMATION, MB_SETFOREGROUND, MB_TOPMOST, IDYES = 0x4, 0x40, 0x10000, 0x40000, 6
    user32 = ctypes.WinDLL("user32")
    user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    user32.MessageBoxW.restype = ctypes.c_int
    return user32.MessageBoxW(None, text, title,
                              MB_YESNO | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST) == IDYES


def _tell(title: str, text: str) -> None:
    import ctypes
    user32 = ctypes.WinDLL("user32")
    user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    user32.MessageBoxW(None, text, title, 0x30 | 0x40000)            # warning icon, on top


def prompt_text(release: dict) -> str:
    notes = (release.get("notes") or "").strip()
    if len(notes) > 700:
        notes = notes[:700].rsplit("\n", 1)[0] + "\n..."
    return (f"Greycell Achievements {release['version']} is available. You have {__version__}.\n\n"
            + (f"What's new:\n{notes}\n\n" if notes else "")
            + "Download and install it now? It takes a minute, the app restarts by itself, "
              "and your achievements stay as they are.")


def due(config_store, release: dict, today: date | None = None) -> bool:
    """Ask once per day per version."""
    asked = (config_store.load().get("update") or {}).get("asked") or {}
    return asked != {"version": release["version"], "on": (today or date.today()).isoformat()}


def mark_asked(config_store, release: dict, today: date | None = None) -> None:
    config_store.edit(lambda c: c.setdefault("update", {}).__setitem__(
        "asked", {"version": release["version"], "on": (today or date.today()).isoformat()}))


def look_once(config_store, quit_app: Callable[[], None], playing: Callable[[], bool],
              ask: Callable[[str, str], bool] = _ask, tell: Callable[[str, str], None] = _tell,
              start: Callable = install) -> str:
    """One look: what happened, for tests and the log."""
    status = update.check(config_store)
    release = status.get("available")
    if not (status.get("installable") and release):
        return "none"
    if STATE["phase"] in ("downloading", "installing") or not due(config_store, release):
        return "already"
    if playing():
        return "playing"                                            # ask after the game, not during it
    mark_asked(config_store, release)
    if not ask("Greycell Achievements update", prompt_text(release)):
        return "declined"
    if not start(release, quit_app):
        tell("Greycell Achievements update", STATE.get("error") or "The update could not start.")
        return "failed"
    return "installing"


def start_prompting(config_store, quit_app: Callable[[], None], stop: threading.Event) -> None:
    """The daily look, on its own thread, in the Windows app only."""
    if sys.platform != "win32" or not update.frozen() or os.environ.get("GREYCELL_ACHIEVEMENTS_NO_UPDATE_PROMPT"):
        return

    def playing() -> bool:
        from . import watching
        return bool(watching.LIVE.get("playing"))

    def loop() -> None:
        if stop.wait(FIRST_LOOK):
            return
        while not stop.is_set():
            try:
                print(f"update look: {look_once(config_store, quit_app, playing)}")
            except Exception as exc:  # noqa: BLE001 - an update look never takes the app down
                print(f"update look failed: {exc}")
            if stop.wait(LOOK_EVERY):
                return

    threading.Thread(target=loop, daemon=True, name="greycell-update-look").start()
