"""The unlock popup window: `python -m openachievements.toast` with JSON on stdin.

    {"cards": [{"name": "From Scratch", "game": "SILENT HILL: Townfall", "points": 0}], "sound": true}

A rounded card slides up at the bottom centre of the screen, holds, and fades,
one card after another. It never takes focus and clicks pass through it, so a
game keeps its input. On Windows it is drawn with the Windows API (overlay.py),
because Tk activates its windows; elsewhere Tk draws it. It shows over windowed,
borderless and fullscreen-optimised games; a game holding true exclusive
fullscreen owns the screen (Steam's popups draw inside the game by hooking it,
which this deliberately does not do).

The chime is made here, two quiet notes, so there is no sound file to license.
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
from pathlib import Path

WIDTH = HEIGHT = 188                  # a rounded square
HOLD_MS = 5000
KEY = "#ff00fe"                       # window colour made transparent: the rounded corners
PANEL, LINE, TEXT, MUTED, ACCENT = "#15161c", "#2c2f3a", "#f4f5f7", "#a3a8b8", "#34d399"


def chime_wav(kind: str = "unlock", name: str | None = None) -> bytes:
    """The chosen sound as WAV bytes (sounds.py makes them)."""
    from . import sounds
    return sounds.wav(kind, name)


def _chime_file(kind: str = "unlock", name: str | None = None) -> Path:
    from . import sounds
    name = sounds.known(kind, name)
    path = Path(tempfile.gettempdir()) / f"greycell-achievements-{kind}-{name}-v11.wav"
    if not path.exists():
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(chime_wav(kind, name))
        os.replace(tmp, path)
    return path


def _play_chime(platinum: bool = False, name: str | None = None, file: str | None = None, kind: str | None = None) -> None:
    """The chosen sound, or the player's own WAV when `name` is "custom"."""
    try:
        import winsound
        path = file if name == "custom" and file and Path(file).exists() else \
            _chime_file(kind or ("platinum" if platinum else "unlock"), None if name == "custom" else name)
        winsound.PlaySound(str(path),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:  # noqa: BLE001 - no sound is better than no popup
        pass


def _work_area(root) -> tuple[int, int, int, int]:
    """The screen minus the taskbar, on Windows; the whole screen elsewhere."""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def _click_through(root) -> None:
    """Never focused, never in the taskbar, and clicks go to the game below."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        GWL_EXSTYLE = -20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        # Tk makes the window layered itself when it sets the transparency;
        # setting that flag here first would leave it layered and invisible.
        style |= 0x00000020 | 0x08000000 | 0x00000080 | 0x00000008
        #        TRANSPARENT   NOACTIVATE   TOOLWINDOW   TOPMOST
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:  # noqa: BLE001
        pass


def _show_without_focus(root) -> bool:
    """Show the popup without activating it. Tk's own deiconify activates the
    window, which takes focus from the game for a moment: enough to minimise
    an exclusive fullscreen game. Windows' SW_SHOWNOACTIVATE does not."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        user32.ShowWindow(hwnd, 4)                                     # SW_SHOWNOACTIVATE
        user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0,       # HWND_TOPMOST
                            0x0001 | 0x0002 | 0x0010 | 0x0040)         # NOSIZE NOMOVE NOACTIVATE SHOWWINDOW
        return True
    except Exception:  # noqa: BLE001
        return False


def _rounded(canvas, x1, y1, x2, y2, r, **kw):
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
              x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, **kw)


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def show(cards: list[dict], sound: bool = True, choice: dict | None = None) -> None:
    choice = choice or {}

    def play(which=False) -> None:                 # a card's kind: "unlock", "rare" or "platinum" (True: platinum)
        kind = which if isinstance(which, str) else ("platinum" if which else "unlock")
        if kind == "rare":
            _play_chime(False, choice.get("rare"), choice.get("rare_file"), kind="rare")
        else:
            _play_chime(kind == "platinum", choice.get(kind), choice.get(f"{kind}_file"))
    if sys.platform == "win32":
        from . import overlay                    # never takes focus; Tk activates its windows
        overlay.show(cards, sound=sound, play=play)
        return
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    # No Tk "-topmost": Tk sets it with a SetWindowPos that activates the
    # window. _click_through and _show_without_focus keep it on top instead.
    try:
        root.attributes("-transparentcolor", KEY)
    except tk.TclError:
        pass
    root.configure(bg=KEY)
    canvas = tk.Canvas(root, width=WIDTH, height=HEIGHT, bg=KEY, highlightthickness=0, bd=0)
    canvas.pack()
    # Make Tk create the window's outer frame now, while it is hidden: the
    # never-focus flags must be on that frame before it is first shown, or Tk
    # creates it later with an activation that takes focus from the game.
    root.update()
    left, top, right, bottom = _work_area(root)
    x = left + (right - left - WIDTH) // 2
    final_y = bottom - HEIGHT - 48
    queue = list(cards)

    def draw(c: dict) -> None:
        canvas.delete("all")
        mid = WIDTH // 2
        _rounded(canvas, 1, 1, WIDTH - 1, HEIGHT - 1, 26, fill=PANEL, outline=LINE)
        canvas.create_oval(mid - 25, 18, mid + 25, 68, fill=ACCENT, outline="")
        canvas.create_text(mid, 44, text="\u2605", fill=PANEL, font=("Segoe UI Symbol", 21, "bold"))
        canvas.create_text(mid, 86, fill=MUTED, font=("Segoe UI", 9),
                           text="Achievement unlocked" + (f" \u00b7 {c['points']} pts" if c.get("points") else ""))
        canvas.create_text(mid, 118, fill=TEXT, font=("Segoe UI Semibold", 12), justify="center",
                           width=WIDTH - 28, text=_fit(str(c.get("name") or "Achievement"), 40))
        if c.get("game"):
            canvas.create_text(mid, 160, fill=MUTED, font=("Segoe UI", 9), justify="center",
                               width=WIDTH - 28, text=_fit(str(c["game"]), 30))

    def place(y: int, alpha: float) -> None:
        root.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")
        root.attributes("-alpha", alpha)

    def slide_in(step: int = 0) -> None:
        steps = 12
        p = 1 - (1 - step / steps) ** 3
        place(final_y + int(26 * (1 - p)), 0.96 * p)
        if step < steps:
            root.after(16, slide_in, step + 1)
        else:
            root.after(HOLD_MS, fade_out)

    def fade_out(step: int = 0) -> None:
        steps = 14
        place(final_y, 0.96 * (1 - step / steps))
        if step < steps:
            root.after(25, fade_out, step + 1)
        else:
            next_card()

    def next_card() -> None:
        if not queue:
            root.destroy()
            return
        draw(queue.pop(0))
        place(final_y + 26, 0.01)
        # Never-focused before it is ever shown: if the popup took focus even
        # for a moment, an exclusive fullscreen game would be minimised.
        root.update_idletasks()
        _click_through(root)
        if not (sys.platform == "win32" and _show_without_focus(root)):
            root.deiconify()
            _click_through(root)
        root.update_idletasks()
        if sound:
            play("platinum" if c.get("platinum") else "rare" if "rare" in c else "unlock")
        slide_in()

    root.after(0, next_card)
    root.mainloop()


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except ValueError:
        return
    cards = [c for c in data.get("cards") or [] if isinstance(c, dict)][:10]
    if cards:
        names = ", ".join(str(c.get("name")) for c in cards)
        print(f"popup: showing {names}")
        show(cards, sound=bool(data.get("sound", True)), choice=data.get("sounds") or {})
        print(f"popup: shown {names}")


if __name__ == "__main__":
    main()
