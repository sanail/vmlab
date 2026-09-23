"""The Guest-side helpers behind the UI contract, one per OS, reached over any Channel.

A helper takes `COMMAND JSON` and prints one JSON object (vmlab.ui has the
commands and shapes); it exits non-zero with a message on stderr when it
cannot do what was asked.

macOS: vmlab-ui, a Swift helper (Accessibility + CGEvent) compiled into
MACOS_HELPER when the Base guest is provisioned. Where it is missing (a Base
guest provisioned by an older vmlab), the JXA fallback guest/macos/vmlab-ui.js
is sent to osascript on stdin with every call: slower, and typing depends on
the keyboard layout. VMLAB_UI_HELPER=jxa forces the fallback, to test it.

Linux: guest/linux/vmlab-ui.py, sent to the Guest's python3 on stdin with
every call, so it never lags behind the Host. It reads AT-SPI and acts through
xdotool in an X11 session or vmlab's GNOME Shell extension (installed when the
Base guest is provisioned) in a Wayland session, whichever is logged in.

Windows: guest/windows/vmlab-ui.ps1, sent on stdin with every call behind a
line of parameters and run as a PowerShell script block (a Windows command
line is too short and too hard to quote for either). It reads UI Automation and
acts through SendInput, in the logged-in user's session every Windows Channel
runs in.
"""

import io
import json
import os
import pkgutil
import tempfile

from vmlab.providers.base import ChannelError, GuestError

MACOS_HELPER = "/usr/local/vmlab/bin/vmlab-ui"
MISSING = 127
# exit 127 when the helper is not installed, whatever the Channel does with a missing program
MACOS_GUARD = 'h=%s; [ -x "$h" ] || exit %d; exec "$h" "$@"' % (MACOS_HELPER, MISSING)


def macos_helper_info(channel, timeout):
    """vmlab-ui's version info over one Channel ({"version", "trusted", ...}), or None if it is not installed."""
    result = channel.exec(["/bin/sh", "-c", MACOS_GUARD, "sh", "version"], timeout, {})
    return None if result.code == MISSING else _result(result, "version", "vmlab-ui")


def for_provider(provider):
    if provider.lab.os == "macos":
        return MacHelper(provider)
    if provider.lab.os == "linux":
        return LinuxHelper(provider)
    if provider.lab.os == "windows":
        return WindowsHelper(provider)
    return Unsupported(provider)


def _result(result, command, helper):
    if not result.ok:
        message = (result.stderr.strip() or result.stdout.strip() or "exit %s" % result.code).splitlines()[-1]
        raise GuestError("UI %s failed in the Guest (%s): %s" % (command, helper, message))
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise GuestError("UI %s: the %s helper printed no JSON: %r" % (command, helper, result.stdout[:200]))


# Reads the parameters line from stdin, then runs the rest as a script block. No double
# quotes: the Channel's command line goes through Windows' quoting rules.
WINDOWS_BOOTSTRAP = (
    "$s = [IO.StreamReader]::new([Console]::OpenStandardInput()).ReadToEnd(); $i = $s.IndexOf([char]10); "
    "& ([ScriptBlock]::Create($s.Substring($i + 1))) '%s' $s.Substring(0, $i).TrimEnd([char]13)"
)
# s for a call that may first compile the helper: ~10 s in a Guest that has no copy yet for this
# vmlab (a Base guest provisioned by an older one; provisioning compiles it into the snapshot)
WINDOWS_COMPILE_TIMEOUT = 60


def windows_call(command, params):
    """(argv, stdin) that run one command of the Windows helper over any Channel."""
    stdin = io.BytesIO(json.dumps(params).encode("utf-8") + b"\n" + pkgutil.get_data("vmlab", "guest/windows/vmlab-ui.ps1"))
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_BOOTSTRAP % command], stdin


def windows_helper_info(channel, timeout):
    """vmlab-ui.ps1's version info over one Channel, compiling it in the Guest if needed."""
    argv, stdin = windows_call("version", {})
    return _result(channel.exec(argv, timeout, {}, stdin=stdin), "version", "vmlab-ui.ps1")


class MacHelper:
    def __init__(self, provider):
        self.provider = provider
        self.jxa = os.environ.get("VMLAB_UI_HELPER") == "jxa"

    def call(self, command, params, timeout):
        args = [command, json.dumps(params)]
        if not self.jxa:
            result = self.provider.exec(["/bin/sh", "-c", MACOS_GUARD, "sh"] + args, timeout)
            if result.code != MISSING:
                return _result(result, command, "vmlab-ui")
            self.jxa = True
        with tempfile.TemporaryFile() as script:
            script.write(pkgutil.get_data("vmlab", "guest/macos/vmlab-ui.js"))
            result = self.provider.exec(["/usr/bin/osascript", "-l", "JavaScript", "-"] + args, timeout, stdin=script)
        return _result(result, command, "JXA fallback")

    def describe(self, timeout):
        """(ok, detail) for doctor: is vmlab-ui installed, and trusted over every Channel that works?

        TCC grants belong to each Channel's own client, so each is asked."""
        trusted, untrusted, version = [], [], None
        for channel in self.provider.channels():
            try:
                info = macos_helper_info(channel, timeout)
            except ChannelError:
                continue  # doctor reports the Channel itself
            if info is None:
                return False, "vmlab-ui is not installed; UI commands use the slower JXA fallback"
            version = info.get("version")
            (trusted if info.get("trusted") else untrusted).append(channel.name)
        if untrusted:
            return False, "vmlab-ui %s has no Accessibility grant over Channel %s" % (version, ", ".join(untrusted))
        return True, "vmlab-ui %s, trusted over %s" % (version, ", ".join(trusted))


class LinuxHelper:
    def __init__(self, provider):
        self.provider = provider

    def _exec(self, command, params, timeout):
        with tempfile.TemporaryFile() as script:
            script.write(pkgutil.get_data("vmlab", "guest/linux/vmlab-ui.py"))
            return self.provider.exec(["python3", "-", command, json.dumps(params)], timeout, stdin=script)

    def call(self, command, params, timeout):
        return _result(self._exec(command, params, timeout), command, "vmlab-ui.py")

    def describe(self, timeout):
        """(ok, detail) for doctor: can the helper reach the desktop session and its input tools?"""
        info = _result(self._exec("version", {}, timeout), "version", "vmlab-ui.py")
        return True, "%s session; input through %s" % (info.get("session"), info.get("input"))


class WindowsHelper:
    def __init__(self, provider):
        self.provider = provider
        self.compiled = False  # has a call succeeded, so the Guest has this helper compiled?

    def call(self, command, params, timeout):
        argv, stdin = windows_call(command, params)
        # The first call may compile the helper: it gets the time for that, even in a
        # Scenario that has less left, rather than time out on every try.
        result = self.provider.exec(argv, timeout if self.compiled else max(timeout, WINDOWS_COMPILE_TIMEOUT), stdin=stdin)
        self.compiled = self.compiled or result.ok
        return _result(result, command, "vmlab-ui.ps1")

    def describe(self, timeout):
        """(ok, detail) for doctor: does the helper compile and reach the desktop?"""
        info = self.call("version", {}, max(timeout, WINDOWS_COMPILE_TIMEOUT))
        screen = info.get("screen") or {}
        return True, "UI Automation; screen %sx%s at %s dpi" % (screen.get("w"), screen.get("h"), info.get("dpi"))


class Unsupported:
    def __init__(self, provider):
        self.provider = provider

    def call(self, command, params, timeout):
        raise GuestError(
            "the UI contract is not implemented for %s Guests yet" % self.provider.lab.os,
            "use g.exec() and g.screenshot() on this Lab for now",
        )

    def describe(self, timeout):
        return None, None
