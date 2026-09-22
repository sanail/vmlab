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
"""

import importlib.util
import re
import sys
import time
import traceback

from vmlab import ui
from vmlab.config import ConfigError, UsageError
from vmlab.providers.base import GuestError, GuestTimeout

DETERMINISTIC, VISUAL = "deterministic", "visual, unverified"


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
        self.arch = lab.arch
        self._provider = provider
        self._run_dir = run_dir
        self._launch_app = launch_app
        self._step_timeout = lab.step_timeout
        self._limit = lab.scenario_timeout
        self._deadline = None
        self.checks = []
        self.screenshots = []
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
        """Launch the app with the Lab's launch recipe; env adds to the Lab's app.env."""
        self._remaining("launch")
        self._launch_app(env)

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

    def wait_for(self, text=None, role=None, app=None, gone=False, process=None, file=None, log=None, pattern=None, timeout=None):
        """Wait until one condition holds: an element appears (or is gone), a process runs, a file
        exists, or a log file has a line matching pattern. Returns {"met": bool, ...}; never raises
        for an unmet condition. timeout defaults to the Lab's step_timeout."""
        condition = ui.condition(text, role, app, gone, process, file, log, pattern)
        return self._ui_call(lambda contract: contract.wait_for(condition, timeout=timeout))

    def _ui_call(self, fn):
        self._remaining("a UI command")
        try:
            return fn(ui.UI(self._provider, lambda doing: min(self._step_timeout, self._remaining(doing))))
        except GuestTimeout as exc:
            if not isinstance(exc, ScenarioTimeout) and time.time() >= self._deadline:
                raise ScenarioTimeout("Scenario exceeded its %ss timeout (during a UI command)" % self._limit)
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


def run_scenario(path, guest, prepare):
    """Execute one Scenario file against guest and return its result dict.

    prepare(fresh, launch) readies the Guest (restore, app reset and launch)
    before the Scenario's clock starts, per its FRESH and LAUNCH declarations.
    """
    started = time.time()
    error = None
    try:
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
    return {
        "name": path.stem,
        "file": str(path),
        "status": status,
        "duration_s": round(time.time() - started, 3),
        "error": error,
        "checks": guest.checks,
        "screenshots": guest.screenshots,
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


def _load(path):
    spec = importlib.util.spec_from_file_location("vmlab_scenario_%s" % path.stem, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "scenario", None)):
        raise ScenarioError("%s does not define scenario(g)" % path)
    return module
