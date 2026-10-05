// vmlab-ui: the macOS side of vmlab's UI contract (Accessibility + CGEvent).
//
// Compiled in the Guest when its Base guest is provisioned, never at first use.
// vmlab runs it over a Channel as `vmlab-ui COMMAND [JSON]` and reads one JSON
// object from stdout; on failure it exits 1 with a message on stderr. The Host
// maps native roles to vmlab's cross-OS roles and does all matching, so this
// helper only reads the tree and acts at coordinates. vmlab-ui.js next to it
// is the JXA fallback with the same commands and output.
//
// Commands:
//   version
//   tree        {"app": name?, "depth": n?}
//   click       {"x": n, "y": n, "expect": {"label": s?, "bounds": {x,y,w,h}?}?}
//   press       {"key": name, "modifiers": [cmd|ctrl|alt|shift]}
//   type        {"text": s}: returns once the focused element holds s (see awaitTyped)
//   clipboard   {"set": s?}
//   focus       {"app": name, "window": title substring?, "timeout": seconds}
//   stage-text  {"text": s, "app": name, "then": {"key", "modifiers"}?, "timeout": seconds}
//   close-staged {"file": path stage-text returned, "app": name, "timeout": seconds}
//   tray        {"app": name, "choose": [label per menu level], "timeout": seconds, "icon_only": bool?}
//   notifications  {}: every Notification in the Notification Center's store, and the Guest's "now"
//
// Traps (README: "macOS Guests with Tart"):
// - WebKit builds a page's accessibility tree lazily and hands it to the NEXT
//   client that connects, not to the one that asked: a cold first read shows
//   no web area at all. So the first time the helper meets an app process it
//   reads it once to wake it up, then reads again from a fresh process. After
//   that, even the app's new windows come through on the first read.
// - AXManualAccessibility turns the tree on in Chromium/Electron apps; WebKit
//   rejects it (-25205). It is set best effort, and the retry above is what
//   makes WebKit work.
// - Keys are sent by physical key code, so chords work on any keyboard layout;
//   text is typed as Unicode strings, also independent of the layout.

import AppKit
import ApplicationServices
import Foundation
import SQLite3

let VERSION = 1
let MAX_NODES = 5000
let STAGE = "vmlab-stage-"  // + 8 hex digits: the name of every file stage-text opens
let SELECT_WAIT = 2.0  // s for a select-all to select a Staged document's text, before it is pressed again
let TYPE_WAIT = 5.0  // s at most for typed keys to show in the focused element
let TYPE_SETTLE = 0.5  // s its text stands still before type stops waiting for text it does not hold
let TRUST_WAIT = 5.0  // s to ask TCC again before saying the Accessibility grant is missing: under load it answered no once

func fail(_ message: String) -> Never {
    FileHandle.standardError.write("vmlab-ui: \(message)\n".data(using: .utf8)!)
    exit(1)
}

func emit(_ object: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else {
        fail("cannot encode the result as JSON")
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

// MARK: - Accessibility

func attribute(_ element: AXUIElement, _ name: String) -> AnyObject? {
    var value: AnyObject?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

func text(_ element: AXUIElement, _ name: String) -> String? {
    switch attribute(element, name) {
    case let s as String: return s
    case let n as NSNumber: return n.stringValue
    default: return nil
    }
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    attribute(element, kAXChildrenAttribute as String) as? [AXUIElement] ?? []
}

func frame(_ element: AXUIElement) -> CGRect? {
    guard let position = attribute(element, kAXPositionAttribute as String),
          let size = attribute(element, kAXSizeAttribute as String),
          CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID()
    else { return nil }
    var origin = CGPoint.zero
    var extent = CGSize.zero
    guard AXValueGetValue(position as! AXValue, .cgPoint, &origin),
          AXValueGetValue(size as! AXValue, .cgSize, &extent) else { return nil }
    return CGRect(origin: origin, size: extent)
}

func bounds(_ rect: CGRect?) -> Any {
    guard let r = rect else { return NSNull() }
    return ["x": Int(r.minX.rounded()), "y": Int(r.minY.rounded()), "w": Int(r.width.rounded()), "h": Int(r.height.rounded())]
}

func label(_ element: AXUIElement) -> String {
    for name in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute] as [String] {
        if let s = text(element, name)?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty { return s }
    }
    return ""
}

/// The tick state: a checkbox's, radio button's or switch's AXValue (0, 1, 2 for mixed), a menu item's
/// mark (a check mark, "-" for mixed; none reads null: macOS does not tell a checkable item apart).
func checked(_ element: AXUIElement, role: String, subrole: String?) -> Any {
    if role == "AXMenuItem" {
        switch text(element, "AXMenuItemMarkChar") {
        case "\u{2713}"?: return true
        case "-"?: return "mixed"
        default: return NSNull()
        }
    }
    guard ["AXCheckBox", "AXRadioButton"].contains(role) || ["AXSwitch", "AXToggle"].contains(subrole ?? ""),
          let n = attribute(element, kAXValueAttribute as String) as? NSNumber else { return NSNull() }
    return n.intValue == 2 ? "mixed" : n.intValue != 0
}

final class Walk {
    let maxDepth: Int
    var nodes = 0
    var truncated = false

    init(maxDepth: Int) { self.maxDepth = maxDepth }

    func node(_ element: AXUIElement, depth: Int) -> [String: Any] {
        nodes += 1
        let role = text(element, kAXRoleAttribute as String) ?? ""
        let kids = children(element)
        var out: [String: Any] = [
            "native_role": role,
            "name": text(element, kAXTitleAttribute as String) ?? "",
            "value": text(element, kAXValueAttribute as String) as Any? ?? NSNull(),
            "description": text(element, kAXDescriptionAttribute as String) as Any? ?? NSNull(),
            "bounds": bounds(frame(element)),
            "focused": (attribute(element, kAXFocusedAttribute as String) as? Bool) ?? false,
            "enabled": (attribute(element, kAXEnabledAttribute as String) as? Bool) ?? true,
        ]
        let sub = text(element, kAXSubroleAttribute as String)
        if let sub = sub { out["native_subrole"] = sub }
        out["checked"] = checked(element, role: role, subrole: sub)
        var list: [[String: Any]] = []
        if depth < maxDepth {
            for child in kids {
                if nodes >= MAX_NODES { truncated = true; break }
                list.append(node(child, depth: depth + 1))
            }
        } else if !kids.isEmpty {
            truncated = true
        }
        out["children"] = list
        return out
    }
}

func matches(_ app: NSRunningApplication, _ wanted: String) -> Bool {
    let w = wanted.lowercased()
    return [app.localizedName, app.executableURL?.lastPathComponent, app.bundleIdentifier]
        .contains { $0?.lowercased() == w }
}

func appsFor(_ wanted: String?) -> [NSRunningApplication] {
    NSWorkspace.shared.runningApplications.filter { app in
        if let w = wanted { return matches(app, w) }
        switch app.activationPolicy {
        case .regular: return true
        case .accessory: return !(app.bundleIdentifier ?? "").hasPrefix("com.apple.")  // tray apps, not system agents
        default: return false
        }
    }
}

func appNode(_ app: NSRunningApplication, _ walk: Walk) -> [String: Any] {
    let ax = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(ax, 3)
    AXUIElementSetAttributeValue(ax, "AXManualAccessibility" as CFString, kCFBooleanTrue)  // best effort; see top
    var kids: [[String: Any]] = []
    for window in attribute(ax, kAXWindowsAttribute as String) as? [AXUIElement] ?? [] {
        kids.append(walk.node(window, depth: 1))
    }
    // Status items (tray icons) live in the app's extras menu bar; the main menu bar is left out as noise.
    if let extras = attribute(ax, kAXExtrasMenuBarAttribute as String), CFGetTypeID(extras) == AXUIElementGetTypeID() {
        kids.append(walk.node(extras as! AXUIElement, depth: 1))
    }
    return [
        "native_role": "AXApplication",
        "name": app.localizedName ?? app.executableURL?.lastPathComponent ?? "",
        "value": NSNull(),
        "description": NSNull(),
        "bounds": NSNull(),
        "focused": app.processIdentifier == NSWorkspace.shared.frontmostApplication?.processIdentifier,
        "enabled": true,
        "pid": Int(app.processIdentifier),
        "bundle_id": app.bundleIdentifier as Any? ?? NSNull(),
        "children": kids,
    ]
}

/// Marks an app process the helper has read before (pid plus launch time, since pids are reused).
func primedMarker(_ app: NSRunningApplication) -> URL {
    let launched = Int(app.launchDate?.timeIntervalSince1970 ?? 0)
    return URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("vmlab-ui-primed-\(app.processIdentifier)-\(launched)")
}

func tree(_ params: [String: Any]) {
    let wanted = params["app"] as? String
    let maxDepth = params["depth"] as? Int ?? 30
    let cold = appsFor(wanted).filter { !FileManager.default.fileExists(atPath: primedMarker($0).path) }
    if !cold.isEmpty && ProcessInfo.processInfo.environment["VMLAB_UI_RETRIED"] == nil {
        // Wake the lazy trees up, then read them as the next client: a fresh process.
        for app in cold {
            _ = appNode(app, Walk(maxDepth: maxDepth))
            FileManager.default.createFile(atPath: primedMarker(app).path, contents: nil)
        }
        let retry = Process()
        retry.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        retry.arguments = Array(CommandLine.arguments.dropFirst())
        retry.environment = ProcessInfo.processInfo.environment.merging(["VMLAB_UI_RETRIED": "1"]) { $1 }
        if (try? retry.run()) != nil {
            retry.waitUntilExit()
            exit(retry.terminationStatus)
        }
    }
    let walk = Walk(maxDepth: maxDepth)
    let apps = appsFor(wanted).map { appNode($0, walk) }
    emit([
        "native_role": "desktop", "name": "", "value": NSNull(), "description": NSNull(), "bounds": bounds(screenFrame()),
        "focused": false, "enabled": true, "truncated": walk.truncated, "children": apps,
    ])
}

func screenFrame() -> CGRect {
    CGDisplayBounds(CGMainDisplayID())
}

// MARK: - Input

let KEY_CODES: [String: CGKeyCode] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "equal": 24, "9": 25,
    "7": 26, "minus": 27, "8": 28, "0": 29, "rightbracket": 30, "o": 31, "u": 32, "leftbracket": 33, "i": 34,
    "p": 35, "enter": 36, "l": 37, "j": 38, "quote": 39, "k": 40, "semicolon": 41, "backslash": 42, "comma": 43,
    "slash": 44, "n": 45, "m": 46, "period": 47, "tab": 48, "space": 49, "grave": 50, "backspace": 51,
    "escape": 53, "f5": 96, "f6": 97, "f7": 98, "f3": 99, "f8": 100, "f9": 101, "f11": 103, "f10": 109,
    "f12": 111, "home": 115, "pageup": 116, "delete": 117, "f4": 118, "end": 119, "f2": 120, "pagedown": 121,
    "f1": 122, "left": 123, "right": 124, "down": 125, "up": 126,
]
let MODIFIERS: [String: (CGKeyCode, CGEventFlags)] = [
    "cmd": (55, .maskCommand), "shift": (56, .maskShift), "alt": (58, .maskAlternate), "ctrl": (59, .maskControl),
]
let source = CGEventSource(stateID: .hidSystemState)

func post(_ event: CGEvent?) {
    event?.post(tap: .cghidEventTap)
    usleep(8_000)  // events posted back to back can be coalesced or reordered
}

func press(_ key: String, _ modifiers: [String]) {
    guard let code = KEY_CODES[key] else { fail("unknown key \(key)") }
    var flags: CGEventFlags = []
    var held: [(CGKeyCode, CGEventFlags)] = []
    for name in modifiers {
        guard let m = MODIFIERS[name] else { fail("unknown modifier \(name)") }
        flags.insert(m.1)
        held.append(m)
        let down = CGEvent(keyboardEventSource: source, virtualKey: m.0, keyDown: true)
        down?.flags = flags
        post(down)
    }
    for isDown in [true, false] {
        let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: isDown)
        event?.flags = flags
        post(event)
    }
    for m in held.reversed() {
        flags.remove(m.1)
        let up = CGEvent(keyboardEventSource: source, virtualKey: m.0, keyDown: false)
        up?.flags = flags
        post(up)
    }
}

func pressCommand(_ params: [String: Any]) {
    guard let key = params["key"] as? String else { fail("press needs a key") }
    let modifiers = params["modifiers"] as? [String] ?? []
    press(key, modifiers)
    emit(["key": key, "modifiers": modifiers])
}

func typeCommand(_ params: [String: Any]) {
    guard let s = params["text"] as? String else { fail("type needs text") }
    for character in s {
        let units = Array(String(character).utf16)
        for isDown in [true, false] {
            let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: isDown)
            event?.flags = []
            event?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
            post(event)
        }
    }
    awaitTyped(s)
    emit(["typed": s.unicodeScalars.count])
}

/// The text of the element holding the keyboard focus; nil when there is none to read (a secure field, no focus).
func focusedValue() -> String? {
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 1)
    guard let focused = attribute(system, kAXFocusedUIElementAttribute as String), CFGetTypeID(focused) == AXUIElementGetTypeID()
    else { return nil }
    return text(focused as! AXUIElement, kAXValueAttribute as String)
}

/// Waits until the focused element holds what was typed: posted keys reach a web page some 150 ms after the
/// last was posted, and a Tab pressed at once took the rest to the next control. A field that changes what
/// is typed (a mask, upper case) never holds it: the wait then ends once its text has stood still for
/// TYPE_SETTLE. Nothing to read: no wait.
func awaitTyped(_ s: String) {
    guard var last = focusedValue() else { return }
    var since = Date()
    let deadline = Date().addingTimeInterval(TYPE_WAIT)
    _ = waitFor(deadline) { () -> Bool? in
        guard let now = focusedValue() else { return true }
        if now.contains(s) { return true }
        if now != last { last = now; since = Date() }
        return Date().timeIntervalSince(since) >= TYPE_SETTLE ? true : nil
    }
}

func elementAt(_ point: CGPoint) -> AXUIElement? {
    var hit: AXUIElement?
    guard AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(point.x), Float(point.y), &hit) == .success
    else { return nil }
    return hit
}

func parent(_ element: AXUIElement) -> AXUIElement? {
    guard let p = attribute(element, kAXParentAttribute as String), CFGetTypeID(p) == AXUIElementGetTypeID() else { return nil }
    return (p as! AXUIElement)
}

/// Is what lies under the point the element vmlab meant (it, or something inside it)?
func hits(_ hit: AXUIElement, expect: [String: Any]) -> Bool {
    let wantedLabel = expect["label"] as? String ?? ""
    let wantedBounds = expect["bounds"] as? [String: Int]
    let said = label(hit)
    // The hit may be text inside the target ("Run" inside a button labelled "Run"), never merely a shorter label.
    if !wantedLabel.isEmpty && said == wantedLabel { return true }
    guard let wb = wantedBounds else { return wantedLabel.isEmpty }
    var element: AXUIElement? = hit
    for _ in 0..<12 {
        guard let e = element else { break }
        if let b = bounds(frame(e)) as? [String: Int], b == wb { return true }
        element = parent(e)
    }
    return false
}

func owner(_ element: AXUIElement) -> String {
    var pid: pid_t = 0
    guard AXUIElementGetPid(element, &pid) == .success, let app = NSRunningApplication(processIdentifier: pid) else { return "?" }
    return app.localizedName ?? app.bundleIdentifier ?? "pid \(pid)"
}

func click(_ params: [String: Any]) {
    guard let x = params["x"] as? Double ?? (params["x"] as? Int).map(Double.init),
          let y = params["y"] as? Double ?? (params["y"] as? Int).map(Double.init) else { fail("click needs x and y") }
    let point = CGPoint(x: x, y: y)
    guard screenFrame().contains(point) else { fail("(\(Int(x)), \(Int(y))) is off the screen \(screenFrame())") }
    let under = elementAt(point)
    if let expect = params["expect"] as? [String: Any] {
        // Refuse rather than click blind: an element scrolled out of view, or covered, still has bounds.
        guard let hit = under, hits(hit, expect: expect) else {
            let what = under.map { "\(text($0, kAXRoleAttribute as String) ?? "?") \"\(label($0))\" of \(owner($0))" } ?? "nothing"
            fail("something else is at (\(Int(x)), \(Int(y))): \(what); the element may be covered or scrolled out of view")
        }
    }
    for type in [CGEventType.mouseMoved, .leftMouseDown, .leftMouseUp] {
        post(CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: .left))
    }
    emit(["x": Int(x), "y": Int(y), "under": under.map { label($0) } as Any? ?? NSNull()])
}

// MARK: - Clipboard and staging

func clipboard(_ params: [String: Any]) {
    let board = NSPasteboard.general
    if let s = params["set"] as? String {
        board.clearContents()
        board.setString(s, forType: .string)
    }
    emit(["text": board.string(forType: .string) as Any? ?? NSNull()])
}

/// Wait for a condition, polling; nil when the deadline passes.
func waitFor<T>(_ deadline: Date, _ what: () -> T?) -> T? {
    while true {
        if let value = what() { return value }
        if Date() >= deadline { return nil }
        // Spin the run loop, not sleep: NSWorkspace and NSRunningApplication refresh only through it.
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    }
}

func frontmostName() -> String {
    NSWorkspace.shared.frontmostApplication?.localizedName ?? ""
}

func focusedSelection(_ app: NSRunningApplication) -> String? {
    let ax = AXUIElementCreateApplication(app.processIdentifier)
    guard let focused = attribute(ax, kAXFocusedUIElementAttribute as String), CFGetTypeID(focused) == AXUIElementGetTypeID()
    else { return nil }
    return text(focused as! AXUIElement, kAXSelectedTextAttribute as String)
}

/// The app whose window has the keyboard focus, which need not be the frontmost app; nil if unknown.
func keyboardFocusApp() -> NSRunningApplication? {
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 3)
    guard let focused = attribute(system, kAXFocusedApplicationAttribute as String), CFGetTypeID(focused) == AXUIElementGetTypeID()
    else { return nil }
    var pid: pid_t = 0
    guard AXUIElementGetPid(focused as! AXUIElement, &pid) == .success else { return nil }
    return NSRunningApplication(processIdentifier: pid)
}

func bringToFront(_ app: NSRunningApplication, _ appName: String, _ deadline: Date) {
    guard waitFor(deadline, { () -> Bool? in
        if NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier { return true }
        app.activate()
        return nil
    }) != nil else { fail("\(appName) did not come to the front in time; frontmost is \(frontmostName())") }
}

/// Bring a running app to the front, first raising its window whose title contains "window".
func focus(_ params: [String: Any]) {
    guard let appName = params["app"] as? String else { fail("focus needs an app") }
    let deadline = Date().addingTimeInterval(params["timeout"] as? Double ?? 30)
    guard let app = appsFor(appName).first else { fail("\(appName) is not running") }
    var raised: Any = NSNull()
    if let wanted = params["window"] as? String {
        let ax = AXUIElementCreateApplication(app.processIdentifier)
        AXUIElementSetMessagingTimeout(ax, 3)
        let windows = attribute(ax, kAXWindowsAttribute as String) as? [AXUIElement] ?? []
        let titles = windows.map { text($0, kAXTitleAttribute as String) ?? "" }
        guard let i = titles.firstIndex(where: { $0.contains(wanted) }) else {
            fail("\(appName) has no window titled like \"\(wanted)\"; its windows: \(titles)")
        }
        AXUIElementPerformAction(windows[i], kAXRaiseAction as CFString)
        AXUIElementSetAttributeValue(windows[i], kAXMainAttribute as CFString, kCFBooleanTrue)
        raised = titles[i]
    }
    bringToFront(app, appName, deadline)
    emit(["app": app.localizedName ?? appName, "window": raised, "frontmost": frontmostName()])
}

/// The Staged document at path, symlinks resolved; fails unless it is a file stage-text writes.
func stagedFile(_ path: String) -> URL {
    let url = URL(fileURLWithPath: path)
    let staging = URL(fileURLWithPath: NSTemporaryDirectory()).resolvingSymlinksInPath()
    guard url.lastPathComponent.range(of: "^\(STAGE)[0-9a-fA-F]{8}\\.txt$", options: .regularExpression) != nil,
          url.deletingLastPathComponent().resolvingSymlinksInPath().path == staging.path
    else { fail("\(path) is not a Staged document: stage-text writes them to \(staging.path) as \(STAGE)XXXXXXXX.txt") }
    return url.resolvingSymlinksInPath()
}

/// The app's window showing file, known by its document's path.
func documentWindow(_ ax: AXUIElement, _ file: URL) -> AXUIElement? {
    let windows = attribute(ax, kAXWindowsAttribute as String) as? [AXUIElement] ?? []
    return windows.first { window in
        guard let doc = text(window, kAXDocumentAttribute as String), let url = URL(string: doc), url.isFileURL else { return false }
        return url.resolvingSymlinksInPath().path == file.path
    }
}

/// Press the close button of file's window until it has gone; false if it is still there at the deadline.
func closeDocument(_ ax: AXUIElement, _ file: URL, _ deadline: Date) -> Bool {
    waitFor(deadline) { () -> Bool? in
        guard let window = documentWindow(ax, file) else { return true }
        if let button = attribute(window, kAXCloseButtonAttribute as String), CFGetTypeID(button) == AXUIElementGetTypeID() {
            AXUIElementPerformAction(button as! AXUIElement, kAXPressAction as CFString)
        }
        return nil
    } != nil
}

/// Close the Staged document params["file"] in params["app"]; the app's other documents stay. Its
/// window's close button asks the app to close it: TextEdit saves a document itself, even one a
/// Scenario typed into, so nothing asks about saving. Pressing it leaves the other windows' order.
func closeStaged(_ params: [String: Any]) {
    guard let path = params["file"] as? String, let appName = params["app"] as? String else { fail("close-staged needs file and app") }
    let file = stagedFile(path)
    let deadline = Date().addingTimeInterval(params["timeout"] as? Double ?? 30)
    guard let app = appsFor(appName).first else { return emit(["file": path, "closed": false]) }
    let ax = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(ax, 3)
    guard documentWindow(ax, file) != nil else { return emit(["file": path, "closed": false]) }
    if !closeDocument(ax, file, deadline) {
        let windows = attribute(ax, kAXWindowsAttribute as String) as? [AXUIElement] ?? []
        fail("\(appName) did not close \(file.lastPathComponent) in time; its windows: \(windows.map { text($0, kAXTitleAttribute as String) ?? "" })")
    }
    emit(["file": path, "closed": true])
}

/// Open text in app, select it all and, in the same call, press the trigger chord,
/// so nothing can take focus in between.
func stageText(_ params: [String: Any]) {
    guard let s = params["text"] as? String, let appName = params["app"] as? String else { fail("stage-text needs text and app") }
    let deadline = Date().addingTimeInterval(params["timeout"] as? Double ?? 30)
    let file = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("\(STAGE)\(UUID().uuidString.prefix(8)).txt")
    do { try s.write(to: file, atomically: true, encoding: .utf8) } catch { fail("cannot write \(file.path): \(error)") }
    let open = Process()
    open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    open.arguments = ["-a", appName, file.path]
    do { try open.run() } catch { fail("cannot run open: \(error)") }
    open.waitUntilExit()
    guard open.terminationStatus == 0 else { fail("open -a \(appName) failed: is the app installed?") }

    let stem = file.deletingPathExtension().lastPathComponent
    var titles: [String] = []
    guard let app = waitFor(deadline, { () -> NSRunningApplication? in
        guard let a = appsFor(appName).first else { return nil }
        let ax = AXUIElementCreateApplication(a.processIdentifier)
        AXUIElementSetMessagingTimeout(ax, 3)
        let windows = attribute(ax, kAXWindowsAttribute as String) as? [AXUIElement] ?? []
        titles = windows.map { text($0, kAXTitleAttribute as String) ?? "" }
        return titles.contains { $0.contains(stem) } ? a : nil
    }) else { fail("\(appName) showed no window for \(file.lastPathComponent) in time; its windows: \(titles)") }

    // Another app's panel that does not activate its app (a launcher's) keeps the keys while the
    // editor is frontmost: a select-all and the chord would go to it, so stage-text waits for it to
    // go, or fails. A select-all that selects nothing is pressed again until the text is selected.
    var selected: String?
    var keys: NSRunningApplication?
    while true {
        bringToFront(app, appName, deadline)
        keys = keyboardFocusApp()
        if keys == nil || keys!.processIdentifier == app.processIdentifier {
            press("a", ["cmd"])
            selected = waitFor(min(deadline, Date().addingTimeInterval(SELECT_WAIT))) { focusedSelection(app) == s ? s : nil }
        } else {
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        if selected != nil || Date() >= deadline { break }
    }
    if let keys = keys, keys.processIdentifier != app.processIdentifier {
        // The Host never learns of a document whose stage-text fails: it is closed and deleted here.
        let ax = AXUIElementCreateApplication(app.processIdentifier)
        AXUIElementSetMessagingTimeout(ax, 3)
        _ = closeDocument(ax, file.resolvingSymlinksInPath(), Date().addingTimeInterval(10))
        try? FileManager.default.removeItem(at: file)
        fail("\(keys.localizedName ?? "pid \(keys.processIdentifier)") has the keyboard focus, though \(appName) is frontmost (a window of it that does not activate it, such as a panel, is open): a select-all and the chord would go to it")
    }
    selected = selected ?? focusedSelection(app)
    let frontmost = frontmostName()  // what the trigger lands on, recorded before it is pressed
    var pressed: Any = NSNull()
    if let then = params["then"] as? [String: Any], let key = then["key"] as? String {
        let modifiers = then["modifiers"] as? [String] ?? []
        press(key, modifiers)
        pressed = ["key": key, "modifiers": modifiers]
    }
    emit(["app": app.localizedName ?? appName, "file": file.path, "frontmost": frontmost, "selected": selected as Any? ?? NSNull(), "pressed": pressed])
}

// MARK: - Tray menus

/// An item of a Tray menu: its element, its node in the contract's shape, its submenu's items.
struct TrayItem {
    let element: AXUIElement
    let node: [String: Any]
    let submenu: [TrayItem]?
}

/// The items of the menu element holds (a Tray icon, or a menu item with a submenu); nil when it holds none.
func trayMenu(_ element: AXUIElement) -> [TrayItem]? {
    guard let menu = children(element).first(where: { text($0, kAXRoleAttribute as String) == "AXMenu" }) else { return nil }
    return children(menu).compactMap { item -> TrayItem? in
        let title = text(item, kAXTitleAttribute as String) ?? ""
        let enabled = (attribute(item, kAXEnabledAttribute as String) as? Bool) ?? true
        if title.isEmpty && !enabled { return nil }  // a separator
        let submenu = trayMenu(item)
        let node: [String: Any] = [
            "name": title, "enabled": enabled,
            "checked": text(item, "AXMenuItemMarkChar") == "\u{2713}",  // a check mark; "-" is the mixed state
            "children": (submenu ?? []).map { $0.node },
        ]
        return TrayItem(element: item, node: node, submenu: submenu)
    }
}

/// Read an app's Tray menu, and choose params["choose"] (a label per menu level) from it. An
/// NSStatusItem's menu, submenus included, is in the app's extras menu bar even while it is closed,
/// and pressing an item there chooses it: no menu opens, so nothing covering the menu bar (loginwindow
/// after a launch) is in the way and nothing is left open.
func tray(_ params: [String: Any]) {
    guard let wanted = params["app"] as? String else { fail("tray needs an app") }
    let path = params["choose"] as? [String] ?? []
    let deadline = Date().addingTimeInterval(params["timeout"] as? Double ?? 10)
    while true {
        var icon: AXUIElement?
        for app in appsFor(wanted) {
            let ax = AXUIElementCreateApplication(app.processIdentifier)
            AXUIElementSetMessagingTimeout(ax, 3)
            if let extras = attribute(ax, kAXExtrasMenuBarAttribute as String), CFGetTypeID(extras) == AXUIElementGetTypeID(),
               let first = children(extras as! AXUIElement).first {
                icon = first
                break
            }
        }
        guard let status = icon else { return emit(["icon": false]) }
        if params["icon_only"] as? Bool == true { return emit(["icon": true]) }  // whether it is there, nothing more
        if let result = choose(status, path) { return emit(result) }
        // The app replaced its menu between the read and the press (some rebuild it once they have
        // started): read the new one and press again.
        if Date() >= deadline { fail("pressing \(path.last ?? "") in the Tray menu failed: the app kept replacing its menu") }
        usleep(200_000)
    }
}

/// Read a Tray icon's menu and choose path from it: the tray command's result, or nil when the item
/// to press had gone (AXError -25202, invalid element) by the time it was pressed.
func choose(_ status: AXUIElement, _ path: [String]) -> [String: Any]? {
    let top = trayMenu(status) ?? []
    let items = top.map { $0.node }
    var level: [TrayItem]? = top
    var chosen: [String] = []
    for (i, label) in path.enumerated() {
        guard let menu = level else {
            return ["icon": true, "items": items, "chosen": NSNull(), "failed": ["at": chosen + [label], "reason": "leaf"]]
        }
        chosen.append(label)
        guard let item = menu.first(where: { $0.node["name"] as? String == label }), item.node["enabled"] as? Bool == true else {
            let reason = menu.contains(where: { $0.node["name"] as? String == label }) ? "disabled" : "missing"
            return ["icon": true, "items": items, "chosen": NSNull(), "failed": ["at": chosen, "reason": reason]]
        }
        if i == path.count - 1 {
            if item.submenu != nil {  // pressing it would choose nothing
                return ["icon": true, "items": items, "chosen": NSNull(), "failed": ["at": chosen, "reason": "submenu"]]
            }
            let pressed = AXUIElementPerformAction(item.element, kAXPressAction as CFString)
            if pressed == .invalidUIElement { return nil }
            if pressed != .success { fail("pressing \(label) in the Tray menu failed (AXError \(pressed.rawValue))") }
        }
        level = item.submenu
    }
    return ["icon": true, "items": items, "chosen": path.isEmpty ? NSNull() : path as Any, "failed": NSNull()]
}

// MARK: - Notifications

// usernoted's store: a record per Notification, as a binary plist, shown on screen or not.
let NOTIFICATION_DB = NSHomeDirectory() + "/Library/Group Containers/group.com.apple.usernoted/db2/db"

let ISO_TIME: ISO8601DateFormatter = {
    let format = ISO8601DateFormatter()
    format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return format
}()

func isoTime(_ date: Date) -> String { ISO_TIME.string(from: date) }

func notifications() {
    var found: [[String: Any]] = []
    let now = isoTime(Date())
    guard FileManager.default.fileExists(atPath: NOTIFICATION_DB) else { return emit(["now": now, "notifications": found]) }
    var db: OpaquePointer?
    guard sqlite3_open_v2(NOTIFICATION_DB, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
        fail("cannot open \(NOTIFICATION_DB): \(String(cString: sqlite3_errmsg(db)))")
    }
    defer { sqlite3_close(db) }
    sqlite3_busy_timeout(db, 2000)
    var statement: OpaquePointer?
    guard sqlite3_prepare_v2(db, "SELECT data, delivered_date FROM record", -1, &statement, nil) == SQLITE_OK else {
        fail("cannot read \(NOTIFICATION_DB): \(String(cString: sqlite3_errmsg(db)))")
    }
    defer { sqlite3_finalize(statement) }
    while sqlite3_step(statement) == SQLITE_ROW {
        guard let bytes = sqlite3_column_blob(statement, 0) else { continue }
        let data = Data(bytes: bytes, count: Int(sqlite3_column_bytes(statement, 0)))
        guard let record = (try? PropertyListSerialization.propertyList(from: data, format: nil)) as? [String: Any] else { continue }
        let request = record["req"] as? [String: Any] ?? [:]
        // Seconds since 2001 (Core Data's epoch); a record not delivered yet has only its own date.
        let delivered = sqlite3_column_type(statement, 1) == SQLITE_NULL ? record["date"] as? Double : sqlite3_column_double(statement, 1)
        guard let seconds = delivered else { continue }
        found.append([
            "app": record["app"] as? String ?? "",
            "title": request["titl"] as? String ?? "",
            "body": request["body"] as? String ?? "",
            "time": isoTime(Date(timeIntervalSinceReferenceDate: seconds)),
        ])
    }
    emit(["now": now, "notifications": found])
}

// MARK: - Main

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage: vmlab-ui COMMAND [JSON]") }
var params: [String: Any] = [:]
if args.count >= 3 {
    guard let data = args[2].data(using: .utf8),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { fail("parameters are not a JSON object: \(args[2])") }
    params = object
}
/// Has this Channel the Accessibility grant? A no is asked again for TRUST_WAIT: on a busy Guest TCC said no
/// once in a series of calls over the same Channel whose others all had it.
func trusted() -> Bool {
    let deadline = Date().addingTimeInterval(TRUST_WAIT)
    while !AXIsProcessTrusted() {
        if Date() >= deadline { return false }
        usleep(250_000)
    }
    return true
}

if !["version", "clipboard", "notifications"].contains(args[1]) && !trusted() {
    fail("no Accessibility permission for this Channel (asked for \(Int(TRUST_WAIT)) s); re-provision the Base guest (vmlab base create NAME --reprovision)")
}
switch args[1] {
case "version": emit(["helper": "swift", "version": VERSION, "trusted": trusted()])
case "tree": tree(params)
case "click": click(params)
case "press": pressCommand(params)
case "type": typeCommand(params)
case "clipboard": clipboard(params)
case "stage-text": stageText(params)
case "close-staged": closeStaged(params)
case "focus": focus(params)
case "tray": tray(params)
case "notifications": notifications()
default: fail("unknown command \(args[1])")
}
