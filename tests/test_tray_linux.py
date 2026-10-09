"""The Linux tray icon's answers to the desktop, without a bus."""
import pytest

from openachievements import tray_linux as tl


class Autostart:
    def __init__(self):
        self.on = False

    def autostart_enabled(self):
        return self.on

    def set_autostart(self, on):
        self.on = on


def made():
    calls = []
    auto = Autostart()
    item = tl.Item(lambda: calls.append("open"), [(1, 1, b"\xff\x00\x00\x00")])
    menu = tl.Menu(lambda: calls.append("open"), lambda: calls.append("quit"), auto)
    return item, menu, calls, auto


def test_the_item_says_what_it_is_and_points_at_its_menu():
    item, menu, _calls, _auto = made()
    sig, (props,) = tl.answer(item, menu, tl.ITEM_PATH, tl.PROPS_IFACE, "GetAll", (tl.ITEM_IFACE,))
    assert sig == "a{sv}" and props["Id"] == ("s", "greycell-achievements") and props["Menu"] == ("o", "/MenuBar")
    assert props["IconPixmap"][0] == "a(iiay)" and props["Status"] == ("s", "Active")
    assert tl.answer(item, menu, tl.ITEM_PATH, tl.PROPS_IFACE, "Get", (tl.ITEM_IFACE, "Title")) == \
        ("v", (("s", "Greycell Achievements"),))


def test_clicking_the_icon_opens_the_library():
    item, menu, calls, _auto = made()
    assert tl.answer(item, menu, tl.ITEM_PATH, tl.ITEM_IFACE, "Activate", (0, 0)) == ("", ())
    assert calls == ["open"]


def test_the_menu_opens_toggles_start_at_login_and_quits():
    item, menu, calls, auto = made()
    revisions = []
    menu.changed = revisions.append
    sig, (revision, (root, _props, children)) = tl.answer(item, menu, tl.MENU_PATH, tl.MENU_IFACE, "GetLayout",
                                                          (0, -1, []))
    labels = [c[1][1].get("label", ("s", "-"))[1] for c in children]
    assert sig == "u(ia{sv}av)" and root == 0 and labels == ["Open Greycell Achievements", "Start at login", "-", "Quit"]
    assert children[1][1][1]["toggle-state"] == ("i", 0)
    tl.answer(item, menu, tl.MENU_PATH, tl.MENU_IFACE, "Event", (tl.Menu.AUTOSTART, "clicked", ("i", 0), 0))
    assert auto.on and revisions == [revision + 1]
    _sig, (rows,) = tl.answer(item, menu, tl.MENU_PATH, tl.MENU_IFACE, "GetGroupProperties",
                              ([tl.Menu.AUTOSTART], ["toggle-state"]))
    assert rows == [(tl.Menu.AUTOSTART, {"toggle-state": ("i", 1)})]
    tl.answer(item, menu, tl.MENU_PATH, tl.MENU_IFACE, "Event", (tl.Menu.OPEN, "hovered", ("i", 0), 0))
    assert calls == []                                                   # only clicks act
    tl.answer(item, menu, tl.MENU_PATH, tl.MENU_IFACE, "EventGroup", ([(tl.Menu.QUIT, "clicked", ("i", 0), 0)],))
    assert calls == ["quit"]


def test_without_a_start_at_login_control_the_menu_leaves_it_out():
    menu = tl.Menu(lambda: None, lambda: None, None)
    _rev, (_root, _props, children) = menu.layout()
    assert [c[1][0] for c in children] == [tl.Menu.OPEN, tl.Menu.SEPARATOR, tl.Menu.QUIT]


def test_unknown_calls_and_paths_are_refused_and_introspection_answers():
    item, menu, _calls, _auto = made()
    assert tl.answer(item, menu, "/Elsewhere", tl.ITEM_IFACE, "Activate", (0, 0)) is None
    assert tl.answer(item, menu, tl.ITEM_PATH, tl.ITEM_IFACE, "Explode", ()) is None
    xml = tl.answer(item, menu, tl.MENU_PATH, "org.freedesktop.DBus.Introspectable", "Introspect", ())[1][0]
    assert "com.canonical.dbusmenu" in xml and "GetLayout" in xml


def test_the_icon_is_argb_in_network_order(tmp_path):
    Image = pytest.importorskip("PIL.Image")          # bundled only in the Linux build
    path = tmp_path / "icon.png"
    Image.new("RGBA", (4, 4), (10, 20, 30, 255)).save(path)
    size, _h, data = tl.pixmaps(path, sizes=(2,))[0]
    assert size == 2 and data[:4] == bytes([255, 10, 20, 30]) and len(data) == 16
    assert tl.pixmaps(tmp_path / "missing.png") == []
