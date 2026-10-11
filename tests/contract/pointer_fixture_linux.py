"""A window that prints the pointer's buttons, for the contract tests, in a Linux Guest: GTK 3.

    python3 pointer_fixture_linux.py TITLE

Its window, titled TITLE, prints a line per button press, release and move with a button held:
"down|move|up BUTTON MODIFIERS", BUTTON left, right, middle, back or forward, MODIFIERS like
ctrl+shift, or - for none. python3 and its GTK bindings only: the Guest installs nothing. Run it
under a name of its own (a link to python3) so it is the app by that name.
"""

import sys

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

BUTTONS = {1: "left", 2: "middle", 3: "right", 8: "back", 9: "forward"}
HELD = ((Gdk.ModifierType.BUTTON1_MASK, "left"), (Gdk.ModifierType.BUTTON2_MASK, "middle"), (Gdk.ModifierType.BUTTON3_MASK, "right"))
MODIFIERS = (
    (Gdk.ModifierType.CONTROL_MASK, "ctrl"), (Gdk.ModifierType.MOD1_MASK, "alt"), (Gdk.ModifierType.SHIFT_MASK, "shift"),
    (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.MOD4_MASK, "cmd"),
)  # fmt: skip


def say(what, button, state):
    print(what, button, "+".join(name for mask, name in MODIFIERS if state & mask) or "-", flush=True)


def pressed(widget, event):
    if event.type == Gdk.EventType.BUTTON_PRESS:  # not the extra event of a double click
        say("down", BUTTONS.get(event.button, "button%d" % event.button), event.state)
    return True


def released(widget, event):
    say("up", BUTTONS.get(event.button, "button%d" % event.button), event.state)
    return True


def moved(widget, event):
    held = [name for mask, name in HELD if event.state & mask]
    if held:
        say("move", held[0], event.state)
    return True


window = Gtk.Window(title=sys.argv[1])
window.set_default_size(600, 400)
window.connect("destroy", Gtk.main_quit)
pad = Gtk.DrawingArea()
pad.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
pad.connect("button-press-event", pressed)
pad.connect("button-release-event", released)
pad.connect("motion-notify-event", moved)
window.add(pad)
window.show_all()
print("window up", flush=True)
Gtk.main()
