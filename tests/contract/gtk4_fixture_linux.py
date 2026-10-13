"""A GTK 4 window for the contract tests, in a Linux Guest: a button that takes clicks and an insensitive one.

    python3 gtk4_fixture_linux.py TITLE [twins|adds]

Its window, titled TITLE: Go, whose click renames it Done, and Off, insensitive. With twins, two
windows alike in title and size instead, the second a second later, so it lies over the first,
offset: each with a narrow button at its bottom left, 1 and 2, its middle clear of the second
window's edge; a click renames it Done 1 or Done 2. With adds, the same two windows each with a
narrow button Add at its bottom right, so the second window, not its Add, covers the first's Add;
a click renames it Added 1 or Added 2. python3 and
its GTK 4 bindings only: the Guest installs nothing. Run it under a name of its own (a link
to python3) so it is the app by that name.
"""

import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402


def twin(app, n, add):
    window = Gtk.ApplicationWindow(application=app, title=sys.argv[1])
    window.set_default_size(320, 160)
    if add:
        button = Gtk.Button(label="Add", halign=Gtk.Align.END, valign=Gtk.Align.END)
        button.connect("clicked", lambda button: button.set_label("Added %d" % n))
    else:
        button = Gtk.Button(label=str(n), halign=Gtk.Align.START, valign=Gtk.Align.END)
        button.connect("clicked", lambda button: button.set_label("Done %d" % n))
    window.set_child(button)
    window.present()
    return False


def activate(app):
    if sys.argv[2:] in (["twins"], ["adds"]):
        add = sys.argv[2] == "adds"
        twin(app, 1, add)
        GLib.timeout_add(1000, twin, app, 2, add)
        return
    window = Gtk.ApplicationWindow(application=app, title=sys.argv[1])
    window.set_default_size(320, 160)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    go = Gtk.Button(label="Go")
    go.connect("clicked", lambda button: button.set_label("Done"))
    off = Gtk.Button(label="Off")
    off.set_sensitive(False)
    box.append(go)
    box.append(off)
    window.set_child(box)
    window.present()
    print("window up", flush=True)


app = Gtk.Application()
app.connect("activate", activate)
app.run([])
