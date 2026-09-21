"""Fake Provider: emulates a Guest in a directory under vmlab's home.

It is shipped (not test-only) so the CLI can be exercised end to end without a
hypervisor. The Guest's filesystem is a plain directory; commands run on the
Host with that directory as the working directory and are recorded in
commands.jsonl. Options, under [labs.<name>.fake]:

    ui_tree = "path/to/tree.json"   # scripted UI tree, relative to the config file
    channels = ["ssh", "exec"]      # Channel names, preferred first
    broken_channels = ["ssh"]       # these Channels fail every call (fault injection)
    hung_channels = ["ssh"]         # these Channels hang until the call times out
    boot_seconds = 0                # how long the Guest takes to become reachable
"""

import json
import os
import shutil
import struct
import subprocess
import time
import zlib

from vmlab import hostproc
from vmlab.config import ConfigError
from vmlab.home import vmlab_home
from vmlab.providers.base import Channel, ChannelError, ExecResult, GuestTimeout, Provider

DEFAULT_TREE = {"role": "desktop", "name": "", "children": []}
DEFAULT_CHANNELS = ["ssh", "exec"]
OPTIONS = ("ui_tree", "channels", "broken_channels", "hung_channels", "boot_seconds")


class FakeProvider(Provider):
    @property
    def state_dir(self):
        path = vmlab_home() / "fake" / self.guest_id
        (path / "fs" / "home").mkdir(parents=True, exist_ok=True)
        return path

    @property
    def fs(self):
        """The Guest's filesystem root; the Guest user's home is fs/home."""
        return self.state_dir / "fs"

    @property
    def _running_marker(self):
        """Exists while the Guest runs; holds the time at which it has finished booting."""
        return self.state_dir / "running"

    def _record(self, event, **fields):
        fields.update(event=event, at=time.time())
        with (self.state_dir / "commands.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(fields) + "\n")

    def detect(self):
        return True, "built in", None

    def is_running(self):
        return self._running_marker.exists()

    def start(self):
        booted_at = time.time() + self.lab.options.get("boot_seconds", 0)
        self._running_marker.write_text(repr(booted_at))
        self._record("up")

    def stop(self):
        self._running_marker.unlink()
        self._record("down")

    def is_reachable(self):
        try:
            return time.time() >= float(self._running_marker.read_text())
        except (OSError, ValueError):
            return False

    def restore(self):
        fs = self.fs
        shutil.rmtree(str(fs))
        (fs / "home").mkdir(parents=True)
        self._record("restore")

    def remove_paths(self, paths, timeout):
        """Guest paths map into fs: ~ to fs/home, / to fs/. Never touches the Host outside fs."""
        self._record("remove_paths", paths=list(paths))
        fs = self.fs.resolve()
        for path in paths:
            if path == "~" or path.startswith("~/"):
                target = fs / "home" / path[2:]
            else:
                target = fs / path.lstrip("/")
            target = target.resolve()
            if fs not in target.parents:
                raise ConfigError(
                    self.project.config_path, "labs.%s.app.state" % self.lab.name, "%r leaves the Guest" % path, "use a path inside the Guest"
                )
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(str(target))
            elif target.exists() or target.is_symlink():
                target.unlink()

    def channels(self):
        return [FakeChannel(self, name) for name in self.lab.options.get("channels", DEFAULT_CHANNELS)]

    def screenshot(self, dest):
        self._record("screenshot")
        dest.write_bytes(_placeholder_png())

    @classmethod
    def validate_options(cls, config_path, key, options):
        for k in sorted(set(options) - set(OPTIONS)):
            raise ConfigError(config_path, "%s.%s" % (key, k), "unknown key", "remove it; allowed keys: %s" % ", ".join(OPTIONS))
        if "ui_tree" in options:
            _read_tree(config_path.parent / options["ui_tree"], config_path, key + ".ui_tree")
        channels = options.get("channels", DEFAULT_CHANNELS)
        if not (isinstance(channels, list) and channels and all(isinstance(c, str) for c in channels)):
            raise ConfigError(config_path, key + ".channels", "must be a non-empty list of names", 'e.g. channels = ["ssh", "exec"]')
        for fault in ("broken_channels", "hung_channels"):
            unknown = [c for c in options.get(fault, []) if c not in channels]
            if unknown:
                raise ConfigError(
                    config_path, "%s.%s" % (key, fault), "unknown Channel %r" % unknown[0], "use names from channels: %s" % ", ".join(channels)
                )
        boot = options.get("boot_seconds", 0)
        if isinstance(boot, bool) or not isinstance(boot, (int, float)) or boot < 0:
            raise ConfigError(config_path, key + ".boot_seconds", "must be a number >= 0", "e.g. boot_seconds = 2")

    def ui_tree(self):
        self._record("ui_tree")
        name = self.lab.options.get("ui_tree")
        if not name:
            return DEFAULT_TREE
        key = "labs.%s.fake.ui_tree" % self.lab.name
        return _read_tree(self.project.vmlab_dir / name, self.project.config_path, key)


class FakeChannel(Channel):
    def __init__(self, provider, name):
        self.provider = provider
        self.name = name

    def exec(self, argv, timeout, env):
        p = self.provider
        options = p.lab.options
        if self.name in options.get("broken_channels", []):
            raise ChannelError(
                "Channel %s is broken (fault injected)" % self.name,
                "remove %r from labs.%s.fake.broken_channels" % (self.name, p.lab.name),
            )
        if not p.is_running():
            raise ChannelError("Guest %s is not running" % p.lab.name, "vmlab up %s" % p.lab.name)
        if not p.is_reachable():
            raise ChannelError("Guest %s is still booting" % p.lab.name)
        p._record("exec", argv=argv, channel=self.name)
        if self.name in options.get("hung_channels", []):
            time.sleep(timeout)
            raise _timeout(argv, timeout, self.name)
        try:
            env = dict(os.environ, HOME=str(p.fs / "home"), **env)
            code, out, err = hostproc.run(argv, timeout, cwd=str(p.fs), env=env)
        except subprocess.TimeoutExpired:
            raise _timeout(argv, timeout, self.name)
        except FileNotFoundError:
            code, out, err = 127, "", "%s: command not found\n" % argv[0]
        return ExecResult(argv, code, out, err)


def _timeout(argv, timeout, channel):
    return GuestTimeout("%s timed out after %ss on Channel %s and was killed" % (argv, timeout, channel))


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
