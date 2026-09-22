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

from vmlab.providers.base import GuestError, GuestTimeout

NODE_DEFAULTS = {"name": "", "value": None, "description": None, "bounds": None, "focused": False, "enabled": True}
POLL_SECONDS = 0.25  # between checks of a wait_for condition; the condition and timeout decide when it ends
EXTRA_KEYS = ("native_subrole", "bundle_id")
STAGE_MARGIN = 5  # s the stage-text helper gives up before its call would be killed
# sh: $1 with a leading ~ expanded to the Guest user's home, as $p
EXPAND_TILDE = 'p=$1; case $p in "~"|"~/"*) p="$HOME${p#"~"}";; esac; '

STAGE_APPS = {"macos": "TextEdit", "windows": "Notepad", "linux": "gedit"}

# Native roles to cross-OS roles. Anything unlisted becomes its lowercased native role.
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
NATIVE_ROLES = {"macos": MACOS_ROLES}

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


class UsageError(Exception):
    """A UI command was asked for something malformed (a bad chord, no condition)."""


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
        out["role"] = NATIVE_ROLES.get(os_name, {}).get(native) or re.sub(r"^AX", "", native or "").lower() or "unknown"
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


def find(tree, text=None, role=None, app=None):
    """Elements matching every given criterion, in tree order, without their children.

    text matches name, value or description: exact matches win; if there are
    none, substring matches. role matches the cross-OS or the native role. app
    limits the search to applications of that name (case-insensitive).
    """
    candidates = []

    def visit(node, app_name):
        if node["role"] == "application":
            app_name = node["name"]
        if node["role"] != "desktop":
            if app and (app_name or "").lower() != app.lower():
                return
            if role is None or role in (node["role"], node["native_role"]):
                candidates.append((node, app_name))
        for child in node["children"]:
            visit(child, app_name)

    visit(tree, None)
    if text is not None:
        exact = [(n, a) for n, a in candidates if text in texts(n)]
        candidates = exact or [(n, a) for n, a in candidates if any(text in t for t in texts(n))]
    return [dict({k: v for k, v in n.items() if k != "children"}, app=a) for n, a in candidates]


class UI:
    """The UI contract on one Guest.

    call_timeout(doing) gives the seconds the next Guest call may take and
    raises when there are none left (a Scenario's deadline).
    """

    def __init__(self, provider, call_timeout):
        self.provider = provider
        self.os = provider.lab.os
        self.call_timeout = call_timeout

    def _call(self, command, params):
        return self.provider.ui_call(command, params, self.call_timeout("ui %s" % command))

    def tree(self, app=None):
        params = {"app": app} if app else {}
        return normalize(self._call("tree", params), self.os)

    def find(self, text=None, role=None, app=None):
        if text is None and role is None:
            raise UsageError("find needs --text or --role (or both)")
        tree = self.tree(app)
        return {"matches": find(tree, text=text, role=role, app=app)}

    def click(self, text=None, role=None, app=None, index=0, at=None):
        """Click an element's middle, refusing if something else is on top of it; or click at (x, y)."""
        if at is not None:
            x, y = at
            result = self._call("click", {"x": x, "y": y})
            return {"x": result["x"], "y": result["y"], "element": None, "under": result.get("under")}
        matches = self.find(text=text, role=role, app=app)["matches"]
        sought = ", ".join("%s=%r" % kv for kv in (("text", text), ("role", role), ("app", app)) if kv[1] is not None)
        if len(matches) <= index:
            raise GuestError("no element to click matches %s (%d found)" % (sought, len(matches)), "look at `vmlab ui tree` for what is on screen")
        element = matches[index]
        b = element["bounds"]
        if not b or b["w"] <= 0 or b["h"] <= 0:
            raise GuestError("the element matching %s has no bounds on screen" % sought, "it may be hidden; wait for it to appear, or click another element")
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

    def stage_text(self, text, app=None, then=None):
        """Open text in a third-party app, select it all and press the chord then, in one Guest call."""
        params = {"text": text, "app": app or STAGE_APPS.get(self.os, "TextEdit")}
        if then:
            key, modifiers = parse_chord(then)
            params["then"] = {"key": key, "modifiers": modifiers}
        timeout = self.call_timeout("ui stage-text")
        params["timeout"] = max(timeout - STAGE_MARGIN, timeout / 2)
        result = self.provider.ui_call("stage-text", params, timeout)
        pressed = result.get("pressed")
        result["pressed"] = chord_text(pressed["key"], pressed["modifiers"]) if pressed else None
        return result

    def wait_for(self, timeout, text=None, role=None, app=None, gone=False, process=None, file=None, log=None, pattern=None):
        """Poll a condition until it holds or timeout seconds pass: {"met", "waited_s", "condition", ...}."""
        element = text is not None or role is not None
        conditions = [element, process is not None, file is not None, log is not None]
        if sum(conditions) != 1:
            raise UsageError("wait-for needs exactly one condition: an element (--text/--role), --process, --file or --log with --pattern")
        if log is not None and pattern is None:
            raise UsageError("wait-for --log needs --pattern")
        if gone and not element:
            raise UsageError("--gone applies to elements only")
        if pattern is not None:
            try:
                regex = re.compile(pattern, re.M)
            except re.error as exc:
                raise UsageError("--pattern %r is not a valid regular expression: %s" % (pattern, exc))
        condition = {k: v for k, v in (("text", text), ("role", role), ("app", app), ("gone", gone or None), ("process", process), ("file", file), ("log", log), ("pattern", pattern)) if v is not None}

        started = time.time()
        deadline = started + timeout
        # A poll may not outlive the wait by more than a moment: a hung Guest call ends the wait unmet.
        poll_timeout = lambda: min(self.call_timeout("wait-for"), max(1, deadline - time.time() + 1))  # noqa: E731
        extra = {}
        while True:
            try:
                if element:
                    tree = normalize(self.provider.ui_call("tree", {"app": app} if app else {}, poll_timeout()), self.os)
                    matches = find(tree, text=text, role=role, app=app)
                    met = not matches if gone else bool(matches)
                    extra = {"matches": matches}
                elif process is not None:
                    met = self.provider.exec(self._process_argv(process), poll_timeout()).ok
                elif file is not None:
                    met = self.provider.exec(self._file_argv(file), poll_timeout()).ok
                else:
                    result = self.provider.exec(self._read_argv(log), poll_timeout())
                    met = result.ok and regex.search(result.stdout) is not None
            except GuestTimeout as exc:
                # A slow or hung poll just means "not met yet". A Scenario running out
                # of time (a subclass) still ends the Scenario.
                if type(exc) is not GuestTimeout:
                    raise
                met = False
            now = time.time()
            if met or now >= deadline:
                return dict({"met": bool(met), "waited_s": round(now - started, 3), "condition": condition}, **extra)
            time.sleep(min(POLL_SECONDS, deadline - now))

    def _process_argv(self, name):
        if self.os == "windows":
            return self.provider.shell_argv("if (Get-Process -Name '%s' -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" % name.replace("'", "''"))
        return ["pgrep", "-x", name]

    def _file_argv(self, path):
        if self.os == "windows":
            return self.provider.shell_argv("if (Test-Path -LiteralPath %s) { exit 0 } else { exit 1 }" % _ps_path(path))
        return ["sh", "-c", EXPAND_TILDE + 'test -e "$p"', "sh", path]

    def _read_argv(self, path):
        if self.os == "windows":
            return self.provider.shell_argv("Get-Content -Raw -LiteralPath %s" % _ps_path(path))
        return ["sh", "-c", EXPAND_TILDE + 'cat -- "$p"', "sh", path]


def _ps_path(path):
    """A PowerShell expression for path, with %VARS% and a leading ~ expanded."""
    return "([Environment]::ExpandEnvironmentVariables('%s') -replace '^~', $env:USERPROFILE)" % path.replace("'", "''")
