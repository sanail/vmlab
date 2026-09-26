"""Step lines for `deploy` and `run`: `LAB: step` when a step starts and `LAB: step done in 41s`
when it ends, so a hung step can be told from a slow one. A step that fails prints no done
line: the error follows it. `--quiet` gives the runner none (QUIET).
"""

import time
from contextlib import contextmanager


def duration(seconds):
    """'41s', or '2m05s' from a minute on; rounded to the second."""
    seconds = int(round(seconds))
    return "%ds" % seconds if seconds < 60 else "%dm%02ds" % divmod(seconds, 60)


class Progress:
    """One Lab's step lines, written to out (a print-like callable), or nowhere when out is None."""

    def __init__(self, out, lab_name):
        self.out, self.lab_name = out, lab_name

    def say(self, what):
        if self.out:
            self.out("%s: %s" % (self.lab_name, what))

    @contextmanager
    def step(self, what, detail=None):
        """Lines for the step what; detail (e.g. a log's path) is told only as it starts."""
        self.say("%s %s" % (what, detail) if detail else what)
        started = time.time()
        yield
        self.say("%s done in %s" % (what, duration(time.time() - started)))


QUIET = Progress(None, None)
