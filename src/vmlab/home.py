"""vmlab's per-user home directory (keys, credentials, registries, Fake Guests)."""

import json
import os
import threading
from pathlib import Path

_lock = threading.Lock()


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
        with _lock:
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
