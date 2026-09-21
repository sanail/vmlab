"""vmlab's per-user home directory (keys, credentials, registries, Fake Guests)."""

import os
from pathlib import Path


def vmlab_home():
    home = Path(os.environ.get("VMLAB_HOME") or Path.home() / ".vmlab")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return home
