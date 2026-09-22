"""The Guest UI contract: the same commands and JSON shapes on every OS.

Each OS has a helper in the Guest that reads the accessibility tree and acts at
coordinates (Provider.ui_call). Everything above that lives here, once:
mapping native roles to cross-OS roles, filling in the node shape, finding
elements, parsing key chords and waiting for conditions.

Every node, on every OS:

    {"role": "button", "name": "Run", "value": null, "description": null,
     "bounds": {"x": 220, "y": 350, "w": 60, "h": 20} or null,
     "focused": false, "enabled": true, "native_role": "AXButton",
     "children": [...]}

The root has role "desktop" and "truncated" (true when a size limit cut the
tree short); its children are applications (with "pid"), whose children are
their windows and tray (status) items.
"""

import re
import time

from vmlab.config import UsageError
from vmlab.providers.base import GuestError, GuestTimeout

NODE_DEFAULTS = {"name": "", "value": None, "description": None, "bounds": None, "focused": False, "enabled": True}
POLL_SECONDS = 0.25  # between checks of a wait_for condition; the condition and timeout decide when it ends
EXTRA_KEYS = ("native_subrole", "bundle_id")
STAGE_MARGIN = 5  # s a helper that waits (focus, stage-text) gives up before its call would be killed
# sh: $1 with a leading ~ expanded to the Guest user's home, as $p
EXPAND_TILDE = 'p=$1; case $p in "~"|"~/"*) p="$HOME${p#"~"}";; esac; '

STAGE_APPS = {"macos": "TextEdit", "windows": "Notepad", "linux": "gnome-text-editor"}

# Native roles to cross-OS roles. Anything unlisted becomes its native role, lowercased, without spaces or "AX".
MACOS_ROLES = {
    "AXApplication": "application",
    "AXWindow": "window",
    "AXSheet": "dialog",
    "AXButton": "button",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radiobutton",
    "AXTextField": "textfield",
    "AXSecureTextField": "textfield",
    "AXSearchField": "textfield",
    "AXTextArea": "textarea",
    "AXStaticText": "text",
    "AXLink": "link",
    "AXImage": "image",
    "AXMenuBar": "menubar",
    "AXMenuBarItem": "menubaritem",
    "AXMenu": "menu",
    "AXMenuItem": "menuitem",
    "AXPopUpButton": "combobox",
    "AXComboBox": "combobox",
    "AXList": "list",
    "AXTable": "table",
    "AXOutline": "tree",
    "AXRow": "row",
    "AXCell": "cell",
    "AXTabGroup": "tabs",
    "AXGroup": "group",
    "AXSplitGroup": "group",
    "AXScrollArea": "scrollarea",
    "AXScrollBar": "scrollbar",
    "AXSlider": "slider",
    "AXToolbar": "toolbar",
    "AXWebArea": "document",
    "AXHeading": "heading",
    "AXProgressIndicator": "progressbar",
}
# AT-SPI role names. "text" is labels, fields and text areas alike: the helper sets their role from their states.
LINUX_ROLES = {
    "frame": "window",
    "window": "window",
    "dialog": "dialog",
    "alert": "dialog",
    "file chooser": "dialog",
    "push button": "button",
    "button": "button",
    "check box": "checkbox",
    "radio button": "radiobutton",
    "entry": "textfield",
    "password text": "textfield",
    "label": "text",
    "static": "text",
    "paragraph": "text",
    "link": "link",
    "image": "image",
    "icon": "image",
    "menu bar": "menubar",
    "menu": "menu",
    "menu item": "menuitem",
    "check menu item": "menuitem",
    "radio menu item": "menuitem",
    "combo box": "combobox",
    "list": "list",
    "list box": "list",
    "table": "table",
    "tree table": "tree",
    "tree": "tree",
    "table row": "row",
    "list item": "row",
    "table cell": "cell",
    "page tab list": "tabs",
    "page tab": "tab",
    "panel": "group",
    "filler": "group",
    "grouping": "group",
    "section": "group",
    "scroll pane": "scrollarea",
    "scroll bar": "scrollbar",
    "slider": "slider",
    "tool bar": "toolbar",
    "document web": "document",
    "document frame": "document",
    "heading": "heading",
    "progress bar": "progressbar",
}
NATIVE_ROLES = {"macos": MACOS_ROLES, "linux": LINUX_ROLES}

MODIFIERS = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "meta": "cmd", "super": "cmd", "win": "cmd",  # Command, the Windows key, Super
}  # fmt: skip
MODIFIER_ORDER = ["ctrl", "alt", "shift", "cmd"]
KEYS = set("abcdefghijklmnopqrstuvwxyz0123456789") | {"f%d" % n for n in range(1, 13)} | {
    "space", "enter", "tab", "escape", "backspace", "delete", "up", "down", "left", "right", "home", "end",
    "pageup", "pagedown", "minus", "equal", "comma", "period", "slash", "semicolon", "quote", "backslash",
    "grave", "leftbracket", "rightbracket",
}  # fmt: skip
KEY_ALIASES = {"return": "enter", "esc": "escape", "del": "delete", "pgup": "pageup", "pgdn": "pagedown"}


def parse_chord(chord):
    """'Shift+Command+Space' -> ('space', ['shift', 'cmd']); UsageError naming what is wrong."""
    parts = [p.strip().lower() for p in str(chord).split("+")]
    if not all(parts):
        raise UsageError("chord %r has an empty part; write it like cmd+shift+space" % chord)
    *mods, key = parts
    key = KEY_ALIASES.get(key, key)
    if key not in KEYS:
        raise UsageError("unknown key %r in chord %r; keys: a-z, 0-9, f1-f12, %s" % (key, chord, ", ".join(sorted(k for k in KEYS if len(k) > 1 and not re.match(r"f\d", k)))))
    unknown = [m for m in mods if m not in MODIFIERS]
    if unknown:
        raise UsageError("unknown modifier %r in chord %r; modifiers: ctrl, alt, shift, cmd (Command/Windows/Super)" % (unknown[0], chord))
    wanted = {MODIFIERS[m] for m in mods}
    return key, [m for m in MODIFIER_ORDER if m in wanted]


def chord_text(key, modifiers):
    return "+".join(list(modifiers) + [key])


def normalize(node, os_name):
    """Fill in the shared node shape and the cross-OS role, recursively."""
    out = dict(NODE_DEFAULTS, **node)
    native = out.get("native_role")
    if "role" not in node:
        out["role"] = NATIVE_ROLES.get(os_name, {}).get(native) or re.sub(r"^AX|\s+", "", native or "").lower() or "unknown"
    out["native_role"] = native if native is not None else out["role"]
    for key in EXTRA_KEYS:  # helper detail outside the shared shape
        out.pop(key, None)
    out["children"] = [normalize(child, os_name) for child in node.get("children", [])]
    return out


def texts(node):
    return [t for t in (node["name"], node["value"], node["description"]) if isinstance(t, str) and t]


def label(node):
    found = texts(node)
    return found[0].strip() if found else ""


class Query:
    """Which elements: by text (name, value or description; exact matches win,
    otherwise substrings), by role (cross-OS or native), inside app (by name,
    case-insensitive). Any may be None."""

    def __init__(self, text=None, role=None, app=None):
        self.text, self.role, self.app = text, role, app

    def require(self, command):
        if self.text is None and self.role is None:
            raise UsageError("%s needs --text or --role (or both)" % command)
        return self

    def __str__(self):
        return ", ".join("%s=%r" % kv for kv in (("text", self.text), ("role", self.role), ("app", self.app)) if kv[1] is not None)

    def matches(self, tree):
        """Matching elements in tree order, without their children, each with its "app"."""
        candidates = []

        def visit(node, app_name):
            if node["role"] == "application":
                app_name = node["name"]
            if node["role"] != "desktop":
                if self.app and (app_name or "").lower() != self.app.lower():
                    return
                if self.role is None or self.role in (node["role"], node["native_role"]):
                    candidates.append((node, app_name))
            for child in node["children"]:
                visit(child, app_name)

        visit(tree, None)
        if self.text is not None:
            exact = [(n, a) for n, a in candidates if self.text in texts(n)]
            candidates = exact or [(n, a) for n, a in candidates if any(self.text in t for t in texts(n))]
        return [dict({k: v for k, v in n.items() if k != "children"}, app=a) for n, a in candidates]


# Conditions for wait_for. poll(ui, timeout) returns (met, extra result fields).


class ElementCondition:
    def __init__(self, query, gone):
        self.query, self.gone = query, gone

    def describe(self):
        return {k: v for k, v in (("text", self.query.text), ("role", self.query.role), ("app", self.query.app), ("gone", self.gone or None)) if v is not None}

    def poll(self, ui, timeout):
        matches = self.query.matches(ui.tree(self.query.app, timeout=timeout))
        return (not matches if self.gone else bool(matches)), {"matches": matches}


class ProcessCondition:
    def __init__(self, name):
        self.name = name

    def describe(self):
        return {"process": self.name}

    def poll(self, ui, timeout):
        return ui.provider.exec(ui.probes.process_argv(ui.provider, self.name), timeout).ok, {}


class FileCondition:
    def __init__(self, path):
        self.path = path

    def describe(self):
        return {"file": self.path}

    def poll(self, ui, timeout):
        return ui.provider.exec(ui.probes.exists_argv(ui.provider, self.path), timeout).ok, {}


class LogCondition:
    def __init__(self, path, pattern):
        self.path, self.pattern = path, pattern
        try:
            self.regex = re.compile(pattern, re.M)
        except re.error as exc:
            raise UsageError("--pattern %r is not a valid regular expression: %s" % (pattern, exc))

    def describe(self):
        return {"log": self.path, "pattern": self.pattern}

    def poll(self, ui, timeout):
        result = ui.provider.exec(ui.probes.read_argv(ui.provider, self.path), timeout)
        return result.ok and self.regex.search(result.stdout) is not None, {}


def condition(text=None, role=None, app=None, gone=False, process=None, file=None, log=None, pattern=None):
    """The one wait_for condition these arguments describe; UsageError unless there is exactly one."""
    element = text is not None or role is not None
    if sum([element, process is not None, file is not None, log is not None]) != 1:
        raise UsageError("wait-for needs exactly one condition: an element (--text/--role), --process, --file or --log with --pattern")
    if gone and not element:
        raise UsageError("--gone applies to elements only")
    if element:
        return ElementCondition(Query(text, role, app), gone)
    if process is not None:
        return ProcessCondition(process)
    if file is not None:
        return FileCondition(file)
    if pattern is None:
        raise UsageError("wait-for --log needs --pattern")
    return LogCondition(log, pattern)


class PosixProbes:
    """Commands that check the Guest from its shell, for process, file and log conditions."""

    def process_argv(self, provider, name):
        return ["pgrep", "-x", name]

    def exists_argv(self, provider, path):
        return ["sh", "-c", EXPAND_TILDE + 'test -e "$p"', "sh", path]

    def read_argv(self, provider, path):
        return ["sh", "-c", EXPAND_TILDE + 'cat -- "$p"', "sh", path]


class PowerShellProbes:
    def process_argv(self, provider, name):
        return provider.shell_argv("if (Get-Process -Name '%s' -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" % name.replace("'", "''"))

    def exists_argv(self, provider, path):
        return provider.shell_argv("if (Test-Path -LiteralPath %s) { exit 0 } else { exit 1 }" % _ps_path(path))

    def read_argv(self, provider, path):
        return provider.shell_argv("Get-Content -Raw -LiteralPath %s" % _ps_path(path))


class UI:
    """The UI contract on one Guest.

    call_timeout(doing) gives the seconds the next Guest call may take and
    raises when there are none left (a Scenario's deadline).
    """

    def __init__(self, provider, call_timeout):
        self.provider = provider
        self.os = provider.lab.os
        self.call_timeout = call_timeout
        self.probes = PowerShellProbes() if self.os == "windows" else PosixProbes()

    def _call(self, command, params):
        return self.provider.ui_call(command, params, self.call_timeout("ui %s" % command))

    def _call_with_deadline(self, command, params):
        """For commands that wait inside the helper: it gets a deadline short of the call's timeout."""
        timeout = self.call_timeout("ui %s" % command)
        return self.provider.ui_call(command, dict(params, timeout=max(timeout - STAGE_MARGIN, timeout / 2)), timeout)

    def tree(self, app=None, timeout=None):
        params = {"app": app} if app else {}
        timeout = self.call_timeout("ui tree") if timeout is None else timeout
        return normalize(self.provider.ui_call("tree", params, timeout), self.os)

    def find(self, query):
        return {"matches": query.require("find").matches(self.tree(query.app))}

    def click(self, query=None, index=0, at=None):
        """Click the index-th match's middle, refusing if something else is on top of it; or click at (x, y)."""
        if at is not None:
            x, y = at
            result = self._call("click", {"x": x, "y": y})
            return {"x": result["x"], "y": result["y"], "element": None, "under": result.get("under")}
        matches = self.find(query.require("click"))["matches"]
        if len(matches) <= index:
            raise GuestError("no element to click matches %s (%d found)" % (query, len(matches)), "look at `vmlab ui tree` for what is on screen")
        element = matches[index]
        b = element["bounds"]
        if not b or b["w"] <= 0 or b["h"] <= 0:
            raise GuestError("the element matching %s has no bounds on screen" % query, "it may be hidden; wait for it to appear, or click another element")
        x, y = b["x"] + b["w"] // 2, b["y"] + b["h"] // 2
        result = self._call("click", {"x": x, "y": y, "expect": {"label": label(element), "bounds": b}})
        return {"x": result["x"], "y": result["y"], "element": element, "under": result.get("under")}

    def press(self, chord):
        key, modifiers = parse_chord(chord)
        self._call("press", {"key": key, "modifiers": modifiers})
        return {"chord": chord_text(key, modifiers)}

    def type(self, text):
        return {"typed": self._call("type", {"text": text})["typed"]}

    def clipboard(self, set=None):
        return {"text": self._call("clipboard", {} if set is None else {"set": set})["text"]}

    def focus(self, app, window=None):
        """Bring a running app to the front, raising its first window whose title contains window."""
        params = {"app": app}
        if window is not None:
            params["window"] = window
        return self._call_with_deadline("focus", params)

    def stage_text(self, text, app=None, then=None):
        """Open text in a third-party app, select it all and press the chord then, in one Guest call."""
        params = {"text": text, "app": app or STAGE_APPS.get(self.os, "TextEdit")}
        if then:
            key, modifiers = parse_chord(then)
            params["then"] = {"key": key, "modifiers": modifiers}
        result = self._call_with_deadline("stage-text", params)
        pressed = result.get("pressed")
        result["pressed"] = chord_text(pressed["key"], pressed["modifiers"]) if pressed else None
        return result

    def wait_for(self, condition, timeout=None):
        """Poll condition until it holds or timeout seconds (default: the Lab's step_timeout) pass.

        Returns {"met", "waited_s", "condition", ...}; an unmet condition is not an error.
        """
        timeout = self.provider.lab.step_timeout if timeout is None else timeout
        started = time.time()
        deadline = started + timeout
        extra = {}
        first = True
        while True:
            # A poll may not outlive the wait by more than a moment, except the first: the answer
            # is never "not met" without one look, even over a Channel slower than the wait.
            poll_timeout = self.call_timeout("wait-for")
            if not first:
                poll_timeout = min(poll_timeout, max(1, deadline - time.time() + 1))
            first = False
            try:
                met, extra = condition.poll(self, poll_timeout)
            except GuestTimeout as exc:
                # A slow or hung poll just means "not met yet". A Scenario running out
                # of time (a subclass) still ends the Scenario.
                if type(exc) is not GuestTimeout:
                    raise
                met = False
            now = time.time()
            if met or now >= deadline:
                return dict({"met": bool(met), "waited_s": round(now - started, 3), "condition": condition.describe()}, **extra)
            time.sleep(min(POLL_SECONDS, deadline - now))


def _ps_path(path):
    """A PowerShell expression for path, with %VARS% and a leading ~ expanded."""
    return "([Environment]::ExpandEnvironmentVariables('%s') -replace '^~', $env:USERPROFILE)" % path.replace("'", "''")
