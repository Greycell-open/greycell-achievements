"""The unlock popup window: `python -m openachievements.toast` with JSON on stdin.

    {"cards": [{"name": "From Scratch", "game": "SILENT HILL: Townfall", "points": 0}], "sound": true}

A pill-shaped card (card.py) slides up at the bottom centre of the screen,
holds, and fades, one card after another. It never takes focus and clicks pass
through it, so a game keeps its input. On Windows it is drawn with the Windows
API (overlay.py), on Linux by Tk cut to shape (popup_linux.py). It shows over
windowed, borderless and fullscreen-optimised games; a game holding true
exclusive fullscreen owns the screen (Steam's popups draw inside the game by
hooking it, which this deliberately does not do).

The sounds are made here (sounds.py), so there is no sound file to license.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


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
        path = file if name == "custom" and file and Path(file).exists() else \
            _chime_file(kind or ("platinum" if platinum else "unlock"), None if name == "custom" else name)
        if sys.platform != "win32":
            from .linux_desktop import play_wav
            play_wav(str(path))
            return
        import winsound
        winsound.PlaySound(str(path),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:  # noqa: BLE001 - no sound is better than no popup
        pass


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
    from . import popup_linux
    popup_linux.show(cards, sound=sound, play=play)


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
