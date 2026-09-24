"""The Scenario API: the object a Scenario's scenario(g) function receives.

A Scenario is a Python file in .vmlab/scenarios defining:

    FRESH = True        # optional: restore Clean state before this Scenario
    LAUNCH = False      # optional: don't launch the app before this Scenario (call g.launch())
    TIMEOUT = 300       # optional: seconds for the whole Scenario (default: the Lab's scenario_timeout)

    def scenario(g):
        r = g.exec(["echo", "hello"])
        g.check("echo prints hello", r.stdout.strip() == "hello")

The timeout is enforced at g.* calls: Python code in a Scenario must not block
outside them.

While a Scenario loads and runs, its folder is on sys.path, so it imports shared
helpers from _name.py files next to it; they are re-imported for every Scenario.
"""

import collections
import contextlib
import importlib.util
import os
import re
import sys
import threading
import time
import traceback

from vmlab import arch, ui
from vmlab.config import ConfigError, UsageError
from vmlab.providers import spawning
from vmlab.providers.base import ChannelError, GuestError, GuestTimeout

DETERMINISTIC, VISUAL = "deterministic", "visual, unverified"
OUTPUT_TAIL = 2000  # characters of a spawned process's output in the report of a Run that did not pass


class ScenarioError(Exception):
    """The Scenario itself is broken (as opposed to a Check failing)."""


class ScenarioTimeout(GuestTimeout):
    """The Scenario as a whole ran out of time."""


class ChannelUse:
    """Which Channels served a series of Guest calls, and the fallbacks on the way."""

    def __init__(self):
        self.channels = {}  # Channel name -> calls it served
        self.fallbacks = []

    def record(self, result):
        self.channels[result.channel] = self.channels.get(result.channel, 0) + 1
        self.fallbacks.extend(dict(f, argv=result.argv) for f in result.fallbacks)


class Guest:
    def __init__(self, lab, provider, run_dir, launch_app):
        self.lab = lab.name
        self.os = lab.os
        self.arch = lab.arch  # the Build artifact's
        self.guest_arch = arch.guest_arch()  # differs from arch when the Guest OS emulates it
        self._provider = provider
        self._run_dir = run_dir
        self._launch_app = launch_app
        self._step_timeout = lab.step_timeout
        self._limit = lab.scenario_timeout
        self._deadline = None
        self.checks = []
        self.screenshots = []
        self.spawned = []  # every Spawned process, stopped at the end of the Run
        self.channel_use = ChannelUse()  # this Run's calls, including its app reset and launch

    def _start_clock(self, limit):
        self._limit = limit
        self._deadline = time.time() + limit

    def _remaining(self, doing):
        remaining = self._deadline - time.time()
        if remaining <= 0:
            raise ScenarioTimeout("Scenario exceeded its %ss timeout (before %s)" % (self._limit, doing))
        return remaining

    def exec(self, argv, timeout=None, env=None):
        """Run argv in the Guest. Returns an object with code, stdout, stderr, ok and channel.

        timeout defaults to the Lab's step_timeout. Raises (failing the Run) when
        no Channel reaches the Guest or the call times out; a non-zero exit code
        is returned, not raised.
        """
        argv = list(argv)
        step = self._step_timeout if timeout is None else timeout
        remaining = self._remaining(argv)
        try:
            result = self._provider.exec(argv, timeout=min(step, remaining), env=env)
        except GuestTimeout:
            if remaining < step:
                raise ScenarioTimeout("Scenario exceeded its %ss timeout (during %s)" % (self._limit, argv))
            raise
        return result

    def launch(self, env=None):
        """Launch the app with the Lab's launch recipe; env adds to the Lab's app.env. Waits for the
        Lab's app.ready condition, if any, on the Scenario's clock; raises when it is not met."""
        self._remaining("launch")
        self._on_clock("launch", lambda call_timeout: self._launch_app(env, call_timeout))

    def put(self, guest_path, content):
        """Write content (str, written as UTF-8, or bytes) to the Guest file guest_path, making its
        folders; ~ is the Guest user's home and, on Windows, %VARS% expand. Returns the absolute
        Guest path. The file stays after the Scenario."""
        data = encode_content(content)
        self._remaining("put")
        return self._on_clock("put %s" % guest_path, lambda call_timeout: self._provider.put_file(guest_path, data, call_timeout("put")))

    def get(self, guest_path, binary=False):
        """The Guest file guest_path's content: str (decoded as UTF-8), or bytes with binary=True.
        Raises if the file is not there."""
        self._remaining("get")
        data = self._on_clock("get %s" % guest_path, lambda call_timeout: self._provider.read_file(guest_path, call_timeout("get")))
        return data if binary else decode_content(guest_path, data)

    def spawn(self, argv, env=None):
        """Start argv detached in the Guest; env adds to its environment. Returns a Spawned handle:
        .stop(), .running(), .output(), .log (its output's Guest path) and .pid. Whatever is still
        running at the end of the Run is stopped then, however the Run ends. Raises if the Guest
        has no such command."""
        argv = spawning.validate_argv(argv)
        self._remaining("spawn")
        started = self._on_clock("spawn %s" % argv, lambda call_timeout: self._provider.spawner().start(argv, dict(env or {}), call_timeout("spawn")))
        process = Spawned(self, argv, started)
        self.spawned.append(process)
        return process

    def _end_spawned(self, with_output):
        """Stop what the Scenario spawned and left running, best-effort and off its clock. Returns the
        report's entries; with_output adds the tail of each process's output."""
        entries, unreachable = [], []  # once a call finds no Guest, the rest would only wait for it too

        def attempt(fn):
            if unreachable:
                raise GuestError("not tried: %s" % unreachable[0])
            try:
                return fn()
            except (ChannelError, GuestTimeout) as exc:
                unreachable.append(str(exc).splitlines()[0])
                raise

        for process in self.spawned:
            entry = {"argv": process.argv, "pid": process.pid, "log": process.log, "ended": process._ended}
            if entry["ended"] is None:
                try:
                    found = attempt(lambda: self._provider.spawner().stop(process._started, self._step_timeout))
                    entry["ended"] = "run" if found == spawning.STOPPED else "exited"
                except Exception as exc:  # noted in the report; it never changes the Run's result
                    entry["ended"], entry["stop_error"] = "failed", str(exc) or type(exc).__name__
            if with_output:
                try:
                    entry["output_tail"] = _text(attempt(lambda: self._provider.read_file(process.log, self._step_timeout)))[-OUTPUT_TAIL:]
                except Exception as exc:
                    entry["output_tail"], entry["output_error"] = None, str(exc) or type(exc).__name__
            entries.append(entry)
        return entries

    # The UI contract (vmlab.ui): each method returns what `vmlab ui <command>` prints.

    def tree(self, app=None):
        """The accessibility tree of the Guest's desktop, or of one app."""
        return self._ui_call(lambda contract: contract.tree(app))

    def find(self, text=None, role=None, app=None):
        """{"matches": [...]}: elements by text (exact beats substring) and/or role, optionally in one app."""
        return self._ui_call(lambda contract: contract.find(ui.Query(text, role, app)))

    def click(self, text=None, role=None, app=None, index=0, at=None):
        """Click the index-th matching element's middle, or the point at=(x, y). Raises if nothing matches."""
        return self._ui_call(lambda contract: contract.click(ui.Query(text, role, app), index=index, at=at))

    def press(self, chord):
        """Press a key chord such as "cmd+shift+space", by physical key (any keyboard layout)."""
        return self._ui_call(lambda contract: contract.press(chord))

    def type(self, text):
        """Type text into whatever has focus."""
        return self._ui_call(lambda contract: contract.type(text))

    def clipboard(self):
        """{"text": ...}: the Guest's clipboard as text."""
        return self._ui_call(lambda contract: contract.clipboard())

    def set_clipboard(self, text):
        """Put text on the Guest's clipboard."""
        return self._ui_call(lambda contract: contract.clipboard(set=text))

    def focus(self, app, window=None):
        """Bring a running app to the front, raising its first window whose title contains window."""
        return self._ui_call(lambda contract: contract.focus(app, window=window))

    def stage_text(self, text, app=None, then=None):
        """Open text in a third-party editor, select it all and press the chord then, all in one Guest call."""
        return self._ui_call(lambda contract: contract.stage_text(text, app=app, then=then))

    def wait_for(self, text=None, role=None, app=None, gone=False, process=None, file=None, log=None, pattern=None, exec=None, timeout=None):
        """Wait until one condition holds: an element appears, a process runs, a file exists, a log
        file has a line matching pattern, or the command exec (an argv) exits 0 (with pattern: its
        stdout matches). gone=True waits for the condition to stop holding instead. Returns
        {"met": bool, ...}; never raises for an unmet condition. timeout defaults to the Lab's step_timeout."""
        condition = ui.condition(text=text, role=role, app=app, gone=gone, process=process, file=file, log=log, pattern=pattern, exec=exec, named=str)
        return self._ui_call(lambda contract: contract.wait_for(condition, timeout=timeout))

    def _ui_call(self, fn):
        self._remaining("a UI command")
        return self._on_clock("a UI command", lambda call_timeout: fn(ui.UI(self._provider, call_timeout)))

    def _on_clock(self, doing, fn):
        """fn(call_timeout), whose Guest calls end with the Scenario's clock (see vmlab.ui.UI)."""
        try:
            return fn(lambda what: min(self._step_timeout, self._remaining(what)))
        except GuestTimeout as exc:
            if not isinstance(exc, ScenarioTimeout) and time.time() >= self._deadline:
                raise ScenarioTimeout("Scenario exceeded its %ss timeout (during %s)" % (self._limit, doing))
            raise

    def screenshot(self, name):
        """Save a screenshot as evidence and return its path relative to the run folder."""
        self._remaining("screenshot")
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "screenshot"
        rel = "screenshots/%02d-%s.png" % (len(self.screenshots) + 1, slug)
        dest = self._run_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._provider.screenshot(dest)
        self.screenshots.append(rel)
        return rel

    def check(self, name, passed, detail=None, visual=False):
        """Record a Check. A failed Check fails the Scenario but does not stop it.

        visual=True marks a judgement made by looking at a screenshot: it is
        reported as "visual, unverified" because it is not deterministic.
        """
        kind = VISUAL if visual else DETERMINISTIC
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail, "kind": kind})
        return bool(passed)


class Spawned:
    """A process g.spawn started in the Guest."""

    def __init__(self, guest, argv, started):
        self.argv = argv
        self.pid = started["pid"]  # on Windows, the cmd.exe that holds its output's redirect
        self.log = started["log"]  # the Guest path of its stdout and stderr, together
        self._guest = guest
        self._started = started
        self._ended = None  # how it ended, once the Scenario stopped it: "scenario" or "exited"

    def __repr__(self):
        return "<Spawned pid %s: %s>" % (self.pid, self.argv)

    def running(self):
        """Is the process still running?"""
        return self._call("running", lambda spawner, timeout: spawner.running(self._started, timeout))

    def stop(self):
        """End the process and the processes it started. Harmless if it has ended already; raises
        if it will not stop."""
        found = self._call("stop", lambda spawner, timeout: spawner.stop(self._started, timeout))
        if self._ended is None:
            self._ended = "scenario" if found == spawning.STOPPED else "exited"

    def output(self):
        """Its stdout and stderr so far, as text (bytes that are not UTF-8 show as \ufffd)."""
        return _text(self._guest.get(self.log, binary=True))

    def _call(self, what, fn):
        g = self._guest
        g._remaining(what)
        return g._on_clock("%s of %s" % (what, self), lambda call_timeout: fn(g._provider.spawner(), call_timeout(what)))


def _text(data):
    return data.decode("utf-8", "replace")


def encode_content(content):
    """g.put's content as bytes: str as UTF-8."""
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    raise UsageError("put takes str or bytes content, not %s" % type(content).__name__)


def decode_content(guest_path, data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError("%s is not UTF-8 text (%s); read it with binary=True" % (guest_path, exc.reason))


def run_scenario(path, guest, prepare):
    """Execute one Scenario file against guest and return its result dict.

    prepare(fresh, launch) readies the Guest (restore, app reset and launch)
    before the Scenario's clock starts, per its FRESH and LAUNCH declarations.
    """
    started = time.time()
    error = None
    try:
        with _folder_on_path(path.parent):
            module = _load(path)
            fresh = _declared(module, "FRESH", False, _is_bool, "True or False")
            launch = _declared(module, "LAUNCH", True, _is_bool, "True or False")
            limit = _declared(module, "TIMEOUT", guest._limit, _is_seconds, "a number of seconds > 0")
            prepare(fresh, launch)
            guest._start_clock(limit)
            module.scenario(guest)
            guest._remaining("the end of the Scenario")
        if not guest.checks:
            raise ScenarioError("recorded no Checks; a Scenario must call g.check() at least once")
    except ConfigError:
        guest._end_spawned(with_output=False)
        raise
    except ScenarioError as exc:
        error = str(exc)
    except (GuestError, UsageError) as exc:
        error = "%s: %s" % (_scenario_line(path, sys.exc_info()[2]), exc)
    except Exception:
        error = traceback.format_exc(limit=-3).strip()

    if error:
        status = "error"
    elif all(c["passed"] for c in guest.checks):
        status = "passed"
    else:
        status = "failed"
    spawned = guest._end_spawned(with_output=status != "passed")
    return {
        "name": path.stem,
        "file": str(path),
        "status": status,
        "duration_s": round(time.time() - started, 3),
        "error": error,
        "checks": guest.checks,
        "screenshots": guest.screenshots,
        "spawned": spawned,
        "channels": guest.channel_use.channels,
        "fallbacks": guest.channel_use.fallbacks,
    }


def _scenario_line(path, tb):
    """'name.py:LINE' of the innermost Scenario frame in tb."""
    lines = [f.lineno for f in traceback.extract_tb(tb) if f.filename == str(path)]
    return "%s:%s" % (path.name, lines[-1]) if lines else path.name


def _declared(module, name, default, valid, expected):
    value = getattr(module, name, default)
    if not valid(value):
        raise ScenarioError("%s = %r is invalid; use %s" % (name, value, expected))
    return value


def _is_bool(value):
    return isinstance(value, bool)


def _is_seconds(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


# Parallel Labs run Scenarios in threads of one process, sharing sys.path and sys.modules.
_import_lock = threading.Lock()
_path_users = collections.Counter()
_scenario_folders = set()


@contextlib.contextmanager
def _folder_on_path(folder):
    """Keep folder on sys.path while any Scenario from it is running."""
    entry = str(folder)
    with _import_lock:
        if not _path_users[entry]:
            sys.path.insert(0, entry)
        _path_users[entry] += 1
        _scenario_folders.add(entry)
    try:
        yield
    finally:
        with _import_lock:
            _path_users[entry] -= 1
            if not _path_users[entry]:
                del _path_users[entry]
                if entry in sys.path:
                    sys.path.remove(entry)


def _load(path):
    spec = importlib.util.spec_from_file_location("vmlab_scenario_%s" % path.stem, str(path))
    module = importlib.util.module_from_spec(spec)
    with _import_lock:
        _forget_scenario_modules()
        # Another Lab's Scenario folder may be on sys.path too; this one's helpers come first.
        entry = str(path.parent)
        sys.path.remove(entry)
        sys.path.insert(0, entry)
        spec.loader.exec_module(module)
    if not callable(getattr(module, "scenario", None)):
        raise ScenarioError("%s does not define scenario(g)" % path)
    return module


def _forget_scenario_modules():
    """Drop modules imported from any Scenario folder, so each Scenario re-imports its own helpers."""
    prefixes = tuple(os.path.join(folder, "") for folder in _scenario_folders)
    for name, module in list(sys.modules.items()):
        if (getattr(module, "__file__", None) or "").startswith(prefixes):
            del sys.modules[name]
