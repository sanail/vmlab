# Scenario API

A Scenario is a Python file defining `scenario(g)`. Before it runs, vmlab quits the app, removes the Lab's `app.state` paths and launches the app (the Lab's `[labs.LAB.app]` recipes).

```python
FRESH = True     # optional: restore Clean state before this Scenario
LAUNCH = False   # optional: skip the launch; call g.launch(env={...}) yourself
TIMEOUT = 120    # optional: seconds for the whole Scenario (default: the Lab's scenario_timeout)

def scenario(g):
    r = g.exec(["cat", "/tmp/myapp.log"])      # r.code, r.stdout, r.stderr, r.ok, r.channel; timeout=, env=
    g.check("the app logged its start", "started" in r.stdout, detail=r.stdout[-500:])
    g.screenshot("after start")                 # saved in the Run folder as evidence
    g.check("the icon looks right", True, visual=True)  # a judgement from a screenshot: "visual, unverified"
    # g.lab, g.os ("macos" | "windows" | "linux"), g.arch (the Build artifact's), g.guest_arch
```

`g.check(name, passed, detail=None, visual=False)` records a Check; a failed Check does not stop the Scenario, and a Scenario records at least one. An exception, a timeout or a call no Channel can carry fails the Run with the Scenario's file and line.

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
| `g.clipboard()`, `g.set_clipboard(text)` | `ui clipboard [--set TEXT]` | `{"text"}` |
| `g.stage_text(text, app=None, then=None)` | `ui stage-text TEXT [--app] [--then CHORD]` | `{"app", "file", "frontmost", "selected", "pressed"}` |
| `g.wait_for(text=, role=, app=, gone=, process=, file=, log=, pattern=, timeout=None)` | `ui wait-for` | `{"met", "waited_s", "condition"[, "matches"]}` |
| `g.screenshot(name)` | `ui screenshot` | `{"path"}` |

- A node has `role` (cross-OS: `application`, `window`, `button`, `textfield`, `textarea`, `text`, `checkbox`, `menuitem`, ...), `name`, `value`, `description`, `bounds` (`{"x", "y", "w", "h"}` or null), `focused`, `enabled`, `native_role` and `children`. Applications carry `pid`.
- `text` matches name, value and description: exact matches win, otherwise substrings. `role` takes the cross-OS or the native role.
- `click` refuses when something else lies over the element's middle, and says what.
- Chords: `+`-joined modifiers (`ctrl`, `alt`/`option`, `shift`, `cmd`/`win`/`super`) and one key (`a`-`z`, `0`-`9`, `f1`-`f12`, `space`, `enter`, `tab`, `escape`, `backspace`, arrows, `minus`, `comma`, `slash`, ...). They are sent by physical key, so they work on any keyboard layout; `type` sends Unicode.
- `wait_for` takes exactly one condition and returns `"met": false` on timeout rather than raising: check it.
- App names: macOS and Linux use the application's name; Windows uses the process name (`Notepad`, `explorer`), and the taskbar and tray belong to `explorer`.

```python
def scenario(g):
    staged = g.stage_text("Ohm's law relates voltage", then="cmd+shift+space")
    g.check("hotkey went to the editor", staged["frontmost"] == staged["app"], detail=staged)
    palette = g.wait_for(text="Selection", app="MyApp", timeout=10)
    g.check("palette read the selection", palette["met"], detail=palette)
```
