"""The Linux UI helper's tree walk, against fake AT-SPI accessibles.

The helper (src/vmlab/guest/linux/vmlab-ui.py) runs in the Guest with gi's
Atspi; here its UI object is built without one, so what it keeps and drops
of a tree can be checked on the Host.
"""

import importlib.util
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent / "src" / "vmlab" / "guest" / "linux" / "vmlab-ui.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("vmlab_ui_linux", str(HELPER))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class State:
    VISIBLE, SHOWING, FOCUSED, ENABLED, MULTI_LINE, SINGLE_LINE = range(6)


class States:
    def __init__(self, states):
        self.states = set(states)

    def contains(self, state):
        return state in self.states


class Extents:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class Node:
    """A fake Atspi.Accessible: role, name, states, extents in screen points and children."""

    def __init__(self, role, name="", states=(State.VISIBLE, State.SHOWING), extents=None, children=()):
        self.role, self.name, self.states, self.extents, self.children = role, name, states, extents, list(children)

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return ""

    def get_interfaces(self):
        return ["Component"] if self.extents else []

    def get_state_set(self):
        return States(self.states)

    def get_extents(self, kind):
        return Extents(*self.extents)

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, i):
        return self.children[i]


class Window:
    def __init__(self, pid, frame):
        self.pid, self.frame, self.buffer = pid, frame, frame
        self.title, self.hidden, self.focused, self.background = "App", False, True, False

    def contains(self, x, y):
        fx, fy, fw, fh = self.frame
        return fx <= x < fx + fw and fy <= y < fy + fh


class Atspi:
    StateType = State

    class CoordType:
        SCREEN, WINDOW = "screen", "window"


def webview_window():
    """What a WebKitGTK window of a Tauri app exposes: its page under
    containers that lack VISIBLE, though the page itself is VISIBLE and SHOWING."""
    button = Node("push button", "Run", extents=(20, 60, 80, 20))
    page = Node("document web", "MyApp", extents=(0, 0, 400, 300), children=[button])
    hidden_tab = Node("panel", "Options", states=(State.VISIBLE,), extents=(0, 0, 400, 300),
                      children=[Node("push button", "Reset", states=(State.VISIBLE,), extents=(20, 60, 80, 20))])  # fmt: skip
    wrapper = Node("filler", states=(), children=[Node("filler", states=(), children=[Node("scroll pane", states=(), children=[page])])])
    closed = Node("filler", states=(), children=[hidden_tab])
    return Node("frame", "MyApp", extents=(0, 0, 400, 300), children=[Node("filler", extents=(0, 0, 400, 300), children=[wrapper, closed])])


class TreeWalkTest(unittest.TestCase):
    def setUp(self):
        helper = load_helper()
        ui = helper.UI.__new__(helper.UI)
        ui.Atspi, ui.S, ui.kind = Atspi, State, "x11"
        ui.screen, ui.windows = (1280, 800), [Window(42, (0, 0, 400, 300))]
        ui.nodes, ui.truncated = 0, False
        self.window = webview_window()
        app = Node("application", "myapp", children=[self.window])
        app.get_process_id = lambda: 42
        ui.apps = lambda: [(app, "myapp", 42)]
        self.ui = ui

    def names(self, node):
        return [node["name"]] + [n for child in node["children"] for n in self.names(child)]

    def test_a_page_under_containers_without_visible_is_in_the_tree(self):
        node = self.ui.node(self.window, (Atspi.CoordType.SCREEN, (0, 0)), 1, 60)
        self.assertIn("Run", self.names(node))

    def test_what_is_not_showing_under_them_stays_out(self):
        node = self.ui.node(self.window, (Atspi.CoordType.SCREEN, (0, 0)), 1, 60)
        self.assertNotIn("Reset", self.names(node))

    def test_a_point_on_the_page_hits_its_element(self):
        chain = self.ui.under(50, 70)
        self.assertEqual(chain[0][0], "Run", chain)


if __name__ == "__main__":
    unittest.main()
