"""The Scenario API: the object a Scenario's scenario(g) function receives.

A Scenario is a Python file in .vmlab/scenarios defining:

    def scenario(g):
        r = g.exec(["echo", "hello"])
        g.check("echo prints hello", r.stdout.strip() == "hello")
"""

import importlib.util
import re
import sys
import time
import traceback

from vmlab.config import ConfigError
from vmlab.providers.base import GuestError

DEFAULT_EXEC_TIMEOUT = 60


class ScenarioError(Exception):
    """The Scenario itself is broken (as opposed to a Check failing)."""


class Guest:
    def __init__(self, lab, provider, run_dir):
        self.lab = lab.name
        self.os = lab.os
        self.arch = lab.arch
        self._provider = provider
        self._run_dir = run_dir
        self.checks = []
        self.screenshots = []
        self.channels = {}  # Channel name -> calls it served
        self.fallbacks = []

    def exec(self, argv, timeout=DEFAULT_EXEC_TIMEOUT, env=None):
        """Run argv in the Guest. Returns an object with code, stdout, stderr, ok and channel.

        Raises (failing the Run) when no Channel reaches the Guest or the call
        exceeds timeout seconds; a non-zero exit code is returned, not raised.
        """
        result = self._provider.exec(list(argv), timeout=timeout, env=env)
        self.channels[result.channel] = self.channels.get(result.channel, 0) + 1
        self.fallbacks.extend(dict(f, argv=result.argv) for f in result.fallbacks)
        return result

    def tree(self):
        """The accessibility tree of the Guest's desktop."""
        return self._provider.ui_tree()

    def screenshot(self, name):
        """Save a screenshot as evidence and return its path relative to the run folder."""
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "screenshot"
        rel = "screenshots/%02d-%s.png" % (len(self.screenshots) + 1, slug)
        dest = self._run_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._provider.screenshot(dest)
        self.screenshots.append(rel)
        return rel

    def check(self, name, passed, detail=None):
        """Record a Check. A failed Check fails the Scenario but does not stop it."""
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail})
        return bool(passed)


def run_scenario(path, guest):
    """Execute one Scenario file against guest and return its result dict."""
    started = time.time()
    error = None
    try:
        _load(path).scenario(guest)
        if not guest.checks:
            raise ScenarioError("recorded no Checks; a Scenario must call g.check() at least once")
    except ConfigError:
        raise
    except ScenarioError as exc:
        error = str(exc)
    except GuestError as exc:
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
        "channels": guest.channels,
        "fallbacks": guest.fallbacks,
    }


def _scenario_line(path, tb):
    """'name.py:LINE' of the innermost Scenario frame in tb."""
    lines = [f.lineno for f in traceback.extract_tb(tb) if f.filename == str(path)]
    return "%s:%s" % (path.name, lines[-1]) if lines else path.name


def _load(path):
    spec = importlib.util.spec_from_file_location("vmlab_scenario_%s" % path.stem, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "scenario", None)):
        raise ScenarioError("%s does not define scenario(g)" % path)
    return module
