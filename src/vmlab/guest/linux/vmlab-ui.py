#!/usr/bin/env python3
"""vmlab-ui for Linux Guests: the Linux side of vmlab's UI contract.

vmlab sends this file to `python3 - COMMAND JSON` on stdin with every call, so
it is never out of step with the Host. It prints one JSON object; on failure it
exits 1 with a message on stderr. The Host maps native roles to vmlab's
cross-OS roles and does all matching (vmlab.ui); this helper only reads the
tree and acts at screen coordinates. Commands and parameters are those of the
macOS helper (guest/macos/vmlab-ui.swift).

The session type, X11 or Wayland, is asked of logind at every call, and the
session's environment (DISPLAY, WAYLAND_DISPLAY, ...) is taken from systemd's
user manager, into which the session imports it: a Channel's own login has none.

- The tree comes from AT-SPI in both sessions.
- X11: windows from the window manager (python3-xlib), input through XTEST
  (xdotool, which also types characters the keyboard layout lacks), the
  clipboard through xclip.
- Wayland (GNOME): a client can neither learn where windows are nor move the
  pointer to a point, so vmlab's GNOME Shell extension (shell-extension/) does
  that, and input and the clipboard go through it too.
- Tray menus, in both sessions, over D-Bus (Tray): panels keep them out of AT-SPI.
- Notifications, in both sessions, from the file vmlab's recorder (a systemd
  user unit provisioning installs: notification-recorder.py) writes: Linux keeps
  no history of them.

Traps (README: "Linux Guests with VMware Fusion"):
- Toolkits disagree about coordinates. GTK 4 reports every element at (0, 0) in
  screen coordinates, in either session; GTK 3 on Wayland reports them relative
  to its window, shadow included. So every element's position is read relative
  to its window, and the window's position comes from the window manager:
  whichever of its rectangles, with or without the client-side shadow, has the
  size the toolkit reports for the window.
- Hidden widgets (a tab in the background, a hidden window) stay in the tree
  without the VISIBLE or SHOWING state; they are left out. But a WebKitGTK page
  (a Tauri app's) sits under containers without VISIBLE while it is on
  screen: under such a container, what is SHOWING is kept.
- A busy editor (on a loaded Guest) takes stage-text's select-all before the new
  document's view has focus or its text, and a close before its save is done
  as "close without saving?". So select-all is pressed again until the view on
  screen that holds the text has it selected, and close-staged closes once the
  file holds what the view shows (or after SAVE_WAIT).
- Registrations of apps that no longer answer would cost AT-SPI's default
  timeout each: timeouts are short.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

VERSION = 1
EXTENSION_VERSION = 1  # of shell-extension/, installed at provisioning
MAX_NODES = 5000
MAX_TEXT = 100000  # characters of an element's text
ATSPI_TIMEOUT_MS = 500
POLL = 0.1
SESSION_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP")
REPROVISION = "re-provision the Base guest: vmlab base create NAME --reprovision"
SELECT_ALL = ("a", ["ctrl"])
SELECT_AGAIN = 0.5  # s between select-alls while the Staged text is not selected
SAVE, CLOSE = ("s", ["ctrl"]), ("w", ["ctrl"])
STAGE = "vmlab-stage-"  # + 8 hex digits: the name of every file stage-text opens
STAGED = re.compile(STAGE + "[0-9a-fA-F]{8}\\.txt")
STAGING = "/tmp"  # where stage-text writes them
CLOSE_WAIT = 5  # s for one staged document to close
SAVE_WAIT = 10  # s for its save to reach the file before it is closed
NOTIFICATIONS = "~/.cache/vmlab/notifications.jsonl"  # what the recorder writes, a JSON line per Notification

# linux/input-event-codes.h, for the Wayland session
EVDEV = dict(
    {c: n for c, n in zip("qwertyuiop", range(16, 26))},
    **{c: n for c, n in zip("asdfghjkl", range(30, 39))},
    **{c: n for c, n in zip("zxcvbnm", range(44, 51))},
    **{str(d): 2 + (d - 1) % 10 for d in range(10)},
    **{"f%d" % n: 58 + n for n in range(1, 11)},
    f11=87, f12=88, space=57, enter=28, tab=15, escape=1, backspace=14, delete=111, up=103, down=108,
    left=105, right=106, home=102, end=107, pageup=104, pagedown=109, minus=12, equal=13, comma=51,
    period=52, slash=53, semicolon=39, quote=40, backslash=43, grave=41, leftbracket=26, rightbracket=27,
    ctrl=29, alt=56, shift=42, cmd=125,
)  # fmt: skip
# X keysym names, for xdotool; letters and digits are their own names
KEYSYMS = dict(
    {"f%d" % n: "F%d" % n for n in range(1, 13)},
    space="space", enter="Return", tab="Tab", escape="Escape", backspace="BackSpace", delete="Delete",
    up="Up", down="Down", left="Left", right="Right", home="Home", end="End", pageup="Prior",
    pagedown="Next", minus="minus", equal="equal", comma="comma", period="period", slash="slash",
    semicolon="semicolon", quote="apostrophe", backslash="backslash", grave="grave",
    leftbracket="bracketleft", rightbracket="bracketright", ctrl="ctrl", alt="alt", shift="shift", cmd="super",
)  # fmt: skip


def fail(message):
    sys.stderr.write("vmlab-ui: %s\n" % message)
    sys.exit(1)


def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")


def output(argv, timeout=10):
    """stdout of argv, or None if it failed or is missing."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def run(argv, what, timeout=30):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        fail("%s is not installed; %s" % (argv[0], REPROVISION))
    except subprocess.TimeoutExpired:
        fail("%s did not finish within %ss" % (what, timeout))
    if result.returncode:
        fail("%s failed: %s" % (what, (result.stderr or result.stdout).strip() or "exit %d" % result.returncode))
    return result.stdout


def session():
    """"x11" or "wayland": the type of the user's graphical session, whose environment is then in os.environ."""
    uid = os.getuid()
    os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % uid)
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=%s/bus" % os.environ["XDG_RUNTIME_DIR"])
    sid = (output(["loginctl", "show-user", str(uid), "-p", "Display", "--value"]) or "").strip()
    kind = (output(["loginctl", "show-session", sid, "-p", "Type", "--value"]) or "").strip() if sid else ""
    if kind not in ("x11", "wayland"):
        fail("no graphical session is logged in (logind: %s); wait for the desktop, e.g. `vmlab up`" % (kind or "none"))
    env = {}
    for line in (output(["systemctl", "--user", "show-environment"]) or "").splitlines():
        key, _, value = line.partition("=")
        if key in SESSION_KEYS:
            env[key] = value
    # After a session change the user manager may still hold the previous session's values.
    if env.get("XDG_SESSION_TYPE") != kind:
        fail("the %s session has not published its environment yet; wait for the desktop" % kind)
    os.environ.update(env)
    return kind


def staged_path(path):
    """The name of the Staged document at path; fails unless it is a file stage-text writes."""
    name = os.path.basename(path)
    if not STAGED.fullmatch(name) or os.path.dirname(os.path.realpath(path)) != os.path.realpath(STAGING):
        fail("%s is not a Staged document: stage-text writes them to %s as %sXXXXXXXX.txt" % (path, STAGING, STAGE))
    return name


def names(text, name):
    """Does text (a title, a tab's name) name the document name? Its name without .txt, and no more
    hex digits after it, so another Staged document's name never counts."""
    return re.search(re.escape(name[: -len(".txt")]) + "(?![0-9a-fA-F])", text) is not None


def process_name(pid):
    try:
        with open("/proc/%d/comm" % pid) as f:
            return f.read().strip()
    except OSError:
        return None


class Tray:
    """Tray menus over D-Bus, in either Desktop session: an app's Tray icon is a StatusNotifierItem
    registered with the session's StatusNotifierWatcher, and its Tray menu is the com.canonical.dbusmenu
    object the item names, which is what panels draw it from. Panels keep both out of AT-SPI."""

    WATCHER = ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher")
    MENU = "com.canonical.dbusmenu"

    def __init__(self):
        from gi.repository import Gio, GLib

        self.Gio, self.GLib = Gio, GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def call(self, dest, path, iface, method, args=None, reply=None):
        GLib = self.GLib
        return self.bus.call_sync(
            dest, path, iface, method, args, GLib.VariantType(reply) if reply else None, self.Gio.DBusCallFlags.NONE, 5000, None
        ).unpack()

    def prop(self, dest, path, iface, name):
        return self.call(dest, path, "org.freedesktop.DBus.Properties", "Get", self.GLib.Variant("(ss)", (iface, name)), "(v)")[0]

    def has_watcher(self):
        args = self.GLib.Variant("(s)", (self.WATCHER[0],))
        return self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner", args, "(b)")[0]

    def icons(self):
        """[(dest, path, pid)] of every registered StatusNotifierItem whose owner still answers."""
        found = []
        for item in self.prop(*self.WATCHER, "RegisteredStatusNotifierItems"):
            # ":1.42/org/ayatana/NotificationItem/x" (libayatana), ":1.42@/path", or a bus name alone:
            # the bus name ends at the first "/" or "@".
            cut = min(i for i in (item.find("/"), item.find("@"), len(item)) if i >= 0)
            dest, path = item[:cut], item[cut:].lstrip("@") or "/StatusNotifierItem"
            try:
                args = self.GLib.Variant("(s)", (dest,))
                pid = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetConnectionUnixProcessID", args, "(u)")[0]
            except Exception:  # its owner has gone
                continue
            found.append((dest, path, pid))
        return found

    def names(self, dest, path, pid):
        """What --app may call this icon's app: its process's name, program and command, the item's Id and Title."""
        found = [process_name(pid)]
        try:
            found.append(os.path.basename(os.readlink("/proc/%d/exe" % pid)))
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                found.append(os.path.basename(f.read().split(b"\0")[0].decode("utf-8", "replace")))
        except OSError:
            pass
        for key in ("Id", "Title"):
            try:
                found.append(self.prop(dest, path, "org.kde.StatusNotifierItem", key))
            except Exception:
                pass
        return {n.lower() for n in found if n}

    def accessible_pids(self, wanted):
        """The pids of the applications AT-SPI knows by the name wanted (lowercase), as the other commands match them."""
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            Atspi.set_timeout(ATSPI_TIMEOUT_MS, ATSPI_TIMEOUT_MS)
            desktop = Atspi.get_desktop(0)
            apps = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
            return {a.get_process_id() for a in apps if a is not None and (a.get_name() or "").lower() == wanted}
        except Exception:  # no accessibility bus: the other names still match
            return set()

    def layout(self, dest, menu, ident):
        """The item ident's children, raw: [(id, properties, children)]."""
        try:
            self.call(dest, menu, self.MENU, "AboutToShow", self.GLib.Variant("(i)", (ident,)), "(b)")
        except Exception:  # optional for the menu's owner
            pass
        _, root = self.call(dest, menu, self.MENU, "GetLayout", self.GLib.Variant("(iias)", (ident, -1, [])), "(u(ia{sv}av))")
        return root[2]

    def items(self, dest, menu, raw):
        """[(item in the contract's shape, its id, whether it has a submenu, that submenu's items as
        items() gives them)], without separators and hidden items."""
        out = []
        for ident, props, children in raw:
            if props.get("type") == "separator" or not props.get("visible", True):
                continue
            submenu = props.get("children-display") == "submenu" or bool(children)
            if submenu and not children:  # filled in when it is about to be shown
                children = self.layout(dest, menu, ident)
            item = {
                "name": re.sub(r"_(.)", r"\1", props.get("label", "")),  # without mnemonics: "_Open" -> "Open", "__" -> "_"
                "enabled": bool(props.get("enabled", True)),
                "checked": props.get("toggle-type") in ("checkmark", "radio") and props.get("toggle-state") == 1,
            }
            kids = self.items(dest, menu, children)
            item["children"] = [k[0] for k in kids]
            out.append((item, ident, submenu, kids))
        return out

    def run(self, params):
        wanted, path = (params.get("app") or "").lower(), params.get("choose") or []
        if not self.has_watcher():
            detail = "the Desktop session has no StatusNotifierWatcher, so its panel shows no Tray icons; see `vmlab doctor`"
            return emit({"icon": False, "detail": detail})
        icons = self.icons()
        icon = next((i for i in icons if wanted in self.names(*i)), None)
        if icon is None:
            pids = self.accessible_pids(wanted)
            icon = next((i for i in icons if i[2] in pids), None)
        if icon is None or params.get("icon_only"):  # icon_only: whether it is there, nothing more
            return emit({"icon": icon is not None})
        dest, item_path, _ = icon
        menu = self.prop(dest, item_path, "org.kde.StatusNotifierItem", "Menu")
        level = self.items(dest, menu, self.layout(dest, menu, 0))
        items = [i[0] for i in level]
        chosen = []
        for n, label in enumerate(path):
            found = next((i for i in level if i[0]["name"] == label), None)
            chosen.append(label)
            if found is None or not found[0]["enabled"]:
                return emit({"icon": True, "items": items, "chosen": None, "failed": {"at": chosen, "reason": "disabled" if found else "missing"}})
            if n + 1 < len(path) and not found[2]:
                return emit({"icon": True, "items": items, "chosen": None, "failed": {"at": chosen + [path[n + 1]], "reason": "leaf"}})
            if n + 1 == len(path):
                if found[2]:  # clicking it would choose nothing
                    return emit({"icon": True, "items": items, "chosen": None, "failed": {"at": chosen, "reason": "submenu"}})
                args = self.GLib.Variant("(isvu)", (found[1], "clicked", self.GLib.Variant("i", 0), 0))
                self.call(dest, menu, self.MENU, "Event", args)
            level = found[3]
        emit({"icon": True, "items": items, "chosen": chosen or None, "failed": None})


class Window:
    """A top-level window as the window manager sees it. frame is what shows on screen,
    buffer includes a client-side shadow; both (x, y, w, h) in screen coordinates."""

    def __init__(self, id, pid, title, frame, buffer, focused, hidden, background):
        self.id, self.pid, self.title = id, pid, title
        self.frame, self.buffer = tuple(frame), tuple(buffer)
        self.focused, self.hidden, self.background = focused, hidden, background

    def contains(self, x, y):
        fx, fy, fw, fh = self.frame
        return fx <= x < fx + fw and fy <= y < fy + fh


class WaylandShell:
    """GNOME on Wayland, through vmlab's Shell extension (org.vmlab.Shell)."""

    def __init__(self):
        from gi.repository import Gio, GLib

        self.GLib = GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.Gio = Gio
        try:
            version = self._call("Version")[0]
        except GLib.Error:
            fail("vmlab's GNOME Shell extension (vmlab-ui@vmlab) is not running; %s" % REPROVISION)
        if version != EXTENSION_VERSION:
            fail("vmlab's GNOME Shell extension is version %s, this vmlab needs %s; %s" % (version, EXTENSION_VERSION, REPROVISION))

    def _call(self, method, signature=None, *args, timeout_ms=30000):
        params = self.GLib.Variant("(%s)" % signature, args) if signature else None
        result = self.bus.call_sync(
            "org.vmlab.Shell", "/org/vmlab/Shell", "org.vmlab.Shell", method, params, None,
            self.Gio.DBusCallFlags.NO_AUTO_START, timeout_ms, None,
        )  # fmt: skip
        return result.unpack() if result is not None else ()

    def describe(self):
        return "vmlab GNOME Shell extension %d" % EXTENSION_VERSION

    def state(self):
        """(screen (w, h), windows bottom to top)."""
        data = json.loads(self._call("Windows")[0])
        # Meta.WindowType: 1 desktop, 2 dock
        windows = [
            Window(w["id"], w["pid"], w["title"], w["frame"], w["buffer"], w["focused"], w["hidden"], w.get("type") in (1, 2))
            for w in data["windows"]
        ]
        return tuple(data["screen"]), windows

    def activate(self, window):
        self._call("Activate", "t", window.id)

    def click(self, x, y):
        self._call("Click", "dd", float(x), float(y))

    def press(self, key, modifiers):
        self._call("Keys", "au", [EVDEV[m] for m in modifiers] + [EVDEV[key]])

    def type(self, text):
        self._call("Type", "s", text)

    def clipboard(self):
        return json.loads(self._call("ClipboardGet")[0])["text"]

    def set_clipboard(self, text):
        self._call("ClipboardSet", "s", text)


class X11:
    """An X11 session: the window manager's EWMH properties, xdotool and xclip."""

    def __init__(self):
        try:
            from Xlib import X, display
        except ImportError:
            fail("python3-xlib is not installed; %s" % REPROVISION)
        self.X = X
        try:
            self.display = display.Display()
        except Exception as exc:
            fail("cannot open the X display %s: %s" % (os.environ.get("DISPLAY"), exc))
        self.root = self.display.screen().root

    def describe(self):
        return "xdotool"

    def _atom(self, name):
        return self.display.intern_atom(name)

    def _prop(self, window, name, kind=0):
        try:
            value = window.get_full_property(self._atom(name), kind)
        except Exception:  # the window went away
            return None
        return value.value if value is not None else None

    def _title(self, window):
        title = self._prop(window, "_NET_WM_NAME", self._atom("UTF8_STRING"))
        if title is None:
            title = self._prop(window, "WM_NAME", self.X.AnyPropertyType)
        if isinstance(title, bytes):
            title = title.decode("utf-8", "replace")
        return title or ""

    def state(self):
        screen = self.display.screen()
        active = (self._prop(self.root, "_NET_ACTIVE_WINDOW") or [0])[0]
        background = {self._atom("_NET_WM_WINDOW_TYPE_DESKTOP"), self._atom("_NET_WM_WINDOW_TYPE_DOCK")}
        hidden_state = self._atom("_NET_WM_STATE_HIDDEN")
        windows = []
        for wid in self._prop(self.root, "_NET_CLIENT_LIST_STACKING") or []:
            window = self.display.create_resource_object("window", wid)
            try:
                geometry = window.get_geometry()
                origin = self.root.translate_coords(window, 0, 0)
                mapped = window.get_attributes().map_state == self.X.IsViewable
            except Exception:
                continue
            buffer = (origin.x, origin.y, geometry.width, geometry.height)
            left, right, top, bottom = list(self._prop(window, "_GTK_FRAME_EXTENTS") or [0, 0, 0, 0])[:4]
            frame = (origin.x + left, origin.y + top, geometry.width - left - right, geometry.height - top - bottom)
            types = set(self._prop(window, "_NET_WM_WINDOW_TYPE") or [])
            hidden = not mapped or hidden_state in set(self._prop(window, "_NET_WM_STATE") or [])
            pid = (self._prop(window, "_NET_WM_PID") or [0])[0]
            windows.append(Window(wid, pid, self._title(window), frame, buffer, wid == active, hidden, bool(types & background)))
        return (screen.width_in_pixels, screen.height_in_pixels), windows

    def activate(self, window):
        from Xlib.protocol import event

        # Source 2: a pager, which window managers obey without focus-stealing prevention.
        message = event.ClientMessage(
            window=self.display.create_resource_object("window", window.id),
            client_type=self._atom("_NET_ACTIVE_WINDOW"),
            data=(32, [2, self.X.CurrentTime, 0, 0, 0]),
        )
        self.root.send_event(message, event_mask=self.X.SubstructureRedirectMask | self.X.SubstructureNotifyMask)
        self.display.flush()

    def click(self, x, y):
        run(["xdotool", "mousemove", "--sync", str(x), str(y), "click", "1"], "xdotool click")

    def press(self, key, modifiers):
        chord = "+".join([KEYSYMS[m] for m in modifiers] + [KEYSYMS.get(key, key)])
        run(["xdotool", "key", "--clearmodifiers", chord], "xdotool key %s" % chord)

    def type(self, text):
        run(["xdotool", "type", "--delay", "8", "--", text], "xdotool type", timeout=30 + len(text) // 10)

    def clipboard(self):
        return output(["xclip", "-selection", "clipboard", "-o"]) or ""  # no owner: empty

    def owned(self):
        return output(["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"]) is not None

    def set_clipboard(self, text):
        if not text:
            return self.clear_clipboard()
        # xclip stays behind to own the selection; it must not hold this call's pipes.
        try:
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard", "-i"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )  # fmt: skip
        except FileNotFoundError:
            fail("xclip is not installed; %s" % REPROVISION)
        proc.communicate(text.encode("utf-8"))
        deadline = time.time() + 5
        while self.clipboard() != text:
            if time.time() >= deadline:
                fail("xclip did not take the clipboard")
            time.sleep(POLL)

    def clear_clipboard(self):
        """Leave the clipboard with no owner, as it is at login. Owning it with no
        text is something else: apps read that as text of an unexpected type."""
        try:
            # -quiet keeps xclip in the foreground, so ending it ends its ownership.
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard", "-i", "-quiet"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )  # fmt: skip
        except FileNotFoundError:
            fail("xclip is not installed; %s" % REPROVISION)
        proc.stdin.close()
        deadline = time.time() + 5
        while not self.owned() and time.time() < deadline:  # taken from whoever held it
            time.sleep(POLL)
        proc.terminate()
        proc.wait(timeout=5)
        while self.owned():
            if time.time() >= deadline:
                fail("the clipboard kept an owner")
            time.sleep(POLL)


class UI:
    def __init__(self, kind):
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        Atspi.set_timeout(ATSPI_TIMEOUT_MS, ATSPI_TIMEOUT_MS)
        self.Atspi = Atspi
        self.S = Atspi.StateType
        self.kind = kind
        self.ws = WaylandShell() if kind == "wayland" else X11()
        self.screen, self.windows = self.ws.state()
        self.nodes, self.truncated = 0, False

    def refresh(self):
        self.screen, self.windows = self.ws.state()

    # MARK: - Reading the tree

    def apps(self):
        """[(accessible, name, pid)] of every application AT-SPI knows."""
        desktop = self.Atspi.get_desktop(0)
        found = []
        for i in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(i)
                if app is not None:
                    found.append((app, app.get_name() or "", app.get_process_id()))
            except Exception:  # an app that stopped answering
                continue
        return found

    def matches(self, name, pid, wanted):
        wanted = wanted.lower()
        return name.lower() == wanted or (process_name(pid) or "").lower() == wanted

    def app_name(self, pid):
        for _, name, p in self.apps():
            if p == pid:
                return name
        return process_name(pid) or ""

    def frontmost(self):
        focused = [w for w in self.windows if w.focused]
        return self.app_name(focused[0].pid) if focused else ""

    def has(self, accessible, state):
        try:
            return accessible.get_state_set().contains(state)
        except Exception:
            return False

    def toplevels(self, app):
        """The app's windows (accessibles) that show on screen."""
        out = []
        try:
            count = app.get_child_count()
        except Exception:
            return out
        for i in range(count):
            try:
                window = app.get_child_at_index(i)
            except Exception:
                continue
            if window is not None and self.has(window, self.S.SHOWING):
                out.append(window)
        return out

    def children(self, accessible):
        try:
            count = accessible.get_child_count()
        except Exception:
            return
        for i in range(count):
            try:
                child = accessible.get_child_at_index(i)
            except Exception:
                continue
            if child is not None:
                yield child

    def shown_children(self, accessible):
        """The children that are on screen: the VISIBLE ones, and in place of a child
        without VISIBLE, whatever under it is SHOWING (a WebKitGTK page)."""
        for child in self.children(accessible):
            if self.has(child, self.S.VISIBLE):
                yield child
            else:
                yield from self.showing_within(child)

    def showing_within(self, hidden):
        for child in self.children(hidden):
            self.nodes += 1
            if self.nodes >= MAX_NODES:
                return
            if self.has(child, self.S.SHOWING):
                yield child
            else:
                yield from self.showing_within(child)

    def placement(self, window, pid):
        """(coordinate type, (dx, dy)) turning the window's elements' extents into screen points."""
        Atspi = self.Atspi
        try:
            ext = window.get_extents(Atspi.CoordType.WINDOW)
            title = window.get_name() or ""
        except Exception:
            return Atspi.CoordType.SCREEN, (0, 0)
        mine = [w for w in reversed(self.windows) if w.pid == pid and not w.hidden]
        for pool in ([w for w in mine if w.title == title], mine):
            for w in pool:
                for x, y, width, height in (w.frame, w.buffer):
                    if abs(width - ext.width) <= 2 and abs(height - ext.height) <= 2:
                        return Atspi.CoordType.WINDOW, (x - ext.x, y - ext.y)
        # A window the window manager does not know by this app and size: trust the toolkit.
        return Atspi.CoordType.SCREEN, (0, 0)

    def bounds(self, accessible, placement):
        kind, (dx, dy) = placement
        try:
            if "Component" not in accessible.get_interfaces():
                return None
            e = accessible.get_extents(kind)
        except Exception:
            return None
        if e.x <= -(1 << 30) or e.y <= -(1 << 30) or not (0 <= e.width < 1 << 16 and 0 <= e.height < 1 << 16):
            return None  # "no position", or garbage from a widget that is not laid out
        return {"x": e.x + dx, "y": e.y + dy, "w": e.width, "h": e.height}

    def text(self, accessible):
        Text = self.Atspi.Text
        count = Text.get_character_count(accessible)
        return Text.get_text(accessible, 0, min(count, MAX_TEXT)).replace("￼", "") if count else ""

    def node(self, accessible, placement, depth, max_depth):
        self.nodes += 1
        S = self.S
        try:
            native = accessible.get_role_name() or "unknown"
            name = accessible.get_name() or ""
            description = accessible.get_description() or None
            interfaces = accessible.get_interfaces()
            states = accessible.get_state_set()
        except Exception:
            return None
        node = {
            "native_role": native, "name": name, "value": None, "description": description,
            "bounds": self.bounds(accessible, placement), "focused": states.contains(S.FOCUSED),
            "enabled": states.contains(S.ENABLED), "children": [],
        }  # fmt: skip
        try:
            if "Text" in interfaces and native != "password text":
                text = self.text(accessible)
                if "EditableText" in interfaces or (text and text != name):
                    node["value"] = text
            elif "Value" in interfaces:
                value = self.Atspi.Value.get_current_value(accessible)
                node["value"] = ("%d" % value) if value == int(value) else ("%g" % value)
        except Exception:
            pass
        if native == "text":  # one AT-SPI role for labels, fields and text areas
            node["role"] = "textarea" if states.contains(S.MULTI_LINE) else "textfield" if states.contains(S.SINGLE_LINE) else "text"
        try:
            count = accessible.get_child_count()
        except Exception:
            count = 0
        if count and depth >= max_depth:
            self.truncated = True
            return node
        for child in self.shown_children(accessible):
            if self.nodes >= MAX_NODES:
                self.truncated = True
                break
            kid = self.node(child, placement, depth + 1, max_depth)
            if kid is not None:
                node["children"].append(kid)
        return node

    def tree(self, params):
        wanted = params.get("app")
        max_depth = params.get("depth") or 60
        focused_pid = next((w.pid for w in self.windows if w.focused), None)
        on_screen = {w.pid for w in self.windows if not w.hidden and not w.background}
        apps = []
        for app, name, pid in self.apps():
            if (self.matches(name, pid, wanted) if wanted else pid in on_screen) and self.nodes < MAX_NODES:
                windows = []
                for window in self.toplevels(app):
                    child = self.node(window, self.placement(window, pid), 1, max_depth)
                    if child is not None:
                        windows.append(child)
                apps.append({
                    "native_role": "application", "name": name, "value": None, "description": None, "bounds": None,
                    "focused": pid == focused_pid, "enabled": True, "pid": pid, "children": windows,
                })  # fmt: skip
        width, height = self.screen
        emit({
            "native_role": "desktop", "name": "", "value": None, "description": None,
            "bounds": {"x": 0, "y": 0, "w": width, "h": height}, "focused": False, "enabled": True,
            "truncated": self.truncated, "children": apps,
        })  # fmt: skip

    # MARK: - Acting

    def under(self, x, y):
        """[(label, bounds)] from the deepest element at the point up to its window, or None if no window is there."""
        window = next((w for w in reversed(self.windows) if not w.hidden and w.contains(x, y)), None)
        if window is None:
            return None
        for app, _, pid in self.apps():
            if pid != window.pid:
                continue
            for top in self.toplevels(app):
                placement = self.placement(top, pid)
                b = self.bounds(top, placement)
                if not b or not (b["x"] <= x < b["x"] + b["w"] and b["y"] <= y < b["y"] + b["h"]):
                    continue
                chain = [(self.label(top), b)]
                node = top
                for _ in range(200):
                    hit = None
                    for child in self.shown_children(node):
                        cb = self.bounds(child, placement)
                        if cb and cb["x"] <= x < cb["x"] + cb["w"] and cb["y"] <= y < cb["y"] + cb["h"]:
                            hit = (child, cb)  # the last child drawn there is on top
                    if hit is None:
                        break
                    node = hit[0]
                    chain.insert(0, (self.label(node), hit[1]))
                return chain
        return [(window.title, {"x": window.frame[0], "y": window.frame[1], "w": window.frame[2], "h": window.frame[3]})]

    def label(self, accessible):
        try:
            name = (accessible.get_name() or "").strip()
            if name:
                return name
            if "Text" in accessible.get_interfaces():
                return self.text(accessible).strip()
            return (accessible.get_description() or "").strip()
        except Exception:
            return ""

    def click(self, params):
        try:
            x, y = int(params["x"]), int(params["y"])
        except (KeyError, TypeError, ValueError):
            fail("click needs x and y")
        width, height = self.screen
        if not (0 <= x < width and 0 <= y < height):
            fail("(%d, %d) is off the screen (%dx%d)" % (x, y, width, height))
        chain = self.under(x, y)
        expect = params.get("expect")
        if expect is not None:
            # Refuse rather than click blind: an element scrolled out of view, or covered, still has bounds.
            wanted_label, wanted_bounds = expect.get("label") or "", expect.get("bounds")
            hit = bool(chain) and (
                (wanted_label and chain[0][0] == wanted_label)
                or (wanted_bounds is not None and any(b == wanted_bounds for _, b in chain[:12]))
                or (not wanted_label and wanted_bounds is None)
            )
            if not hit:
                what = '"%s"' % chain[0][0] if chain else "nothing"
                fail("something else is at (%d, %d): %s; the element may be covered or scrolled out of view" % (x, y, what))
        self.ws.click(x, y)
        emit({"x": x, "y": y, "under": chain[0][0] if chain else None})

    def press(self, key, modifiers):
        table = EVDEV if self.kind == "wayland" else KEYSYMS
        for name in list(modifiers) + [key]:
            if name not in table and not (self.kind == "x11" and len(name) == 1 and name.isalnum()):
                fail("unknown key %s" % name)
        self.ws.press(key, modifiers)

    def wait_for(self, deadline, what):
        while True:
            value = what()
            if value is not None:
                return value
            if time.time() >= deadline:
                return None
            time.sleep(POLL)

    def bring_to_front(self, window, app_name, deadline):
        def front():
            self.refresh()
            if any(w.focused and w.id == window.id for w in self.windows):
                return True
            self.ws.activate(window)
            return None

        if self.wait_for(deadline, front) is None:
            fail("%s did not come to the front in time; frontmost is %s" % (app_name, self.frontmost() or "nothing"))

    def focus(self, params):
        wanted = params.get("app") or fail("focus needs an app")
        deadline = time.time() + float(params.get("timeout") or 30)
        pids = {pid for _, name, pid in self.apps() if self.matches(name, pid, wanted)}
        pids |= {w.pid for w in self.windows if (process_name(w.pid) or "").lower() == wanted.lower()}
        windows = [w for w in self.windows if w.pid in pids and not w.background]
        if not windows:
            fail("%s is not running" % wanted if not pids else "%s has no windows" % wanted)
        raised = None
        if "window" in params:
            titled = [w for w in windows if params["window"] in w.title]
            if not titled:
                fail('%s has no window titled like "%s"; its windows: %s' % (wanted, params["window"], [w.title for w in windows]))
            target, raised = titled[-1], titled[-1].title
        else:
            target = windows[-1]
        name = self.app_name(target.pid)
        self.bring_to_front(target, name, deadline)
        emit({"app": name, "window": raised, "frontmost": self.frontmost()})

    def on_screen(self, accessible):
        """The descendants of accessible that are on screen, depth first: a tab in the background
        keeps its text view, and may keep it FOCUSED, but not SHOWING."""
        self.nodes = 0  # showing_within's count; each read walks afresh
        stack, seen = [accessible], 0
        while stack and seen < MAX_NODES:
            node = stack.pop()
            seen += 1
            yield node
            stack.extend(reversed([c for c in self.shown_children(node) if self.has(c, self.S.SHOWING)]))

    def selected(self, accessible):
        """The text selected in a text element, or None."""
        Text = self.Atspi.Text
        try:
            if not Text.get_n_selections(accessible):
                return None
            r = Text.get_selection(accessible, 0)
            return Text.get_text(accessible, r.start_offset, r.end_offset)
        except Exception:
            return None

    def selection(self, pid):
        """The selected text in the app's focused element on screen, or None."""
        for app, _, p in self.apps():
            if p != pid:
                continue
            for top in self.toplevels(app):
                for node in self.on_screen(top):
                    try:
                        if node.get_state_set().contains(self.S.FOCUSED) and "Text" in node.get_interfaces():
                            return self.selected(node)
                    except Exception:
                        continue
        return None

    def views(self, window):
        """[(text view, its text)] on screen in the window: the tab in front's, not a background tab's."""
        found = []
        for app, _, pid in self.apps():
            if pid != window.pid:
                continue
            tops = self.toplevels(app)
            for top in [t for t in tops if (t.get_name() or "") == window.title] or (tops if len(tops) == 1 else []):
                for node in self.on_screen(top):
                    try:
                        if "EditableText" in node.get_interfaces():
                            found.append((node, self.text(node)))
                    except Exception:
                        continue
        return found

    def front_view(self, window):
        """(The document's text view on screen in the window, a multi-line one first, its text), or None."""
        views = self.views(window)
        return next((v for v in views if self.has(v[0], self.S.MULTI_LINE)), views[0] if views else None)

    def document_view(self, window, text):
        """The text view on screen in the window that holds exactly text (a Staged document's own
        view, once the editor has loaded the file into it), or None."""
        return next((view for view, shown in self.views(window) if shown == text), None)

    def page_tabs(self, top):
        """The page tabs under a window's accessible (an editor's documents)."""
        stack, seen, tabs = [top], 0, []
        while stack and seen < MAX_NODES:
            node = stack.pop()
            seen += 1
            try:
                if node.get_role() == self.Atspi.Role.PAGE_TAB:
                    tabs.append(node)
                    continue
            except Exception:
                continue
            stack.extend(self.children(node))
        return tabs

    def staged_document(self, wanted, name):
        """(Window, its accessible, page tab or None) showing the Staged document name in the app, or
        None. A tab whose description is a path (gnome-text-editor's: its tooltip) must be the
        document's; otherwise the file's name, whose random hex digits are one stage-text's, tells."""
        tops = {pid: self.toplevels(app) for app, n, pid in self.apps() if self.matches(n, pid, wanted)}
        for window in reversed(self.windows):
            if window.pid not in tops or window.background:
                continue
            mine = tops[window.pid]
            top = next((t for t in mine if (t.get_name() or "") == window.title), mine[0] if len(mine) == 1 else None)
            if top is None:
                continue
            tabs = self.page_tabs(top)
            for tab in tabs:
                if names(tab.get_name() or "", name) and self.tab_path(tab) in (None, os.path.join(os.path.realpath(STAGING), name)):
                    return window, top, tab
            if not tabs and names(window.title, name):
                return window, top, None
        return None

    def tab_path(self, tab):
        """The file a tab's description names (symlinks resolved), or None if it names none."""
        try:
            description = tab.get_description() or ""
        except Exception:
            return None
        return os.path.realpath(description) if description.startswith("/") else None

    def select_tab(self, window, top, tab, shown, deadline):
        """Bring a tab to the front of its window: keys go to the tab in front, the one the window's
        title names. It is clicked (GTK's tabs take no action). False if shown(the window's title)
        does not hold in time."""
        b = self.bounds(tab, self.placement(top, window.pid))
        if b:
            self.ws.click(b["x"] + b["w"] // 3, b["y"] + b["h"] // 2)  # its title, clear of its close button

        def in_front():
            self.refresh()
            return True if any(w.id == window.id and shown(w.title) for w in self.windows) else None

        return self.wait_for(deadline, in_front) is not None

    def close_staged(self, params):
        """Save and close the Staged document params["file"] in params["app"]; the app's other
        documents stay, and the tab in front of its window before comes back to the front. Saved
        first (a Scenario may have typed into it), so the editor does not ask about saving."""
        path, app = params.get("file"), params.get("app")
        if not path or not app:
            fail("close-staged needs file and app")
        name = staged_path(path)
        deadline = time.time() + float(params.get("timeout") or 30)
        self.refresh()
        found = self.staged_document(app, name)
        if found is None:
            emit({"file": path, "closed": False})
            return
        window, top, tab = found
        # The tab in front of that window: its name is in the window's title.
        before = next((n for n in (t.get_name() or "" for t in self.page_tabs(top)) if n and n in window.title and not names(n, name)), None)
        while tab is not None:
            self.bring_to_front(window, app, deadline)
            if self.select_tab(window, top, tab, lambda title: names(title, name), min(deadline, time.time() + CLOSE_WAIT)):
                break
            if time.time() >= deadline:
                fail("%s did not bring %s to the front of its window in time; the window: %s" % (app, name, window.title))
            self.refresh()
            found = self.staged_document(app, name)
            if found is None:  # closed meanwhile
                emit({"file": path, "closed": False})
                return
            window, top, tab = found
        else:
            self.bring_to_front(window, app, deadline)
        self.refresh()
        window = next((w for w in self.windows if w.id == window.id), window)  # its title now names the document
        view = self.front_view(window)
        typed = view[1] if view is not None and len(view[1]) < MAX_TEXT else None  # None: cannot tell when it is saved
        self.press(*SAVE)
        if typed is not None:
            # A close before the save is done asks about saving (on a busy Guest): wait for the file.
            # An editor that changes the text as it saves (trims, CRLF) never matches: close anyway.
            staged = os.path.join(STAGING, name)

            def saved():
                try:
                    with open(staged, encoding="utf-8", errors="replace") as f:
                        return True if f.read() in (typed, typed + "\n") else None
                except OSError:
                    return None

            self.wait_for(min(deadline, time.time() + SAVE_WAIT), saved)
        self.press(*CLOSE)

        def closed():
            self.refresh()
            return None if self.staged_document(app, name) else True

        if self.wait_for(deadline, closed) is None:
            fail("%s did not close %s in time; its windows: %s" % (app, name, [w.title for w in self.windows if w.pid == window.pid]))
        if before and not any(w.id == window.id and before in w.title for w in self.windows):
            back = next((b for b in self.page_tabs(top) if (b.get_name() or "") == before), None)
            if back is not None:
                self.select_tab(window, top, back, lambda title: before in title, min(deadline, time.time() + CLOSE_WAIT))
        emit({"file": path, "closed": True})

    def stage_text(self, params):
        text, app = params.get("text"), params.get("app")
        if text is None or not app:
            fail("stage-text needs text and app")
        deadline = time.time() + float(params.get("timeout") or 30)
        doc = STAGE + uuid.uuid4().hex[:8]
        path = os.path.join(STAGING, doc + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            # Its own session and no pipes: the editor outlives this call.
            subprocess.Popen([app, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            fail("cannot start %s: %s; is it installed?" % (app, exc))

        def shown():
            self.refresh()
            return next((w for w in reversed(self.windows) if doc in w.title), None)

        window = self.wait_for(deadline, shown)
        if window is None:
            fail("%s showed no window for %s in time; windows: %s" % (app, os.path.basename(path), [w.title for w in self.windows]))
        name = self.wait_for(deadline, lambda: next((n for _, n, p in self.apps() if p == window.pid), None)) or app
        self.bring_to_front(window, name, deadline)
        # The editor may take the keys before the new tab's view has focus, or before the file is
        # loaded into it (on a busy Guest): a select-all then selects nothing, so it is pressed
        # again until the document's own view has its text selected.
        pressed_at = []

        def read():
            view = self.document_view(window, text)
            return self.selected(view) if view is not None else self.selection(window.pid)

        def select():
            if read() == text:
                return text
            if not pressed_at or time.time() - pressed_at[-1] >= SELECT_AGAIN:
                self.bring_to_front(window, name, deadline)
                self.press(*SELECT_ALL)
                pressed_at.append(time.time())
            return None

        if text:
            selected = self.wait_for(deadline, select)
            if selected is None:
                selected = read()
        else:  # an empty document: select-all as elsewhere, but no selection to wait for
            self.press(*SELECT_ALL)
            selected = ""
        self.refresh()
        frontmost = self.frontmost()  # what the trigger lands on, recorded before it is pressed
        pressed = None
        then = params.get("then")
        if then:
            self.press(then["key"], then.get("modifiers") or [])
            pressed = {"key": then["key"], "modifiers": then.get("modifiers") or []}
        emit({"app": name, "file": path, "frontmost": frontmost, "selected": selected, "pressed": pressed})


def notifications():
    """Every Notification the recorder wrote down, and the Guest's "now"."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    try:
        with open(os.path.expanduser(NOTIFICATIONS), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        fail("no Notification recorder has run in this Guest (%s is missing); %s" % (NOTIFICATIONS, REPROVISION))
    found = []
    for line in lines:
        try:
            found.append(json.loads(line))
        except ValueError:
            continue  # the line the recorder is writing
    emit({"now": now, "notifications": found})


def main(argv):
    if len(argv) < 2:
        fail("usage: vmlab-ui COMMAND [JSON]")
    command = argv[1]
    try:
        params = json.loads(argv[2]) if len(argv) > 2 else {}
    except ValueError:
        fail("parameters are not JSON: %s" % argv[2])
    if command == "notifications":
        return notifications()
    kind = session()
    if command == "tray":
        return Tray().run(params)
    ui = UI(kind)
    if command == "version":
        emit({"helper": "linux", "version": VERSION, "session": kind, "input": ui.ws.describe(), "trusted": True})
    elif command == "tree":
        ui.tree(params)
    elif command == "click":
        ui.click(params)
    elif command == "press":
        ui.press(params.get("key"), params.get("modifiers") or [])
        emit({"key": params.get("key"), "modifiers": params.get("modifiers") or []})
    elif command == "type":
        text = params.get("text")
        if text is None:
            fail("type needs text")
        ui.ws.type(text)
        emit({"typed": len(text)})
    elif command == "clipboard":
        if "set" in params:
            ui.ws.set_clipboard(params["set"])
        emit({"text": ui.ws.clipboard()})
    elif command == "focus":
        ui.focus(params)
    elif command == "stage-text":
        ui.stage_text(params)
    elif command == "close-staged":
        ui.close_staged(params)
    else:
        fail("unknown command %s" % command)


if __name__ == "__main__":
    main(sys.argv)
