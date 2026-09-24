// A Tray icon for the contract tests, in a macOS Guest: an NSStatusItem with a menu.
//
//     swiftc -o vmlab-tray-XXXX tray_fixture_macos.swift && ./vmlab-tray-XXXX RECORD
//
// Its Tray menu: Open, a separator, Settings (a submenu: Advanced, and Dark mode,
// checked), Pinned, checked, and Update, disabled. Choosing an item appends its
// title to the file RECORD. Built with the Guest's own swiftc: the Guest installs
// nothing. It runs as an accessory app (no Dock icon), named after its executable.

import AppKit

final class Fixture: NSObject {
    let record: String
    var item: NSStatusItem?

    init(record: String) { self.record = record }

    @objc func chosen(_ sender: NSMenuItem) {
        let line = sender.title + "\n"
        if let handle = FileHandle(forWritingAtPath: record) {
            handle.seekToEndOfFile()
            handle.write(line.data(using: .utf8)!)
            handle.closeFile()
        } else {
            FileManager.default.createFile(atPath: record, contents: line.data(using: .utf8))
        }
    }

    func entry(_ title: String, checked: Bool = false, enabled: Bool = true) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: enabled ? #selector(chosen(_:)) : nil, keyEquivalent: "")
        entry.target = self
        entry.state = checked ? .on : .off
        return entry
    }

    func start() {
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.addItem(entry("Open"))
        menu.addItem(NSMenuItem.separator())
        let settings = NSMenuItem(title: "Settings", action: nil, keyEquivalent: "")
        let submenu = NSMenu(title: "Settings")
        submenu.addItem(entry("Advanced"))
        submenu.addItem(entry("Dark mode", checked: true))
        settings.submenu = submenu
        menu.addItem(settings)
        menu.addItem(entry("Pinned", checked: true))
        let update = entry("Update", enabled: false)
        update.isEnabled = false
        menu.addItem(update)
        let status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        status.button?.title = "V"
        status.menu = menu
        item = status
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let fixture = Fixture(record: CommandLine.arguments[1])
fixture.start()
print("tray icon up")
fflush(stdout)
app.run()
