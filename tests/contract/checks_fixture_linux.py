"""A window of tick states for the contract tests, in a Linux Guest: GTK 3 check and radio buttons.

    python3 checks_fixture_linux.py TITLE

Its window, titled TITLE: Pictures (ticked), Music (unticked), Some (in its middle state),
radio buttons Light (chosen) and Dark, and a Save button. python3 and its GTK bindings
only: the Guest installs nothing. Run it under a name of its own (a link to python3) so it
is the app by that name.
"""

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

window = Gtk.Window(title=sys.argv[1])
window.set_default_size(320, 260)
window.connect("destroy", Gtk.main_quit)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
for label, ticked in (("Pictures", True), ("Music", False)):
    check = Gtk.CheckButton(label=label)
    check.set_active(ticked)
    box.add(check)
some = Gtk.CheckButton(label="Some")
some.set_inconsistent(True)
box.add(some)
light = Gtk.RadioButton.new_with_label_from_widget(None, "Light")
dark = Gtk.RadioButton.new_with_label_from_widget(light, "Dark")
light.set_active(True)
box.add(light)
box.add(dark)
box.add(Gtk.Button(label="Save"))
window.add(box)
window.show_all()
print("window up", flush=True)
Gtk.main()
