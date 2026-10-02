"""GreycellAchievements.exe: a quiet app in the system tray.

The Windows app is this module frozen with PyInstaller as a windowed program
(scripts/build-exe.ps1), so there is no console window. With no arguments it
serves the library on this computer only, watches games, saves and Steam in
the background, opens the browser, and sits in the tray (tray.py) until Quit.
The first run creates a profile named after the Windows user.

    GreycellAchievements.exe                the app, and the library in the browser
    GreycellAchievements.exe --background   into the tray only (Start with Windows)
    GreycellAchievements.exe <command>      any `openachievements` command; output
                                            goes to the terminal it was started from
    GreycellAchievements.exe --toast        the unlock popup (started by the app)

A second launch while the app runs only opens the browser. A windowed program
has nowhere to print, so the app's own output goes to app.log in the machine
settings folder. Updates: a frozen app cannot pip-install over itself; it
downloads the new installer from greycell.app and runs it (self_update.py).
"""
from __future__ import annotations

import getpass
import os
import socket
import sys
import threading
import time
import webbrowser

# GREYCELL_ACHIEVEMENTS_PORT and GREYCELL_ACHIEVEMENTS_NO_BROWSER exist for
# testing a build next to a running copy; players never need them.
PORT = int(os.environ.get("GREYCELL_ACHIEVEMENTS_PORT") or 8788)
URL = f"http://127.0.0.1:{PORT}"
LOG_LIMIT = 2 * 1024 * 1024


def _running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def _first_profile() -> bool:
    """Create a profile named after the Windows user if there is none yet.
    True when one was created."""
    from .profile import default_home
    profiles = default_home() / "profiles"
    if profiles.exists() and any(profiles.iterdir()):
        return False
    from . import cli
    cli.main(["profile", "create", getpass.getuser() or "Player"])
    return True


def _autostart_by_default(profile) -> None:
    """Start with Windows is on unless the player turned it off: switched on
    once, the first time the app runs on this machine, and kept pointing at
    this exe if it was moved. Only in the built app, never a source checkout."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    if os.environ.get("GREYCELL_ACHIEVEMENTS_PORT"):
        return                                           # a test copy next to the real one leaves its Run value alone
    from . import tray
    try:
        offered = (profile.config.load().get("autostart") or {}).get("offered")
        if not offered:
            tray.set_autostart(True)
            profile.config.edit(lambda c: c.setdefault("autostart", {}).__setitem__("offered", True))
        elif tray.autostart_enabled() and tray.autostart_value() != tray.autostart_command():
            tray.set_autostart(True)                     # the exe moved: follow it
    except OSError as exc:
        print(f"could not set Start with Windows: {exc}")


def _edge() -> str | None:
    """Microsoft Edge, which every Windows 10 and 11 has: its app mode gives the
    library a window of its own, with no tabs or address bar."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r"Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                    path = winreg.QueryValue(key, None)
                    if path and os.path.exists(path):
                        return path
            except OSError:
                continue
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        path = os.path.join(base or "", "Microsoft", "Edge", "Application", "msedge.exe")
        if base and os.path.exists(path):
            return path
    return None


def _open_browser() -> None:
    """Open the library as its own app window (Edge app mode), else in the
    default browser."""
    if os.environ.get("GREYCELL_ACHIEVEMENTS_NO_BROWSER"):
        return
    edge = _edge()
    if edge:
        import subprocess
        try:
            subprocess.Popen([edge, f"--app={URL}", "--window-size=1280,860"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        except OSError:
            pass
    webbrowser.open(URL)


def _open_browser_soon() -> None:
    def go() -> None:
        for _ in range(40):
            if _running():
                _open_browser()
                return
            time.sleep(0.25)
    threading.Thread(target=go, daemon=True).start()


def _log_to_file() -> None:
    """A windowed program has no console: keep its output in app.log."""
    from .profile import default_config_dir
    folder = default_config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "app.log"
    if path.exists() and path.stat().st_size > LOG_LIMIT:
        path.replace(folder / "app.log.1")
    log = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = log


def _attach_console() -> None:
    """Commands typed in a terminal print there, though the exe is windowed."""
    if os.name != "nt" or sys.stdout is not None:
        return
    import ctypes
    if ctypes.windll.kernel32.AttachConsole(-1):              # ATTACH_PARENT_PROCESS
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = sys.stdout


def serve_in_tray(background: bool) -> int:
    import uvicorn
    from .local_app import create_local_app
    from .profile import Profile
    from .watching import start_in_background

    created = _first_profile()
    profile = Profile.open()
    _autostart_by_default(profile)
    stop_watching = start_in_background(profile)
    server = uvicorn.Server(uvicorn.Config(create_local_app(profile), host="127.0.0.1", port=PORT,
                                           log_level="warning", log_config=None))
    thread = threading.Thread(target=server.run, daemon=True, name="greycell-achievements-server")
    thread.start()
    print(f"Greycell Achievements on {URL}")
    if not background:
        _open_browser_soon()

    def quit_app() -> None:
        stop_watching.set()
        server.should_exit = True

    if sys.platform != "win32":
        thread.join()
        return 0
    from . import self_update
    from .tray import Tray
    notice = ("Greycell Achievements is running",
              "It lives here in the tray and watches your games. Right-click for the menu.") if created else None
    tray = Tray(on_open=_open_browser, on_quit=quit_app)
    self_update.QUIT = tray.close
    self_update.start_prompting(profile.config, tray.close, stop_watching)
    try:
        tray.run(first_notice=notice)
    except OSError as exc:                       # no tray: keep serving rather than vanish
        print(f"tray unavailable ({exc}); the library keeps running at {URL}")
        thread.join()
        return 0
    thread.join(timeout=5)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--toast"]:
        from . import toast
        toast.main()
        return 0
    background = argv[:1] == ["--background"]
    if argv and not background:
        _attach_console()
        from . import cli
        return cli.main(argv)
    if _running():
        if not background:
            _open_browser()
        return 0
    if getattr(sys, "frozen", False) or sys.stdout is None:
        _log_to_file()
    return serve_in_tray(background)


if __name__ == "__main__":
    sys.exit(main())
