// vmlab-ui JXA fallback: the same commands and output as vmlab-ui.swift, for
// Guests where the Swift helper is not installed. vmlab sends this file to
// `osascript -l JavaScript - COMMAND JSON` on stdin with every call.
//
// Slower (System Events answers one Apple event per element), and it lacks
// what the Swift helper does beyond that: it types with System Events
// keystrokes, which follow the Guest's keyboard layout; a click does not check
// what lies under the point first; and there is no wake-up read for lazy
// WebKit trees, nor AXManualAccessibility for Electron.

ObjC.import("AppKit");
ObjC.import("CoreGraphics");

const VERSION = 1;
const MAX_NODES = 2000;
const KEY_CODES = {
  a: 0, s: 1, d: 2, f: 3, h: 4, g: 5, z: 6, x: 7, c: 8, v: 9, b: 11, q: 12, w: 13, e: 14, r: 15, y: 16, t: 17,
  "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, equal: 24, "9": 25, "7": 26, minus: 27, "8": 28, "0": 29,
  rightbracket: 30, o: 31, u: 32, leftbracket: 33, i: 34, p: 35, enter: 36, l: 37, j: 38, quote: 39, k: 40,
  semicolon: 41, backslash: 42, comma: 43, slash: 44, n: 45, m: 46, period: 47, tab: 48, space: 49, grave: 50,
  backspace: 51, escape: 53, f5: 96, f6: 97, f7: 98, f3: 99, f8: 100, f9: 101, f11: 103, f10: 109, f12: 111,
  home: 115, pageup: 116, delete: 117, f4: 118, end: 119, f2: 120, pagedown: 121, f1: 122, left: 123, right: 124,
  down: 125, up: 126,
};
const USING = { cmd: "command down", ctrl: "control down", alt: "option down", shift: "shift down" };

const events = Application("System Events");

function fail(message) {
  throw new Error("vmlab-ui (JXA): " + message);
}

function nsApps(wanted) {
  const apps = ObjC.unwrap($.NSWorkspace.sharedWorkspace.runningApplications);
  return apps.filter((app) => {
    const names = [app.localizedName, app.bundleIdentifier, app.executableURL.lastPathComponent].map((n) => ObjC.unwrap(n));
    if (wanted) return names.some((n) => n && n.toLowerCase() === wanted.toLowerCase());
    const policy = app.activationPolicy;
    if (policy === $.NSApplicationActivationPolicyRegular) return true;
    return policy === $.NSApplicationActivationPolicyAccessory && !(ObjC.unwrap(app.bundleIdentifier) || "").startsWith("com.apple.");
  });
}

function frontmostPid() {
  const app = $.NSWorkspace.sharedWorkspace.frontmostApplication;
  return app.isNil() ? 0 : app.processIdentifier;
}

function str(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

function walker(maxDepth) {
  const walk = { nodes: 0, truncated: false };
  walk.node = function (element, depth) {
    walk.nodes += 1;
    let p = {};
    try { p = element.properties(); } catch (e) {}
    const pos = p.position, size = p.size;
    const node = {
      native_role: str(p.role) || "",
      name: str(p.name) || "",
      value: str(p.value),
      description: str(p.description),
      bounds: pos && size ? { x: Math.round(pos[0]), y: Math.round(pos[1]), w: Math.round(size[0]), h: Math.round(size[1]) } : null,
      focused: p.focused === true,
      enabled: p.enabled !== false,
      children: [],
    };
    let kids = [];
    try { kids = element.uiElements(); } catch (e) {}
    if (depth >= maxDepth) {
      if (kids.length) walk.truncated = true;
      return node;
    }
    for (const kid of kids) {
      if (walk.nodes >= MAX_NODES) { walk.truncated = true; break; }
      node.children.push(walk.node(kid, depth + 1));
    }
    return node;
  };
  return walk;
}

function screenBounds() {
  const frame = $.NSScreen.mainScreen.frame;
  return { x: 0, y: 0, w: Math.round(frame.size.width), h: Math.round(frame.size.height) };
}

function tree(params) {
  const walk = walker(params.depth || 30);
  const front = frontmostPid();
  const apps = nsApps(params.app).map((app) => {
    const pid = app.processIdentifier;
    const children = [];
    const procs = events.processes.whose({ unixId: pid })();
    if (procs.length) {
      let windows = [];
      try { windows = procs[0].windows(); } catch (e) {}
      for (const w of windows) children.push(walk.node(w, 1));
      try {
        const bars = procs[0].menuBars();
        if (bars.length > 1) children.push(walk.node(bars[1], 1)); // the extras (status item) bar
      } catch (e) {}
    }
    return {
      native_role: "AXApplication", name: ObjC.unwrap(app.localizedName) || "", value: null, description: null,
      bounds: null, focused: pid === front, enabled: true, pid: pid, bundle_id: ObjC.unwrap(app.bundleIdentifier) || null,
      children: children,
    };
  });
  return {
    native_role: "desktop", name: "", value: null, description: null, bounds: screenBounds(), focused: false,
    enabled: true, truncated: walk.truncated, children: apps,
  };
}

function click(params) {
  const point = { x: params.x, y: params.y };
  for (const type of [$.kCGEventMouseMoved, $.kCGEventLeftMouseDown, $.kCGEventLeftMouseUp]) {
    $.CGEventPost($.kCGHIDEventTap, $.CGEventCreateMouseEvent(null, type, point, $.kCGMouseButtonLeft));
    delay(0.01);
  }
  return { x: params.x, y: params.y, under: null };
}

function press(key, modifiers) {
  if (!(key in KEY_CODES)) fail("unknown key " + key);
  events.keyCode(KEY_CODES[key], { using: modifiers.map((m) => USING[m] || fail("unknown modifier " + m)) });
}

function clipboard(params) {
  const board = $.NSPasteboard.generalPasteboard;
  if (params.set !== undefined) {
    board.clearContents;
    board.setStringForType($(params.set), $.NSPasteboardTypeString);
  }
  const text = board.stringForType($.NSPasteboardTypeString);
  return { text: text.isNil() ? null : ObjC.unwrap(text) };
}

function waitFor(deadline, what) {
  for (;;) {
    const value = what();
    if (value) return value;
    if (Date.now() >= deadline) return null;
    delay(0.1);
  }
}

function selectedText(proc) {
  try {
    const focused = proc.attributes.byName("AXFocusedUIElement").value();
    return str(focused.attributes.byName("AXSelectedText").value());
  } catch (e) {
    return null;
  }
}

function focus(params) {
  const deadline = Date.now() + (params.timeout || 30) * 1000;
  const app = nsApps(params.app)[0];
  if (!app) fail(params.app + " is not running");
  const pid = app.processIdentifier;
  const proc = events.processes.whose({ unixId: pid })()[0];
  let raised = null;
  if (params.window !== undefined) {
    const windows = proc.windows();
    const titles = windows.map((w) => str(w.name()) || "");
    const i = titles.findIndex((t) => t.includes(params.window));
    if (i < 0) fail(params.app + ' has no window titled like "' + params.window + '"; its windows: ' + JSON.stringify(titles));
    windows[i].actions.byName("AXRaise").perform();
    raised = titles[i];
  }
  if (!waitFor(deadline, () => { if (frontmostPid() === pid) return true; proc.frontmost = true; return false; })) {
    fail(params.app + " did not come to the front in time");
  }
  const front = $.NSWorkspace.sharedWorkspace.frontmostApplication;
  return { app: ObjC.unwrap(app.localizedName) || params.app, window: raised, frontmost: front.isNil() ? "" : ObjC.unwrap(front.localizedName) };
}

function stageText(params) {
  const deadline = Date.now() + (params.timeout || 30) * 1000;
  const stem = "vmlab-stage-" + Math.random().toString(16).slice(2, 10);
  const file = ObjC.unwrap($.NSTemporaryDirectory()) + stem + ".txt";
  $(params.text).writeToFileAtomicallyEncodingError(file, true, $.NSUTF8StringEncoding, null);
  const shell = Application.currentApplication();
  shell.includeStandardAdditions = true;
  try {
    shell.doShellScript("open -a " + quoted(params.app) + " " + quoted(file));
  } catch (e) {
    fail("open -a " + params.app + " failed: is the app installed?");
  }
  const proc = waitFor(deadline, () => {
    const app = nsApps(params.app)[0];
    if (!app) return null;
    const procs = events.processes.whose({ unixId: app.processIdentifier })();
    if (!procs.length) return null;
    try {
      return procs[0].windows().some((w) => (str(w.name()) || "").includes(stem)) ? procs[0] : null;
    } catch (e) {
      return null;
    }
  });
  if (!proc) fail(params.app + " showed no window for " + stem + ".txt in time");
  const pid = proc.unixId();
  if (!waitFor(deadline, () => { if (frontmostPid() === pid) return true; proc.frontmost = true; return false; })) {
    fail(params.app + " did not come to the front in time");
  }
  press("a", ["cmd"]);
  const selected = waitFor(deadline, () => (selectedText(proc) === params.text ? params.text : null)) || selectedText(proc);
  const front = $.NSWorkspace.sharedWorkspace.frontmostApplication;
  const frontmost = front.isNil() ? "" : ObjC.unwrap(front.localizedName);
  let pressed = null;
  if (params.then) {
    press(params.then.key, params.then.modifiers || []);
    pressed = { key: params.then.key, modifiers: params.then.modifiers || [] };
  }
  return { app: str(proc.name()) || params.app, file: file, frontmost: frontmost, selected: selected, pressed: pressed };
}

function quoted(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

function run(argv) {
  const command = argv[0];
  const params = argv.length > 1 ? JSON.parse(argv[1]) : {};
  let result;
  switch (command) {
    case "version": result = { helper: "jxa", version: VERSION, trusted: $.AXIsProcessTrusted() }; break;
    case "tree": result = tree(params); break;
    case "click": result = click(params); break;
    case "press": press(params.key, params.modifiers || []); result = { key: params.key, modifiers: params.modifiers || [] }; break;
    case "type": events.keystroke(params.text); result = { typed: Array.from(params.text).length }; break;
    case "clipboard": result = clipboard(params); break;
    case "stage-text": result = stageText(params); break;
    case "focus": result = focus(params); break;
    default: fail("unknown command " + command);
  }
  return JSON.stringify(result);
}
