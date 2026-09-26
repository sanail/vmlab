# Linux Guest traps

Symptoms first, then the cause and what to do. vmlab already handles, so Scenarios need nothing for them: input, pointer and window focus on Wayland (vmlab's GNOME Shell extension; `xdotool` reaches only Xwayland apps there, and `ydotool` misplaces the pointer), typing any character on any layout, GTK's wrong element coordinates, hidden widgets left in the tree, a WebKitGTK page under containers that say they are not visible, WebKitGTK frozen on its first frame, a busy editor that takes a select-all before its new document is loaded or a close before its save is done, reboots blocked by an editor, and screen lock, blanking and suspend.

Drive input only through `g.press`, `g.type`, `g.click` and `g.clipboard`: the same Scenario then works in both Desktop sessions.

## Which Desktop session

A Lab runs GNOME on Wayland (`session = "wayland"`) or Xfce on X11 (`"x11"`). What differs for apps: on Wayland an app cannot grab a global hotkey, read another app's window, or read the clipboard unless it is focused. An app that works on X11 and fails on Wayland is often a finding, not a Scenario bug; testing both means two Labs.

## Global hotkeys on Wayland

**Symptom**: the app's hotkey works on X11 and does nothing on Wayland.

**Cause**: on Wayland an app binds a global shortcut through the xdg-desktop-portal GlobalShortcuts interface, and the desktop asks the user to approve it the first time.

**Do**: the approval dialog is part of the app's first-run flow: `g.wait_for` it, click its button with `g.click`, `wait_for(..., gone=True)` until it has gone, then press the hotkey.

## Web content missing from the tree

**Symptom**: `ui tree` shows an Electron or Chromium app's window with no page inside.

**Do**: launch it with `--force-renderer-accessibility` in the `launch` recipe. WebKitGTK apps (Tauri) expose their page through AT-SPI without a flag. A WebKitGTK window that stays blank: set `WEBKIT_DISABLE_COMPOSITING_MODE=1` in the Lab's `app.env`.

## Tray menus

Panels keep Tray icons and their menus out of `ui tree`: read and choose from a Tray menu with `g.tray(APP, choose=...)`, which drives it over D-Bus, the way the panel does, in both Desktop sessions, and wait for the icon with `g.wait_for(tray=APP)` (or `ready = { tray = "APP" }`), which asks the same StatusNotifierWatcher and reads nothing of the menu; without a watcher it is never met, and its result's `"detail"` says so. It finds StatusNotifierItem icons only (GTK's, Qt's, Electron's, libayatana's); an app with an old XEmbed tray icon has none to find. `vmlab doctor` warns when the Desktop session has no StatusNotifierWatcher, which means no panel shows Tray icons.

## Notifications

**Do**: read them with `g.wait_for(notification=NONCE)` and `g.notifications()` ([Scenario API](scenarios.md)), not from the screen. Linux keeps no history of Notifications, so vmlab's recorder, a user unit Base guests start at login in both Desktop sessions, writes down every `Notify` call on the session bus, by the app name the app gives it. `vmlab doctor` warns when a Guest has no recorder (a Base guest provisioned by an older vmlab: `vmlab base create <base> --reprovision`). Put a nonce in what the app sends. A Notification that never comes while the app logged that it notified points at the app, not the Guest: Base guests run a notification server in both Desktop sessions.

## Processes and packages

- `pkill -x NAME` matches the kernel's process name, cut to 15 characters: use `pkill -f /path/to/binary` for longer names.
- `apt-get` in `install` fails with a lock while another apt runs. Base guests turn background updates off, so a lock means something in the Guest started one: pass `-o DPkg::Lock::Timeout=300` to wait for it.
- UI calls fail with "no graphical session is logged in" after dozens of Runs on one boot, and the Guest's journal says "Too many open files": the Base guest predates provisioning v6, whose sessions keep AT-SPI off peer-to-peer connections (`ATSPI_DISABLE_P2P=1`); over them every UI call left file descriptors in each GTK app until the session died. `vmlab base create <base> --reprovision`.
- An app's list of recent files stays empty: Base guests remember no recent files (`org.gnome.desktop.privacy remember-recent-files`), since every Run opens files and the list would only grow. A Scenario that tests such a list turns it on first: `g.exec(["gsettings", "set", "org.gnome.desktop.privacy", "remember-recent-files", "true"])`.
- A black screenshot means the Guest's screen is off; restart it (`vmlab down LAB && vmlab up LAB`) and report it if it returns, since provisioning turns blanking off.

## Keyboard layouts

A Check that depends on the layout sets it explicitly and records it: `gsettings set org.gnome.desktop.input-sources sources "[('xkb', 'ru')]"` on GNOME, `setxkbmap ru` on X11.
