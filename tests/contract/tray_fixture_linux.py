"""A Tray icon for the contract tests, in a Linux Guest: a StatusNotifierItem with a dbusmenu.

    python3 tray_fixture_linux.py RECORD

Its Tray menu: Open, a separator, Settings (a submenu: Advanced, and Dark mode,
checked), Pinned, checked, and Update, disabled. Choosing an item appends its label (with its
mnemonic) to the file RECORD. python3 and its Gio bindings only: the Guest
installs nothing. Run it under a name of its own (a link to python3) so it is
the app by that name.
"""

import os
import sys
import warnings

from gi.repository import Gio, GLib

RECORD = sys.argv[1]
warnings.simplefilter("ignore", DeprecationWarning)  # register_object, the call PyGObject still offers everywhere
ITEM_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"
ITEM_XML = """
<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewIcon"/>
  <signal name="NewStatus"><arg type="s"/></signal>
</interface></node>
"""
MENU_XML = """
<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
    <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
  </method>
  <method name="Event">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/>
  </method>
  <method name="EventGroup">
    <arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/>
  </method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup">
    <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  <signal name="ItemActivationRequested"><arg type="i"/><arg type="u"/></signal>
</interface></node>
"""

# id: (properties, child ids)
MENU = {
    0: ({"children-display": GLib.Variant("s", "submenu")}, [1, 2, 3, 7, 6]),
    1: ({"label": GLib.Variant("s", "_Open")}, []),
    2: ({"type": GLib.Variant("s", "separator")}, []),
    3: ({"label": GLib.Variant("s", "_Settings"), "children-display": GLib.Variant("s", "submenu")}, [4, 5]),
    4: ({"label": GLib.Variant("s", "_Advanced")}, []),
    5: ({"label": GLib.Variant("s", "_Dark mode"), "toggle-type": GLib.Variant("s", "checkmark"), "toggle-state": GLib.Variant("i", 1)}, []),
    6: ({"label": GLib.Variant("s", "_Update"), "enabled": GLib.Variant("b", False)}, []),
    7: ({"label": GLib.Variant("s", "_Pinned"), "toggle-type": GLib.Variant("s", "checkmark"), "toggle-state": GLib.Variant("i", 1)}, []),
}


def layout(ident, depth):
    props, children = MENU[ident]
    kids = [] if depth == 0 else [GLib.Variant("(ia{sv}av)", layout(c, depth - 1)) for c in children]
    return (ident, props, kids)


def on_menu_call(conn, sender, path, iface, method, args, invocation):
    if method == "GetLayout":
        parent, depth, _ = args.unpack()
        invocation.return_value(GLib.Variant("(u(ia{sv}av))", (1, layout(parent, depth))))
    elif method == "GetGroupProperties":
        ids, _ = args.unpack()
        invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, MENU[i][0]) for i in ids if i in MENU],)))
    elif method == "GetProperty":
        ident, name = args.unpack()
        invocation.return_value(GLib.Variant("(v)", (MENU[ident][0][name],)))
    elif method == "Event":
        ident, event = args.unpack()[:2]
        props = MENU.get(ident, ({}, []))[0]
        if event == "clicked" and "label" in props and props.get("enabled", GLib.Variant("b", True)).unpack():
            with open(RECORD, "a", encoding="utf-8") as f:
                f.write(props["label"].unpack() + "\n")
        invocation.return_value(None)
    elif method == "EventGroup":
        invocation.return_value(GLib.Variant("(ai)", ([],)))
    elif method == "AboutToShow":
        invocation.return_value(GLib.Variant("(b)", (False,)))
    elif method == "AboutToShowGroup":
        invocation.return_value(GLib.Variant("(aiai)", ([], [])))


def on_menu_property(conn, sender, path, iface, name):
    return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}[name]  # fmt: skip


def on_item_call(conn, sender, path, iface, method, args, invocation):
    invocation.return_value(None)


def on_item_property(conn, sender, path, iface, name):
    return {"Category": GLib.Variant("s", "ApplicationStatus"), "Id": GLib.Variant("s", "vmlab-tray-fixture"),
            "Title": GLib.Variant("s", "vmlab tray fixture"), "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", "dialog-information"), "ItemIsMenu": GLib.Variant("b", True),
            "Menu": GLib.Variant("o", MENU_PATH)}[name]  # fmt: skip


bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
bus.register_object(ITEM_PATH, Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0], on_item_call, on_item_property, None)
bus.register_object(MENU_PATH, Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0], on_menu_call, on_menu_property, None)
name = "org.kde.StatusNotifierItem-%d-1" % os.getpid()
bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
              GLib.Variant("(su)", (name, 4)), None, Gio.DBusCallFlags.NONE, 5000, None)  # fmt: skip
bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
              GLib.Variant("(s)", (name,)), None, Gio.DBusCallFlags.NONE, 5000, None)  # fmt: skip
print("registered %s" % name, flush=True)
GLib.MainLoop().run()
