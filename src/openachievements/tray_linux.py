"""The tray icon on Linux: a StatusNotifierItem over D-Bus.

KDE Plasma (the Steam Deck's desktop), Cinnamon, XFCE, LXQt, Budgie and GNOME
with the AppIndicator extension show tray icons through the
StatusNotifierItem specification: the app owns a bus name, serves the
`org.kde.StatusNotifierItem` interface at /StatusNotifierItem, a
`com.canonical.dbusmenu` menu at /MenuBar, and registers with
`org.kde.StatusNotifierWatcher`. Clicking the icon opens the library; the
menu has Open, Start at login and Quit.

The D-Bus side is jeepney (pure Python, bundled with the Linux app). What
answers each call is plain Python below it (`Item`, `Menu`), so it is tested
without a bus. A desktop with no tray (GNOME without the extension) simply
has no watcher: nothing is shown, and the page's Settings keep Start at login
and Quit, as before. The tray never stops the app: any failure ends only the
tray.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
ITEM_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
WATCHER = "org.kde.StatusNotifierWatcher"
TITLE = "Greycell Achievements"

INTROSPECT = {
    ITEM_PATH: f"""<node><interface name="{ITEM_IFACE}">
<property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
<property name="IconName" type="s" access="read"/><property name="IconPixmap" type="a(iiay)" access="read"/>
<property name="ToolTip" type="(sa(iiay)ss)" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
</interface></node>""",
    MENU_PATH: f"""<node><interface name="{MENU_IFACE}">
<property name="Version" type="u" access="read"/><property name="Status" type="s" access="read"/>
<property name="TextDirection" type="s" access="read"/>
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
<arg type="u" direction="out"/><arg type="(ia{{sv}}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
<arg type="a(ia{{sv}})" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/>
<arg type="u" direction="in"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>""",
}


def pixmaps(icon: Path, sizes=(22, 32, 48, 64)) -> list[tuple[int, int, bytes]]:
    """The icon as the specification wants it: ARGB32, network byte order."""
    try:
        from PIL import Image
        base = Image.open(icon).convert("RGBA")
    except Exception:  # noqa: BLE001 - no Pillow or no icon: the desktop's generic icon
        return []
    out = []
    for size in sizes:
        r, g, b, a = base.resize((size, size), Image.LANCZOS).split()
        out.append((size, size, Image.merge("RGBA", (a, r, g, b)).tobytes()))
    return out


class Item:
    """org.kde.StatusNotifierItem: what the tray shows, and its clicks."""

    def __init__(self, on_open: Callable[[], None], icon_pixmaps: list, icon_name: str = ""):
        self.on_open = on_open
        self.icon_pixmaps = icon_pixmaps
        self.icon_name = icon_name

    def properties(self) -> dict:
        return {"Category": ("s", "ApplicationStatus"), "Id": ("s", "greycell-achievements"),
                "Title": ("s", TITLE), "Status": ("s", "Active"), "WindowId": ("i", 0),
                "IconName": ("s", self.icon_name), "IconPixmap": ("a(iiay)", self.icon_pixmaps),
                "OverlayIconName": ("s", ""), "AttentionIconName": ("s", ""),
                "ToolTip": ("(sa(iiay)ss)", ("", [], TITLE, "Watching your games")),
                "ItemIsMenu": ("b", False), "Menu": ("o", MENU_PATH)}

    def call(self, member: str, body: tuple) -> tuple[str, tuple] | None:
        if member in ("Activate", "SecondaryActivate"):
            self.on_open()
            return "", ()
        if member in ("ContextMenu", "Scroll"):
            return "", ()                            # the menu is served by /MenuBar
        return None


class Menu:
    """com.canonical.dbusmenu: Open, Start at login, Quit."""

    OPEN, AUTOSTART, SEPARATOR, QUIT = 1, 2, 3, 4

    def __init__(self, on_open: Callable[[], None], on_quit: Callable[[], None],
                 autostart: object | None = None):
        self.on_open, self.on_quit, self.autostart = on_open, on_quit, autostart
        self.revision = 1
        self.changed: Callable[[int], None] = lambda revision: None

    def _items(self) -> dict[int, dict]:
        items = {self.OPEN: {"label": ("s", "Open Greycell Achievements")}}
        if self.autostart is not None:
            try:
                on = bool(self.autostart.autostart_enabled())
            except OSError:
                on = False
            items[self.AUTOSTART] = {"label": ("s", "Start at login"), "toggle-type": ("s", "checkmark"),
                                     "toggle-state": ("i", 1 if on else 0)}
        items[self.SEPARATOR] = {"type": ("s", "separator")}
        items[self.QUIT] = {"label": ("s", "Quit")}
        return items

    def properties(self) -> dict:
        return {"Version": ("u", 3), "Status": ("s", "normal"), "TextDirection": ("s", "ltr"),
                "IconThemePath": ("as", [])}

    def layout(self) -> tuple[int, tuple]:
        children = [("(ia{sv}av)", (item_id, props, [])) for item_id, props in self._items().items()]
        return self.revision, (0, {"children-display": ("s", "submenu")}, children)

    def event(self, item_id: int, event_id: str) -> None:
        if event_id != "clicked":
            return
        if item_id == self.OPEN:
            self.on_open()
        elif item_id == self.QUIT:
            self.on_quit()
        elif item_id == self.AUTOSTART and self.autostart is not None:
            try:
                self.autostart.set_autostart(not self.autostart.autostart_enabled())
            except OSError:
                pass
            self.revision += 1
            self.changed(self.revision)

    def call(self, member: str, body: tuple) -> tuple[str, tuple] | None:
        if member == "GetLayout":
            revision, layout = self.layout()
            return "u(ia{sv}av)", (revision, layout)
        if member == "GetGroupProperties":
            ids, names = body
            items = self._items()
            rows = [(i, {k: v for k, v in items[i].items() if not names or k in names}) for i in ids if i in items]
            return "a(ia{sv})", (rows,)
        if member == "GetProperty":
            item_id, name = body
            value = self._items().get(item_id, {}).get(name)
            return ("v", (value,)) if value else None
        if member == "Event":
            self.event(body[0], body[1])
            return "", ()
        if member == "EventGroup":
            for item_id, event_id, _data, _time in body[0]:
                self.event(item_id, event_id)
            return "ai", ([],)
        if member == "AboutToShow":
            return "b", (False,)
        if member == "AboutToShowGroup":
            return "aiai", ([], [])
        return None


def answer(item: Item, menu: Menu, path: str, interface: str, member: str, body: tuple) -> tuple[str, tuple] | None:
    """The reply (signature, body) to one method call, or None for an unknown one."""
    if interface == "org.freedesktop.DBus.Introspectable" and member == "Introspect":
        return ("s", (INTROSPECT[path],)) if path in INTROSPECT else None
    target = item if path == ITEM_PATH else menu if path == MENU_PATH else None
    if target is None:
        return None
    if interface == PROPS_IFACE:
        props = target.properties()
        if member == "GetAll":
            return "a{sv}", (props,)
        if member == "Get" and body[1] in props:
            return "v", (props[body[1]],)
        return None
    return target.call(member, body)


class Tray:
    """The icon, on its own thread. `start()` returns False when there is no
    session bus, no tray on this desktop, or no jeepney."""

    def __init__(self, on_open: Callable[[], None], on_quit: Callable[[], None], autostart: object | None = None,
                 icon: Path | None = None):
        self.item = Item(on_open, pixmaps(icon) if icon else [])
        self.menu = Menu(on_open, on_quit, autostart)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.conn = None

    def start(self) -> bool:
        try:
            from jeepney import DBusAddress, new_method_call
            from jeepney.bus_messages import MatchRule, message_bus
            from jeepney.io.blocking import open_dbus_connection
            self.conn = open_dbus_connection(bus="SESSION")
            self.conn.send_and_get_reply(message_bus.RequestName(self.name, 4))
            rule = MatchRule(type="signal", sender="org.freedesktop.DBus", interface="org.freedesktop.DBus",
                             member="NameOwnerChanged", path="/org/freedesktop/DBus")
            rule.add_arg_condition(0, WATCHER)               # in place: the watcher coming back
            self.conn.send_and_get_reply(message_bus.AddMatch(rule))
            self._watcher = DBusAddress("/StatusNotifierWatcher", bus_name=WATCHER, interface=WATCHER)
            self._new_call = new_method_call
            if not self._register():
                self.conn.close()
                return False
        except Exception as exc:  # noqa: BLE001 - no bus, no jeepney: no tray, the app goes on
            print(f"tray unavailable: {exc}")
            return False
        self.menu.changed = self._layout_updated
        threading.Thread(target=self._loop, daemon=True, name="greycell-tray").start()
        return True

    def _register(self) -> bool:
        from jeepney import MessageType
        reply = self.conn.send_and_get_reply(self._new_call(self._watcher, "RegisterStatusNotifierItem", "s",
                                                            (self.name,)))
        return reply.header.message_type == MessageType.method_return

    def _layout_updated(self, revision: int) -> None:
        from jeepney import DBusAddress, new_signal
        self.conn.send(new_signal(DBusAddress(MENU_PATH, interface=MENU_IFACE), "LayoutUpdated", "ui", (revision, 0)))

    def _loop(self) -> None:
        from jeepney import HeaderFields, MessageType, new_error, new_method_return
        while True:
            try:
                msg = self.conn.receive()
            except Exception as exc:  # noqa: BLE001 - the bus went away: the tray goes, the app stays
                print(f"tray stopped: {exc}")
                return
            fields = msg.header.fields
            if msg.header.message_type == MessageType.signal:
                if fields.get(HeaderFields.member) == "NameOwnerChanged" and msg.body[0] == WATCHER and msg.body[2]:
                    try:                                 # the desktop's tray came back: no wait for the
                        self.conn.send(self._new_call(self._watcher, "RegisterStatusNotifierItem", "s",
                                                      (self.name,)))   # answer here, it would eat calls
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if msg.header.message_type != MessageType.method_call:
                continue
            try:
                reply = answer(self.item, self.menu, fields.get(HeaderFields.path, ""),
                               fields.get(HeaderFields.interface, ""), fields.get(HeaderFields.member, ""),
                               msg.body)
                if reply is None:
                    self.conn.send(new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod"))
                else:
                    self.conn.send(new_method_return(msg, *reply))
            except Exception as exc:  # noqa: BLE001 - one bad call never ends the tray
                try:
                    self.conn.send(new_error(msg, "org.freedesktop.DBus.Error.Failed", "s", (str(exc),)))
                except Exception:  # noqa: BLE001
                    return
