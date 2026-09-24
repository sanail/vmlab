# Linux Guest traps

Symptoms first, then the cause and what to do. vmlab already handles, so Scenarios need nothing for them: input, pointer and window focus on Wayland (vmlab's GNOME Shell extension; `xdotool` reaches only Xwayland apps there, and `ydotool` misplaces the pointer), typing any character on any layout, GTK's wrong element coordinates, hidden widgets left in the tree, a WebKitGTK page under containers that say they are not visible, WebKitGTK frozen on its first frame, reboots blocked by an editor, earlier staged documents piling up as editor tabs (each `stage-text` saves and closes them; the tabs a Scenario opened itself stay), and screen lock, blanking and suspend.

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

**Symptom**: the app's tray icon is on the panel, but neither `ui tree` nor `ui find` shows it or its menu.

**Cause**: panels keep tray icons and their menus out of the accessibility tree, and Wayland gives no coordinates to click at.

**Do**: drive the menu the way the panel does, over D-Bus: the icon is a StatusNotifierItem registered with `org.kde.StatusNotifierWatcher` (`RegisteredStatusNotifierItems`), its `Menu` property names a `com.canonical.dbusmenu` object, `GetLayout` lists the items and `Event(id, "clicked", ...)` chooses one. Run it with `g.exec` as a small `python3` script (`gi.repository.Gio`), with `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus`. A libayatana item is registered as `:1.42/org/ayatana/NotificationItem/x`: the bus name ends at the first `/`.

## Notifications

**Do**: record them on the session bus rather than on screen: start `dbus-monitor --session "interface='org.freedesktop.Notifications',member='Notify'"` with `g.spawn` (same bus address as above, in `env=`) before the step, and read its output (`wait_for(log=handle.log, pattern=...)`, `handle.output()`): each `Notify` call lists the app name, icon, summary and body as its first four strings. Put a nonce in what the app sends. A `Notify` that never comes while the app logged that it notified points at the app, not the Guest: Base guests run a notification server in both Desktop sessions.

## Processes and packages

- `pkill -x NAME` matches the kernel's process name, cut to 15 characters: use `pkill -f /path/to/binary` for longer names.
- `apt-get` in `install` fails with a lock while another apt runs. Base guests turn background updates off, so a lock means something in the Guest started one: pass `-o DPkg::Lock::Timeout=300` to wait for it.
- A black screenshot means the Guest's screen is off; restart it (`vmlab down LAB && vmlab up LAB`) and report it if it returns, since provisioning turns blanking off.

## Keyboard layouts

A Check that depends on the layout sets it explicitly and records it: `gsettings set org.gnome.desktop.input-sources sources "[('xkb', 'ru')]"` on GNOME, `setxkbmap ru` on X11.
