# vmlab

An agent skill plus a host CLI for testing desktop applications inside macOS, Windows and Linux **Guests**. Vocabulary: `CONTEXT.md`. Design: `docs/spec/0001-vmlab.md` and `docs/adr/`.

Status: walking skeleton. Only the Fake Provider exists.

## Build and test

Python 3.9+ and the standard library only.

```sh
python3 tools/build.py                   # -> dist/vmlab.pyz
python3 -m unittest discover -s tests    # Seam 1: drives the built zipapp as a subprocess
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
provider = "fake"        # fake (tart, fusion: planned)
os = "macos"             # macos | windows | linux
arch = "arm64"           # arm64 | x86_64; defaults to the Host's
boot_timeout = 300       # seconds from power-on until the Guest must be reachable
step_timeout = 60        # default seconds per Guest call
scenario_timeout = 600   # default seconds per Scenario

[labs.mac.app]
state = ["~/Library/Application Support/MyApp"]  # Guest paths removed before every Run

[labs.mac.fake]
ui_tree = "tree.json"        # optional scripted UI tree, relative to .vmlab/
channels = ["ssh", "exec"]   # Channel names, preferred first
broken_channels = []         # fault injection: these Channels fail every call
hung_channels = []           # fault injection: these Channels hang until the call times out
boot_seconds = 0             # fault injection: a slow boot
```

A Scenario is a Python file defining `scenario(g)`:

```python
FRESH = True     # optional: restore Clean state before this Scenario
TIMEOUT = 120    # optional: seconds for the whole Scenario (default: the Lab's scenario_timeout)

def scenario(g):
    r = g.exec(["echo", "hello"])          # r.code, r.stdout, r.stderr, r.ok, r.channel; timeout=, env=
    g.check("echo prints hello", r.stdout.strip() == "hello", detail=r.stderr)
    g.screenshot("after echo")             # saved in the Run folder as evidence
    g.check("icon looks right", True, visual=True)  # a judgement from a screenshot: reported "visual, unverified"
```

Commands reach the Guest over its first working Channel (ADR 0003). When a Channel fails, the call falls back to the next Channel and the report notes it. A non-zero exit code is returned to the Scenario and never triggers a fallback. A call that exceeds its timeout is killed and fails the Run with the Scenario file and line; so does a call no Channel can carry.

## Lifecycle

- A **Regression suite** (`vmlab run` with saved Scenario names, or none for all) restores Clean state once per Lab at its start, and before each Scenario that declares `FRESH = True`.
- An **Ad-hoc run** (`vmlab run path/to/scenario.py` outside `scenarios/`) keeps Guest state for fast iteration and leaves its Guests running, printing the stop command.
- `--fresh` restores before every Scenario; `--keep` leaves Guests running.
- Before every Run, the Lab's `app.state` paths are removed.
- vmlab stops only Guests it started (recorded in `$VMLAB_HOME/started.json`), including ones an earlier Ad-hoc or `--keep` run left running. A Guest started outside vmlab, or with `vmlab up`, is never stopped by `vmlab run`.

## CLI

```
vmlab init | vmlab self-update [--from PYZ]
vmlab run [SCENARIO|FILE...] [--lab LAB]... [--keep] [--fresh]
                                         # exit 0 all passed, 1 a Check failed or a Run errored, 2 usage/config error
vmlab up [LAB...] | vmlab down [LAB...]  # default: all Labs
vmlab status [--json]
vmlab doctor [LAB...] [--json]           # Provider, Guest and per-Channel checks with fixes; exit 1 on FAIL
vmlab version
```

`VMLAB_HOME` (default `~/.vmlab`) holds host state; the Fake Provider keeps its Guests under `$VMLAB_HOME/fake/`, with the Guest user's home at `fs/home`.
