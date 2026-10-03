"""The unlock card's pixels, the same on every system: a pill with the trophy
logo, plain, rare (silver) or Platinum (platinum to gold). Pure Python, so the
Windows popup (overlay.py) and the Linux one (popup_linux.py) draw one card.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

W, H = 380, 88                      # owner, 2026-10-02: a pill, the Xbox way, not a square
RADIUS = H / 2                      # fully rounded ends
LOGO = Path(__file__).resolve().parent / "web" / "logo-64.png"
LOGO_TOP = 12
LOGO_LEFT = 12
TEXT_LEFT, TEXT_RIGHT = 92, W - 30
HOLD_S = 5.0
PANEL, LINE, TEXT, MUTED, ACCENT = (0x15, 0x16, 0x1C), (0x2C, 0x2F, 0x3A), (0xF4, 0xF5, 0xF7), \
    (0xA3, 0xA8, 0xB8), (0x34, 0xD3, 0x99)
PLATINUM, GOLD = (0xE5, 0xE4, 0xE2), (0xF5, 0xC4, 0x51)
SILVER_LIGHT, SILVER = (0xF4, 0xF6, 0xFA), (0x9C, 0xA6, 0xB4)    # a rare achievement: the Platinum card in silver
ICON = 56                                                         # the achievement picture, in the circle


def _clamp(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def _rounded_rect_distance(x: float, y: float, w: int, h: int, r: float) -> float:
    """Signed distance to a rounded rectangle's edge (negative inside)."""
    qx, qy = abs(x - w / 2) - (w / 2 - r), abs(y - h / 2) - (h / 2 - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - r


def png_rgba(path: Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGBA, non-interlaced PNG (the app's own logo): width,
    height and raw RGBA rows. Enough PNG for one known file, no image library."""
    data = path.read_bytes()
    width, height, depth, kind, _c, _f, interlace = struct.unpack(">IIBBBBB", data[16:29])
    if (depth, kind, interlace) != (8, 6, 0):
        raise ValueError("only 8-bit RGBA non-interlaced PNG")
    chunks, at = [], 8
    while at < len(data):
        length, tag = struct.unpack(">I4s", data[at:at + 8])
        if tag == b"IDAT":
            chunks.append(data[at + 8:at + 8 + length])
        at += 12 + length
    raw, stride, rows, prev = zlib.decompress(b"".join(chunks)), width * 4, bytearray(), bytearray(width * 4)
    for y in range(height):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b, c = prev[i], prev[i - 4] if i >= 4 else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows += line
        prev = line
    return width, height, bytes(rows)


def card_pixels(platinum: bool | str = False, logo: bool = True) -> list[list[float]]:
    """The card without text: [r, g, b, a] per pixel, colour not premultiplied.
    A soft rounded panel with a 1 px edge and the app's trophy logo on top.
    Platinum: a 2 px edge running from platinum to gold, and a warm glow.
    "rare": the same in silver, a cool glow."""
    style = "platinum" if platinum is True else (platinum or "plain")
    special = style in ("platinum", "rare")
    a_col, b_col = (PLATINUM, GOLD) if style == "platinum" else (SILVER_LIGHT, SILVER)
    px = []
    for y in range(H):
        for x in range(W):
            fx, fy = x + 0.5, y + 0.5
            d = _rounded_rect_distance(fx, fy, W, H, RADIUS)
            alpha = _clamp(0.5 - d)
            if special:
                t = (fx + fy) / (W + H)
                edge = [a_col[i] + (b_col[i] - a_col[i]) * t for i in range(3)]
                inner = _clamp(0.5 - (d + 2.0))
                glow = max(0.0, 1 - math.hypot(fx - H / 2, fy - H / 2) / 110) * 0.22
                fill = [PANEL[i] + (b_col[i] - PANEL[i]) * glow for i in range(3)]
                col = [edge[i] + (fill[i] - edge[i]) * inner for i in range(3)]
            else:
                inner = _clamp(0.5 - (d + 1.0))                # 1 px border of LINE
                col = [LINE[i] + (PANEL[i] - LINE[i]) * inner for i in range(3)]
            disc = _clamp(0.5 - (math.hypot(fx - H / 2, fy - H / 2) - 34))   # the trophy's own circle
            if disc:
                ring = b_col if special else LINE
                inside = _clamp(0.5 - (math.hypot(fx - H / 2, fy - H / 2) - 33))
                shade = [ring[i] + ((0x0E, 0x0F, 0x14)[i] - ring[i]) * inside for i in range(3)]
                col = [col[i] + (shade[i] - col[i]) * disc for i in range(3)]
            px.append([col[0], col[1], col[2], alpha])
    if not logo:
        return px
    try:
        lw, lh, rgba = png_rgba(LOGO)
    except (OSError, ValueError, zlib.error):
        return px                                              # no logo is better than no popup
    left = LOGO_LEFT
    for y in range(lh):
        for x in range(lw):
            r, g, b, a = rgba[(y * lw + x) * 4:(y * lw + x) * 4 + 4]
            if a:
                p, k = px[(LOGO_TOP + y) * W + left + x], a / 255.0
                p[0], p[1], p[2] = p[0] + (r - p[0]) * k, p[1] + (g - p[1]) * k, p[2] + (b - p[2]) * k
    return px


def draw_picture(px: list, rgba: bytes, size: int = ICON) -> None:
    """The achievement picture, clipped to a circle in the card's trophy slot."""
    left = top = int(H / 2 - size / 2)
    r = size / 2
    for y in range(size):
        for x in range(size):
            inside = _clamp(0.5 - (math.hypot(x + 0.5 - r, y + 0.5 - r) - r))
            red, green, blue, a = rgba[(y * size + x) * 4:(y * size + x) * 4 + 4]
            k = inside * a / 255.0
            if k:
                p = px[(top + y) * W + left + x]
                p[0], p[1], p[2] = p[0] + (red - p[0]) * k, p[1] + (green - p[1]) * k, p[2] + (blue - p[2]) * k


def _fit(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
