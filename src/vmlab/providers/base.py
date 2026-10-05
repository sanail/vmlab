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
from collections import namedtuple
from pathlib import Path

from vmlab import hostpower
from vmlab.progress import QUIET


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


class BootTimeout(GuestError):
    """The Guest was not reachable within its Lab's boot_timeout after starting."""


BootState = namedtuple("BootState", "reached_os evidence")  # how far a boot got: evidence says what shows it
# The language a Guest shows, for doctor's Language row: shown in the Guest OS's form (config.language_forms),
# or None when nothing tells it yet; where says what showed it ("the Guest", "the clone"), or, when shown is
# None, why it is not known, with fix.
LanguageShown = namedtuple("LanguageShown", "shown where fix")


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

    def send_file(self, local, guest_path, timeout):
        """Copy the Host file local to the Guest path guest_path. Channels that carry files of any
        size themselves (scp, the hypervisor's own copy) implement it: Windows Guests copy Build
        artifacts with it, since their Channels hold a call's stdin whole. Raise ChannelError
        when the Channel cannot carry it, GuestTimeout (after killing the copy) when it does not
        finish within timeout seconds."""
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
    HOST_SCREENSHOTS = False  # screenshot() works from the Host, also while the Guest is not reachable

    def __init__(self, project, lab):
        self.project = project
        self.lab = lab
        self.on_exec = None  # called with every ExecResult, so reports can note Channels and fallbacks
        self.progress = QUIET  # the Lab's step lines (vmlab.progress), which `deploy` and `run` print
        self._ui_helper = None
        self._guest_os = None

    @property
    def guest_os(self):
        """The Guest OS object (vmlab.guestos) that decides how to run commands, name paths and
        drive the UI in this Guest: by default the one for lab.os."""
        if self._guest_os is None:
            from vmlab import guestos

            self._guest_os = guestos.for_os(self.lab.os)
        return self._guest_os

    @guest_os.setter
    def guest_os(self, value):
        self._guest_os = value

    @property
    def vcpus(self):
        """The vCPUs the Guest takes when running, which `run --parallel` budgets; 0: not counted."""
        return 0

    @classmethod
    def validate_options(cls, config_path, key, options, os_name):
        """Raise ConfigError if this Provider's [labs.<name>.<provider>] table is wrong."""

    @classmethod
    def base_of(cls, options, os_name):
        """The Base guest a Lab with these (valid) options clones, or None for Providers without them."""
        return None

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

    def shown_language(self, running):
        """The LanguageShown of the Guest: read in it when running, else from what vmlab recorded about it
        (its clone, its Base guest). GuestError when it cannot be read; None when the Provider cannot tell."""
        return None

    def language_fix(self):
        """How to make the Guest show the Lab language, for doctor's Language row. By default the clone
        is made again, in the Lab language, at its next start."""
        return "vmlab down %s && vmlab up %s   (the clone is made again, in %s)" % (self.lab.name, self.lab.name, self.lab.language)

    def is_running(self):
        raise NotImplementedError

    def start(self):
        """Power the Guest on; return without waiting for it to boot. Making the Guest first (a clone
        of its Base guest) is the step self.progress.step("cloning"), powering it on the step "booting"."""
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
        """Return the Guest to its Clean state, running or not; it is running and reachable
        afterwards. A stopped Guest boots once, into its Clean state."""
        raise NotImplementedError

    def boot_state(self):
        """How far a Guest that is not reachable got in its boot: a BootState, or None when the
        Provider cannot tell. Asked once its boot_timeout is over; bounded like every hypervisor call."""
        return None

    def copy_in(self, src, guest_dir, timeout=None):
        """Copy the Host file or folder src into guest_dir (created; ~ is the Guest user's home),
        within timeout seconds (default: the Lab's app.install_timeout).

        Returns the absolute Guest path of the copy.
        """
        raise NotImplementedError

    def put_file(self, guest_path, data, timeout):
        """Write the bytes data to guest_path, making its folders; ~ is the Guest user's home and,
        on Windows, %VARS% expand. Returns the absolute Guest path. Built on copy_in."""
        folder, name = self.guest_os.split_path(guest_path)
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
        result = self.exec(self.guest_os.read_file_argv(path), timeout)
        if result.code == NO_FILE:
            raise GuestError("no file %s in Guest %s" % (guest_path, self.lab.name), "check the path; ~ is the Guest user's home")
        if not result.ok:
            raise GuestError("reading %s in the Guest failed: %s" % (guest_path, result.stderr.strip() or "exit %d" % result.code))
        try:
            return base64.b64decode(result.stdout)
        except ValueError:
            raise GuestError("reading %s in the Guest returned no base64: %r" % (guest_path, result.stdout[:200]))

    def spawner(self):
        """How g.spawn starts, checks and stops background processes in this Guest (vmlab.providers.spawning)."""
        return self.guest_os.spawner(self)

    def send_file(self, local, guest_path, timeout):
        """Copy the Host file local into the Guest over the first Channel that can carry it, all
        within timeout seconds. A Channel that times out has spent them: GuestTimeout, no fallback."""
        failures = []
        deadline = hostpower.awake_time() + timeout
        for channel in self.channels():
            remaining = deadline - hostpower.awake_time()
            if remaining <= 0:
                raise GuestTimeout("copying %s into Guest %s did not finish within %ss" % (local, self.lab.name, timeout))
            try:
                channel.send_file(local, guest_path, remaining)
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
        script = 'd=$1; %smkdir -p "$d" && tar -xf - -C "$d" && cd "$d" && pwd' % sh_expand_tilde("d")
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
        return self.guest_os.shell_argv(command)

    def remove_paths(self, paths, timeout):
        """Delete Guest paths (files or folders; a leading ~ is the Guest user's home) if they exist."""
        if not paths:
            return
        result = self.exec(self.guest_os.remove_paths_argv(paths), timeout)
        if not result.ok:
            raise GuestError("removing %s from the Guest failed: %s" % (paths, result.stderr.strip()))

    def probe_argv(self):
        """A command that succeeds on any healthy Guest, used to test Channels."""
        return self.guest_os.probe_argv()

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
            self._ui_helper = self.guest_os.ui_helper(self)
        return self._ui_helper

    def up(self):
        """Start the Guest if needed and wait until it is reachable. Idempotent."""
        if not self.is_running():
            self.start()
        # boot_timeout counts the Host's awake time: while the Host sleeps, its Guests do too.
        deadline = hostpower.awake_time() + self.lab.boot_timeout
        if self.is_reachable():
            return
        with self.progress.step("waiting for Channels"):
            while True:
                if hostpower.awake_time() >= deadline:
                    raise self._boot_timeout()
                time.sleep(BOOT_POLL_SECONDS)
                if self.is_reachable():
                    return

    def _boot_timeout(self):
        """The error for a Guest not reachable in time: a slow boot only when its OS came up."""
        name, limit = self.lab.name, self.lab.boot_timeout
        state = self.boot_state()
        if state and not state.reached_os:
            return BootTimeout(
                "Guest %s did not reach its OS within %ss of starting: %s" % (name, limit, state.evidence),
                "look at its screen (vmlab ui screenshot --lab %s); `vmlab down %s && vmlab up %s` boots it again" % (name, name, name),
            )
        slow = "raise labs.%s.boot_timeout if it is just slow; otherwise check `vmlab doctor %s`" % (name, name)
        if state:
            return BootTimeout("Guest %s's OS is up (%s), but its Channels did not answer within %ss of starting" % (name, state.evidence, limit), slow)
        return BootTimeout("Guest %s was not reachable within %ss of starting" % (name, limit), slow)

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


def ps_quote(text):
    """text as a PowerShell string literal, taken as is (no $ or ` expansion)."""
    return "'%s'" % text.replace("'", "''")


def ps_path(path):
    """A PowerShell expression for the Guest path path, in parentheses so it also works as a
    command's argument: %VARS% expand, and ~ alone or before a slash or backslash is the user's
    profile (~foo stays as it is)."""
    if path == "~" or path[:2] in ("~/", "~\\"):
        return "($env:USERPROFILE + [Environment]::ExpandEnvironmentVariables(%s))" % ps_quote(path[1:])
    return "([Environment]::ExpandEnvironmentVariables(%s))" % ps_quote(path)


def sh_expand_tilde(var):
    """POSIX sh that expands a leading ~ in $var, alone or before a slash, to the Guest user's
    home (~foo stays as it is); a statement to put before the ones that use $var."""
    return 'case $%s in "~"|"~/"*) %s="$HOME${%s#"~"}";; esac; ' % (var, var, var)
