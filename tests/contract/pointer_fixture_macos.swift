// A window that prints the pointer's buttons, for the contract tests, in a macOS Guest.
//
//     swiftc -o vmlab-chk-XXXX pointer_fixture_macos.swift && ./vmlab-chk-XXXX TITLE
//
// Its window, titled TITLE, prints a line per button press, release and move with a button
// held: "down|move|up BUTTON MODIFIERS", BUTTON left, right, middle, back or forward,
// MODIFIERS like ctrl+shift, or - for none. Built with the Guest's own swiftc: the Guest
// installs nothing.

import AppKit

let OTHER_BUTTONS = [2: "middle", 3: "back", 4: "forward"]
let MODIFIERS: [(String, NSEvent.ModifierFlags)] = [("ctrl", .control), ("alt", .option), ("shift", .shift), ("cmd", .command)]

func say(_ what: String, _ event: NSEvent, _ button: String? = nil) {
    let held = MODIFIERS.filter { event.modifierFlags.contains($0.1) }.map { $0.0 }
    let name = button ?? OTHER_BUTTONS[event.buttonNumber] ?? "button\(event.buttonNumber)"
    print("\(what) \(name) \(held.isEmpty ? "-" : held.joined(separator: "+"))")
    fflush(stdout)
}

final class Pad: NSView {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) { say("down", event, "left") }
    override func mouseDragged(with event: NSEvent) { say("move", event, "left") }
    override func mouseUp(with event: NSEvent) { say("up", event, "left") }
    override func rightMouseDown(with event: NSEvent) { say("down", event, "right") }
    override func rightMouseDragged(with event: NSEvent) { say("move", event, "right") }
    override func rightMouseUp(with event: NSEvent) { say("up", event, "right") }
    override func otherMouseDown(with event: NSEvent) { say("down", event) }
    override func otherMouseDragged(with event: NSEvent) { say("move", event) }
    override func otherMouseUp(with event: NSEvent) { say("up", event) }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let window = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 600, height: 400), styleMask: [.titled, .closable],
                      backing: .buffered, defer: false)
window.title = CommandLine.arguments[1]
window.contentView = Pad()
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)
print("window up")
fflush(stdout)
app.run()
