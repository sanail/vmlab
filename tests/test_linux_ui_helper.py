"""The Linux UI helper's tree walk, against fake AT-SPI accessibles.

The helper (src/vmlab/guest/linux/vmlab-ui.py) runs in the Guest with gi's
Atspi; here its UI object is built without one, so what it keeps and drops
of a tree can be checked on the Host.
"""

import importlib.util
import io
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

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

    def __init__(self, role, name="", states=(State.VISIBLE, State.SHOWING), extents=None, children=(), text=None):
        self.role, self.name, self.states, self.extents, self.children = role, name, states, extents, list(children)
        self.text, self.selection = text, None  # an editable text's content and selected (start, end)

    def get_role_name(self):
        return self.role

    def get_role(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return ""

    def get_interfaces(self):
        return (["Component"] if self.extents else []) + (["Text", "EditableText"] if self.text is not None else [])

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


class Range:
    def __init__(self, start, end):
        self.start_offset, self.end_offset = start, end


class Text:
    """Atspi.Text's functions over fake nodes."""

    get_character_count = staticmethod(lambda node: len(node.text))
    get_text = staticmethod(lambda node, start, end: node.text[start:end])
    get_n_selections = staticmethod(lambda node: 1 if node.selection else 0)
    get_selection = staticmethod(lambda node, i: Range(*node.selection))


class Atspi:
    StateType = State
    Text = Text

    class Role:
        PAGE_TAB = "page tab"

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


class Editor:
    """A tabbed text editor (gnome-text-editor's shape) for stage-text: it opens the file in a new tab
    of its window, which shows the file's text only after `loads` refreshes of the window list. Ctrl+A
    selects all of the view in front, if its text is there, unless every press misses. Its other tab,
    in the background, keeps a focused view with a selection. Ctrl+S writes the file `saves` seconds
    later, with a newline at its end; Ctrl+W before then asks about saving, in the window, and closes
    nothing."""

    PID = 7

    def __init__(self, helper, loads, misses=False, saves=0, writes=lambda text: text + "\n"):
        self.writes = writes  # what its save writes for the text
        self.helper, self.loads, self.misses, self.saves, self.doc, self.focused = helper, loads, misses, saves, None, False
        self.content = self.saved = None
        self.asking = self.closed = False
        self.keys, self.timers = [], []
        self.old = Node("text", states=(State.VISIBLE, State.FOCUSED, State.MULTI_LINE), text="old text")
        self.old.selection = (0, len("old text"))
        self.view = Node("text", states=(State.VISIBLE, State.SHOWING, State.FOCUSED, State.MULTI_LINE), text="")
        pages = Node("panel", children=[Node("panel", children=[self.view]), Node("panel", states=(State.VISIBLE,), children=[self.old])])
        self.frame = Node("frame", children=[pages])
        self.app = Node("application", "gnome-text-editor", children=[self.frame])

    def popen(self, argv, **kwargs):
        with open(argv[1], encoding="utf-8") as f:
            self.content = f.read()
        self.doc, self.path = os.path.basename(argv[1]), argv[1]

    def state(self):
        if self.doc is None:
            return (1280, 800), []
        self.frame.name = "%s (/tmp) - Text Editor" % self.doc[: -len(".txt")]
        if self.loads:
            self.loads -= 1
        elif self.content is not None:
            self.view.text, self.content = self.content, None
        if self.closed:
            return (1280, 800), []
        window = self.helper.Window(1, self.PID, self.frame.name, (0, 0, 800, 600), (0, 0, 800, 600), self.focused, False, False)
        return (1280, 800), [window]

    def activate(self, window):
        self.focused = True

    def press(self, key, modifiers):
        self.keys.append((key, list(modifiers)))
        if (key, list(modifiers)) == ("a", ["ctrl"]) and not self.misses:
            self.view.selection = (0, len(self.view.text)) if self.view.text else None
        elif (key, list(modifiers)) == ("s", ["ctrl"]) and not self.asking:
            text = self.view.text
            timer = threading.Timer(self.saves, self.save, [text])
            timer.start()
            self.timers.append(timer)
        elif (key, list(modifiers)) == ("w", ["ctrl"]) and not self.asking:
            self.asking = self.saved != self.view.text
            self.closed = not self.asking

    def save(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.writes(text))
        self.saved = text

    def type(self, text):
        self.view.text, self.view.selection = text, None


class StageTextTest(unittest.TestCase):
    def ui(self, editor):
        ui = editor.helper.UI.__new__(editor.helper.UI)
        ui.Atspi, ui.S, ui.kind, ui.ws = Atspi, State, "x11", editor
        ui.screen, ui.windows = editor.state()
        ui.nodes, ui.truncated = 0, False
        ui.apps = lambda: [(editor.app, "gnome-text-editor", Editor.PID)]
        return ui

    def call(self, editor, command, params):
        """Run a helper command on the editor; its JSON result."""
        helper = editor.helper
        out = io.StringIO()
        with mock.patch.object(helper, "STAGING", self.staging), mock.patch.object(helper, "POLL", 0.01), \
                mock.patch.object(helper, "SELECT_AGAIN", 0.05), mock.patch.object(helper, "SAVE_WAIT", 1), \
                mock.patch.object(helper.subprocess, "Popen", editor.popen), \
                mock.patch.object(helper, "process_name", lambda pid: None), redirect_stdout(out):  # fmt: skip
            getattr(self.ui(editor), command)(params)
        return helper.json.loads(out.getvalue())

    def stage(self, loads, misses=False, saves=0, writes=lambda text: text + "\n"):
        self.staging = os.path.realpath(tempfile.mkdtemp(prefix="vmlab-staging-"))
        self.addCleanup(shutil.rmtree, self.staging, ignore_errors=True)
        editor = Editor(load_helper(), loads, misses, saves, writes)
        result = self.call(editor, "stage_text", {"text": "second tag", "app": "gnome-text-editor", "timeout": 1 if misses else 5})
        return result, editor

    def test_its_text_is_selected_when_the_document_opens_at_once(self):
        result, editor = self.stage(loads=0)
        self.assertEqual((result["selected"], result["frontmost"]), ("second tag", "gnome-text-editor"), result)

    def test_a_select_all_pressed_before_the_file_loaded_is_pressed_again(self):
        result, editor = self.stage(loads=5)
        self.assertEqual(result["selected"], "second tag", result)
        self.assertGreater(editor.keys.count(("a", ["ctrl"])), 1, editor.keys)

    def test_the_selection_is_read_from_the_view_in_front_not_a_background_tab(self):
        result, editor = self.stage(loads=0, misses=True)
        self.assertIsNone(result["selected"], result)

    def test_a_background_tabs_selection_is_not_read_as_the_apps(self):
        editor = Editor(load_helper(), loads=0)
        self.assertIsNone(self.ui(editor).selection(Editor.PID))

    def test_a_changed_document_closes_once_its_slow_save_is_done(self):
        result, editor = self.stage(loads=0, saves=0.3)
        editor.type("changed tag")
        closed = self.call(editor, "close_staged", {"file": result["file"], "app": "gnome-text-editor", "timeout": 5})
        for timer in editor.timers:
            timer.join()
        self.assertEqual(closed, {"file": result["file"], "closed": True})
        self.assertFalse(editor.asking, "Ctrl+W came before the save was done")
        self.assertEqual(Path(result["file"]).read_text(encoding="utf-8"), "changed tag\n")

    def test_a_document_whose_saved_file_never_matches_its_view_still_closes(self):
        result, editor = self.stage(loads=0, writes=lambda text: text.rstrip() + "\n")  # it trims trailing spaces
        editor.type("changed tag  ")
        closed = self.call(editor, "close_staged", {"file": result["file"], "app": "gnome-text-editor", "timeout": 5})
        for timer in editor.timers:
            timer.join()
        self.assertEqual(closed, {"file": result["file"], "closed": True})
        self.assertFalse(editor.asking, "Ctrl+W came before the save was done")


if __name__ == "__main__":
    unittest.main()
