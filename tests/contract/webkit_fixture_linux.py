"""A WebKitGTK page in a GTK 3 window with a header bar, for the contract tests, in a Linux Guest.

    python3 webkit_fixture_linux.py TITLE

Its window, titled TITLE, draws its own shadow and header bar, as a Tauri 2 app's does, and holds
a page whose button Continue, clicked, renames itself Continued. The page's elements live in
WebKit's web process, not the window's. python3, its GTK 3 bindings and WebKit2 4.1 only. Run it
under a name of its own (a link to python3) so it is the app by that name.
"""

import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2  # noqa: E402

PAGE = """<!doctype html><html><body style="margin: 0; padding: 40px">
<h1>Welcome</h1>
<button id="go" style="margin: 120px 0 0 300px" onclick="this.textContent = 'Continued'">Continue</button>
</body></html>"""

window = Gtk.Window(title=sys.argv[1])
window.set_default_size(620, 400)
header = Gtk.HeaderBar(title=sys.argv[1], show_close_button=True)
window.set_titlebar(header)
view = WebKit2.WebView()
view.load_html(PAGE, None)
window.add(view)
window.connect("destroy", Gtk.main_quit)
window.show_all()
print("window up", flush=True)
Gtk.main()
