"""The unlock popup on Linux: the same pill card as on Windows (card.py).

The card is rendered here into an image, text included (Pillow, with the
DejaVu Sans that ships with the app), because the Tk inside the app draws text
with X's core fonts only, which look like a terminal's. Tk then shows the
image in an override-redirect window, which the window manager never gives
focus, and the X Shape extension cuts it to the pill and gives it an empty
input region, so the corners are see-through and clicks go to the game below.
On Wayland desktops it runs through XWayland like any X program. When there is
no X display at all (a Wayland session without XWayland), the desktop's own
notification says the same thing instead.
"""
from __future__ import annotations

import ctypes
import shutil

from pathlib import Path

from .card import (GOLD, H, HOLD_S, ICON, MUTED, PANEL, PLATINUM, SILVER_LIGHT, TEXT, TEXT_LEFT, TEXT_RIGHT, W, _fit,
                   card_pixels, draw_picture)
from . import linux_desktop
from .linux_desktop import APP_ID, NAME, icon_file
from .linux_desktop import ICON as APP_ICON

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONTS = ("Noto Sans", "Cantarell", "Ubuntu", "DejaVu Sans", "Liberation Sans", "Helvetica")   # Tk's, without Pillow
SHADOW = (0x0E, 0x0F, 0x14)
SHAPE_BOUNDING, SHAPE_INPUT, SHAPE_SET = 0, 2, 0


class XRectangle(ctypes.Structure):
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short), ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]


def _spans(px: list[list[float]]) -> list[tuple[int, int, int]]:
    """Each row's inside as (y, x, width): where the card is at least half there."""
    out = []
    for y in range(H):
        row = [px[y * W + x][3] >= 0.5 for x in range(W)]
        if True in row:
            left = row.index(True)
            right = W - row[::-1].index(True)
            out.append((y, left, right - left))
    return out


def _rgb(px: list[list[float]]) -> bytes:
    """The soft edge is blended into the circle's dark rather than into nothing."""
    body = bytearray()
    for r, g, b, a in px:
        body += bytes(int(SHADOW[i] + (c - SHADOW[i]) * a) for i, c in enumerate((r, g, b)))
    return bytes(body)


def card_ppm(px: list[list[float]]) -> bytes:
    """The card as a PPM image for Tk."""
    return f"P6 {W} {H} 255\n".encode() + _rgb(px)


def picture_rgba(path: str, size: int = ICON) -> bytes | None:
    """An achievement picture (PNG or JPEG from the picture cache) as RGBA at
    the circle's size, or None (the card keeps its trophy)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.convert("RGBA").resize((size, size), Image.LANCZOS).tobytes()
    except Exception:  # noqa: BLE001 - no picture is never a reason to lose the popup
        return None


def _ellipsize(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "\u2026", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "\u2026"


def render(c: dict, bases: dict) -> tuple[bytes, list] | None:
    """The finished card for `c`, text and picture drawn in, as PPM and its
    outline; None without Pillow (Tk then writes the text)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    plat = bool(c.get("platinum"))
    style = "platinum" if plat else "rare" if "rare" in c else "plain"
    picture = picture_rgba(c["icon"]) if c.get("icon") and not plat else None
    key = (style, picture is None)
    if key not in bases:
        bases[key] = card_pixels(style, logo=picture is None)
    px = [q[:] for q in bases[key]]
    if picture:
        draw_picture(px, picture)
    image = Image.frombytes("RGB", (W, H), _rgb(px))
    draw = ImageDraw.Draw(image)
    for text, top, bottom, size, colour in lines(c):
        bold = size > 12 or text == "PLATINUM" or text.startswith("Rare")
        font = ImageFont.truetype(str(FONT_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)
        draw.text((TEXT_LEFT, (top + bottom) / 2), _ellipsize(draw, text, font, TEXT_RIGHT - TEXT_LEFT),
                  font=font, fill=colour, anchor="lm")
    return f"P6 {W} {H} 255\n".encode() + image.tobytes(), _spans(px)


class _Shape:
    """The X Shape extension through ctypes: the pill's outline, and no input."""

    def __init__(self):
        self.x11 = ctypes.CDLL("libX11.so.6")
        self.xext = ctypes.CDLL("libXext.so.6")
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XFlush.argtypes = [ctypes.c_void_p]
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.xext.XShapeCombineRectangles.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                                      ctypes.c_int, ctypes.POINTER(XRectangle), ctypes.c_int,
                                                      ctypes.c_int, ctypes.c_int]
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise OSError("no X display")

    def apply(self, window: int, spans: list[tuple[int, int, int]]) -> None:
        rects = (XRectangle * len(spans))(*[XRectangle(x, y, w, 1) for y, x, w in spans])
        self.xext.XShapeCombineRectangles(self.display, window, SHAPE_BOUNDING, 0, 0, rects, len(spans), SHAPE_SET, 0)
        self.xext.XShapeCombineRectangles(self.display, window, SHAPE_INPUT, 0, 0, None, 0, SHAPE_SET, 0)
        self.x11.XFlush(self.display)

    def close(self) -> None:
        self.x11.XCloseDisplay(self.display)


def lines(c: dict) -> list[tuple[str, int, int, int, tuple]]:
    """The card's text as (text, top, bottom, pixel size, colour), placed as on Windows."""
    if c.get("platinum"):
        return [("PLATINUM", 13, 31, 12, GOLD), (_fit(c.get("game") or "", 60), 32, 56, 17, TEXT),
                ("Every achievement unlocked", 56, 75, 12, PLATINUM)]
    top = 13 if c.get("game") else 22
    rare = "rare" in c
    points = f" · {c['points']} pts" if c.get("points") else ""
    label = f"Rare achievement · {c['rare']:g}% of players" if rare else "Achievement unlocked" + points
    out = [(label, top, top + 18, 12, SILVER_LIGHT if rare else MUTED),
           (_fit(c.get("name") or "Achievement", 60), top + 19, top + 43, 17, TEXT)]
    if c.get("game"):
        out.append((_fit(c["game"], 50), 56, 75, 12, MUTED))
    return out


def _font_family(root) -> str:
    """The desktop's sans: Tk draws a missing family in a bitmap fallback."""
    try:
        from tkinter import font
        installed = {f.lower() for f in font.families(root)}
    except Exception:  # noqa: BLE001
        return FONTS[-1]
    return next((f for f in FONTS if f.lower() in installed), FONTS[-1])     # Helvetica: Tk maps it to a sans


def notify(cards: list[dict]) -> bool:
    """The desktop's own notification, when no window can be shown."""
    sender = shutil.which("notify-send")
    if not sender:
        return False
    icon = icon_file() if icon_file().exists() else APP_ICON
    for c in cards:
        text = [t for t, *_ in lines(c)]
        linux_desktop._spawn([sender, "-a", NAME, "-i", str(icon), text[0], "\n".join(text[1:])])
    return True


def show(cards: list[dict], sound: bool = True, play=None) -> None:
    try:
        import tkinter as tk
        root = tk.Tk(className=APP_ID)
    except Exception:  # noqa: BLE001 - no X display, no Tk: the desktop's notification instead
        if sound and play and cards:
            c = cards[0]
            play("platinum" if c.get("platinum") else "rare" if "rare" in c else "unlock")
        notify(cards)
        return
    root.withdraw()
    root.overrideredirect(True)
    for name, value in (("-type", "notification"), ("-topmost", True)):
        try:
            root.attributes(name, value)
        except tk.TclError:
            pass
    x = (root.winfo_screenwidth() - W) // 2
    final_y = root.winfo_screenheight() - H - 64        # above a bottom panel
    canvas = tk.Canvas(root, width=W, height=H, bg="#%02x%02x%02x" % PANEL, highlightthickness=0, bd=0)
    canvas.pack()
    root.geometry(f"{W}x{H}+{x}+{final_y + 26}")
    root.update_idletasks()
    try:
        shape = _Shape()
    except (OSError, AttributeError):
        shape = None                                      # square corners rather than no popup
    queue, bases, images = list(cards), {}, []
    font = _font_family(root)

    def place(y: int, alpha: float) -> None:
        root.geometry(f"+{x}+{y}")
        try:
            root.attributes("-alpha", alpha)
        except tk.TclError:
            pass

    def draw(c: dict) -> None:
        canvas.delete("all")
        made = render(c, bases)
        if made:
            image, spans = tk.PhotoImage(data=made[0], format="ppm"), made[1]
            canvas.create_image(0, 0, image=image, anchor="nw")
        else:                                             # no Pillow (a source checkout): Tk writes the text
            style = "platinum" if c.get("platinum") else "rare" if "rare" in c else "plain"
            px = card_pixels(style)
            image, spans = tk.PhotoImage(data=card_ppm(px), format="ppm"), _spans(px)
            canvas.create_image(0, 0, image=image, anchor="nw")
            for text, top, bottom, size, colour in lines(c):
                weight = "bold" if size > 12 or text == "PLATINUM" or text.startswith("Rare") else "normal"
                canvas.create_text(TEXT_LEFT, (top + bottom) // 2, text=text, anchor="w",
                                   fill="#%02x%02x%02x" % colour, font=(font, -size, weight))
        images[:] = [image]                               # Tk drops an image nothing in Python holds
        if shape:
            root.update_idletasks()
            shape.apply(int(root.wm_frame(), 16), spans)

    def slide_in(step: int = 0) -> None:
        p = 1 - (1 - step / 12) ** 3
        place(final_y + int(26 * (1 - p)), 0.96 * p)
        root.after(16, slide_in, step + 1) if step < 12 else root.after(int(HOLD_S * 1000), fade_out)

    def fade_out(step: int = 0) -> None:
        place(final_y, 0.96 * (1 - step / 14))
        root.after(25, fade_out, step + 1) if step < 14 else next_card()

    def next_card() -> None:
        if not queue:
            root.destroy()
            return
        c = queue.pop(0)
        draw(c)
        place(final_y + 26, 0.01)
        root.deiconify()
        root.lift()
        if sound and play:
            play("platinum" if c.get("platinum") else "rare" if "rare" in c else "unlock")
        slide_in()

    root.after(0, next_card)
    try:
        root.mainloop()
    finally:
        if shape:
            shape.close()
