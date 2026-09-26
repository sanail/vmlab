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
const STAGE = "vmlab-stage-"; // + 8 hex digits: the name of every file stage-text opens
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

// The process's extras menu bar, which holds its Tray icons, or null. Not "menu bar 2":
// an accessory app has no main menu bar, so its extras bar is its only one.
function extrasBar(proc) {
  try {
    return proc.attributes.byName("AXExtrasMenuBar").value() || null;
  } catch (e) {
    return null;
  }
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
      const extras = extrasBar(procs[0]);
      if (extras) children.push(walk.node(extras, 1));
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

// A path with symlinks resolved, as NSString resolves them (both sides of a comparison alike).
function resolved(path) {
  return ObjC.unwrap($(path).stringByResolvingSymlinksInPath);
}

// The Staged document at path, resolved; fails unless it is a file stage-text writes.
function stagedFile(path) {
  const staging = resolved(ObjC.unwrap($.NSTemporaryDirectory()));
  const name = ObjC.unwrap($(path).lastPathComponent);
  if (!new RegExp("^" + STAGE + "[0-9a-fA-F]{8}\\.txt$").test(name) || resolved(ObjC.unwrap($(path).stringByDeletingLastPathComponent)) !== staging) {
    fail(path + " is not a Staged document: stage-text writes them to " + staging + " as " + STAGE + "XXXXXXXX.txt");
  }
  return resolved(path);
}

// The window of proc showing file, known by its document's path; null if none.
function documentWindow(proc, file) {
  let windows;
  try {
    windows = proc.windows();
  } catch (e) {
    return null; // the app quit meanwhile
  }
  return windows.find((w) => {
    try {
      const url = $.NSURL.URLWithString(w.attributes.byName("AXDocument").value());
      return !url.isNil() && url.isFileURL && resolved(ObjC.unwrap(url.path)) === file;
    } catch (e) {
      return false; // no document, or the window closed meanwhile
    }
  }) || null;
}

// Close the Staged document params.file in params.app; the app's other documents stay. Its
// window's close button asks the app to close it: TextEdit saves a document itself, even one a
// Scenario typed into, so nothing asks about saving. Pressing it leaves the other windows' order.
function closeStaged(params) {
  if (!params.file || !params.app) fail("close-staged needs file and app");
  const file = stagedFile(params.file);
  const deadline = Date.now() + (params.timeout || 30) * 1000;
  // Through System Events, not nsApps: NSWorkspace's list of apps, once read, never refreshes
  // here (delay does not run the run loop).
  const procs = events.processes.whose({ _or: [{ name: params.app }, { bundleIdentifier: params.app }] })();
  if (!procs.length || !documentWindow(procs[0], file)) return { file: params.file, closed: false };
  const closed = waitFor(deadline, () => {
    const window = documentWindow(procs[0], file);
    if (!window) return true;
    try {
      window.buttons.whose({ subrole: "AXCloseButton" })()[0].click();
    } catch (e) {
      // closed meanwhile
    }
    return false;
  });
  if (!closed) {
    let titles = [];
    try {
      titles = procs[0].windows().map((w) => str(w.name()));
    } catch (e) {}
    fail(params.app + " did not close " + ObjC.unwrap($(file).lastPathComponent) + " in time; its windows: " + JSON.stringify(titles));
  }
  return { file: params.file, closed: true };
}

function stageText(params) {
  const deadline = Date.now() + (params.timeout || 30) * 1000;
  const stem = STAGE + ObjC.unwrap($.NSUUID.UUID.UUIDString).slice(0, 8);
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

// The items of a Tray icon's menu, or of a menu item's submenu (null when it has none), as
// [{element, node, submenu}]. They are there while the menu is closed; pressing one chooses it.
function trayMenu(element) {
  let menus = [];
  try { menus = element.menus(); } catch (e) {}
  if (!menus.length) return null;
  const out = [];
  for (const item of menus[0].menuItems()) {
    const name = str(item.name()) || "";
    const enabled = item.enabled() !== false;
    if (!name && !enabled) continue; // a separator
    let mark = null;
    try { mark = item.attributes.byName("AXMenuItemMarkChar").value(); } catch (e) {}
    const submenu = trayMenu(item);
    const checked = str(mark) === "\u2713"; // a check mark; "-" is the mixed state
    const node = { name: name, enabled: enabled, checked: checked, children: (submenu || []).map((i) => i.node) };
    out.push({ element: item, node: node, submenu: submenu });
  }
  return out;
}

function tray(params) {
  const path = params.choose || [];
  let icon = null;
  for (const app of nsApps(params.app)) {
    const procs = events.processes.whose({ unixId: app.processIdentifier })();
    const extras = procs.length ? extrasBar(procs[0]) : null;
    const items = extras ? extras.menuBarItems() : [];
    if (items.length) { icon = items[0]; break; }
  }
  if (!icon || params.icon_only) return { icon: !!icon }; // icon_only: whether it is there, nothing more
  const top = trayMenu(icon) || [];
  const items = top.map((i) => i.node);
  let level = top;
  const chosen = [];
  for (let n = 0; n < path.length; n++) {
    if (!level) return { icon: true, items: items, chosen: null, failed: { at: chosen.concat([path[n]]), reason: "leaf" } };
    chosen.push(path[n]);
    const item = level.find((i) => i.node.name === path[n]);
    if (!item || !item.node.enabled) return { icon: true, items: items, chosen: null, failed: { at: chosen, reason: item ? "disabled" : "missing" } };
    if (n === path.length - 1) {
      if (item.submenu) return { icon: true, items: items, chosen: null, failed: { at: chosen, reason: "submenu" } }; // clicking it would choose nothing
      item.element.click();
    }
    level = item.submenu;
  }
  return { icon: true, items: items, chosen: path.length ? path : null, failed: null };
}

// usernoted's store: a record per Notification, as a binary plist, shown on screen or not.
const NOTIFICATION_DB = ObjC.unwrap($.NSHomeDirectory()) + "/Library/Group Containers/group.com.apple.usernoted/db2/db";
const BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

function hexToBase64(hex) {
  let out = "";
  for (let i = 0; i < hex.length; i += 6) {
    const chunk = hex.slice(i, i + 6);
    const n = parseInt((chunk + "000000").slice(0, 6), 16);
    const chars = [18, 12, 6, 0].map((shift) => BASE64[(n >> shift) & 63]);
    out += chars.slice(0, chunk.length / 2 + 1).join("") + "==".slice(0, 3 - chunk.length / 2);
  }
  return out;
}

function notifications() {
  const now = new Date().toISOString();
  if (!$.NSFileManager.defaultManager.fileExistsAtPath(NOTIFICATION_DB)) return { now: now, notifications: [] };
  // sqlite3's hex(), not base64(), which macOS's sqlite3 has only since 3.41 (-json: 3.33, macOS 11).
  const sql = "SELECT hex(data) AS data, delivered_date AS delivered FROM record";
  const shell = Application.currentApplication();
  shell.includeStandardAdditions = true;
  const out = shell.doShellScript("/usr/bin/sqlite3 -readonly -json " + quoted(NOTIFICATION_DB) + " " + quoted(sql));
  const found = [];
  for (const row of out.trim() ? JSON.parse(out) : []) {
    const data = $.NSData.alloc.initWithBase64EncodedStringOptions(hexToBase64(row.data), 0);
    const record = ObjC.deepUnwrap($.NSPropertyListSerialization.propertyListWithDataOptionsFormatError(data, 0, null, null));
    if (!record) continue;
    const request = record.req || {};
    // Seconds since 2001 (Core Data's epoch); a record not delivered yet has only its own date.
    const seconds = row.delivered === null ? record.date : row.delivered;
    if (typeof seconds !== "number") continue;
    found.push({ app: record.app || "", title: request.titl || "", body: request.body || "", time: new Date((seconds + 978307200) * 1000).toISOString() });
  }
  return { now: now, notifications: found };
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
    case "close-staged": result = closeStaged(params); break;
    case "focus": result = focus(params); break;
    case "tray": result = tray(params); break;
    case "notifications": result = notifications(); break;
    default: fail("unknown command " + command);
  }
  return JSON.stringify(result);
}
