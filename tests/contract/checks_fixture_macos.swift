// A window of tick states for the contract tests, in a macOS Guest: AppKit checkboxes and radio buttons.
//
//     swiftc -o vmlab-chk-XXXX checks_fixture_macos.swift && ./vmlab-chk-XXXX TITLE
//
// Its window, titled TITLE: Pictures (ticked), Music (unticked), Some (in its middle
// state), radio buttons Light (chosen) and Dark, and a Save button. Built with the
// Guest's own swiftc: the Guest installs nothing.

import AppKit

final class Fixture: NSObject {
    var window: NSWindow?

    @objc func chose(_ sender: NSButton) {}  // radio buttons with one action are one group

    func start(title: String) {
        let window = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 320, height: 260), styleMask: [.titled, .closable],
                              backing: .buffered, defer: false)
        window.title = title
        let views: [NSView] = [
            check("Pictures", .on), check("Music", .off), check("Some", .mixed),
            radio("Light", .on), radio("Dark", .off), NSButton(title: "Save", target: nil, action: nil),
        ]
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.frame = window.contentView!.bounds.insetBy(dx: 20, dy: 20)
        window.contentView!.addSubview(stack)
        window.makeKeyAndOrderFront(nil)
        self.window = window
    }

    func check(_ title: String, _ state: NSControl.StateValue) -> NSButton {
        let button = NSButton(checkboxWithTitle: title, target: nil, action: nil)
        button.allowsMixedState = state == .mixed
        button.state = state
        return button
    }

    func radio(_ title: String, _ state: NSControl.StateValue) -> NSButton {
        let button = NSButton(radioButtonWithTitle: title, target: self, action: #selector(chose(_:)))
        button.state = state
        return button
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let fixture = Fixture()
fixture.start(title: CommandLine.arguments[1])
app.activate(ignoringOtherApps: true)
print("window up")
fflush(stdout)
app.run()
