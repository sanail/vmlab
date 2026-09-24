# Scenario API

A Scenario is a Python file defining `scenario(g)`. Before it runs, vmlab quits the app, removes the Lab's `app.state` paths and launches the app (the Lab's `[labs.LAB.app]` recipes), waiting for its `ready` condition if the Lab has one; that wait is not on the Scenario's clock.

```python
FRESH = True     # optional: restore Clean state before this Scenario
LAUNCH = False   # optional: skip the launch; call g.launch(env={...}) yourself: it waits for app.ready too, on the Scenario's clock
TIMEOUT = 120    # optional: seconds for the whole Scenario (default: the Lab's scenario_timeout)

def scenario(g):
    r = g.exec(["cat", "/tmp/myapp.log"])      # r.code, r.stdout, r.stderr, r.ok, r.channel; timeout=, env=
    g.check("the app logged its start", "started" in r.stdout, detail=r.stdout[-500:])
    path = g.put("~/Documents/input.txt", "Ohm's law\n")  # str (UTF-8) or bytes; folders made; returns the absolute Guest path
    settings = g.get("~/.config/myapp/settings.json")   # str; g.get(path, binary=True) for bytes
    mock = g.spawn(["python3", "-m", "http.server", "8080"])  # detached; stopped with the Run (see below)
    g.screenshot("after start")                 # saved in the Run folder as evidence
    g.check("the icon looks right", True, visual=True)  # a judgement from a screenshot: "visual, unverified"
    # g.lab, g.os ("macos" | "windows" | "linux"), g.arch (the Build artifact's), g.guest_arch
```

`g.put(guest_path, content)` writes a file into the Guest and `g.get(guest_path, binary=False)` reads one back, with no quoting or shell in between: `~` alone or before a slash is the Guest user's home (`~name` is just a name), and on Windows `%VARS%` expand (`g.put(r"%TEMP%\input.txt", data)`). Spaces and non-ASCII names need nothing special. `put` replaces the file at the path: a symlink there is replaced by the file, and what it pointed to is left alone. `get` raises when the file is not there; both count against the Scenario's timeout, each call (the whole copy) against the Lab's `step_timeout`. Files stay after the Run, so "put a file, restart the app, it reads it" works; `app.state`, `FRESH` and nonces keep leftovers out of later Runs. The CLI has the same: `vmlab put GUEST_PATH [--from HOSTFILE]` (piped stdin when no `--from`, and an empty pipe makes an empty file; prints the Guest path) and `vmlab get GUEST_PATH` (to stdout).

`g.check(name, passed, detail=None, visual=False)` records a Check; a failed Check does not stop the Scenario, and a Scenario records at least one. An exception, a timeout or a call no Channel can carry fails the Run with the Scenario's file and line.

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
- `g.spawn` raises when the Guest has no such command; a command that starts and then fails shows why in its output. Log files stay in the Guest's temp folder after the Run.
- A process vmlab cannot stop at the end of the Run does not change the Run's result: the report notes it (`"ended": "failed"`) and vmlab prints a warning.

## UI methods

Each mirrors a `vmlab ui` command and returns the same JSON as a dict.

| Scenario | CLI | Result |
| --- | --- | --- |
| `g.tree(app=None)` | `ui tree [--app APP]` | node tree |
| `g.find(text=, role=, app=)` | `ui find` | `{"matches": [node without children, plus "app"]}` |
| `g.click(text=, role=, app=, index=0)`, `g.click(at=(x, y))` | `ui click` | `{"x", "y", "element", "under"}` |
| `g.press("cmd+shift+space")` | `ui press CHORD` | `{"chord"}` |
| `g.type(text)` | `ui type TEXT` | `{"typed": n}` |
| `g.focus(app, window=None)` | `ui focus --app APP [--window T]` | `{"app", "window", "frontmost"}` |
| `g.clipboard()`, `g.set_clipboard(text)` | `ui clipboard [--set TEXT]` | `{"text"}`; `""` empties the clipboard |
| `g.stage_text(text, app=None, then=None)` | `ui stage-text TEXT [--app] [--then CHORD]` | `{"app", "file", "frontmost", "selected", "pressed"}` |
| `g.wait_for(text=, role=, app=, process=, file=, log=, exec=, pattern=, gone=False, timeout=None)` | `ui wait-for` | `{"met", "waited_s", "condition"[, "matches"][, "code", "stdout"][, "error"]}` |
| `g.screenshot(name)` | `ui screenshot` | `{"path"}` |

- A node has `role` (cross-OS: `application`, `window`, `button`, `textfield`, `textarea`, `text`, `checkbox`, `menuitem`, ...), `name`, `value`, `description`, `bounds` (`{"x", "y", "w", "h"}` or null), `focused`, `enabled`, `native_role` and `children`. Applications carry `pid`.
- `text` matches name, value and description: exact matches win, otherwise substrings. `role` takes the cross-OS or the native role.
- `click` refuses when something else lies over the element's middle, and says what.
- Chords: `+`-joined modifiers (`ctrl`, `alt`/`option`, `shift`, `cmd`/`win`/`super`) and one key (`a`-`z`, `0`-`9`, `f1`-`f12`, `space`, `enter`, `tab`, `escape`, `backspace`, arrows, `minus`, `comma`, `slash`, ...). They are sent by physical key, so they work on any keyboard layout; `type` sends Unicode.
- `wait_for` takes exactly one condition and returns `"met": false` on timeout rather than raising: check it. The conditions: an element (`text=`/`role=`, with `"matches"`; `app=` narrows it to one app's and goes with them only), a process by name (`process=`), a Guest path (`file=`), a line in a Guest file (`log=` with `pattern=`, a Python regex), or a command (`exec=[argv]`: it exits 0; with `pattern=`, its stdout matches, whatever the exit code; the result carries the last answer's `code` and the tail of its `stdout`).
- `gone=True` waits for any condition to stop holding: the element or process gone, the file removed, no matching log line, the command failing.
- A poll that gets no answer from the Guest (a failed Channel, a hung call) counts as "not met yet", with `gone=True` too; `"error"` says why the last one failed.
- A command the Guest does not have (exit 127, 9009 on Windows, or PowerShell's CommandNotFoundException) raises at once, so a typo in `exec=` does not wait out the timeout. To wait for a command an install is still putting in place, wait for its file (`file=`) first.
- `process=` matches a process's name exactly, as text. Linux keeps only the first 15 bytes of a longer name; vmlab matches those, then the full name in the process's command line.
- To show that nothing happens for N seconds, wait for it with `timeout=N` and check `"met"` is false.
- App names: macOS and Linux use the application's name; Windows uses the process name (`Notepad`, `explorer`), and the taskbar and tray belong to `explorer`.

```python
def scenario(g):
    staged = g.stage_text("Ohm's law relates voltage", then="cmd+shift+space")
    g.check("hotkey went to the editor", staged["frontmost"] == staged["app"], detail=staged)
    palette = g.wait_for(text="Selection", app="MyApp", timeout=10)
    g.check("palette read the selection", palette["met"], detail=palette)

    g.press("cmd+q")
    closed = g.wait_for(process="MyApp", gone=True, timeout=10)
    g.check("the app quits", closed["met"], detail=closed)

    # A mock server the app talks to answers; the result shows what curl said if it never does.
    up = g.wait_for(exec=["curl", "-fsS", "http://127.0.0.1:8080/health"], pattern="ok", timeout=20)
    g.check("the mock server is up", up["met"], detail=up)
```

## Traps on every OS

- **Prove the precondition.** An empty read cannot tell "the app is wrong" from "the window is not up yet": every read-based Check first `wait_for`s the window or element it reads, and fails with that as its reason when it never comes.
- **Nonces.** Test data carries a value unique to the Run (`uuid.uuid4().hex[:8]`), so leftovers of an earlier Run (a log line, a notification, a file) never pass a Check.
- **Argv, not shell strings.** `g.exec` takes a list; each shell a string passes through parses it again (Host, Guest shell, `osascript` or PowerShell). Put longer test data in a Guest file with `g.put` and pass its path; `g.exec` takes no stdin.
- **Look before concluding.** Input that does nothing usually means something else holds the screen (a consent prompt, a dialog): `g.screenshot` and look.

Per OS: [macOS](traps-macos.md), [Windows](traps-windows.md), [Linux](traps-linux.md).
