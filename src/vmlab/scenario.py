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
# How a spawned process ended: its report entry's "ended".
BY_SCENARIO, WITH_RUN, BY_ITSELF, STILL_RUNNING = "scenario", "run", "exited", "failed"
# How a Staged document ended: its report entry's "ended" (BY_SCENARIO and WITH_RUN too).
ALREADY_GONE, STILL_OPEN = "gone", "failed"


class ScenarioError(Exception):
    """The Scenario itself is broken (as opposed to a Check failing)."""


class ScenarioTimeout(GuestTimeout):
    """The Scenario as a whole ran out of time."""


class Interrupted(KeyboardInterrupt):
    """Ctrl-C, taken by another thread: this Lab's Suite run ends as a serial one does on Ctrl-C."""

    def __init__(self):
        super().__init__("the Run was interrupted (Ctrl-C)")


class ChannelUse:
    """Which Channels served a series of Guest calls, and the fallbacks on the way."""

    def __init__(self):
        self.channels = {}  # Channel name -> calls it served
        self.fallbacks = []

    def record(self, result):
        self.channels[result.channel] = self.channels.get(result.channel, 0) + 1
        self.fallbacks.extend(dict(f, argv=result.argv) for f in result.fallbacks)


class Guest:
    def __init__(self, lab, provider, run_dir, shots_dir, launch_app, quit_app, interrupt=None):
        self.lab = lab.name
        self.os = lab.os
        self.arch = lab.arch  # the Build artifact's
        self.guest_arch = arch.guest_arch()  # differs from arch when the Guest OS emulates it
        self._provider = provider
        self._run_dir = run_dir
        self._shots_dir = shots_dir  # this Scenario's screenshots folder, relative to the run folder
        self._launch_app = launch_app
        self._quit_app = quit_app
        self._notification_id = lab.app.notification_id
        self._started = time.time()  # the Run's start: Notifications from before it are left out
        self._step_timeout = lab.step_timeout
        self._limit = lab.scenario_timeout
        self._deadline = None
        self._interrupt = interrupt  # a threading.Event set on Ctrl-C in a parallel Run
        self.checks = []
        self.screenshots = []
        self.spawned = []  # every process the Scenario spawned (Spawned), stopped at the end of the Run
        self.staged = []  # every Staged document it opened ({"file", "app", "ended"}), closed at the end of the Run
        self._spawner = provider.spawner()
        self.channel_use = ChannelUse()  # this Run's calls, including its app reset and launch

    def _start_clock(self, limit):
        self._limit = limit
        self._deadline = time.time() + limit

    def _remaining(self, doing):
        if self._interrupt is not None and self._interrupt.is_set():
            raise Interrupted()
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

    def quit(self, env=None):
        """Quit the app with the Lab's quit recipe; env adds to the Lab's app.env. Waits, on the
        Scenario's clock, until the app's process (the Lab's app.process, or ready's process) has
        gone; raises when it has not within app.quit_timeout. With no such process, raises when
        the recipe exits non-zero, and waits for nothing."""
        self._remaining("quit")
        self._on_clock("quit", lambda call_timeout: self._quit_app(env, call_timeout))

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
        process = self._spawner_call("spawn", "spawn %s" % argv, lambda spawner, timeout: spawner.start(argv, dict(env or {}), timeout))
        handle = Spawned(self, argv, process)
        self.spawned.append(handle)
        return handle

    def _spawner_call(self, what, doing, fn):
        """fn(spawner, timeout) on the Scenario's clock: a call of what the spawned processes ride on."""
        self._remaining(what)
        return self._on_clock(doing, lambda call_timeout: fn(self._spawner, call_timeout(what)))

    def _end_run(self, with_output):
        """End what the Scenario left behind in the Guest, best-effort and off its clock: close its
        Staged documents, stop what it spawned. Returns the report's entries (staged, spawned);
        with_output adds the tail of each spawned process's output. Once a call finds no Guest, the
        rest are not tried."""
        unreachable = []

        def attempt(fn):
            if unreachable:
                raise GuestError("not tried: %s" % unreachable[0])
            try:
                return fn()
            except (ChannelError, GuestTimeout) as exc:
                unreachable.append(str(exc).splitlines()[0])
                raise

        return self._close_staged_left(attempt), self._end_spawned(attempt, with_output)

    def _close_staged_left(self, attempt):
        contract = ui.UI(self._provider, lambda what: self._step_timeout)
        for doc in self.staged:
            if doc["ended"] is None:
                try:
                    closed = attempt(lambda: contract.close_staged(doc["file"], app=doc["app"]))["closed"]
                    doc["ended"] = WITH_RUN if closed else ALREADY_GONE
                except Exception as exc:  # noted in the report; it never changes the Run's result
                    doc["ended"], doc["close_error"] = STILL_OPEN, str(exc) or type(exc).__name__
        return [dict(doc) for doc in self.staged]

    def _end_spawned(self, attempt, with_output):
        """with_output keeps each process's whole output in the run folder (spawned/<its log's
        name>) and its tail in the report. The logs of the processes that ended are deleted from
        the Guest; one still running keeps writing to its own."""
        entries, ended_logs = [], []
        for handle in self.spawned:
            entry = {"argv": handle.argv, "pid": handle.pid, "log": handle.log, "ended": handle._ended}
            if entry["ended"] is None:
                try:
                    entry["ended"] = _ended(attempt(lambda: self._spawner.stop(handle._process, self._step_timeout)), WITH_RUN)
                except Exception as exc:  # noted in the report; it never changes the Run's result
                    entry["ended"], entry["stop_error"] = STILL_RUNNING, str(exc) or type(exc).__name__
            if with_output:
                try:
                    output = _text(attempt(lambda: self._provider.read_file(handle.log, self._step_timeout)))
                    entry["output_tail"] = output[-OUTPUT_TAIL:]
                    entry["output_file"] = self._keep_output(handle.log, output)
                except Exception as exc:
                    entry["output_tail"], entry["output_error"] = None, str(exc) or type(exc).__name__
            if entry["ended"] != STILL_RUNNING:
                ended_logs.append(handle.log)
            entries.append(entry)
        try:
            attempt(lambda: self._provider.remove_paths(ended_logs, self._step_timeout))
        except Exception:  # best-effort: a log left in the Guest's temp folder harms no Run
            pass
        return entries

    def _keep_output(self, guest_log, output):
        """Write a spawned process's output to the run folder; its path relative to the folder."""
        rel = "spawned/%s" % re.split(r"[\\/]", guest_log)[-1]
        dest = self._run_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(output, encoding="utf-8")
        return rel

    # The UI contract (vmlab.ui): each method returns what `vmlab ui <command>` prints.

    def tree(self, app=None):
        """The accessibility tree of the Guest's desktop, or of one app."""
        return self._ui_call(lambda contract: contract.tree(app))

    def find(self, text=None, role=None, app=None):
        """{"matches": [...]}: elements by text (exact beats substring) and/or role, optionally in one app."""
        return self._ui_call(lambda contract: contract.find(ui.Query(text, role, app)))

    def click(self, text=None, role=None, app=None, index=0, at=None, timeout=None):
        """Click the index-th matching element's middle, or the point at=(x, y). Raises if nothing
        matches or something covers it; with timeout (seconds), first waits for it to be there and
        uncovered (on the Scenario's clock), then raises with the last reason."""
        return self._ui_call(lambda contract: contract.click(ui.Query(text, role, app), index=index, at=at, timeout=timeout))

    def tray(self, app, choose=None, timeout=None):
        """Read app's Tray menu: {"items": [{"name", "enabled", "checked", "children"}], "chosen"}.
        choose, a label or a list of labels (one per submenu level), chooses that item. Raises when
        the app has no Tray icon, no item has a label, the item is disabled, or it opens a submenu;
        with timeout (seconds), first waits for the Tray icon to appear (on the Scenario's clock)."""
        return self._ui_call(lambda contract: contract.tray(app, choose=choose, timeout=timeout))

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
        """Open text in a third-party editor, select it all and press the chord then, all in one Guest
        call. The result's "file" is the Staged document: close it with close_staged when done;
        whatever is left open is closed at the end of the Run."""
        result = self._ui_call(lambda contract: contract.stage_text(text, app=app, then=then))
        self.staged.append({"file": result["file"], "app": result["app"], "ended": None})
        return result

    def close_staged(self, staged):
        """Save and close one Staged document: a stage_text result, or its "file". Returns
        {"file", "closed"}; closed is false when it was no longer open."""
        file, app = (staged.get("file"), staged.get("app")) if isinstance(staged, dict) else (staged, None)
        if not isinstance(file, str):
            raise UsageError("close_staged takes a stage_text result or its \"file\", not %r" % (staged,))
        doc = next((d for d in self.staged if d["file"] == file), None)
        if doc:
            app = doc["app"]
        result = self._ui_call(lambda contract: contract.close_staged(file, app=app))
        if doc and doc["ended"] is None:
            doc["ended"] = BY_SCENARIO
        return result

    def notifications(self, app=None, text=None, since=None):
        """{"notifications": [{"app", "title", "body", "time"}]}: the Notifications the Guest's OS
        recorded, oldest first, time in ISO 8601 UTC on the Guest's clock. app is the OS's id for
        the sender (default: the Lab's app.notification_id, else every app's); text a pattern
        searched for in title and body; since a Guest time (ISO 8601), by default the Run's start,
        or "all"."""
        app, since = self._notification_args(app, since)
        return self._ui_call(lambda contract: contract.notifications(app, text, since))

    def _notification_args(self, app, since):
        """app and since as vmlab.ui takes them: the Lab's notification_id, the Run's start, "all"."""
        if since is not None and not isinstance(since, str):
            raise UsageError('since takes a Guest time in ISO 8601 or "all", not %r' % (since,))
        if since is None:
            since = ui.HostTime(self._started)
        elif since == "all":
            since = None
        return (self._notification_id if app is None else app), since

    def wait_for(self, text=None, role=None, app=None, gone=False, process=None, file=None, log=None, pattern=None, exec=None,
                 notification=None, since=None, tray=None, timeout=None):  # fmt: skip
        """Wait until one condition holds: an element appears, a process runs, a file exists, a log
        file has a line matching pattern, the command exec (an argv) exits 0 (with pattern: its
        stdout matches), a Notification matching the pattern notification is posted (app and
        since as for notifications), or the app tray's Tray icon is there (as g.tray finds it; its
        menu is not opened). gone=True waits for the condition to stop holding instead.
        Returns {"met": bool, ...}; never raises for an unmet condition. timeout defaults to the Lab's step_timeout."""
        if notification is not None:
            app, since = self._notification_args(app, since)
        condition = ui.condition(
            text=text, role=role, app=app, gone=gone, process=process, file=file, log=log, pattern=pattern, exec=exec,
            notification=notification, since=since, tray=tray, named=str,
        )  # fmt: skip
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
        rel = "%s/%02d-%s.png" % (self._shots_dir, len(self.screenshots) + 1, slug)
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
        self.checks.append({"name": name, "passed": bool(passed), "skipped": False, "detail": detail, "kind": kind})
        return bool(passed)

    def skip(self, name, reason):
        """Record a Skipped Check: one this Run cannot measure (it depends on an earlier Check
        that failed, or does not apply on this Guest OS), with the reason why. It neither passes
        nor fails the Scenario, and its "passed" in the report is None.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise UsageError("g.skip needs a reason: why this Run cannot measure %r, not %r" % (name, reason))
        self.checks.append({"name": name, "passed": None, "skipped": True, "detail": reason, "kind": DETERMINISTIC})


PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"  # a Check's outcome, as the console and summary.md mark it


def outcome(check):
    """A Check's (its report entry's) PASS, FAIL or SKIP: a Skipped Check neither passed nor failed."""
    return SKIP if check["skipped"] else PASS if check["passed"] else FAIL


def failed(check):
    return outcome(check) == FAIL


class Spawned:
    """The handle g.spawn returns for a process it started in the Guest."""

    def __init__(self, guest, argv, process):
        self.argv = argv
        self._guest = guest
        self._process = process  # spawning.Process
        self._ended = None  # how it ended, once the Scenario stopped it: BY_SCENARIO or BY_ITSELF

    @property
    def pid(self):
        """The program's pid (on Windows, cmd.exe's for a batch file, which cmd.exe runs)."""
        return self._process.pid

    @property
    def log(self):
        """The Guest path of its stdout and stderr, together."""
        return self._process.log

    def __repr__(self):
        return "<Spawned pid %s: %s>" % (self.pid, self.argv)

    def running(self):
        """Is the process itself still running? (What it started may run on after it.)"""
        return self._guest._spawner_call("running", "running of %s" % self, lambda spawner, timeout: spawner.running(self._process, timeout))

    def stop(self):
        """End the process and the processes it started, those too if it has exited itself. Harmless
        once all have ended; raises if they will not stop."""
        found = self._guest._spawner_call("stop", "stop of %s" % self, lambda spawner, timeout: spawner.stop(self._process, timeout))
        if self._ended is None:
            self._ended = _ended(found, BY_SCENARIO)

    def output(self):
        """Its stdout and stderr so far, as text (bytes that are not UTF-8 show as \ufffd)."""
        return _text(self._guest.get(self.log, binary=True))


def _ended(found, stopped_by):
    """A spawned process's report "ended" once stop found what spawning.STOPPED or EXITED says."""
    return stopped_by if found == spawning.STOPPED else BY_ITSELF


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


def run_scenario(path, guest, prepare, unreported):
    """Execute one Scenario file against guest and return its result dict.

    prepare(fresh, launch) readies the Guest (restore, app reset and launch)
    before the Scenario's clock starts, per its FRESH and LAUNCH declarations.

    What the Scenario staged and spawned ends with the Run, however it ends.
    sys.exit() in the Scenario errors it like any exception. When it ends with
    no result (a ConfigError, Ctrl-C), the exception goes on after that, and
    unreported(staged, spawned) gets the report entries of its Staged documents
    and spawned processes, since no report will name them.
    """
    started = time.time()
    try:
        error = _execute(path, guest, prepare)
    except BaseException:
        unreported(*guest._end_run(with_output=False))
        raise

    if error:
        status = "error"
    elif any(failed(c) for c in guest.checks):
        status = "failed"
    else:
        status = "passed"
    staged, spawned = guest._end_run(with_output=status != "passed")
    return {
        "name": path.stem,
        "file": str(path),
        "status": status,
        "duration_s": round(time.time() - started, 3),
        "error": error,
        "checks": guest.checks,
        "screenshots": guest.screenshots,
        "staged": staged,
        "spawned": spawned,
        "channels": guest.channel_use.channels,
        "fallbacks": guest.channel_use.fallbacks,
    }


def _execute(path, guest, prepare):
    """Run the Scenario: its error as the report gives it, or None. Raises what ends the Run with no result."""
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
        if all(c["skipped"] for c in guest.checks):
            recorded = "every Check it recorded is skipped" if guest.checks else "it recorded no Checks"
            raise ScenarioError("measured no Check (%s); a Scenario must call g.check() at least once" % recorded)
    except ConfigError:
        raise
    except ScenarioError as exc:
        return str(exc)
    except (GuestError, UsageError) as exc:
        return "%s: %s" % (_scenario_line(path, sys.exc_info()[2]), exc)
    except SystemExit as exc:  # the Scenario author means "stop this Scenario", not the Runner
        called = "sys.exit()" if exc.code is None else "sys.exit(%r)" % (exc.code,)
        return "%s: the Scenario called %s" % (_scenario_line(path, sys.exc_info()[2]), called)
    except Exception:
        return traceback.format_exc(limit=-3).strip()
    return None


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
