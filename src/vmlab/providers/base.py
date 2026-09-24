"""The Provider and Channel interfaces every hypervisor adapter implements.

A Provider starts and stops the Guest that realises one Lab; commands reach the
Guest through the Provider's Channels, in order of preference. A Channel that
fails (cannot connect, dropped session) hands the call to the next one; a
command that exits non-zero, or a call that times out, does not (ADR 0003).
"""

import base64
import hashlib
import re
import tarfile
import tempfile
import time
from pathlib import Path

from vmlab.config import UsageError

BOOT_POLL_SECONDS = 0.2
# How bad a doctor finding is (vmlab.doctor). Only FAIL makes doctor fail.
OK, INFO, WARN, FAIL = "ok", "info", "warn", "FAIL"
NO_FILE = 3  # read_file's exit code in the Guest when the path is not a file


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

    def send_file(self, local, guest_path):
        """Copy the Host file local to the Guest path guest_path. Channels that carry files
        themselves implement it; Windows Guests need it, where exec's stdin cannot carry much."""
        raise NotImplementedError

    def exec(self, argv, timeout, env, stdin=None):
        """Run argv; return ExecResult whatever its exit code.

        stdin is an open binary file fed to the command (e.g. an archive for
        copy_in), or None. Raise ChannelError when the Channel itself fails, GuestTimeout (after
        killing the call) when it does not finish within timeout seconds.
        """
        raise NotImplementedError


class Provider:
    """Starts, stops and talks to the Guest that realises one Lab.

    Implementations provide detect, is_running, start, stop, is_reachable,
    channels, restore, copy_in and screenshot; up, down, exec, shell_argv,
    remove_paths and ui_call are built on those.
    """

    NOT_IMPLEMENTED = None  # set by stubs: the fix shown when a Lab selects this Provider
    HYPERVISOR = None  # its hypervisor's name, for doctor's list of what this Host has
    SUPPORTED_OS = None  # the Lab OSes this Provider can run, or None for all

    def __init__(self, project, lab):
        self.project = project
        self.lab = lab
        self.on_exec = None  # called with every ExecResult, so reports can note Channels and fallbacks
        self._ui_helper = None

    @classmethod
    def validate_options(cls, config_path, key, options, os_name):
        """Raise ConfigError if this Provider's [labs.<name>.<provider>] table is wrong."""

    @property
    def guest_id(self):
        """Stable per project and Lab, so projects never share a Guest by accident."""
        digest = hashlib.sha1(str(self.project.root).encode("utf-8")).hexdigest()[:8]
        slug = re.sub(r"[^a-z0-9]+", "-", self.project.root.name.lower()).strip("-") or "project"
        return "vmlab-%s-%s-%s" % (slug, digest, self.lab.name)

    @classmethod
    def hypervisor(cls):
        """(found, detail, fix) for the hypervisor named HYPERVISOR, without a Lab."""
        raise NotImplementedError

    def detect(self):
        """(ok, detail, fix): is the hypervisor behind this Provider installed and usable?"""
        raise NotImplementedError

    def diagnose(self):
        """doctor's checks of what the Guest is made from, answerable while it is stopped: its
        Base guest, its clone. [(check, status, detail, fix)]; a FAIL means the Guest cannot start."""
        return []

    def diagnose_guest(self):
        """doctor's checks of the running Guest beyond its Channels and UI helper, which work by now:
        [(check, status, detail, fix)]."""
        return []

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

    def copy_in(self, src, guest_dir, timeout=None):
        """Copy the Host file or folder src into guest_dir (created; ~ is the Guest user's home),
        within timeout seconds (default: the Lab's app.install_timeout).

        Returns the absolute Guest path of the copy.
        """
        raise NotImplementedError

    def put_file(self, guest_path, data, timeout):
        """Write the bytes data to guest_path, making its folders; ~ is the Guest user's home and,
        on Windows, %VARS% expand. Returns the absolute Guest path. Built on copy_in."""
        folder, name = split_guest_path(guest_path, self.lab.os)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / name
            local.write_bytes(data)
            return self.copy_in(local, folder, timeout)

    def read_file(self, guest_path, timeout):
        """The bytes of the Guest file guest_path (same path rules as put_file); GuestError if it is
        not there. They travel base64-encoded on exec's stdout, which carries text."""
        return self._read_file(guest_path, guest_path, timeout)

    def _read_file(self, guest_path, path, timeout):
        """read_file of path in the Guest, named guest_path in errors."""
        if self.lab.os == "windows":
            script = (
                "$p = %s; if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { exit %d }; "
                "[Convert]::ToBase64String([IO.File]::ReadAllBytes((Get-Item -LiteralPath $p).FullName))" % (ps_path(path), NO_FILE)
            )
            argv = self.shell_argv(script)
        else:
            script = 'p=$1; case $p in "~"|"~/"*) p="$HOME${p#"~"}";; esac; [ -f "$p" ] || exit %d; base64 < "$p"' % NO_FILE
            argv = ["sh", "-c", script, "sh", path]
        result = self.exec(argv, timeout)
        if result.code == NO_FILE:
            raise GuestError("no file %s in Guest %s" % (guest_path, self.lab.name), "check the path; ~ is the Guest user's home")
        if not result.ok:
            raise GuestError("reading %s in the Guest failed: %s" % (guest_path, result.stderr.strip() or "exit %d" % result.code))
        try:
            return base64.b64decode(result.stdout)
        except ValueError:
            raise GuestError("reading %s in the Guest returned no base64: %r" % (guest_path, result.stdout[:200]))

    def send_file(self, local, guest_path):
        """Copy the Host file local into the Guest over the first Channel that can carry it."""
        failures = []
        for channel in self.channels():
            try:
                channel.send_file(local, guest_path)
                return
            except ChannelError as exc:
                failures.append((channel.name, exc))
        raise ChannelError(
            "no Channel carried %s into Guest %s: %s" % (local, self.lab.name, "; ".join("%s: %s" % (name, exc.message) for name, exc in failures)),
            "run `vmlab doctor %s`" % self.lab.name,
        )

    def copy_in_by_tar(self, src, guest_dir, timeout=None):
        """copy_in for POSIX Guests: a tar stream over exec's stdin keeps bundles intact
        (symlinks, modes); the Guest-side script expands ~ and prints the absolute folder."""
        script = 'd=$1; case $d in "~"|"~/"*) d="$HOME${d#"~"}";; esac; mkdir -p "$d" && tar -xf - -C "$d" && cd "$d" && pwd'
        argv = ["/bin/sh", "-c", script, "sh", guest_dir]
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar_file:
                tar_file.add(str(src), arcname=src.name)
            result = self.exec(argv, self.lab.app.install_timeout if timeout is None else timeout, stdin=archive)
        if not result.ok:
            detail = "\n".join(result.stderr.strip().splitlines()[-15:]) or "(no output)"
            raise GuestError("copying %s into the Guest failed: %s" % (src, detail), "check free disk space in the Guest")
        return "%s/%s" % (result.stdout.strip(), src.name)

    def shell_argv(self, command):
        """argv that runs a command line in the Guest's shell: sh, or PowerShell on Windows."""
        if self.lab.os == "windows":
            return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        return ["sh", "-c", command]

    def remove_paths(self, paths, timeout):
        """Delete Guest paths (files or folders; a leading ~ is the Guest user's home) if they exist."""
        if not paths:
            return
        if self.lab.os == "windows":
            # %VARS% expand; ~ is the user profile.
            quoted = ["'%s'" % p.replace("'", "''") for p in paths]
            # exit 0: PowerShell exits 1 when its last command failed, even with the error silenced,
            # and a path that is not there is exactly what this asks for.
            script = (
                "foreach ($p in @(%s)) { $p = [Environment]::ExpandEnvironmentVariables($p) -replace '^~', $env:USERPROFILE; "
                "Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }; exit 0" % ", ".join(quoted)
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

    def ui_call(self, command, params, timeout):
        """Run one UI contract command in the Guest's helper (vmlab.uihelpers) and return its JSON result.

        vmlab.ui builds the cross-OS commands on this; a Provider overrides it only
        when its Guests need no helper (the Fake Provider).
        """
        return self.ui_helper().call(command, params, timeout)

    def ui_helper(self):
        if self._ui_helper is None:
            from vmlab import uihelpers

            self._ui_helper = uihelpers.for_provider(self)
        return self._ui_helper

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

    def wrap_argv(self, argv, env):
        """argv as the Channels run it, with env. A Provider may wrap every command, e.g. to give
        it the desktop session's environment; results and reports still show argv."""
        return argv

    def exec(self, argv, timeout, env=None, stdin=None):
        """Run argv in the Guest over the first Channel that works; stdin is an optional binary file."""
        failures = []
        wrapped = self.wrap_argv(list(argv), dict(env or {}))
        for channel in self.channels():
            if stdin is not None:
                stdin.seek(0)  # a failed Channel may have read some of it
            try:
                result = channel.exec(list(wrapped), timeout, dict(env or {}), stdin=stdin)
            except ChannelError as exc:
                failures.append((channel.name, exc))
                continue
            result.argv = list(argv)
            result.channel = channel.name
            result.fallbacks = [
                {"from": name, "to": channel.name, "reason": exc.message} for name, exc in failures
            ]
            if self.on_exec:
                self.on_exec(result)
            return result
        raise ChannelError(
            "no Channel reached Guest %s for %s: %s"
            % (self.lab.name, argv, "; ".join("%s: %s" % (name, exc.message) for name, exc in failures)),
            "run `vmlab doctor %s`" % self.lab.name,
        )


def split_guest_path(guest_path, os_name):
    """(folder, name) of a Guest file path; a bare name is in the Guest user's home."""
    seps = "\\/" if os_name == "windows" else "/"
    cut = max(guest_path.rfind(sep) for sep in seps)
    folder, name = guest_path[:cut], guest_path[cut + 1 :]
    if not name or name in (".", "..", "~"):
        raise UsageError("%r is not a file path; give the file's name, e.g. ~/notes.txt" % guest_path)
    if cut < 0:
        folder = "~"
    elif not folder or folder.endswith(":"):
        folder = guest_path[: cut + 1]  # the root, / or C:\
    return folder, name


def ps_path(path):
    """A PowerShell expression for path, with %VARS% and a leading ~ expanded."""
    return "([Environment]::ExpandEnvironmentVariables('%s') -replace '^~', $env:USERPROFILE)" % path.replace("'", "''")
