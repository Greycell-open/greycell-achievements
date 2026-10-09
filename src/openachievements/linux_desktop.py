"""The Linux app's desktop side, the freedesktop way.

What the Windows app gets from the registry, Edge and the tray: Start at login
is an XDG autostart entry, the app menu entry is a .desktop file with the
icon (so the AppImage shows up in the menu and can be added to Steam on the
Steam Deck), the library opens as an app window in a Chromium-family browser
when there is one, else in the default browser, and sounds play through
PipeWire, PulseAudio or ALSA, whichever is installed. Standard library only.

The built app is an AppImage: $APPIMAGE is the file the player keeps, while
sys.executable lives in a folder that only exists while the app runs, so
everything written to disk points at $APPIMAGE.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "greycell-achievements"
NAME = "Greycell Achievements"
ICON = Path(__file__).resolve().parent / "web" / "logo-512.png"
BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser",
            "microsoft-edge", "microsoft-edge-stable", "vivaldi")
PLAYERS = (("pw-play",), ("paplay",), ("aplay", "-q"))


def system_env(env: dict | None = None) -> dict:
    """The environment for a program of the system (a browser, a sound
    player): without the one-file bootloader's variables and with the library
    path it had before this app started, or the system's programs would load
    this app's bundled libraries."""
    env = dict(os.environ if env is None else env)
    for key in list(env):
        if key.startswith("_PYI_") or key == "_MEIPASS2":
            del env[key]
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    return env


def _spawn(command: list[str]) -> bool:
    try:
        subprocess.Popen(command, env=system_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return True
    except OSError:
        return False


def app_path() -> str | None:
    """The program to start again later: the AppImage, the Debian package's
    command, else the built app. None when running from source, where nothing
    is written for the desktop."""
    appimage = os.environ.get("APPIMAGE")
    if appimage and os.path.isfile(appimage):
        return appimage
    from .update import deb_install
    if deb_install() and os.path.isfile("/usr/bin/greycell-achievements"):
        return "/usr/bin/greycell-achievements"
    return sys.executable if getattr(sys, "frozen", False) else None


def _quote(arg: str) -> str:
    """One argument of a desktop entry's Exec line (freedesktop quoting)."""
    if not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _entry(exec_line: str, extra: str = "") -> str:
    return ("[Desktop Entry]\nType=Application\n"
            f"Name={NAME}\nComment=Every game deserves achievements\n"
            f"Exec={exec_line}\nIcon={APP_ID}\nTerminal=false\nCategories=Game;Utility;\n"
            f"StartupWMClass={APP_ID}\n{extra}")


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ---- Start at login ---------------------------------------------------------------------

def autostart_file() -> Path:
    return _config_home() / "autostart" / f"{APP_ID}.desktop"


def autostart_command() -> str:
    path = app_path()
    return f"{_quote(path)} --background" if path else ""


def autostart_value(path: Path | None = None) -> str | None:
    """The Exec line of the autostart entry, or None when there is none."""
    try:
        text = Path(path or autostart_file()).read_text(encoding="utf-8")
    except OSError:
        return None
    if "X-GNOME-Autostart-enabled=false" in text or "Hidden=true" in text:
        return None
    for line in text.splitlines():
        if line.startswith("Exec="):
            return line[5:]
    return None


def autostart_enabled(path: Path | None = None) -> bool:
    return autostart_value(path) is not None


def set_autostart(on: bool, path: Path | None = None) -> None:
    target = Path(path or autostart_file())
    if not on:
        target.unlink(missing_ok=True)
        return
    command = autostart_command()
    if not command:
        raise OSError("Start at login needs the installed app, not a source checkout")
    _write(target, _entry(command, "X-GNOME-Autostart-enabled=true\n"))


# ---- the app menu -------------------------------------------------------------------------

def menu_entry_file() -> Path:
    return _data_home() / "applications" / f"{APP_ID}.desktop"


def icon_file() -> Path:
    return _data_home() / "icons" / "hicolor" / "512x512" / "apps" / f"{APP_ID}.png"


def install_menu_entry() -> bool:
    """Put the app in the desktop's app menu, pointing at this AppImage, and
    keep it pointing there if the file was moved. True when written. The
    Debian package brings its own entry for every user: none is added."""
    from .update import deb_install
    path = app_path()
    if not path or deb_install():
        return False
    text = _entry(_quote(path))
    target = menu_entry_file()
    try:
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            _write(target, text)
            if ICON.exists():
                icon_file().parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ICON, icon_file())
            return True
    except OSError:
        pass
    return False


# ---- the library window, sounds ----------------------------------------------------------

def browser_app() -> str | None:
    return next((p for p in (shutil.which(name) for name in BROWSERS) if p), None)


def open_url(url: str, window: bool = True) -> None:
    """The library as its own app window when a Chromium-family browser is
    installed, else in the default browser."""
    browser = browser_app() if window else None
    if browser and _spawn([browser, f"--app={url}", "--window-size=1280,860", f"--class={APP_ID}"]):
        return
    opener = shutil.which("xdg-open")
    if opener and _spawn([opener, url]):
        return
    import webbrowser
    webbrowser.open(url)


def play_wav(path: str) -> bool:
    """Play a WAV without waiting for it, through whichever player exists."""
    for player in PLAYERS:
        exe = shutil.which(player[0])
        if exe and _spawn([exe, *player[1:], str(path)]):
            return True
    return False


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
