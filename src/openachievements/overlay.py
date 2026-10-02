"""The unlock popup on Windows, drawn with the Windows API itself (ctypes).

Why not Tk: Tk activates its windows when it shows them, and no flag set from
outside stops it. A popup that takes focus, even for a frame, can minimise a
game running in exclusive fullscreen. This window is created never-activatable
(WS_EX_NOACTIVATE), shown with SW_SHOWNOACTIVATE, sits topmost, is left out of
the taskbar, and lets clicks through, so the game keeps focus and input.

It is a per-pixel-alpha layered window: the card is drawn here into a 32-bit
buffer with anti-aliased rounded corners, text comes from GDI (grey-scale
anti-aliasing, used as coverage), and UpdateLayeredWindow places it, so the
slide and fade are only new positions and alphas. Standard library only.
"""
from __future__ import annotations

import ctypes
import math
import struct
import time
import zlib
from ctypes import wintypes
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

WS_POPUP = 0x80000000
WS_EX = 0x00080000 | 0x00000020 | 0x08000000 | 0x00000080 | 0x00000008
#        LAYERED      TRANSPARENT  NOACTIVATE   TOOLWINDOW   TOPMOST
SW_SHOWNOACTIVATE, ULW_ALPHA, AC_SRC_ALPHA = 4, 2, 1
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


def _api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    H_, P = wintypes.HANDLE, ctypes.c_void_p
    sigs = {
        (kernel32, "GetModuleHandleW"): (wintypes.HMODULE, [wintypes.LPCWSTR]),
        (user32, "RegisterClassW"): (wintypes.ATOM, [P]),
        (user32, "UnregisterClassW"): (wintypes.BOOL, [wintypes.LPCWSTR, wintypes.HINSTANCE]),
        (user32, "CreateWindowExW"): (wintypes.HWND, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                                      wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                                      ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                                      wintypes.HINSTANCE, P]),
        (user32, "DefWindowProcW"): (LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
        (user32, "DestroyWindow"): (wintypes.BOOL, [wintypes.HWND]),
        (user32, "ShowWindow"): (wintypes.BOOL, [wintypes.HWND, ctypes.c_int]),
        (user32, "UpdateLayeredWindow"): (wintypes.BOOL, [wintypes.HWND, wintypes.HDC, P, P, wintypes.HDC, P,
                                                          wintypes.COLORREF, P, wintypes.DWORD]),
        (user32, "PeekMessageW"): (wintypes.BOOL, [P, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]),
        (user32, "TranslateMessage"): (wintypes.BOOL, [P]),
        (user32, "DispatchMessageW"): (LRESULT, [P]),
        (user32, "GetDC"): (wintypes.HDC, [wintypes.HWND]),
        (user32, "ReleaseDC"): (ctypes.c_int, [wintypes.HWND, wintypes.HDC]),
        (user32, "SystemParametersInfoW"): (wintypes.BOOL, [wintypes.UINT, wintypes.UINT, P, wintypes.UINT]),
        (user32, "DrawTextW"): (ctypes.c_int, [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, P, wintypes.UINT]),
        (gdi32, "CreateCompatibleDC"): (wintypes.HDC, [wintypes.HDC]),
        (gdi32, "DeleteDC"): (wintypes.BOOL, [wintypes.HDC]),
        (gdi32, "CreateDIBSection"): (wintypes.HBITMAP, [wintypes.HDC, P, wintypes.UINT, P, H_, wintypes.DWORD]),
        (gdi32, "SelectObject"): (H_, [wintypes.HDC, H_]),
        (gdi32, "DeleteObject"): (wintypes.BOOL, [H_]),
        (gdi32, "CreateFontW"): (wintypes.HFONT, [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]),
        (gdi32, "SetTextColor"): (wintypes.COLORREF, [wintypes.HDC, wintypes.COLORREF]),
        (gdi32, "SetBkMode"): (ctypes.c_int, [wintypes.HDC, ctypes.c_int]),
    }
    for (dll, name), (restype, argtypes) in sigs.items():
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = restype, argtypes
    return user32, gdi32, kernel32


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


def card_pixels(platinum: bool = False) -> list[list[float]]:
    """The card without text: [r, g, b, a] per pixel, colour not premultiplied.
    A soft rounded panel with a 1 px edge and the app's trophy logo on top.
    Platinum: a 2 px edge running from platinum to gold, and a warm glow."""
    px = []
    for y in range(H):
        for x in range(W):
            fx, fy = x + 0.5, y + 0.5
            d = _rounded_rect_distance(fx, fy, W, H, RADIUS)
            alpha = _clamp(0.5 - d)
            if platinum:
                t = (fx + fy) / (W + H)
                edge = [PLATINUM[i] + (GOLD[i] - PLATINUM[i]) * t for i in range(3)]
                inner = _clamp(0.5 - (d + 2.0))
                glow = max(0.0, 1 - math.hypot(fx - H / 2, fy - H / 2) / 110) * 0.22
                fill = [PANEL[i] + (GOLD[i] - PANEL[i]) * glow for i in range(3)]
                col = [edge[i] + (fill[i] - edge[i]) * inner for i in range(3)]
            else:
                inner = _clamp(0.5 - (d + 1.0))                # 1 px border of LINE
                col = [LINE[i] + (PANEL[i] - LINE[i]) * inner for i in range(3)]
            disc = _clamp(0.5 - (math.hypot(fx - H / 2, fy - H / 2) - 34))   # the trophy's own circle
            if disc:
                ring = GOLD if platinum else LINE
                inside = _clamp(0.5 - (math.hypot(fx - H / 2, fy - H / 2) - 33))
                shade = [ring[i] + ((0x0E, 0x0F, 0x14)[i] - ring[i]) * inside for i in range(3)]
                col = [col[i] + (shade[i] - col[i]) * disc for i in range(3)]
            px.append([col[0], col[1], col[2], alpha])
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


class _Text:
    """GDI text rendered white on black; the grey level is the coverage."""

    def __init__(self, gdi32, user32, screen_dc):
        self.gdi32, self.user32 = gdi32, user32
        self.dc = gdi32.CreateCompatibleDC(screen_dc)
        info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), W, -H, 1, 32, 0, 0, 0, 0, 0, 0)
        self.bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(self.dc, ctypes.byref(info), 0, ctypes.byref(self.bits), None, 0)
        self.old = gdi32.SelectObject(self.dc, self.bitmap)
        gdi32.SetBkMode(self.dc, 1)                              # TRANSPARENT
        gdi32.SetTextColor(self.dc, 0x00FFFFFF)

    def draw(self, px: list[list[float]], text: str, top: int, bottom: int, size: int, weight: int,
             face: str, colour: tuple[int, int, int], wrap: bool = False) -> None:
        ctypes.memset(self.bits, 0, W * H * 4)
        font = self.gdi32.CreateFontW(-size, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 4, 0, face)   # ANTIALIASED_QUALITY
        old = self.gdi32.SelectObject(self.dc, font)
        rect = wintypes.RECT(TEXT_LEFT, top, TEXT_RIGHT, bottom)
        flags = 0x8000 | 0x4 | 0x20                              # LEFT END_ELLIPSIS VCENTER SINGLELINE
        self.user32.DrawTextW(self.dc, text, -1, ctypes.byref(rect), flags)
        self.gdi32.SelectObject(self.dc, old)
        self.gdi32.DeleteObject(font)
        raw = ctypes.string_at(self.bits, W * H * 4)
        for i in range(W * H):
            cover = raw[i * 4 + 1] / 255.0                        # green channel of white text
            if cover:
                p = px[i]
                for c in range(3):
                    p[c] += (colour[c] - p[c]) * cover

    def close(self) -> None:
        self.gdi32.SelectObject(self.dc, self.old)
        self.gdi32.DeleteObject(self.bitmap)
        self.gdi32.DeleteDC(self.dc)


def _fit(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def show(cards: list[dict], sound: bool = True, play=None) -> None:
    user32, gdi32, kernel32 = _api()
    instance = kernel32.GetModuleHandleW(None)
    proc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
    cls = WNDCLASSW()
    cls.lpfnWndProc, cls.hInstance, cls.lpszClassName = proc, instance, "GreycellAchievementsPopup"
    user32.RegisterClassW(ctypes.byref(cls))
    area = wintypes.RECT()
    if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):     # SPI_GETWORKAREA
        area = wintypes.RECT(0, 0, 1920, 1080)
    x = area.left + (area.right - area.left - W) // 2
    final_y = area.bottom - H - 48
    hwnd = user32.CreateWindowExW(WS_EX, cls.lpszClassName, None, WS_POPUP, x, final_y, W, H,
                                  None, None, instance, None)
    screen = user32.GetDC(None)
    text = _Text(gdi32, user32, screen)
    canvas = gdi32.CreateCompatibleDC(screen)
    info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), W, -H, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(canvas, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(canvas, bitmap)
    size, source = wintypes.SIZE(W, H), wintypes.POINT(0, 0)
    msg = wintypes.MSG()
    bases = {}
    shown = False

    def frame(y: int, alpha: float) -> None:
        nonlocal shown
        blend = BLENDFUNCTION(0, 0, max(0, min(255, int(alpha * 255))), AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wintypes.POINT(x, y)), ctypes.byref(size), canvas,
                                   ctypes.byref(source), 0, ctypes.byref(blend), ULW_ALPHA)
        if not shown:
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            shown = True
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):              # PM_REMOVE
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    try:
        for c in cards:
            plat = bool(c.get("platinum"))
            if plat not in bases:
                bases[plat] = card_pixels(plat)
            px = [p[:] for p in bases[plat]]
            if plat:
                text.draw(px, "PLATINUM", 13, 31, 12, 700, "Segoe UI", GOLD)
                text.draw(px, _fit(c.get("game") or "", 60), 32, 56, 17, 600, "Segoe UI", TEXT)
                text.draw(px, "Every achievement unlocked", 56, 75, 12, 400, "Segoe UI", PLATINUM)
            else:
                points = f" · {c['points']} pts" if c.get("points") else ""
                top = 13 if c.get("game") else 22
                text.draw(px, "Achievement unlocked" + points, top, top + 18, 12, 400, "Segoe UI", MUTED)
                text.draw(px, _fit(c.get("name") or "Achievement", 60), top + 19, top + 43, 17, 600, "Segoe UI", TEXT)
                if c.get("game"):
                    text.draw(px, _fit(c["game"], 50), 56, 75, 12, 400, "Segoe UI", MUTED)
            out = bytearray(W * H * 4)
            for i, (r, g, b, a) in enumerate(px):                # premultiplied BGRA
                out[i * 4:i * 4 + 4] = bytes((int(b * a), int(g * a), int(r * a), int(a * 255)))
            ctypes.memmove(bits, bytes(out), len(out))
            if sound and play:
                play(plat)
            for step in range(13):                               # slide up and fade in
                p = 1 - (1 - step / 12) ** 3
                frame(final_y + int(26 * (1 - p)), 0.96 * p)
                time.sleep(0.016)
            end = time.monotonic() + HOLD_S
            while time.monotonic() < end:
                frame(final_y, 0.96)
                time.sleep(0.05)
            for step in range(15):                               # fade out
                frame(final_y, 0.96 * (1 - step / 14))
                time.sleep(0.025)
    finally:
        user32.DestroyWindow(hwnd)
        gdi32.SelectObject(canvas, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(canvas)
        text.close()
        user32.ReleaseDC(None, screen)
        user32.UnregisterClassW(cls.lpszClassName, instance)
