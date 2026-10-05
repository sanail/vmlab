# Scenario API

A Scenario is a Python file defining `scenario(g)`. Before it runs, vmlab quits the app (waiting for its process to go, if the Lab names one), removes the Lab's `app.state` paths and launches the app (the Lab's `[labs.LAB.app]` recipes), waiting for its `ready` condition if the Lab has one; that wait is not on the Scenario's clock.

```python
FRESH = True     # optional: restore Clean state before this Scenario
LAUNCH = False   # optional: skip the launch; call g.launch(env={...}) yourself: it waits for app.ready too, on the Scenario's clock
                 # g.quit(env={...}) is its counterpart: the quit recipe, then a wait for the app's process to go (see below)
TIMEOUT = 120    # optional: seconds for the whole Scenario (default: the Lab's scenario_timeout)

def scenario(g):
    r = g.exec(["cat", "/tmp/myapp.log"])      # r.code, r.stdout, r.stderr, r.ok, r.channel; timeout=, env=
    g.check("the app logged its start", "started" in r.stdout, detail=r.stdout[-500:])
    path = g.put("~/Documents/input.txt", "Ohm's law\n")  # str (UTF-8) or bytes; folders made; returns the absolute Guest path
    settings = g.get("~/.config/myapp/settings.json")   # str; g.get(path, binary=True) for bytes
    mock = g.spawn(["python3", "-m", "http.server", "8080"])  # detached; stopped with the Run (see below)
    g.screenshot("after start")                 # evidence: screenshots/<scenario>/01-after-start.png in the Run folder
    g.check("the icon looks right", True, visual=True)  # a judgement from a screenshot: "visual, unverified"
    # g.lab, g.os ("macos" | "windows" | "linux"), g.language (the Lab language, e.g. "ru-RU"),
    # g.arch (the Build artifact's), g.guest_arch
```

`g.put(guest_path, content)` writes a file into the Guest and `g.get(guest_path, binary=False)` reads one back, with no quoting or shell in between: `~` alone or before a slash is the Guest user's home (`~name` is just a name), and on Windows `%VARS%` expand (`g.put(r"%TEMP%\input.txt", data)`). Spaces and non-ASCII names need nothing special. `put` replaces the file at the path: a symlink there is replaced by the file, and what it pointed to is left alone. `get` raises when the file is not there; both count against the Scenario's timeout, each call (the whole copy) against the Lab's `step_timeout`. Files stay after the Run, so "put a file, restart the app, it reads it" works; `app.state`, `FRESH` and nonces keep leftovers out of later Runs. The CLI has the same: `vmlab put GUEST_PATH [--from HOSTFILE]` (piped stdin when no `--from`, and an empty pipe makes an empty file; prints the Guest path) and `vmlab get GUEST_PATH` (to stdout).

`g.check(name, passed, detail=None, visual=False)` records a Check; a failed Check does not stop the Scenario. `g.skip(name, reason)` records a Skipped Check, one this Run cannot measure: a control that only means something when an earlier Check passed (`if not g.check(...): g.skip(..., "the palette did not open")`), or a Check that does not apply on this Guest OS. `reason` is required; it is the Check's detail. A Skipped Check shows as `SKIP` in the console, `summary.md` and `junit.xml` (`<skipped>`), is counted apart (`skipped_checks` in `report.json`, where its `"passed"` is `null` and `"skipped"` is `true`) and neither passes nor fails the Run. Never skip to hide a failure: a Check that ran and saw the wrong thing is a failed Check. A Scenario still measures at least one Check: one whose Checks are all skipped errors. An exception, a timeout or a call no Channel can carry fails the Run with the Scenario's file and line, and keeps the screen as the Scenario left it (`screenshots/<scenario>/NN-error.png`); the first failed Check keeps the screen it saw too (`NN-failed.png`). So does `sys.exit()` (or `raise SystemExit`), in `scenario(g)` or at the top of the file: it errors only that Scenario's Run (`spawn.py:5: the Scenario called sys.exit(3)`), and the Suite run goes on to the next Scenario and writes its report. Ctrl-C is different: it ends the Suite run on every Lab (under `--parallel` too) with no report, after stopping what the Scenario spawned.

## Restarting the app

`g.quit(env=None)` runs the Lab's `app.quit` recipe (`env` adds to `app.env`, as for `g.launch`) and waits, on the Scenario's clock, until the app's process has gone: the Lab's `app.process`, or `ready`'s process when `ready = { process = "X" }`. It raises naming the process when it is still there after `app.quit_timeout` (default: the Lab's `step_timeout`); the recipe's exit code is not checked, since the wait says whether the app went. A Lab with neither names no process: `g.quit()` then raises when the recipe exits non-zero, and waits for nothing, so an app that saves its settings on the way out may still be writing them. A Lab without a `quit` recipe raises. With `g.launch()` it restarts the app, to check what persisted:

```python
def scenario(g):
    g.tray("MyApp", choose=["Settings", "Dark mode"])
    g.quit()                                   # the app has gone once this returns
    g.launch()                                 # and is ready again (app.ready) once this returns
    settings = g.get("~/.config/myapp/settings.json")
    g.check("dark mode survives a restart", '"theme": "dark"' in settings, detail=settings)
```

Quitting through the app's own UI (its Tray menu's Quit, closing its window) is a Scenario's own steps, then `g.wait_for(process="X", gone=True)`.

Code shared by several Scenarios lives in `_name.py` next to them (`.vmlab/scenarios/_helpers.py`, or in `.vmlab/runs/ad-hoc/`), imported plainly at the top of the Scenario: `import _helpers`. The Scenario's folder is on `sys.path` while it runs; don't add it yourself. Helpers are re-imported for each Scenario, so their module-level state does not carry over to the next one.

## Background processes

`g.spawn(argv, env=None)` starts a command detached in the Guest (a mock server, a log recorder) and returns a handle; `env` adds to its environment. Whatever a Scenario spawns and leaves running is stopped at the end of its Run, however the Run ends (passed, failed, an exception, a timeout, Ctrl-C), so the next Scenario starts without it; a Run that did not pass reports each spawned process's command and the end of its output. There is no CLI counterpart.

```python
def scenario(g):
    # A mock server the app talks to; its stdout and stderr go together to a Guest file, mock.log.
    mock = g.spawn(["python3", "-m", "http.server", "8080", "--bind", "127.0.0.1"])
    up = g.wait_for(exec=["curl", "-fsS", "http://127.0.0.1:8080/"], timeout=20)
    g.check("the mock server answers", up["met"], detail=[up, mock.output()])
    ...
    asked = g.wait_for(log=mock.log, pattern=r"GET /api/v1/items", timeout=10)
    g.check("the app asked the mock server", asked["met"], detail=mock.output()[-2000:])
    mock.stop()                               # optional: the Run stops it anyway
```

- `handle.stop()` ends the process and everything it started, also once the process itself has exited and left them running. On macOS and Linux that is its process group, sent SIGTERM, then SIGKILL after 5 s. On Windows it is its Job Object: each of its windows is asked to close (a GUI app gets 5 s, as if its window's close button were clicked), then the whole job is ended; a console program has no signal to be asked by, so it is ended at once, with no chance to clean up. Stopping one that has already ended is harmless; one that will not stop raises.
- `handle.running()` is true while the process itself runs; `handle.output()` is its output so far, as text; `handle.log` is that output's Guest path (for `wait_for(log=...)`, `g.get`); `handle.pid` is its process id. On Windows a batch file (`.bat`, `.cmd`) runs in `cmd.exe`, whose id that is.
- Output goes to a file, so many programs buffer it and a `wait_for(log=...)` sees it late: run Python with `-u` (or `env={"PYTHONUNBUFFERED": "1"}`), and wait for what the process does (it answers, a file appears) rather than for what it prints, where you can.
- `g.spawn` raises when the Guest has no such command; a command that starts and then fails shows why in its output. Its log is deleted from the Guest at the end of the Run: read it before then (`handle.output()`, `g.get(handle.log)`). A Run that did not pass keeps each process's whole output in its run folder (`spawned/<log name>`, the report's `output_file`).
- A process vmlab cannot stop at the end of the Run does not change the Run's result: the report notes it (`"ended": "failed"`) and vmlab prints a warning.

## UI methods

Each mirrors a `vmlab ui` command and returns the same JSON as a dict.

| Scenario | CLI | Result |
| --- | --- | --- |
| `g.tree(app=None)` | `ui tree [--app APP]` | node tree |
| `g.find(text=, role=, app=)` | `ui find` | `{"matches": [node without children, plus "app"]}` |
| `g.click(text=, role=, app=, index=0, timeout=None)`, `g.click(at=(x, y))` | `ui click [--timeout S]` | `{"x", "y", "element", "under"}` |
| `g.press("cmd+shift+space")` | `ui press CHORD` | `{"chord"}` |
| `g.type(text)` | `ui type TEXT` | `{"typed": n}` |
| `g.focus(app, window=None)` | `ui focus --app APP [--window T]` | `{"app", "window", "frontmost"}` |
| `g.clipboard()`, `g.set_clipboard(text)` | `ui clipboard [--set TEXT]` | `{"text"}`; `""` empties the clipboard |
| `g.stage_text(text, app=None, then=None)` | `ui stage-text TEXT [--app] [--then CHORD]` | `{"app", "file", "frontmost", "selected", "pressed"}` |
| `g.close_staged(staged)` | `ui close-staged --file PATH [--app]` | `{"file", "closed"}` |
| `g.tray(app, choose=None, timeout=None)` | `ui tray --app APP [--choose LABEL]... [--timeout S]` | `{"items": [{"name", "enabled", "checked", "children"}], "chosen"}` |
| `g.notifications(app=None, text=None, since=None)` | `ui notifications [--app ID] [--text PATTERN] [--since TIME]` | `{"notifications": [{"app", "title", "body", "time"}]}` |
| `g.wait_for(text=, role=, app=, process=, file=, log=, exec=, pattern=, notification=, since=, tray=, gone=False, timeout=None)` | `ui wait-for` | `{"met", "waited_s", "condition"[, "matches"][, "code", "stdout"][, "notifications"][, "detail"][, "error"]}` |
| `g.screenshot(name)` | `ui screenshot` | `{"path"}` |

- A node has `role` (cross-OS: `application`, `window`, `button`, `textfield`, `textarea`, `text`, `checkbox`, `menuitem`, ...), `name`, `value`, `description`, `bounds` (`{"x", "y", "w", "h"}` or null), `focused`, `enabled`, `native_role` and `children`. Applications carry `pid`.
- `stage_text` closes nothing: every call opens one more Staged document (its `"file"`). Close each with `g.close_staged(staged)` (the `stage_text` result or its `"file"`) once the Check that needed it is done, so an old selection cannot answer an app that reads "the selection". It saves the document (changes typed into it too), closes just it and deletes its file; the editor's other documents, and the one in front, stay. `"closed"` is false when it was already gone, and a path that is not a Staged document raises. What a Scenario leaves open is closed at the end of its Run, however it ends; one vmlab cannot close is a warning in the report.
- `tray` reads the app's Tray menu (its items in order, without separators; a submenu's items in `children` on every OS, `[]` for an item without one) and, with `choose`, chooses an item: a label, or a list of labels, one per menu level (`choose=["Settings", "Advanced"]`), matched exactly without mnemonics. It raises when the app has no Tray icon, no item has a label, the item is disabled, or it opens a submenu (choose one of its items), naming the items there. `timeout=` waits for the Tray icon to appear (on the Scenario's clock), since tray apps put it up after their launch, and for the item to choose to be there (apps fill or rebuild their menu after the icon is up); `wait_for(tray=APP)` or the Lab's `ready = { tray = "APP" }` waits for it without reading the menu. Nothing is left open. Use it instead of clicking the tray: panels keep Linux Tray menus out of the tree, and a Windows menu opens only on a click on the icon.
- `notifications` lists the Notifications the Guest's OS recorded, oldest first, shown on screen or not. `app` is the OS's id for the sender: the macOS bundle id, the Windows AppUserModelID, the `app_name` a Linux app gives the notification server; it defaults to the Lab's `app.notification_id`, and with neither every app's come back, each with its `"app"` (which is how to find the id). `text` is a Python regex searched for in title and body. `time` is ISO 8601 UTC on the Guest's clock, and so is `since`; in a Scenario `since` defaults to the Run's start, so Notifications of earlier Runs never show up (`since="all"`: the whole history). The CLI lists everything unless given `--since`. Put a nonce in what the app sends. Windows records an app's Notifications only under the AppUserModelID its installer gave its Start menu shortcut: a bare `.exe` has none, and its list stays empty.
- `text` matches name, value and description: exact matches win, otherwise substrings. `role` takes the cross-OS or the native role.
- `click` refuses when nothing matches or something else lies over the element's middle, and says what. With `timeout=` (seconds, on the Scenario's clock) it first waits for the element to be there and uncovered, then raises with the last reason; use it for an element that is about to appear instead of a retry loop of your own. `at=(x, y)` ignores `timeout`.
- Chords: `+`-joined modifiers (`ctrl`, `alt`/`option`, `shift`, `cmd`/`win`/`super`) and one key (`a`-`z`, `0`-`9`, `f1`-`f12`, `space`, `enter`, `tab`, `escape`, `backspace`, arrows, `minus`, `comma`, `slash`, ...). They are sent by physical key, so they work on any keyboard layout; `type` sends Unicode.
- `wait_for` takes exactly one condition and returns `"met": false` on timeout rather than raising: check it. The conditions: an element (`text=`/`role=`, with `"matches"`; `app=` narrows it to one app's and goes with them only), a process by name (`process=`), a Guest path (`file=`), a line in a Guest file (`log=` with `pattern=`, a Python regex), a command (`exec=[argv]`: it exits 0; with `pattern=`, its stdout matches, whatever the exit code; the result carries the last answer's `code` and the tail of its `stdout`), a Notification (`notification=PATTERN`, a Python regex searched for in title and body, posted after the Run's start; `app=` and `since=` as for `notifications`; the result carries the matching `"notifications"`; the CLI counts those after `--since`, by default after its own start), or an app's Tray icon (`tray=APP`, the app as `g.tray` takes it: met once `g.tray` would find the icon; it never opens the menu; `gone=True` waits for the icon to go, e.g. after Quit in its menu; where the Guest shows no Tray icons at all, it is never met, `gone=True` or not, and the result's `"detail"` says why).
- `gone=True` waits for any condition but a Notification to stop holding: the element or process gone, the file removed, no matching log line, the command failing.
- A poll that gets no answer from the Guest (a failed Channel, a hung call) counts as "not met yet", with `gone=True` too; `"error"` says why the last one failed.
- A command the Guest does not have (exit 127, 9009 on Windows, or PowerShell's CommandNotFoundException) raises at once, so a typo in `exec=` does not wait out the timeout. To wait for a command an install is still putting in place, wait for its file (`file=`) first.
- `process=` matches a process's name exactly, as text. Linux keeps only the first 15 bytes of a longer name; vmlab matches those, then the full name in the process's command line.
- To show that nothing happens for N seconds, wait for it with `timeout=N` and check `"met"` is false.
- App names (`app=`, `--app`): macOS takes the app's name, executable name or bundle id (`com.apple.TextEdit`); Linux the AT-SPI application name or the process name (only its first 15 bytes); Windows the process name (`Notepad`, `explorer`), and the taskbar belongs to `explorer`. A match's `"app"` is the application node's `name` whichever you gave. `g.tray` takes the app's own name on every OS.

```python
def scenario(g):
    staged = g.stage_text("Ohm's law relates voltage", then="cmd+shift+space")
    g.check("hotkey went to the editor", staged["frontmost"] == staged["app"], detail=staged)
    palette = g.wait_for(text="Selection", app="MyApp", timeout=10)
    g.check("palette read the selection", palette["met"], detail=palette)
    g.close_staged(staged)

    g.press("cmd+q")
    closed = g.wait_for(process="MyApp", gone=True, timeout=10)
    g.check("the app quits", closed["met"], detail=closed)

    # A mock server the app talks to answers; the result shows what curl said if it never does.
    up = g.wait_for(exec=["curl", "-fsS", "http://127.0.0.1:8080/health"], pattern="ok", timeout=20)
    g.check("the mock server is up", up["met"], detail=up)
```

## Language-dependent apps

A Lab's Guest shows the Lab language (`[labs.NAME] language`, `en-US` by default): the OS's own menus, buttons, dialogs and formats come in it, and an app that follows the OS language shows its translated text. One Lab per language (`mac`, `mac-ru`) tests an app in several.

- **System element names are translated.** `g.find(text="Open")` finds nothing in another language. Match the app's own elements by role, or by a text the Scenario knows for that language; find the names of system elements (a menu, a file dialog's buttons) in the Guest itself with `vmlab ui find` or `vmlab ui tree` on that Lab, never from memory.
- **Test data and Checks name the expected translation.** A Check that the app shows its settings title in the Lab language names that title as the app's translation files have it, and dates, numbers and currencies as the Lab's regional formats write them.
- **One Scenario, several language Labs.** Keep the expected text in a table keyed by `g.language`, and branch on it only where the texts differ; `VMLAB_LANGUAGE` gives the app's recipes the same tag.

```python
TITLE = {"en-US": "Settings", "de-DE": "Einstellungen"}

def scenario(g):
    title = TITLE[g.language]
    shown = g.wait_for(text=title, app="MyApp", timeout=10)
    g.check("the settings window is titled in the Lab language", shown["met"], detail=shown)
```

- Typing is unchanged: `g.type` types any text in every Lab language, and `g.press` presses keys of the US keyboard layout, which stays active (on Windows the Lab language's keyboard is installed next to it).
- On Windows, some inbox apps and system names can stay English after vmlab installs a language pack: prefer the app's own text, and look names up with `vmlab ui find` on that Lab.

## Traps on every OS

- **Prove the precondition.** An empty read cannot tell "the app is wrong" from "the window is not up yet": every read-based Check first `wait_for`s the window or element it reads, and fails with that as its reason when it never comes.
- **Nonces.** Test data carries a value unique to the Run (`uuid.uuid4().hex[:8]`), so leftovers of an earlier Run (a log line, a notification, a file) never pass a Check. Patterns and `text` are searched for, not matched whole: give each expectation its own nonce, or text no other one contains (`"seen " + nonce` is found in `"unseen " + nonce`).
- **Match the text as drawn.** `text` is case-sensitive, and web views (WebKit on macOS and Linux, WebView2) put a label into the tree as it is drawn: a heading written `Providers` and styled `text-transform: uppercase` is `PROVIDERS` there. Look with `vmlab ui find` or `ui tree` before writing the match.
- **Argv, not shell strings.** `g.exec` takes a list; each shell a string passes through parses it again (Host, Guest shell, `osascript` or PowerShell). Put longer test data in a Guest file with `g.put` and pass its path; `g.exec` takes no stdin.
- **Look before concluding.** Input that does nothing usually means something else holds the screen (a consent prompt, a dialog): `g.screenshot` and look.

Per OS: [macOS](traps-macos.md), [Windows](traps-windows.md), [Linux](traps-linux.md).
