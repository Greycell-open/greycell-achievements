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


def _autostart_module():
    """Start with Windows (tray.py, the registry) or Start at login on Linux
    (linux_desktop.py, an XDG autostart entry); None elsewhere."""
    if sys.platform == "win32":
        from . import tray
        return tray
    if sys.platform.startswith("linux"):
        from . import linux_desktop
        return linux_desktop
    return None


def _autostart_by_default(profile) -> None:
    """Start with Windows (Start at login on Linux) is on unless the player
    turned it off: switched on once, the first time the app runs on this
    machine, and kept pointing at this exe if it was moved. Only in the built
    app, never a source checkout. On Linux the app menu entry is kept the same way."""
    tray = _autostart_module()
    if tray is None or not getattr(sys, "frozen", False):
        return
    if os.environ.get("GREYCELL_ACHIEVEMENTS_PORT"):
        return                                           # a test copy next to the real one leaves its Run value alone
    if tray.__name__.endswith("linux_desktop"):
        tray.install_menu_entry()
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
    if sys.platform.startswith("linux"):
        from . import linux_desktop
        linux_desktop.open_url(URL)
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


class _Stamped:
    """app.log with the time at the start of every line, and the process id,
    since the tray app and its popups write to the same file."""

    def __init__(self, stream):
        self.stream, self._fresh = stream, True

    def write(self, text: str) -> int:
        out = []
        for piece in text.splitlines(keepends=True):
            if self._fresh:
                out.append(time.strftime("%Y-%m-%d %H:%M:%S ") + f"[{os.getpid()}] ")
            out.append(piece)
            self._fresh = piece.endswith("\n")
        self.stream.write("".join(out))
        return len(text)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _log_to_file(rotate: bool = True) -> None:
    """A windowed program has no console: keep its output in app.log."""
    from .profile import default_config_dir
    folder = default_config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "app.log"
    # Only the tray app rotates: a popup process runs while the tray holds the
    # file open, and Windows refuses to rename an open file.
    if rotate and path.exists() and path.stat().st_size > LOG_LIMIT:
        path.replace(folder / "app.log.1")
    log = _Stamped(open(path, "a", encoding="utf-8", errors="replace", buffering=1))
    sys.stdout = sys.stderr = log


def _attach_console() -> None:
    """Commands typed in a terminal print there, though the exe is windowed."""
    if os.name != "nt" or sys.stdout is not None:
        return
    import ctypes
    if ctypes.windll.kernel32.AttachConsole(-1):              # ATTACH_PARENT_PROCESS
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = sys.stdout


def _controls_in_the_page(quit_app) -> None:
    """No tray on Linux: the page's Settings menu has Start at login and Quit,
    and Install update restarts the app by itself."""
    from . import local_app, self_update
    self_update.QUIT = quit_app
    local_app.APP_CONTROLS.update(quit=quit_app, autostart=_autostart_module() if getattr(sys, "frozen", False)
                                  else None)


def serve_in_tray(background: bool) -> int:
    import uvicorn
    from .local_app import create_local_app
    from .profile import Profile
    from .watching import start_in_background

    created = _first_profile()
    profile = Profile.open()
    _autostart_by_default(profile)
    if getattr(sys, "frozen", False) and not os.environ.get("GREYCELL_ACHIEVEMENTS_PORT"):
        from . import community                          # a new install, counted once and anonymously
        try:
            community.first_run(profile.config)
        except Exception as exc:  # noqa: BLE001 - a count is never a reason not to start
            print(f"community stats: {exc}")
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
        _controls_in_the_page(quit_app)
        if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
            try:                                         # a tray icon where the desktop has a tray
                from . import linux_desktop, tray_linux
                tray_linux.Tray(_open_browser, quit_app, _autostart_module(), linux_desktop.ICON).start()
            except Exception as exc:  # noqa: BLE001 - the page's controls stay either way
                print(f"tray unavailable: {exc}")
        thread.join()
        return 0
    from . import self_update
    from .tray import Tray
    notice = ("Greycell Achievements is running",
              "It lives here in the tray and watches your games. Right-click for the menu.") if created else None
    def check_updates() -> None:                 # the tray's Check for updates, off the tray's thread
        def run() -> None:
            try:
                print(f"update check from the tray: {self_update.check_now(profile.config, tray.close, tray.notice)}")
            except Exception as exc:  # noqa: BLE001 - say so, keep running
                print(f"update check from the tray failed: {exc}")
                tray.notice("Could not check for updates", "Try again later.")
        threading.Thread(target=run, daemon=True, name="greycell-update-check").start()

    tray = Tray(on_open=_open_browser, on_quit=quit_app, on_check=check_updates)
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
        if getattr(sys, "frozen", False) or sys.stdout is None:
            try:
                _log_to_file(rotate=False)       # a popup that fails says why in app.log, not nowhere
            except Exception:                    # noqa: BLE001 - no log is no reason to skip the popup
                pass
        try:
            toast.main()
        except Exception:                        # noqa: BLE001 - recorded, then the popup process ends
            import traceback
            print("popup: failed\n" + traceback.format_exc())
            return 1
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
