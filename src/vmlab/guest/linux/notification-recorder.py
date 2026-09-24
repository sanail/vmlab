#!/usr/bin/env python3
"""vmlab's Notification recorder for Linux Guests.

Linux keeps no history of Notifications: an app hands each one to the
notification server (org.freedesktop.Notifications.Notify on the session bus),
which shows it and forgets it. So provisioning installs this recorder as a
systemd user unit, started when the user's manager starts at login, before
either Desktop session. It runs dbus-monitor on the session bus, in binary so
that the calls' arguments come through intact, and appends one JSON line per
Notify call to LOG: {"app", "title", "body", "time"}, time in ISO 8601 UTC.
The UI helper (vmlab-ui.py notifications) reads that file.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from gi.repository import Gio, GLib

LOG = os.path.expanduser("~/.cache/vmlab/notifications.jsonl")
RULE = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
HEADER = 16  # bytes of a D-Bus message that tell its whole length


def owner(bus):
    """The unique name of the notification server, or None."""
    try:
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
                              GLib.Variant("(s)", ("org.freedesktop.Notifications",)), None, Gio.DBusCallFlags.NONE, 2000, None)  # fmt: skip
    except GLib.Error:
        return None
    return reply.unpack()[0]


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    log = open(LOG, "a", encoding="utf-8", buffering=1)  # there from the start: the helper takes a missing one for no recorder
    monitor = subprocess.Popen(["dbus-monitor", "--session", "--binary", RULE], stdout=subprocess.PIPE)
    stream, bus = monitor.stdout, Gio.bus_get_sync(Gio.BusType.SESSION, None)
    while True:
        blob = stream.read(HEADER)
        if len(blob) < HEADER:
            break  # the session bus went away: systemd starts the recorder again
        blob += stream.read(Gio.DBusMessage.bytes_needed(blob) - HEADER)
        message = Gio.DBusMessage.new_from_blob(blob, Gio.DBusCapabilityFlags.UNIX_FD_PASSING)
        body = message.get_body()
        if message.get_member() != "Notify" or body is None or not body.get_type_string().startswith("(susss"):
            continue
        # GNOME's notification service (org.gnome.Shell.Notifications) hands each call on to
        # GNOME Shell: a call from the one that owns the name is one recorded already.
        if message.get_sender() == owner(bus):
            continue
        app, _replaces, _icon, title, text = body.unpack()[:5]
        moment = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        log.write(json.dumps({"app": app, "title": title, "body": text, "time": moment}) + "\n")
    sys.exit(monitor.wait() or 1)


if __name__ == "__main__":
    main()
