"""Fake Provider: emulates a Guest in a directory under vmlab's home.

It is shipped (not test-only) so the CLI can be exercised end to end without a
hypervisor. The Guest's filesystem is a plain directory; commands run on the
Host with that directory as the working directory and are recorded in
commands.jsonl. Options, under [labs.<name>.fake]:

    ui_tree = "path/to/tree.json"   # scripted UI tree, relative to the config file
"""

import json
import struct
import subprocess
import time
import zlib

from vmlab.config import ConfigError
from vmlab.home import vmlab_home
from vmlab.providers.base import ExecResult, Provider

DEFAULT_TREE = {"role": "desktop", "name": "", "children": []}


class FakeProvider(Provider):
    @property
    def state_dir(self):
        path = vmlab_home() / "fake" / self.guest_id
        (path / "fs").mkdir(parents=True, exist_ok=True)
        return path

    @property
    def _running_marker(self):
        return self.state_dir / "running"

    def _record(self, event, **fields):
        fields.update(event=event, at=time.time())
        with (self.state_dir / "commands.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(fields) + "\n")

    def is_running(self):
        return self._running_marker.exists()

    def up(self):
        if not self.is_running():
            self._running_marker.touch()
            self._record("up")

    def down(self):
        if self.is_running():
            self._running_marker.unlink()
            self._record("down")

    def exec(self, argv, timeout):
        self._record("exec", argv=list(argv))
        proc = subprocess.run(
            list(argv), cwd=str(self.state_dir / "fs"), capture_output=True, text=True, timeout=timeout
        )
        return ExecResult(list(argv), proc.returncode, proc.stdout, proc.stderr)

    def screenshot(self, dest):
        self._record("screenshot")
        dest.write_bytes(_placeholder_png())

    @classmethod
    def validate_options(cls, config_path, key, options):
        for k in sorted(set(options) - {"ui_tree"}):
            raise ConfigError(config_path, "%s.%s" % (key, k), "unknown key", "remove it; allowed keys: ui_tree")
        if "ui_tree" in options:
            _read_tree(config_path.parent / options["ui_tree"], config_path, key + ".ui_tree")

    def ui_tree(self):
        self._record("ui_tree")
        name = self.lab.options.get("ui_tree")
        if not name:
            return DEFAULT_TREE
        key = "labs.%s.fake.ui_tree" % self.lab.name
        return _read_tree(self.project.vmlab_dir / name, self.project.config_path, key)


def _read_tree(path, config_path, key):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(
            config_path,
            key,
            "cannot read scripted UI tree %s: %s" % (path, exc),
            "point it at a JSON file relative to %s" % config_path.parent,
        )

def _placeholder_png():
    """A valid 1x1 grey PNG."""

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b"")
