"""Fake Provider: emulates a Guest in a directory under vmlab's home.

It is shipped (not test-only) so the CLI can be exercised end to end without a
hypervisor. The Guest's filesystem is a plain directory; commands run on the
Host with that directory as the working directory and are recorded in
commands.jsonl. The UI contract serves the scripted tree and emulates a text
editor for stage-text (ui_call). Options, under [labs.<name>.fake]:

    ui_tree = "path/to/tree.json"   # scripted UI tree, relative to the config file
    channels = ["ssh", "exec"]      # Channel names, preferred first
    broken_channels = ["ssh"]       # these Channels fail every call (fault injection)
    hung_channels = ["ssh"]         # these Channels hang until the call times out
    latency = {ssh = 0.2}           # seconds each call over a Channel takes on top (doctor --bench)
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
from vmlab.providers import spawning
from vmlab.providers.base import Channel, ChannelError, ExecResult, GuestError, GuestTimeout, Provider

DEFAULT_TREE = {"role": "desktop", "name": "", "children": []}
DEFAULT_CHANNELS = ["ssh", "exec"]
OPTIONS = ("ui_tree", "channels", "broken_channels", "hung_channels", "latency", "boot_seconds")


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

    def _host_path(self, guest_path, key):
        """Guest paths map into fs: ~ to fs/home, / to fs/. Never outside fs."""
        fs = self.fs.resolve()
        if guest_path == "~" or guest_path.startswith("~/"):
            target = fs / "home" / guest_path[2:]
        else:
            target = fs / guest_path.lstrip("/")
        target = target.resolve()
        if fs not in target.parents:
            raise ConfigError(self.project.config_path, key, "%r leaves the Guest" % guest_path, "use a path inside the Guest")
        return target

    def shell_argv(self, command):
        return ["sh", "-c", command]  # Guest commands run on the Host, whatever the Lab's os

    def copy_in(self, src, guest_dir, timeout=None):
        self._record("copy_in", src=str(src), guest_dir=guest_dir)
        dest = self._host_path(guest_dir, "labs.%s.app" % self.lab.name) / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(str(src), str(dest), symlinks=True)
        else:
            shutil.copy2(str(src), str(dest))
        return str(dest)

    def read_file(self, guest_path, timeout):
        # Commands run on the Host: read where copy_in (and so put_file) writes, over the Channels.
        try:
            path = self._host_path(guest_path, "the Guest path")
        except ConfigError as exc:
            raise GuestError("%s: %s" % (guest_path, exc.problem))
        return self._read_file(guest_path, str(path), timeout)

    def spawner(self):
        # Commands run on the Host, whatever the Lab's os: logs go in the Guest's home, where
        # read_file (g.get) finds them.
        return spawning.PosixSpawner(self, log_dir="~")

    def remove_paths(self, paths, timeout):
        self._record("remove_paths", paths=list(paths))
        for path in paths:
            target = self._host_path(path, "labs.%s.app.state" % self.lab.name)
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
    def validate_options(cls, config_path, key, options, os_name):
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
        latency = options.get("latency", {})
        if not isinstance(latency, dict):
            raise ConfigError(config_path, key + ".latency", "must be a table of Channel = seconds", "e.g. latency = {ssh = 0.2}")
        for name, seconds in sorted(latency.items()):
            if name not in channels:
                raise ConfigError(config_path, "%s.latency.%s" % (key, name), "unknown Channel %r" % name, "use names from channels: %s" % ", ".join(channels))
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
                raise ConfigError(config_path, "%s.latency.%s" % (key, name), "must be a number of seconds >= 0", "e.g. latency = {ssh = 0.2}")
        boot = options.get("boot_seconds", 0)
        if isinstance(boot, bool) or not isinstance(boot, (int, float)) or boot < 0:
            raise ConfigError(config_path, key + ".boot_seconds", "must be a number >= 0", "e.g. boot_seconds = 2")

    def ui_call(self, command, params, timeout):
        """The UI contract without a helper: the scripted tree, plus a text editor that stage-text opens.

        Clicks and key presses are recorded; in the editor, a click focuses the text
        area, cmd+a selects all, cmd+c / cmd+v copy and paste (or ctrl+...), and typing replaces
        the selection. The clipboard and the editor live in the Guest's filesystem,
        so restoring Clean state clears them.
        """
        self._record("ui", command=command, params=params)
        if not self.is_reachable():
            raise ChannelError("Guest %s is not running" % self.lab.name, "vmlab up %s" % self.lab.name)
        state = self._ui_state()
        editor = state.get("editor")
        area = editor["children"][0]["children"][0] if editor else None
        if command == "tree":
            tree = self._scripted_tree()
            apps = tree.get("children", []) + ([editor] if editor else [])
            if state.get("frontmost"):
                apps = [dict(a, focused=a.get("name") == state["frontmost"]) for a in apps]
            if params.get("app"):
                apps = [a for a in apps if a.get("role") == "application" and a.get("name", "").lower() == params["app"].lower()]
            return dict(tree, children=apps)
        if command == "click":
            if area and _inside(area["bounds"], params["x"], params["y"]):
                area["focused"], state["selected"] = True, False
            self._save_ui_state(state)
            return {"x": params["x"], "y": params["y"], "under": None}
        if command == "press":
            shortcut = _shortcut(params)
            if area and area["focused"]:
                if shortcut == "a":
                    state["selected"] = True
                elif shortcut == "c" and state["selected"]:
                    state["clipboard"] = area["value"]
                elif shortcut == "v":
                    _type_into(area, state, state.get("clipboard") or "")
            self._save_ui_state(state)
            return {"key": params["key"], "modifiers": params["modifiers"]}
        if command == "type":
            if area and area["focused"]:
                _type_into(area, state, params["text"])
                self._save_ui_state(state)
            return {"typed": len(params["text"])}
        if command == "clipboard":
            if "set" in params:
                state["clipboard"] = params["set"]
                self._save_ui_state(state)
            return {"text": state.get("clipboard")}
        if command == "focus":
            apps = self._scripted_tree().get("children", []) + ([editor] if editor else [])
            [app] = [a for a in apps if a.get("role") == "application" and a.get("name", "").lower() == params["app"].lower()] or [None]
            if app is None:
                raise GuestError("%s is not running" % params["app"])
            window = None
            if "window" in params:
                titles = [w.get("name", "") for w in app.get("children", []) if w.get("role") == "window"]
                window = next((t for t in titles if params["window"] in t), None)
                if window is None:
                    raise GuestError("%s has no window titled like %r; its windows: %s" % (app["name"], params["window"], titles))
            state["frontmost"] = app["name"]
            self._save_ui_state(state)
            return {"app": app["name"], "window": window, "frontmost": app["name"]}
        if command == "stage-text":
            path = "/tmp/vmlab-stage.txt"
            window = {"role": "window", "name": "vmlab-stage.txt", "bounds": {"x": 100, "y": 100, "w": 600, "h": 400}, "children": [
                {"role": "textarea", "value": params["text"], "focused": True, "bounds": {"x": 100, "y": 130, "w": 600, "h": 370}},
            ]}  # fmt: skip
            state["editor"] = {"role": "application", "name": params["app"], "pid": 0, "focused": True, "children": [window]}
            state["selected"], state["frontmost"] = True, params["app"]
            pressed = params.get("then")
            if pressed and _shortcut(pressed) == "c":
                state["clipboard"] = params["text"]
            self._save_ui_state(state)
            return {"app": params["app"], "file": path, "frontmost": params["app"], "selected": params["text"], "pressed": pressed}
        raise GuestError("unknown UI command %r" % command)

    def ui_helper(self):
        return _EmulatedHelper()

    def _scripted_tree(self):
        name = self.lab.options.get("ui_tree")
        if not name:
            return DEFAULT_TREE
        key = "labs.%s.fake.ui_tree" % self.lab.name
        return _read_tree(self.project.vmlab_dir / name, self.project.config_path, key)

    def _ui_state(self):
        path = self.fs / "ui-state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"clipboard": None, "editor": None, "selected": False}

    def _save_ui_state(self, state):
        (self.fs / "ui-state.json").write_text(json.dumps(state), encoding="utf-8")


class FakeChannel(Channel):
    def __init__(self, provider, name):
        self.provider = provider
        self.name = name

    def exec(self, argv, timeout, env, stdin=None):
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
        time.sleep(options.get("latency", {}).get(self.name, 0))
        if self.name in options.get("hung_channels", []):
            time.sleep(timeout)
            raise _timeout(argv, timeout, self.name)
        try:
            env = dict(os.environ, HOME=str(p.fs / "home"), **env)
            code, out, err = hostproc.run(argv, timeout, cwd=str(p.fs), env=env, stdin=stdin)
        except subprocess.TimeoutExpired:
            raise _timeout(argv, timeout, self.name)
        except FileNotFoundError:
            code, out, err = 127, "", "%s: command not found\n" % argv[0]
        return ExecResult(argv, code, out, err)


class _EmulatedHelper:
    def describe(self, timeout):
        return True, "emulated by the Fake Provider"


def _shortcut(chord):
    """The key of an editing shortcut, cmd+<key> or ctrl+<key> (whichever the Lab's OS uses), else None."""
    return chord["key"] if chord["modifiers"] in (["cmd"], ["ctrl"]) else None


def _inside(bounds, x, y):
    return bounds["x"] <= x < bounds["x"] + bounds["w"] and bounds["y"] <= y < bounds["y"] + bounds["h"]


def _type_into(area, state, text):
    area["value"] = text if state["selected"] else area["value"] + text
    state["selected"] = False


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
