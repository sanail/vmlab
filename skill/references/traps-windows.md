# Windows Guest traps

Symptoms first, then the cause and what to do. vmlab already handles, so Scenarios need nothing for them: bringing a window to the front (Windows refuses `SetForegroundWindow` from a background process), console windows stealing focus from the app (calls run headless), 200 % scaling, chords on any keyboard layout, typing pace, and output encoding (UTF-8 everywhere).

## Installing needs elevation

**Symptom**: `install` fails with "access denied", or exits 0 and nothing is installed.

**Cause**: every Guest call runs as the logged-in user, unelevated. A per-machine installer (MSI, an NSIS installer for all users) needs an administrator token.

**Do**: elevate inside the recipe: `Start-Process -Verb RunAs -Wait msiexec "/i $env:VMLAB_ARTIFACT /qn"`. Base guests let administrators elevate without a prompt, so no one has to click. A per-user installer needs no elevation.

## Web content arrives late in the tree

**Symptom**: `ui find` sees the window's title bar and Close button but none of the page (WebView2: Tauri, Electron, .NET WebView2), and a Check reading the page fails.

**Cause**: WebView2 turns on accessibility when the first UI Automation client arrives, and fills the page's subtree after the window's frame is already there.

**Do**: wait for the page itself, `g.wait_for(role="Document", app=APP)`, or better an element of your own page by its text, then read. Match the page by role or by the app's own text: the frame's names are localised (e.g. "MyApp — Web content" in the Guest's display language).

## Controls inside WebView2 refuse UI Automation

**Symptom**: focusing an element fails ("the target element cannot receive focus") while its window is in front; expanding a combo box returns without opening it.

**Do**: drive the page with keys: `tab` until the element is focused (check `focused` in the tree), `alt+down` to open a combo box.

**Popups belong to another process**: an open drop-down list is drawn by `msedgewebview2` in its own window, not by the app's process, so `--app APP` never finds it. Look at the whole tree, and check what opened by comparing it before and after.

## Theme, language and layout settings

A web view reads the Windows theme (`AppsUseLightTheme`) once, when it is created. A Scenario that tests a theme sets it before launch: `LAUNCH = False`, set the registry value with `g.exec`, then `g.launch()`. The same goes for any setting an app reads at start. A Check that depends on the keyboard layout sets the layout explicitly and records it, since a Guest's default is whatever its Windows install chose.

## Taskbar and tray

Whether a window has a taskbar button cannot be read from its styles: frameworks remove buttons through `ITaskbarList`, which leaves the styles untouched. Ask the taskbar: `g.find(role="button", text="MyApp", app="explorer")`. Tray icons are `explorer`'s too. The shell caches button icons across reinstalls and reboots: a Check about an icon after an update must first plant the old icon (install the old version, show its window), or it never goes red.

## PowerShell parses twice

`g.exec(["powershell", "-Command", ...])` hands PowerShell a string it parses again: unquoted spaces split arguments, and a comma turns a value into an array. Pass arguments as argv items to a script file (`-File`), or put test data in a file and pass its path. For the clipboard, `g.clipboard()` and `g.set_clipboard()`.

## x64 builds under emulation

On an Apple Silicon Host an x64 build runs under Windows' emulation. It proves the build starts and behaves; it says nothing about x64 hardware. Tell the user so when reporting.
