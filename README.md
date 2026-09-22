# vmlab

An agent skill plus a host CLI for testing desktop applications inside macOS, Windows and Linux **Guests**. Vocabulary: `CONTEXT.md`. Design: `docs/spec/0001-vmlab.md` and `docs/adr/`.

Status: CLI core, the Fake Provider, the Tart Provider (macOS Guests) and the UI contract on macOS. VMware Fusion is planned; UTM and Parallels are stubs. To add a hypervisor, see `docs/adding-a-provider.md`.

## Build and test

Python 3.9+ and the standard library only.

```sh
python3 tools/build.py                   # -> dist/vmlab.pyz
python3 -m unittest discover -s tests    # Seam 1: drives the built zipapp as a subprocess
# Seam 2, the Provider/Channel contract against a real Lab (docs/adding-a-provider.md):
VMLAB_CONTRACT_LAB_FILE=my-lab.toml VMLAB_CONTRACT_LAB=mac python3 -m unittest discover -s tests -p 'test_contract.py' -v
```

## Project layout

`vmlab init` creates it (idempotent; an existing config is never touched):

```
.vmlab/
  vmlab.toml        # Labs; a commented template to start from
  vmlab.pyz         # the pinned CLI (ADR 0002): run it as `python3 .vmlab/vmlab.pyz ...`
  scenarios/*.py    # Scenarios
  .gitignore        # ignores runs/
  runs/             # per invocation and Lab: <UTC timestamp>-<lab>/ with report.json, junit.xml, summary.md, screenshots/
```

`vmlab self-update [--from PYZ]` replaces the vendored copy and prints `old -> new`. Run from a skill copy, it vendors itself; run from the vendored copy, it picks the newest `<skill>/scripts/vmlab.pyz` among `.claude`, `.cursor`, `.agents` and `.codex` skill folders in the project and in `~`. It refuses to downgrade.

```toml
[labs.mac]
provider = "fake"        # fake | tart (macOS) (fusion: planned; utm, parallels: stubs)
os = "macos"             # macos | windows | linux
arch = "arm64"           # arm64 | x86_64; defaults to the Host's
memory_gb = 4            # Host RAM the Guest takes; used by --parallel
boot_timeout = 300       # seconds from power-on until the Guest must be reachable
step_timeout = 60        # default seconds per Guest call
scenario_timeout = 600   # default seconds per Scenario

[labs.mac.app]                                   # all optional; see "Deploy"
artifact = "target/release/bundle/macos/MyApp.app"  # Host path or glob (newest match), relative to the project root
build = "npm run tauri build"                    # Host command, run in the project root when the artifact is stale
inputs = ["src", "src-tauri", "package.json"]    # stale = missing, or older than any of these
build_timeout = 1800
install = "rm -rf /Applications/MyApp.app && cp -R \"$VMLAB_ARTIFACT\" /Applications/"
install_timeout = 600
quit = "pkill -x MyApp"                          # exit code ignored
launch = "open -a /Applications/MyApp.app"
env = { RUST_LOG = "debug" }                     # for install, quit and launch
state = ["~/Library/Application Support/MyApp"]  # Guest paths removed before every Run

[labs.mac.fake]
ui_tree = "tree.json"        # optional scripted UI tree, relative to .vmlab/
channels = ["ssh", "exec"]   # Channel names, preferred first
broken_channels = []         # fault injection: these Channels fail every call
hung_channels = []           # fault injection: these Channels hang until the call times out
boot_seconds = 0             # fault injection: a slow boot
```

## macOS Guests with Tart

Needs an Apple Silicon Mac with [Tart](https://tart.run) (`brew install cirruslabs/cli/tart`). A **Base guest** is created once per Host and shared by all projects; each Lab runs in its own APFS clone of it, which costs almost no disk:

```sh
vmlab base create macos-tahoe   # asks before downloading the image (tens of GB); --yes to allow it; idempotent
vmlab base list
```

`base create` clones a cirruslabs `*-base` image and provisions it: vmlab's SSH key, Remote Login, no sleep or screen saver, window and app restore off, the UI helper, TCC grants (Accessibility, Screen Recording, Input Monitoring, Apple Events to System Events and Finder) and Screen Recording approvals for both Channels. It then reboots the Guest from outside, checks `kern.boottime` changed, and proves each Channel reaches System Events and holds the UI helper's Accessibility grant. `--reprovision` runs it again; so does a newer vmlab whose provisioning changed.

**Guest-only security trade-off.** Granting automation permissions without MDM means writing TCC.db directly, which needs SIP off. The cirruslabs `*-base` images ship with SIP off. This affects only the throwaway Guest, never the Host.

```toml
[labs.mac]
provider = "tart"
os = "macos"
memory_gb = 4

[labs.mac.tart]
base = "macos-tahoe"         # the Base guest to clone
cpu = 4
display = "1920x1080"        # fixed, so coordinates stay stable between Runs
channels = ["ssh", "exec"]   # SSH (multiplexed), then `tart exec` (the Tart guest agent)
```

- Guests run headless, with no shared clipboard (it would overwrite what you copied) and no audio (a Guest's audio device can grab your Bluetooth headset).
- Clean state = delete the clone and clone the Base guest again.
- SSH uses vmlab's own key and known_hosts in `$VMLAB_HOME/ssh/`. The Guest's host key is pinned at provisioning under the Base guest's name, never its IP, so reused DHCP addresses can't break or confuse it. Your `~/.ssh` is never read or written.
- Screenshots are taken in the Guest with `screencapture`, because Tart has no Host-side screenshot.
- The screen is `display` in points (Retina, 2x pixels). macOS would otherwise keep the display mode it last used whenever the display still offers it (the image pins 1024x768), so vmlab clears the saved mode each time it stops a Guest, and every boot fits the Lab's `display`.
- macOS runs at most two macOS VMs at once, so `--parallel` can't run a third.
- `VMLAB_TART` overrides the `tart` binary.

Provisioning also readies the desktop for UI work: loginwindow no longer relaunches the apps that ran at power-off (the image ships with Terminal), apps don't rewrite typed text (automatic capitalisation, spelling correction and smart punctuation are off), and the UI helper `vmlab-ui` (Swift: Accessibility + CGEvent) is compiled into `/usr/local/vmlab/bin`. Nothing is compiled at first use. Where the helper is missing (a Base guest provisioned by an older vmlab, or no Command Line Tools), UI commands fall back to a JXA script, which is slower, types through System Events (so it follows the Guest's keyboard layout), does not check what lies under a click, and does not wake lazy WebKit or Electron trees; `vmlab doctor` warns about it. Input events are paced a few milliseconds apart so the window server keeps their order; that is pacing, not waiting, which is always condition-plus-timeout.

macOS traps the helper handles, so Scenarios don't have to:

- **WebKit's accessibility tree is lazy** and goes to the *next* client that connects: a cold first read of a WebKit app (Tauri, Safari, WKWebView) shows no page content at all. The first time the helper meets an app process it reads it once to wake it, then reads again from a fresh process; after that even new windows come through on the first read.
- **AXManualAccessibility** turns the tree on in Chromium and Electron apps. WebKit rejects it (error -25205), so it is set best effort and the wake-up read above is what makes WebKit work.
- **Keyboard layouts**: chords are sent by physical key code (`cmd+c` copies on a Russian layout too) and text is typed as Unicode, independent of the layout.
- **Focus stealing**: staging text in another app and pressing the app's hotkey happen in one Guest call (`stage-text --then`), and the result records which app was frontmost when the chord went out.
- **Screen Recording consent**: even with the TCC grant, macOS 15+ asks again every 30 days ("... is requesting to bypass the system private window picker") when a client captures the screen without the picker, and the alert covers the app under test. Provisioning dates the next alert for vmlab's Channel clients to 2100 (`ScreenCaptureApprovals.plist` in the Guest), part of the same Guest-only trade-off as the TCC grants.
- **Covered elements**: an element under the Dock, another window or scrolled out of view still has bounds. `click` asks Accessibility what lies under the point first and refuses with what it found there.

A Scenario is a Python file defining `scenario(g)`:

```python
FRESH = True     # optional: restore Clean state before this Scenario
LAUNCH = False   # optional: don't launch the app before this Scenario; call g.launch(env={...}) yourself
TIMEOUT = 120    # optional: seconds for the whole Scenario (default: the Lab's scenario_timeout)

def scenario(g):
    r = g.exec(["echo", "hello"])          # r.code, r.stdout, r.stderr, r.ok, r.channel; timeout=, env=
    g.check("echo prints hello", r.stdout.strip() == "hello", detail=r.stderr)
    g.screenshot("after echo")             # saved in the Run folder as evidence
    g.check("icon looks right", True, visual=True)  # a judgement from a screenshot: reported "visual, unverified"
```

## UI contract

Scenarios and the agent read and drive the Guest's UI with the same commands and JSON on every OS (macOS now; Linux and Windows next). `vmlab ui COMMAND [--lab LAB]` prints JSON; each Scenario method returns the same object:

| CLI | Scenario | Result |
| --- | --- | --- |
| `ui tree [--app APP]` | `g.tree(app=None)` | the node tree below |
| `ui find [--text T] [--role R] [--app APP]` | `g.find(text=, role=, app=)` | `{"matches": [node without children, plus "app"]}` |
| `ui click [--text/--role/--app] [--index N]`, `ui click --at X Y` | `g.click(..., index=0)`, `g.click(at=(x, y))` | `{"x", "y", "element", "under"}` |
| `ui press CHORD` | `g.press("cmd+shift+space")` | `{"chord": "shift+cmd+space"}` |
| `ui type TEXT` | `g.type(text)` | `{"typed": n}` |
| `ui focus --app APP [--window TITLE]` | `g.focus(app, window=None)` | `{"app", "window", "frontmost"}` |
| `ui clipboard [--set TEXT]` | `g.clipboard()`, `g.set_clipboard(text)` | `{"text"}` |
| `ui stage-text TEXT [--app APP] [--then CHORD]` | `g.stage_text(text, app=None, then=None)` | `{"app", "file", "frontmost", "selected", "pressed"}` |
| `ui wait-for CONDITION [--timeout S]` | `g.wait_for(..., timeout=None)` | `{"met", "waited_s", "condition"[, "matches"]}` |
| `ui screenshot [--out PATH]` | `g.screenshot(name)` | `{"path"}` (Scenarios: the path in the Run folder) |

Every node has `role` (cross-OS: `application`, `window`, `button`, `textfield`, `textarea`, `text`, `checkbox`, `menuitem`, ...), `name`, `value`, `description`, `bounds` (`{"x", "y", "w", "h"}` in screen points, or null), `focused`, `enabled`, `native_role` (e.g. `AXButton`) and `children`. The root is the `desktop`, with `truncated` true when a size limit cut the tree short; its children are applications (with `pid`), holding their windows and tray items.

- `find` matches `--text` against name, value and description: exact matches win, otherwise substrings. `--role` takes the cross-OS or the native role.
- `click` clicks the middle of the first match (or the `--index`th) with a real mouse event, after checking the element is what lies under that point.
- Chords are `+`-joined modifiers (`ctrl`, `alt`/`option`, `shift`, `cmd`/`command`/`win`/`super`) and one key: `a`-`z`, `0`-`9`, `f1`-`f12`, `space`, `enter`, `tab`, `escape`, `backspace`, `delete`, arrows, `home`, `end`, `pageup`, `pagedown` and punctuation names (`minus`, `comma`, `slash`, ...). An unknown key is a usage error (exit 2).
- `focus` brings a running app to the front and waits until it is frontmost; `--window` first raises its first window whose title contains TITLE. An app that is not running, or a window that is not there, is an error naming what is.
- `stage-text` opens the text in a third-party editor (default: TextEdit, Notepad or gedit), selects it all, and presses `--then` in the same Guest call, so nothing can steal focus in between.
- `wait-for` takes exactly one condition: an element (`--text`/`--role`/`--app`, or `--gone` for its disappearance), `--process NAME`, `--file PATH` or `--log PATH --pattern REGEX`. It polls until the condition holds or the timeout (default: the Lab's `step_timeout`) passes, never with fixed sleeps. Unmet, the CLI exits 1 and a Scenario gets `"met": false` to check.
- UI commands need a running Guest (`vmlab up` or `vmlab deploy`). In a Scenario they count against its timeout like `g.exec`.

```python
def scenario(g):
    staged = g.stage_text("Ohm's law relates voltage", then="cmd+shift+space")
    g.check("hotkey went to the editor", staged["frontmost"] == staged["app"], detail=staged)
    palette = g.wait_for(text="Selection", app="MyApp", timeout=10)
    g.check("palette read the selection", palette["met"], detail=palette)
```

Commands reach the Guest over its first working Channel (ADR 0003). When a Channel fails, the call falls back to the next Channel and the report notes it. A non-zero exit code is returned to the Scenario and never triggers a fallback. A call that exceeds its timeout is killed and fails the Run with the Scenario file and line; so does a call no Channel can carry.

## Deploy

Before Labs start, each Lab's build hook runs on the Host (with `VMLAB_LAB`, `VMLAB_OS`, `VMLAB_ARCH`) if its Build artifact is missing or older than one of its `inputs`; Labs sharing an artifact build it once. Its output lands in the Run folder as `build.log`. The artifact is then copied into a uniquely named Guest folder under `~/vmlab/artifacts/` and `install` runs. That happens once per suite, and again after any restore. Before every Run, `quit` runs, the `state` paths are removed and `launch` runs. Guest recipes run in `sh` (PowerShell on Windows) with `env` plus `VMLAB_LAB`, `VMLAB_OS`, `VMLAB_ARCH` and `VMLAB_ARTIFACT`, the Guest path of the delivered copy.

`vmlab deploy [LAB...]` does the same without Scenarios and leaves the Guests running, for exploring by hand.

## Lifecycle

- A **Regression suite** (`vmlab run` with saved Scenario names, or none for all) restores Clean state once per Lab at its start, and before each Scenario that declares `FRESH = True`.
- An **Ad-hoc run** (`vmlab run path/to/scenario.py` outside `scenarios/`) keeps Guest state for fast iteration and leaves its Guests running, printing the stop command.
- `--fresh` restores before every Scenario; `--keep` leaves Guests running.
- Before every Run, the Lab's `app.state` paths are removed.
- Labs run one after another. `--parallel` runs them concurrently, starting a Lab only while its `memory_gb` fits in free Host memory (free + inactive pages; override with `VMLAB_FREE_MEMORY_GB`) and queueing the rest. A Guest that is already running needs no memory, and a Lab larger than all free memory runs alone. Each Lab keeps its own Run folder and reports.
- vmlab stops only Guests it started (recorded in `$VMLAB_HOME/started.json`), including ones an earlier Ad-hoc or `--keep` run left running. A Guest started outside vmlab, or with `vmlab up`, is never stopped by `vmlab run`.

## CLI

```
vmlab init | vmlab self-update [--from PYZ]
vmlab base create NAME [--image IMAGE] [--yes] [--reprovision] | vmlab base list
vmlab run [SCENARIO|FILE...] [--lab LAB]... [--keep] [--fresh] [--parallel]
                                         # exit 0 all passed, 1 a Check failed or a Run errored, 2 usage/config error
vmlab deploy [LAB...]                    # build if stale, install, launch; Guests stay running
vmlab up [LAB...] | vmlab down [LAB...]  # default: all Labs
vmlab status [--json]
vmlab doctor [LAB...] [--json]           # Provider, Guest, per-Channel and UI helper checks with fixes; exit 1 on FAIL
vmlab ui tree|find|click|press|type|focus|clipboard|stage-text|wait-for|screenshot [--lab LAB] ...  # JSON; see "UI contract"
vmlab version
```

`VMLAB_HOME` (default `~/.vmlab`) holds host state: the Base guest registry (`bases.json`), vmlab's SSH key and known_hosts (`ssh/`), `tart run` logs (`tart/`), and the Fake Provider's Guests (`fake/`, with the Guest user's home at `fs/home`).
