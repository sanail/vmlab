"""vmlab's per-user home directory (keys, credentials, registries, Fake Guests)."""

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows Hosts (not supported in v1): threads are still serialised
    fcntl = None

_lock = threading.Lock()
ACQUIRE_TRIES, ACQUIRE_PAUSE = 5, 0.05  # s


def vmlab_home():
    home = Path(os.environ.get("VMLAB_HOME") or Path.home() / ".vmlab")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return home


class StartedGuests:
    """The Guests vmlab started and still owns. vmlab stops only these; a Guest the
    user started (or claimed with `vmlab up`) is never in here."""

    def __init__(self):
        self.path = vmlab_home() / "started.json"

    def _update(self, change):
        # The thread lock covers --parallel; flock covers concurrent vmlab processes.
        with _lock, (vmlab_home() / "started.lock").open("a") as lock_file:
            if fcntl:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                keys = set(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                keys = set()
            result = change(keys)
            self.path.write_text(json.dumps(sorted(keys)), encoding="utf-8")
            return result

    def add(self, key):
        self._update(lambda keys: keys.add(key))

    def discard(self, key):
        self._update(lambda keys: keys.discard(key))

    def __contains__(self, key):
        return self._update(lambda keys: key in keys)


class GuestInUse(Exception):
    """Another vmlab process holds the Guest's lock."""


class GuestLock:
    """Held while one vmlab process uses a Guest, so two Runs never share it.

    An flock, so it dies with its process. The lock file names the holder for
    the error the next process shows.
    """

    def __init__(self, key):
        self.key = key
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        self.path = vmlab_home() / "locks" / ("%s.lock" % digest)
        self._file = None

    def acquire(self):
        """Take the lock or raise GuestInUse naming the holder."""
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        lock_file = self.path.open("a+")
        if fcntl:
            # A few tries: `vmlab doctor` holds it for a moment to see who has it (holder).
            for attempt in range(ACQUIRE_TRIES):
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if attempt == ACQUIRE_TRIES - 1:
                        lock_file.seek(0)
                        holder = lock_file.read().strip() or "unknown"
                        lock_file.close()
                        raise GuestInUse(holder)
                    time.sleep(ACQUIRE_PAUSE)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write("pid %d: %s" % (os.getpid(), " ".join(["vmlab"] + sys.argv[1:])))
        lock_file.flush()
        self._file = lock_file

    def holder(self):
        """Who holds the lock now, or None, without taking it for longer than a moment."""
        if not (fcntl and self.path.exists()):
            return None
        with self.path.open("r") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return lock_file.read().strip() or "unknown"
        return None

    def release(self):
        if self._file:
            self._file.close()  # closing drops the flock
            self._file = None


class BaseGuestLock:
    """Held while a Lab copies a Base guest, and while vmlab changes it (installs a language
    into a Windows one), across vmlab processes and the threads of a --parallel Run: each
    holder opens the file itself, and flock sets two open files of one process against each
    other too. A context manager; waiting() is called once when another holder makes it wait."""

    def __init__(self, name, waiting=None):
        self.path = vmlab_home() / "locks" / ("base-%s.lock" % name)
        self.waiting = waiting
        self._file = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        self._file = self.path.open("a")
        if fcntl:
            try:
                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if self.waiting:
                    self.waiting()
                fcntl.flock(self._file, fcntl.LOCK_EX)
        else:
            _lock.acquire()
        return self

    def __exit__(self, *exc):
        if not fcntl:
            _lock.release()
        self._file.close()
        self._file = None
