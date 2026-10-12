"""The Linux UI helper's tree walk, against fake AT-SPI accessibles.

The helper (src/vmlab/guest/linux/vmlab-ui.py) runs in the Guest with gi's
Atspi; here its UI object is built without one, so what it keeps and drops
of a tree can be checked on the Host.
"""

import importlib.util
import io
import json
import os
import shutil
import tempfile
import threading
import time
import types
import unittest
import xml.etree.ElementTree as ET
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
    VISIBLE, SHOWING, FOCUSED, ENABLED, MULTI_LINE, SINGLE_LINE, CHECKED, CHECKABLE, PRESSED, INDETERMINATE = range(10)


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


def walk(node):
    yield node
    for child in node["children"]:
        yield from walk(child)


class TreeWalkTest(unittest.TestCase):
    def setUp(self):
        helper = load_helper()
        ui = helper.UI.__new__(helper.UI)
        ui.Atspi, ui.S, ui.kind, ui.ws = Atspi, State, "x11", types.SimpleNamespace(toolkit_scale=1.0)
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

    def test_a_press_on_something_else_than_expected_is_refused_before_any_event(self):
        sent = []
        self.ui.ws.pointer = lambda button, events: sent.append(events)
        events = [{"do": "move", "x": 50, "y": 70}, {"do": "down", "x": 50, "y": 70}, {"do": "up", "x": 50, "y": 70}]
        with redirect_stdout(io.StringIO()) as out:
            self.ui.pointer({"events": events, "expect": {"label": "Run"}})
        self.assertEqual(json.loads(out.getvalue())["under"], "Run")
        with mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit):
            self.ui.pointer({"events": events, "expect": {"label": "Save"}})
        self.assertIn('something else is at (50, 70): "Run"', err.getvalue())
        self.assertEqual(len(sent), 1)

    def test_at_scale_2_toolkit_units_become_desktop_pixels(self):
        # Wayland at scale 2: the window manager's frame already in Desktop pixels, the toolkit's extents logical.
        self.ui.ws.toolkit_scale = 2.0
        self.ui.windows = [Window(42, (100, 50, 800, 600))]
        self.window.extents = (0, 0, 400, 300)
        node = self.ui.node(self.window, self.ui.placement(self.window, 42), 1, 60)
        [run] = [n for n in walk(node) if n["name"] == "Run"]
        self.assertEqual(run["bounds"], {"x": 140, "y": 170, "w": 160, "h": 40})
        self.assertEqual(self.ui.under(220, 190)[0][0], "Run")


class PointerTest(unittest.TestCase):
    """A drag's events as each session's input sends them: one xdotool chain, or the Shell extension's Pointer."""

    EVENTS = [{"do": "keydown", "key": "shift"}, {"do": "move", "x": 10, "y": 20}, {"do": "down", "x": 10, "y": 20},
              {"do": "wait", "s": 0.1}, {"do": "move", "x": 30, "y": 40}, {"do": "up", "x": 30, "y": 40}, {"do": "keyup", "key": "shift"}]

    def setUp(self):
        self.helper = load_helper()

    def test_x11_sends_one_xdotool_chain(self):
        x11 = self.helper.X11.__new__(self.helper.X11)
        with mock.patch.object(self.helper, "run") as run:
            x11.pointer(self.helper.BUTTONS["back"], self.EVENTS)
        self.assertEqual(run.call_args[0][0], [
            "xdotool", "keydown", "shift", "mousemove", "--sync", "10", "20", "mousedown", "8", "sleep", "0.1",
            "mousemove", "--sync", "30", "40", "mouseup", "8", "keyup", "shift",
        ])  # fmt: skip

    def test_wayland_sends_stage_points_and_evdev_keys(self):
        shell = self.helper.WaylandShell.__new__(self.helper.WaylandShell)
        shell.stage_scale, calls = 2.0, []
        shell._call = lambda *args: calls.append(args)
        shell.pointer(self.helper.BUTTONS["right"], self.EVENTS)
        [(method, signature, sent)] = calls
        sent = json.loads(sent)
        self.assertEqual((method, signature), ("Pointer", "s"))
        self.assertEqual(sent[0], {"do": "keydown", "key": 42, "button": 3})
        self.assertEqual(sent[4], {"do": "move", "x": 15, "y": 20, "button": 3})

    def test_wayland_takes_an_apps_tooltip_for_no_window_of_its_own(self):
        # A tab's tooltip, up while the pointer rests on the tab, is the app's topmost window, and never takes the focus.
        shell = self.helper.WaylandShell.__new__(self.helper.WaylandShell)
        shell.stage_scale = 1.0
        window = {"pid": 7, "frame": [0, 0, 700, 520], "buffer": [0, 0, 700, 520], "focused": False, "hidden": False}
        shell._call = lambda *args: (json.dumps({"screen": [1280, 800], "windows": [
            dict(window, id=1, title="doc.txt - Text Editor", type=0), dict(window, id=2, title="", type=11)]}),)
        _, windows = shell.state()
        self.assertEqual([w.background for w in windows], [False, True])


class TickStateTest(unittest.TestCase):
    """How AT-SPI states become a node's "checked"."""

    def setUp(self):
        helper = load_helper()
        ui = helper.UI.__new__(helper.UI)
        ui.Atspi, ui.S, ui.nodes, ui.ws = Atspi, State, 0, types.SimpleNamespace(toolkit_scale=1.0)
        self.ui = ui

    def checked(self, role, *states):
        return self.ui.node(Node(role, states=(State.VISIBLE, State.SHOWING) + states), (Atspi.CoordType.SCREEN, (0, 0)), 1, 60)["checked"]

    def test_a_checkbox_reads_ticked_unticked_or_mixed(self):
        self.assertIs(self.checked("check box", State.CHECKED), True)
        self.assertIs(self.checked("check box"), False)
        self.assertEqual(self.checked("check box", State.INDETERMINATE), "mixed")

    def test_a_radio_button_and_a_switch_read_like_a_checkbox(self):
        self.assertIs(self.checked("radio button", State.CHECKED), True)
        self.assertIs(self.checked("radio button"), False)
        self.assertIs(self.checked("switch", State.CHECKED), True)

    def test_a_toggle_button_is_ticked_when_pressed(self):
        self.assertIs(self.checked("toggle button", State.PRESSED), True)
        self.assertIs(self.checked("toggle button"), False)

    def test_a_menu_item_is_checkable_when_its_role_or_state_says_so(self):
        self.assertIs(self.checked("check menu item"), False)
        self.assertIs(self.checked("menu item", State.CHECKABLE), False)
        self.assertIs(self.checked("menu item", State.CHECKABLE, State.CHECKED), True)
        self.assertIsNone(self.checked("menu item"))

    def test_an_element_with_no_tick_state_reads_null(self):
        self.assertIsNone(self.checked("push button"))
        self.assertIsNone(self.checked("progress bar", State.INDETERMINATE))


class Editor:
    """A tabbed text editor (gnome-text-editor's shape) for stage-text: it opens the file in a new tab
    of its window, which shows the file's text only after `loads` refreshes of the window list. Ctrl+A
    selects all of the view in front, if its text is there, unless every press misses. Its other tab,
    in the background, keeps a focused view with a selection. Ctrl+S writes the file `saves` seconds
    later, with a newline at its end; Ctrl+W before then asks about saving, in the window, and closes
    nothing."""

    toolkit_scale = 1.0  # Desktop pixels per AT-SPI unit, as on a Display at scale 1

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

    def stage(self, loads, misses=False, saves=0, writes=lambda text: text + "\n", text="second tag"):
        self.staging = os.path.realpath(tempfile.mkdtemp(prefix="vmlab-staging-"))
        self.addCleanup(shutil.rmtree, self.staging, ignore_errors=True)
        editor = Editor(load_helper(), loads, misses, saves, writes)
        result = self.call(editor, "stage_text", {"text": text, "app": "gnome-text-editor", "timeout": 1 if misses else 5})
        return result, editor

    def test_its_text_is_selected_when_the_document_opens_at_once(self):
        result, editor = self.stage(loads=0)
        self.assertEqual((result["selected"], result["frontmost"]), ("second tag", "gnome-text-editor"), result)

    def test_a_select_all_pressed_before_the_file_loaded_is_pressed_again(self):
        result, editor = self.stage(loads=5)
        self.assertEqual(result["selected"], "second tag", result)
        self.assertGreater(editor.keys.count(("a", ["ctrl"])), 1, editor.keys)

    def test_an_empty_document_has_nothing_to_select_and_says_so_at_once(self):
        started = time.time()
        result, editor = self.stage(loads=0, text="")
        self.assertEqual(result["selected"], "", result)
        self.assertLess(time.time() - started, 2, "it waited for a selection an empty document cannot have")

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


class SetScaleTest(unittest.TestCase):
    """set-scale.py picks the mode that keeps the Desktop about its size at scale 1."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("vmlab_set_scale", str(HELPER.parent / "set-scale.py"))
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)
        # GetCurrentState of a Fusion Guest at 1280x800, its modes cut to a few (id, w, h, rate, preferred scale, scales, props)
        modes = [
            ("2560x1600@59.987", 2560, 1600, 59.986587524414062, 1.0, [1.0, 1.25, 1.3333333730697632, 1.6666666269302368, 2.0, 2.5], {}),
            ("1920x1200@59.950", 1920, 1200, 59.950172424316406, 1.0, [1.0, 1.25, 1.3333333730697632, 1.5, 1.6666666269302368, 2.0], {}),
            ("1792x1344@60.000", 1792, 1344, 59.999790191650391, 1.0, [1.0, 1.3333333730697632, 1.75, 2.0], {}),
            ("1680x1050@59.954", 1680, 1050, 59.954250335693359, 1.0, [1.0, 1.25, 1.5, 1.6666666269302368, 1.75, 2.0], {}),
            ("1600x900@60.000", 1600, 900, 60.0, 1.0, [1.0, 1.25, 1.3333333730697632, 1.6666666269302368], {}),
            ("1280x800@60.000", 1280, 800, 60.0, 1.0, [1.0, 1.25, 1.3333333730697632], {"is-current": True, "is-preferred": True}),
        ]
        self.state = (1, [(("Virtual-1", "unknown", "unknown", "unknown"), modes, {})], [], {})

    def test_each_scale_gets_the_mode_nearest_the_desktops_size(self):
        for scale, size in ((1, (1280, 800)), (1.25, (1600, 900)), (1.5, (1920, 1200)), (1.75, (1792, 1344)), (2, (2560, 1600))):
            with self.subTest(scale):
                self.assertEqual(self.script.choose(self.state, scale)[1][:2], size)

    def test_monitors_xml_names_the_monitor_its_mode_and_scale(self):
        xml = self.script.monitors_xml(*self.script.choose(self.state, 1.5))
        for part in ("<connector>Virtual-1</connector>", "<width>1920</width>", "<rate>59.950172424316406</rate>", "<scale>1.5</scale>"):
            self.assertIn(part, xml)

    def test_xsettings_xml_sets_the_window_scaling_in_a_new_channel(self):
        channel = ET.fromstring(self.script.xsettings_xml(None, 2))
        self.assertEqual(channel.get("name"), "xsettings")
        factor = channel.find("./property[@name='Gdk']/property[@name='WindowScalingFactor']")
        self.assertEqual((factor.get("type"), factor.get("value")), ("int", "2"))

    def test_xsettings_xml_keeps_the_rest_of_an_existing_channel(self):
        existing = """<?xml version="1.0" encoding="UTF-8"?>

<channel name="xsettings" version="1.0">
  <property name="Net" type="empty">
    <property name="ThemeName" type="string" value="Greybird"/>
  </property>
  <property name="Gdk" type="empty">
    <property name="WindowScalingFactor" type="empty"/>
  </property>
</channel>
"""
        channel = ET.fromstring(self.script.xsettings_xml(existing, 2))
        self.assertEqual(channel.find("./property[@name='Net']/property[@name='ThemeName']").get("value"), "Greybird")
        factors = channel.findall("./property[@name='Gdk']/property[@name='WindowScalingFactor']")
        self.assertEqual([(f.get("type"), f.get("value")) for f in factors], [("int", "2")])
