"""The system tray icon of GreycellAchievements.exe (Windows).

Standard library only (ctypes over the Windows shell API), so the app needs no
GUI toolkit. The icon's menu: Open library, Start with Windows, Quit; a
double-click opens the library. The window procedure runs on the thread that
created the icon, which is the main thread; the server and the watcher run on
their own threads.

"Start with Windows" is a value under HKCU\\...\\Run that starts the exe with
--background: into the tray, without opening the browser. Turning it off
deletes the value. Nothing is written for other users or the machine.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "GreycellAchievements"

WM_USER, WM_COMMAND, WM_DESTROY, WM_CLOSE = 0x0400, 0x0111, 0x0002, 0x0010
WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0203, 0x0205, 0x007B
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
MF_STRING, MF_SEPARATOR, MF_CHECKED = 0, 0x800, 8
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 2, 0x100, 0x80
CALLBACK = WM_USER + 20
CMD_OPEN, CMD_AUTOSTART, CMD_QUIT, CMD_UPDATES = 1, 2, 3, 4

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


def _bind():
    """Declare every call's types: undeclared, ctypes returns a C int and a
    64-bit handle comes back cut in half, so the window silently never exists."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    H, U, P = wintypes.HWND, wintypes.UINT, ctypes.c_void_p
    sigs = {
        (kernel32, "GetModuleHandleW"): (wintypes.HMODULE, [wintypes.LPCWSTR]),
        (user32, "RegisterClassW"): (wintypes.ATOM, [P]),
        (user32, "CreateWindowExW"): (H, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, wintypes.HMENU,
                                          wintypes.HINSTANCE, P]),
        (user32, "DefWindowProcW"): (LRESULT, [H, U, wintypes.WPARAM, wintypes.LPARAM]),
        (user32, "DestroyWindow"): (wintypes.BOOL, [H]),
        (user32, "LoadIconW"): (wintypes.HICON, [wintypes.HINSTANCE, wintypes.LPCWSTR]),
        (user32, "CreatePopupMenu"): (wintypes.HMENU, []),
        (user32, "AppendMenuW"): (wintypes.BOOL, [wintypes.HMENU, U, ctypes.c_size_t, wintypes.LPCWSTR]),
        (user32, "TrackPopupMenu"): (ctypes.c_int, [wintypes.HMENU, U, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, P]),
        (user32, "DestroyMenu"): (wintypes.BOOL, [wintypes.HMENU]),
        (user32, "SetForegroundWindow"): (wintypes.BOOL, [H]),
        (user32, "GetCursorPos"): (wintypes.BOOL, [P]),
        (user32, "GetMessageW"): (wintypes.BOOL, [P, H, U, U]),
        (user32, "TranslateMessage"): (wintypes.BOOL, [P]),
        (user32, "DispatchMessageW"): (LRESULT, [P]),
        (user32, "PostQuitMessage"): (None, [ctypes.c_int]),
        (user32, "PostMessageW"): (wintypes.BOOL, [H, U, wintypes.WPARAM, wintypes.LPARAM]),
        (shell32, "ExtractIconW"): (wintypes.HICON, [wintypes.HINSTANCE, wintypes.LPCWSTR, U]),
        (shell32, "Shell_NotifyIconW"): (wintypes.BOOL, [wintypes.DWORD, P]),
    }
    for (dll, name), (restype, argtypes) in sigs.items():
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = restype, argtypes
    return user32, shell32, kernel32


# ---- Start with Windows ------------------------------------------------------------

def autostart_command() -> str:
    return f'"{sys.executable}" --background'


def autostart_value(reg=None) -> str | None:
    reg = reg or __import__("winreg")
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return reg.QueryValueEx(key, RUN_NAME)[0]
    except OSError:
        return None


def autostart_enabled(reg=None) -> bool:
    reg = reg or __import__("winreg")
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            reg.QueryValueEx(key, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool, reg=None) -> None:
    reg = reg or __import__("winreg")
    with reg.CreateKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if on:
            reg.SetValueEx(key, RUN_NAME, 0, reg.REG_SZ, autostart_command())
        else:
            try:
                reg.DeleteValue(key, RUN_NAME)
            except OSError:
                pass


# ---- the icon ----------------------------------------------------------------------

class Tray:
    def __init__(self, on_open: Callable[[], None], on_quit: Callable[[], None], tip: str = "Greycell Achievements",
                 on_check: Callable[[], None] | None = None):
        self.on_open, self.on_quit, self.tip, self.on_check = on_open, on_quit, tip, on_check
        self.user32, self.shell32, self.kernel32 = _bind()
        self._proc = WNDPROC(self._wndproc)            # kept alive for the window's lifetime
        self.hwnd = None

    def _icon(self):
        icon = self.shell32.ExtractIconW(self.kernel32.GetModuleHandleW(None), sys.executable, 0)
        return icon if icon and icon > 1 else self.user32.LoadIconW(None, wintypes.LPCWSTR(32512))  # IDI_APPLICATION

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize, nid.hWnd, nid.uID, nid.uFlags = ctypes.sizeof(NOTIFYICONDATAW), self.hwnd, 1, flags
        return nid

    def notice(self, title: str, text: str) -> None:
        """A one-off balloon from the tray icon."""
        nid = self._data(NIF_INFO)
        nid.szInfoTitle, nid.szInfo = title[:63], text[:255]
        self.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _menu(self) -> None:
        menu = self.user32.CreatePopupMenu()
        self.user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "Open dashboard")
        self.user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if autostart_enabled() else 0), CMD_AUTOSTART,
                                "Start with Windows")
        if self.on_check:
            self.user32.AppendMenuW(menu, MF_STRING, CMD_UPDATES, "Check for updates")
        self.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        self.user32.AppendMenuW(menu, MF_STRING, CMD_QUIT, "Quit")
        point = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        self.user32.SetForegroundWindow(self.hwnd)    # so the menu closes when clicking elsewhere
        cmd = self.user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                         point.x, point.y, 0, self.hwnd, None)
        self.user32.DestroyMenu(menu)
        if cmd == CMD_OPEN:
            self.on_open()
        elif cmd == CMD_AUTOSTART:
            set_autostart(not autostart_enabled())
        elif cmd == CMD_UPDATES and self.on_check:
            self.on_check()
        elif cmd == CMD_QUIT:
            self.user32.DestroyWindow(self.hwnd)

    def close(self) -> None:
        """Quit from any thread (the updater's): the window closes on its own thread."""
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == CALLBACK:
            if lparam == WM_LBUTTONDBLCLK:
                self.on_open()
            elif lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._menu()
            return 0
        if msg == WM_DESTROY:
            self.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))
            self.on_quit()
            self.user32.PostQuitMessage(0)
            return 0
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self, first_notice: tuple[str, str] | None = None) -> None:
        """Show the icon and handle its messages until Quit."""
        instance = self.kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, instance, "GreycellAchievementsTray"
        if not self.user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
        self.hwnd = self.user32.CreateWindowExW(0, wc.lpszClassName, "Greycell Achievements", 0, 0, 0, 0, 0,
                                                None, None, instance, None)
        if not self.hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        nid = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.uCallbackMessage, nid.hIcon, nid.szTip = CALLBACK, self._icon(), self.tip[:127]
        if not self.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise OSError(ctypes.get_last_error(), "Shell_NotifyIconW failed")
        if first_notice:
            self.notice(*first_notice)
        msg = wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))
