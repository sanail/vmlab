"""The Provider and Channel interfaces every hypervisor adapter implements.

A Provider starts and stops the Guest that realises one Lab; commands reach the
Guest through the Provider's Channels, in order of preference. A Channel that
fails (cannot connect, dropped session) hands the call to the next one; a
command that exits non-zero, or a call that times out, does not (ADR 0003).
"""

import hashlib
import re
import time

BOOT_POLL_SECONDS = 0.2


class GuestError(Exception):
    """The Guest could not do what was asked, as opposed to a command exiting non-zero."""

    def __init__(self, message, fix=None):
        self.message, self.fix = message, fix
        super().__init__(message + ("\n  fix: %s" % fix if fix else ""))


class ChannelError(GuestError):
    """A Channel could not carry the call. The next Channel may."""


class GuestTimeout(GuestError):
    """A call did not finish in time and was killed."""


class ExecResult:
    def __init__(self, argv, code, stdout, stderr):
        self.argv = argv
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.channel = None  # name of the Channel that served the call
        self.fallbacks = []  # [{"from": name, "to": name, "reason": str}] for Channels that failed first

    @property
    def ok(self):
        return self.code == 0


class Channel:
    """One way of executing commands in a Guest."""

    name = None

    def exec(self, argv, timeout, env):
        """Run argv; return ExecResult whatever its exit code.

        Raise ChannelError when the Channel itself fails, GuestTimeout (after
        killing the call) when it does not finish within timeout seconds.
        """
        raise NotImplementedError


class Provider:
    """Starts, stops and talks to the Guest that realises one Lab.

    Implementations provide detect, is_running, start, stop, is_reachable,
    channels, restore, screenshot and ui_tree; up, down, exec and remove_paths
    are built on those.
    """

    def __init__(self, project, lab):
        self.project = project
        self.lab = lab

    @classmethod
    def validate_options(cls, config_path, key, options):
        """Raise ConfigError if this Provider's [labs.<name>.<provider>] table is wrong."""

    @property
    def guest_id(self):
        """Stable per project and Lab, so projects never share a Guest by accident."""
        digest = hashlib.sha1(str(self.project.root).encode("utf-8")).hexdigest()[:8]
        slug = re.sub(r"[^a-z0-9]+", "-", self.project.root.name.lower()).strip("-") or "project"
        return "vmlab-%s-%s-%s" % (slug, digest, self.lab.name)

    def detect(self):
        """(ok, detail, fix): is the hypervisor behind this Provider installed and usable?"""
        raise NotImplementedError

    def is_running(self):
        raise NotImplementedError

    def start(self):
        """Power the Guest on; return without waiting for it to boot."""
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def is_reachable(self):
        """Has the Guest booted far enough for its Channels to work?"""
        raise NotImplementedError

    def channels(self):
        """The Guest's Channels, preferred first."""
        raise NotImplementedError

    def restore(self):
        """Return the running Guest to its Clean state; it is reachable again afterwards."""
        raise NotImplementedError

    def remove_paths(self, paths, timeout):
        """Delete Guest paths (files or folders; a leading ~ is the Guest user's home) if they exist."""
        if not paths:
            return
        if self.lab.os == "windows":
            # %VARS% expand; ~ is the user profile.
            quoted = ["'%s'" % p.replace("'", "''") for p in paths]
            script = (
                "foreach ($p in @(%s)) { $p = [Environment]::ExpandEnvironmentVariables($p) -replace '^~', $env:USERPROFILE; "
                "Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }" % ", ".join(quoted)
            )
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        else:
            script = 'for p in "$@"; do case $p in "~"|"~/"*) p="$HOME${p#"~"}";; esac; rm -rf -- "$p"; done'
            argv = ["sh", "-c", script, "sh"] + list(paths)
        result = self.exec(argv, timeout)
        if not result.ok:
            raise GuestError("resetting app state %s failed: %s" % (paths, result.stderr.strip()))

    def probe_argv(self):
        """A command that succeeds on any healthy Guest, used to test Channels."""
        return ["cmd", "/c", "exit 0"] if self.lab.os == "windows" else ["true"]

    def screenshot(self, dest):
        """Write a PNG screenshot of the Guest's screen to dest."""
        raise NotImplementedError

    def ui_tree(self):
        """Return the accessibility tree of the Guest's desktop as JSON-able data."""
        raise NotImplementedError

    def up(self):
        """Start the Guest if needed and wait until it is reachable. Idempotent."""
        if not self.is_running():
            self.start()
        deadline = time.time() + self.lab.boot_timeout
        while not self.is_reachable():
            if time.time() >= deadline:
                raise GuestError(
                    "Guest %s was not reachable within %ss of starting" % (self.lab.name, self.lab.boot_timeout),
                    "raise labs.%s.boot_timeout if it is just slow; otherwise check `vmlab doctor %s`"
                    % (self.lab.name, self.lab.name),
                )
            time.sleep(BOOT_POLL_SECONDS)

    def down(self):
        """Stop the Guest if it is running. Idempotent."""
        if self.is_running():
            self.stop()

    def exec(self, argv, timeout, env=None):
        """Run argv in the Guest over the first Channel that works."""
        failures = []
        for channel in self.channels():
            try:
                result = channel.exec(list(argv), timeout, dict(env or {}))
            except ChannelError as exc:
                failures.append((channel.name, exc))
                continue
            result.channel = channel.name
            result.fallbacks = [
                {"from": name, "to": channel.name, "reason": exc.message} for name, exc in failures
            ]
            return result
        raise ChannelError(
            "no Channel reached Guest %s for %s: %s"
            % (self.lab.name, argv, "; ".join("%s: %s" % (name, exc.message) for name, exc in failures)),
            "run `vmlab doctor %s`" % self.lab.name,
        )
