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
"""

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
    return Unsupported(provider)


def _result(result, command, helper):
    if not result.ok:
        message = (result.stderr.strip() or result.stdout.strip() or "exit %s" % result.code).splitlines()[-1]
        raise GuestError("UI %s failed in the Guest (%s): %s" % (command, helper, message))
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise GuestError("UI %s: the %s helper printed no JSON: %r" % (command, helper, result.stdout[:200]))


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
