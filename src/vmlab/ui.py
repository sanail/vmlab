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
their windows and, on macOS, their Tray icons (menubaritem). A Tray menu is read
and chosen from with the tray command, in a shape of its own (UI.tray).
"""

import re
import time
from datetime import datetime, timedelta, timezone

from vmlab.config import UsageError, is_argv
from vmlab.providers.base import ChannelError, GuestError, GuestTimeout, ps_path, ps_quote, sh_expand_tilde

NODE_DEFAULTS = {"name": "", "value": None, "description": None, "bounds": None, "focused": False, "enabled": True}
POLL_SECONDS = 0.25  # between checks of a wait_for condition or tries of a click with a timeout; they and the timeout decide when it ends
EXTRA_KEYS = ("native_subrole", "bundle_id")
STAGE_MARGIN = 5  # s a helper that waits (focus, stage-text, close-staged) gives up before its call would be killed
# sh: $1 with a leading ~ expanded to the Guest user's home, as $p
EXPAND_TILDE = "p=$1; " + sh_expand_tilde("p")

STAGE_APPS = {"macos": "TextEdit", "windows": "Notepad", "linux": "gnome-text-editor"}
# The name of every file stage-text writes (8 random hex digits); the helper checks its folder.
STAGED_NAME = re.compile(r"vmlab-stage-[0-9a-f]{8}\.txt", re.I)

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
# UI Automation control types, without "ControlType.". The helper sets text areas' role itself (Edit, Document).
WINDOWS_ROLES = {
    "Window": "window",
    "Button": "button",
    "SplitButton": "button",
    "CheckBox": "checkbox",
    "RadioButton": "radiobutton",
    "Edit": "textfield",
    "Text": "text",
    "Hyperlink": "link",
    "Image": "image",
    "MenuBar": "menubar",
    "Menu": "menu",
    "MenuItem": "menuitem",
    "ComboBox": "combobox",
    "List": "list",
    "ListItem": "row",
    "DataGrid": "table",
    "Table": "table",
    "DataItem": "row",
    "Tree": "tree",
    "TreeItem": "row",
    "Tab": "tabs",
    "TabItem": "tab",
    "Group": "group",
    "Pane": "group",
    "ScrollBar": "scrollbar",
    "Slider": "slider",
    "ToolBar": "toolbar",
    "Document": "document",
    "ProgressBar": "progressbar",
}
NATIVE_ROLES = {"macos": MACOS_ROLES, "linux": LINUX_ROLES, "windows": WINDOWS_ROLES}

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
    otherwise substrings), by role (cross-OS or native), inside app. Any may be None.

    The Guest's helper chooses the app (by name, bundle id or process name, per OS):
    matches takes the tree tree(app) returns and does not filter by app again."""

    def __init__(self, text=None, role=None, app=None):
        self.text, self.role, self.app = text, role, app

    def require(self, command):
        if self.text is None and self.role is None:
            raise UsageError("%s needs --text or --role (or both)" % command)
        return self

    def __str__(self):
        return ", ".join("%s=%r" % kv for kv in (("text", self.text), ("role", self.role), ("app", self.app)) if kv[1] is not None)

    def matches(self, tree):
        """Matching elements of tree (as tree(self.app) returns it) in tree order, without their children, each with its app's name as "app"."""
        candidates = []

        def visit(node, app_name):
            if node["role"] == "application":
                app_name = node["name"]
            if node["role"] != "desktop":
                if self.role is None or self.role in (node["role"], node["native_role"]):
                    candidates.append((node, app_name))
            for child in node["children"]:
                visit(child, app_name)

        visit(tree, None)
        if self.text is not None:
            exact = [(n, a) for n, a in candidates if self.text in texts(n)]
            candidates = exact or [(n, a) for n, a in candidates if any(self.text in t for t in texts(n))]
        return [dict({k: v for k, v in n.items() if k != "children"}, app=a) for n, a in candidates]


# Conditions for wait_for. poll(ui, timeout) returns (holds, extra result fields); Gone inverts
# holds. unanswered is the extra fields before any poll has answered. A poll the Guest did not
# answer raises instead (NoAnswer, ChannelError, GuestTimeout), which wait_for counts as "not met
# yet" for every condition: gone=True never holds on a failed call.
STDOUT_TAIL = 2000  # characters of an exec condition's last output kept in the result
# How a Guest says it has no such command: sh's 127; on Windows, what vmlab's call runner and
# cmd.exe exit with (9009); or PowerShell's error for an unknown command (exit 1), which Windows
# PowerShell names CommandNotFoundException in every language. PowerShell 7's concise error view
# shows only the message, which is localized: its English one is known too.
MISSING_CODES = (127, 9009)
MISSING_MARKS = ("CommandNotFoundException", "is not recognized as")
LINUX_NAME_BYTES = 15  # of a process's name, Linux keeps (and pgrep matches) this many


# How every helper refuses a click on a covered element (or one scrolled out of view).
COVERED = "something else is at ("


class NotClickable(GuestError):
    """A click found no element to click, or something else on top of it: a click with a timeout tries again."""


class NoAnswer(GuestError):
    """A condition's command came back without the Guest's answer: a Channel failed without saying so."""


class TrayError(GuestError):
    """A tray command that failed in the Guest: no Tray icon, no such item, or a disabled one.
    result is what the command prints: the items it read, "chosen": null and the "error"."""

    def __init__(self, message, fix, items):
        super().__init__(message, fix)
        self.result = {"items": items, "chosen": None, "error": message}


TRAY_ITEM_DEFAULTS = {"name": "", "enabled": True, "checked": False, "children": []}


def tray_labels(choose):
    """g.tray's choose (None, a label or a list of labels) as a list of labels."""
    labels = [] if choose is None else [choose] if isinstance(choose, str) else choose
    if not isinstance(labels, (list, tuple)) or not all(isinstance(l, str) and l for l in labels):
        raise UsageError("choose takes a label or a list of labels, one per menu level, not %r" % (choose,))
    return list(labels)


def tray_items(items):
    """A Tray menu's items in the contract's shape; children is None where the helper could not list a submenu."""
    out = []
    for item in items:
        item = dict(TRAY_ITEM_DEFAULTS, **{k: v for k, v in item.items() if k in TRAY_ITEM_DEFAULTS})
        if item["children"] is not None:
            item["children"] = tray_items(item["children"])
        out.append(item)
    return out


def tray_level(items, path):
    """The items of the menu that path (labels) opens, or None where they were not listed."""
    for name in path:
        item = next((i for i in items or [] if i["name"] == name), None)
        items = item and item["children"]
    return items


class HostTime:
    """A moment on the Host's clock (time.time()), for a since that is not a Guest time: each
    notifications call turns it into the Guest's time by the Guest's "now" in its answer."""

    def __init__(self, at):
        self.at = at


def guest_time(value, named):
    """An ISO 8601 time (a Guest time, UTC unless it says otherwise) as an aware datetime; UsageError naming named."""
    try:
        # Python before 3.11 reads neither Z nor fractions other than of 3 or 6 digits (.NET prints 7).
        text = re.sub(r"[Zz]$", "+00:00", value) if isinstance(value, str) else None
        text = text and re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "00000")[:6], text, count=1)
        parsed = datetime.fromisoformat(text) if text else None
    except ValueError:
        parsed = None
    if parsed is None:
        raise UsageError("%s %r is not an ISO 8601 time, e.g. 2026-09-25T10:00:00Z" % (named, value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(moment):
    """An aware datetime as the contract prints times: UTC, to the millisecond."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _helper_time(value):
    try:
        return guest_time(value, "time")
    except UsageError as exc:
        raise GuestError("UI notifications: the Guest's helper gave a %s" % exc)


def text_pattern(text, named):
    """text compiled as a Python regular expression, or None; UsageError naming named."""
    try:
        return None if text is None else re.compile(text, re.M)
    except re.error as exc:
        raise UsageError("%s %r is not a valid regular expression: %s" % (named, text, exc))


class ElementCondition:
    unanswered = {}

    def __init__(self, query):
        self.query = query

    def describe(self):
        return {k: v for k, v in (("text", self.query.text), ("role", self.query.role), ("app", self.query.app)) if v is not None}

    def poll(self, ui, timeout):
        matches = self.query.matches(ui.tree(self.query.app, timeout=timeout))
        return bool(matches), {"matches": matches}


class ProcessCondition:
    unanswered = {}

    def __init__(self, name):
        self.name = name

    def describe(self):
        return {"process": self.name}

    def poll(self, ui, timeout):
        return ui.ask(self, ui.probes.process_argv(ui.provider, self.name), timeout, answers=(0, 1)).ok, {}


class FileCondition:
    unanswered = {}

    def __init__(self, path):
        self.path = path

    def describe(self):
        return {"file": self.path}

    def poll(self, ui, timeout):
        return ui.ask(self, ui.probes.exists_argv(ui.provider, self.path), timeout, answers=(0, 1)).ok, {}


class LogCondition:
    unanswered = {}

    def __init__(self, path, pattern):
        self.path, self.pattern = path, pattern
        self.regex = re.compile(pattern, re.M)

    def describe(self):
        return {"log": self.path, "pattern": self.pattern}

    def poll(self, ui, timeout):
        # A file that is not there has no matching line; one that cannot be read gives no answer.
        result = ui.ask(self, ui.probes.read_argv(ui.provider, self.path), timeout, answers=(0,))
        return self.regex.search(result.stdout) is not None, {}


class ExecCondition:
    """argv exits 0, or with a pattern, its stdout matches whatever the exit code."""

    unanswered = {"code": None, "stdout": ""}

    def __init__(self, argv, pattern):
        self.argv, self.pattern = argv, pattern
        self.regex = None if pattern is None else re.compile(pattern, re.M)

    def describe(self):
        return dict({"exec": self.argv}, **({} if self.pattern is None else {"pattern": self.pattern}))

    def poll(self, ui, timeout):
        result = ui.ask(self, ui.probes.exec_argv(self.argv), timeout)
        if result.code in MISSING_CODES or (result.code and any(mark in result.stderr for mark in MISSING_MARKS)):
            detail = (result.stderr.strip().splitlines() or ["exit %d" % result.code])[-1]
            raise GuestError("wait-for --exec %s: the Guest has no such command (%s)" % (self.argv, detail), "check the command's name and that it is installed in the Guest")
        held = result.ok if self.regex is None else self.regex.search(result.stdout) is not None
        return held, {"code": result.code, "stdout": result.stdout[-STDOUT_TAIL:]}


class NotificationCondition:
    """A Notification matching pattern was posted by app (None: any app) at or after since."""

    unanswered = {}

    def __init__(self, pattern, app, since):
        self.pattern, self.app, self.since = pattern, app, since

    def describe(self):
        found = {"notification": self.pattern}
        if self.app is not None:
            found["app"] = self.app
        if isinstance(self.since, str):  # a Guest time the caller gave, not the wait's own start
            found["since"] = self.since
        return found

    def poll(self, ui, timeout):
        posted = ui.notifications(self.app, self.pattern, self.since, timeout=timeout)["notifications"]
        return bool(posted), {"notifications": posted}


class Gone:
    """Another condition, inverted: it holds while the other does not."""

    def __init__(self, inner):
        self.inner = inner
        self.unanswered = inner.unanswered

    def describe(self):
        return dict(self.inner.describe(), gone=True)

    def poll(self, ui, timeout):
        held, extra = self.inner.poll(ui, timeout)
        return not held, extra


class ConditionError(UsageError):
    """A malformed wait_for condition. key is the argument at fault (None: the set of them) and
    problem what is wrong with it, naming other arguments as the caller spells them."""

    def __init__(self, key, problem, named):
        self.key, self.problem = key, problem
        super().__init__("%s %s" % (named(key), problem) if key else problem)


def flag(key):
    """How `vmlab ui wait-for` spells condition()'s argument key."""
    return "--" + key


def condition(text=None, role=None, app=None, gone=False, process=None, file=None, log=None, pattern=None, exec=None, notification=None, since=None, named=flag):
    """The one wait_for condition these arguments describe; ConditionError unless there is exactly one.

    gone=True inverts it. exec is an argv. notification is a pattern; since (a Guest time in ISO
    8601, or a HostTime) goes with it, and None counts every Notification. named(key) spells an
    argument in errors (default: its flag)."""
    element = text is not None or role is not None
    if sum([element, process is not None, file is not None, log is not None, exec is not None, notification is not None]) != 1:
        raise ConditionError(
            None,
            "wait-for needs exactly one condition: an element (%s/%s), %s, %s, %s with %s, %s, or %s"
            % tuple(map(named, ("text", "role", "process", "file", "log", "pattern", "exec", "notification"))),
            named,
        )
    if app is not None and not element and notification is None:
        raise ConditionError("app", "goes with %s or %s (an element in one app) or %s (one app's)" % (named("text"), named("role"), named("notification")), named)
    if notification is not None:
        if gone:
            raise ConditionError("gone", "does not go with %s: a Notification, once posted, stays posted" % named("notification"), named)
        try:
            re.compile(notification, re.M)
        except re.error as exc:
            raise ConditionError("notification", "%r is not a valid regular expression: %s" % (notification, exc), named)
        if isinstance(since, str):
            try:
                guest_time(since, "since")
            except UsageError:
                raise ConditionError("since", "%r is not an ISO 8601 time, e.g. 2026-09-25T10:00:00Z" % since, named)
    elif since is not None:
        raise ConditionError("since", "goes with %s" % named("notification"), named)
    if pattern is not None and log is None and exec is None:
        raise ConditionError("pattern", "goes with %s or %s" % (named("log"), named("exec")), named)
    if log is not None and pattern is None:
        raise ConditionError("log", "needs %s" % named("pattern"), named)
    if exec is not None and not is_argv(exec):
        raise ConditionError("exec", "needs a command: a non-empty list of strings, the program first", named)
    if pattern is not None:
        try:
            re.compile(pattern, re.M)
        except re.error as exc:
            raise ConditionError("pattern", "%r is not a valid regular expression: %s" % (pattern, exc), named)
    if element:
        found = ElementCondition(Query(text, role, app))
    elif process is not None:
        found = ProcessCondition(process)
    elif file is not None:
        found = FileCondition(file)
    elif log is not None:
        found = LogCondition(log, pattern)
    elif notification is not None:
        found = NotificationCondition(notification, app, since)
    else:
        found = ExecCondition(list(exec), pattern)
    return Gone(found) if gone else found


# A condition's command runs in sh, which then writes its exit code as the last line of stderr:
# a result without that line is not the Guest's answer (ssh's own exit 255, a call killed).
POSIX_ANSWER = '\nc=$?; printf "\\nvmlab-answered %d\\n" "$c" >&2; exit "$c"'
POSIX_ANSWER_LINE = re.compile(r"\n?vmlab-answered (\d+)\n\Z")
# Is a process running? $1 is a pgrep pattern (extended regex, matched against the name the
# kernel keeps); $2, when not empty, the full name to confirm each match by, from its command line
# (its program's path or name, which for a script is the script's). Never this sh itself.
POSIX_PROCESS = r"""pids=$(pgrep -- "$1"); c=$?
if [ "$c" -eq 0 ]; then
    c=1
    for pid in $pids; do
        [ "$pid" = "$$" ] && continue
        if [ -z "$2" ]; then c=0; break; fi
        args=$(ps -o args= -p "$pid") || continue
        case $args in "$2"|"$2 "*|*/"$2"|*/"$2 "*) c=0; break;; esac
    done
fi
(exit "$c")"""


class PosixProbes:
    """Commands that check the Guest from its shell, for wait_for's conditions."""

    def __init__(self, os_name):
        self.os = os_name

    def _answered(self, script, args):
        return ["sh", "-c", script + POSIX_ANSWER, "sh"] + list(args)

    def exec_argv(self, argv):
        return self._answered('"$@"', argv)

    def process_argv(self, provider, name):
        """pgrep matches a name exactly (as text, not a regex). Linux keeps only the first 15 bytes
        of a longer one: those match, then the full name in the process's command line."""
        cut = name.encode("utf-8")[:LINUX_NAME_BYTES].decode("utf-8", "ignore")
        if self.os == "linux" and cut != name:
            return self._answered(POSIX_PROCESS, ["^" + _ere(cut), name])
        return self._answered(POSIX_PROCESS, ["^%s$" % _ere(name), ""])

    def exists_argv(self, provider, path):
        return self._answered(EXPAND_TILDE + 'test -e "$p"', [path])

    def read_argv(self, provider, path):
        return self._answered(EXPAND_TILDE + '[ ! -e "$p" ] || cat -- "$p"', [path])

    def answer(self, result):
        """result, if it is the Guest's answer (its stderr without the answer line); else None."""
        found = POSIX_ANSWER_LINE.search(result.stderr)
        if not found or int(found.group(1)) != result.code:
            return None
        result.stderr = result.stderr[: found.start()]
        return result


class PowerShellProbes:
    """The same on Windows. Every Windows call's result comes from vmlab's call runner in the Guest
    (vmlab.providers.windows), so it is the Guest's answer; a failed call raises."""

    def exec_argv(self, argv):
        return argv

    def process_argv(self, provider, name):
        return provider.shell_argv("if (Get-Process -Name %s -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" % ps_quote(name))

    def exists_argv(self, provider, path):
        return provider.shell_argv("if (Test-Path -LiteralPath %s) { exit 0 } else { exit 1 }" % ps_path(path))

    def read_argv(self, provider, path):
        return provider.shell_argv("$p = %s; if (Test-Path -LiteralPath $p) { Get-Content -Raw -LiteralPath $p }" % ps_path(path))

    def answer(self, result):
        return result


def _ere(text):
    """text as a POSIX extended regex that matches it literally."""
    return re.sub(r"([.\[\]()*+?{}|^$\\])", r"\\\1", text)


class UI:
    """The UI contract on one Guest.

    call_timeout(doing) gives the seconds the next Guest call may take and
    raises when there are none left (a Scenario's deadline).
    """

    def __init__(self, provider, call_timeout):
        self.provider = provider
        self.os = provider.lab.os
        self.call_timeout = call_timeout
        self.probes = PowerShellProbes() if self.os == "windows" else PosixProbes(self.os)

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

    def click(self, query=None, index=0, at=None, timeout=None):
        """Click the index-th match's middle, refusing if something else is on top of it; or click at (x, y).

        With timeout (seconds), retry until the element is there and uncovered; when time runs
        out, raise with the last reason. at=(x, y) clicks ignore timeout.
        """
        if at is not None:
            x, y = at
            result = self._call("click", {"x": x, "y": y})
            return {"x": result["x"], "y": result["y"], "element": None, "under": result.get("under")}
        query.require("click")
        if timeout is None:
            return self._click(query, index)
        deadline = time.time() + timeout
        while True:
            try:
                return self._click(query, index)
            except NotClickable as exc:
                now = time.time()
                if now >= deadline:
                    raise GuestError("could not click %s within %gs: %s" % (query, timeout, exc.message), exc.fix)
                time.sleep(min(POLL_SECONDS, deadline - now))

    def _click(self, query, index):
        """One try at clicking; NotClickable when the element is not there or is covered."""
        matches = self.find(query)["matches"]
        if len(matches) <= index:
            raise NotClickable("no element to click matches %s (%d found)" % (query, len(matches)), "look at `vmlab ui tree` for what is on screen")
        element = matches[index]
        b = element["bounds"]
        if not b or b["w"] <= 0 or b["h"] <= 0:
            raise NotClickable("the element matching %s has no bounds on screen" % query, "it may be hidden; wait for it to appear, or click another element")
        x, y = b["x"] + b["w"] // 2, b["y"] + b["h"] // 2
        try:
            result = self._call("click", {"x": x, "y": y, "expect": {"label": label(element), "bounds": b}})
        except GuestError as exc:
            if type(exc) is GuestError and COVERED in exc.message:
                raise NotClickable(exc.message)
            raise
        return {"x": result["x"], "y": result["y"], "element": element, "under": result.get("under")}

    def tray(self, app, choose=None, timeout=None):
        """Read app's Tray menu and, with choose (a label per menu level), choose that item.

        {"items": [{"name", "enabled", "checked", "children"}], "chosen": labels or None}.
        With timeout (seconds), wait for the Tray icon to appear. TrayError, carrying the
        items, when there is no Tray icon, no item of a label or a disabled one.
        """
        if not app:
            raise UsageError("tray needs the app whose Tray icon to use")
        path = tray_labels(choose)
        deadline = time.time() + (timeout or 0)
        while True:
            result = self._call_with_deadline("tray", {"app": app, "choose": path})
            if result.get("icon"):
                break
            now = time.time()
            if now >= deadline:
                within = " within %gs" % timeout if timeout else ""
                detail = ": %s" % result["detail"] if result.get("detail") else ""
                raise TrayError(
                    "%s has no Tray icon%s%s" % (app, within, detail),
                    "check that %s is running and has put its icon in the tray; wait for it with a timeout" % app,
                    [],
                )
            time.sleep(min(POLL_SECONDS, deadline - now))
        items = tray_items(result["items"])
        failed = result.get("failed")
        if failed:
            at = failed["at"]
            label, under = at[-1], (" under %s" % " > ".join(at[:-1]) if len(at) > 1 else "")
            if failed["reason"] == "disabled":
                message = "%s's Tray menu item \"%s\"%s is disabled" % (app, label, under)
            elif failed["reason"] == "leaf":
                message = "%s's Tray menu item \"%s\" has no submenu to choose \"%s\" from" % (app, at[-2], label)
            else:
                level = tray_level(items, at[:-1])
                names = "unknown" if level is None else ", ".join(i["name"] for i in level) or "none"
                message = "%s's Tray menu has no item \"%s\"%s; its items: %s" % (app, label, under, names)
            raise TrayError(message, "labels match exactly, one per menu level; look at `vmlab ui tray --app %s`" % app, items)
        return {"items": items, "chosen": path or None}

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

    def close_staged(self, file, app=None):
        """Close the Staged document at the Guest path file in the editor app (default: the OS's
        stock one), saving it first. {"file", "closed"}; closed is false when it was not open."""
        if not STAGED_NAME.fullmatch(re.split(r"[\\/]", file)[-1]):
            raise UsageError("%s is not a Staged document: close-staged takes the \"file\" a stage-text returned" % file)
        params = {"file": file, "app": app or STAGE_APPS.get(self.os, "TextEdit")}
        return {"file": file, "closed": bool(self._call_with_deadline("close-staged", params)["closed"])}

    def notifications(self, app=None, text=None, since=None, timeout=None):
        """The Notifications the Guest's OS recorded, oldest first: {"notifications": [{"app",
        "title", "body", "time"}]}, time in ISO 8601 UTC on the Guest's clock.

        app is the OS's id for the sender (None: every app's); text a pattern searched for in
        title and body; since a Guest time (ISO 8601) or a HostTime, before which none count."""
        regex = text_pattern(text, "text")
        cutoff = guest_time(since, "since") if isinstance(since, str) else None
        timeout = self.call_timeout("ui notifications") if timeout is None else timeout
        result = self.provider.ui_call("notifications", {}, timeout)
        if isinstance(since, HostTime):  # that moment on the Guest's clock: its "now", less the time since then
            cutoff = _helper_time(result["now"]) - timedelta(seconds=time.time() - since.at)
        found = []
        for posted in result["notifications"]:
            n = {"app": posted.get("app") or "", "title": posted.get("title") or "", "body": posted.get("body") or ""}
            moment = _helper_time(posted["time"])
            if app is not None and n["app"].lower() != app.lower():
                continue
            if (cutoff and moment < cutoff) or (regex and not (regex.search(n["title"]) or regex.search(n["body"]))):
                continue
            found.append((moment, dict(n, time=iso(moment))))
        return {"notifications": [n for _, n in sorted(found, key=lambda pair: pair[0])]}

    def ask(self, condition, argv, timeout, answers=None):
        """The result of condition's command argv, as the Guest answered it. NoAnswer when it came
        back without an answer, or with an exit code other than answers (None: any code)."""
        raw = self.provider.exec(argv, timeout)
        result = self.probes.answer(raw)
        if result is None or (answers is not None and result.code not in answers):
            detail = (raw.stderr.strip().splitlines() or ["no output"])[-1]
            raise NoAnswer("no answer from the Guest to wait-for %s: exit %s, %s" % (condition.describe(), raw.code, detail))
        return result

    def wait_for(self, condition, timeout=None):
        """Poll condition until it holds or timeout seconds (default: the Lab's step_timeout) pass.

        Returns {"met", "waited_s", "condition", ...}, with "error" when the last poll got no
        answer (a failed Channel, a hung call); an unmet condition is not an error. When the
        caller's own clock runs out first (a Scenario's), its GuestTimeout carries that result
        so far as .unmet.
        """
        timeout = self.provider.lab.step_timeout if timeout is None else timeout
        started = time.time()
        state = {"extra": dict(condition.unanswered), "error": None}
        try:
            return self._wait(condition, started, started + timeout, state)
        except GuestTimeout as exc:
            exc.unmet = self._waited(condition, started, False, **state)
            raise

    def _waited(self, condition, started, met, extra, error):
        result = dict({"met": bool(met), "waited_s": round(time.time() - started, 3), "condition": condition.describe()}, **extra)
        if error:
            result["error"] = error  # why the last poll got no answer
        return result

    def _wait(self, condition, started, deadline, state):
        first = True
        while True:
            # A poll may not outlive the wait by more than a moment, except the first: the answer
            # is never "not met" without one look, even over a Channel slower than the wait.
            poll_timeout = self.call_timeout("wait-for")
            if not first:
                poll_timeout = min(poll_timeout, max(1, deadline - time.time() + 1))
            first = False
            try:
                met, state["extra"] = condition.poll(self, poll_timeout)
                state["error"] = None
            except (NoAnswer, ChannelError, GuestTimeout) as exc:
                # A poll the Guest did not answer (a failed Channel, a slow or hung call) just
                # means "not met yet", for every condition and gone=True alike. A Scenario
                # running out of time (a subclass of GuestTimeout) still ends the Scenario.
                if isinstance(exc, GuestTimeout) and type(exc) is not GuestTimeout:
                    raise
                met, state["error"] = False, exc.message
            now = time.time()
            if met or now >= deadline:
                return self._waited(condition, started, met, **state)
            time.sleep(min(POLL_SECONDS, deadline - now))
