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
provider = "fake"     # fake (tart, fusion: planned)
os = "macos"          # macos | windows | linux
arch = "arm64"        # arm64 | x86_64; defaults to the Host's
boot_timeout = 300    # seconds from power-on until a Channel must work

[labs.mac.fake]
ui_tree = "tree.json"        # optional scripted UI tree, relative to .vmlab/
channels = ["ssh", "exec"]   # Channel names, preferred first
broken_channels = []         # fault injection: these Channels fail every call
hung_channels = []           # fault injection: these Channels hang until the call times out
boot_seconds = 0             # fault injection: a slow boot
```

A Scenario is a Python file defining `scenario(g)`:

```python
def scenario(g):
    r = g.exec(["echo", "hello"])          # r.code, r.stdout, r.stderr, r.ok, r.channel; timeout=60, env={}
    g.check("echo prints hello", r.stdout.strip() == "hello", detail=r.stderr)
    g.screenshot("after echo")             # saved in the Run folder as evidence
```

Commands reach the Guest over its first working Channel (ADR 0003). A Channel failure falls back to the next Channel and is noted in the report; a non-zero exit code is returned to the Scenario, and a call that exceeds its timeout is killed. Either of the last two failing Channels makes the Run an error that names the Scenario line.

## CLI

```
vmlab init | vmlab self-update [--from PYZ]
vmlab run [SCENARIO...] [--lab LAB]...   # starts Guests as needed and stops those it started; exit 0 all passed, 1 a Check failed or a Scenario errored, 2 usage/config error
vmlab up [LAB...] | vmlab down [LAB...]  # default: all Labs
vmlab status [--json]
vmlab doctor [LAB...] [--json]           # Provider, Guest and per-Channel checks with fixes; exit 1 on FAIL
vmlab version
```

`VMLAB_HOME` (default `~/.vmlab`) holds host state; the Fake Provider keeps its Guests under `$VMLAB_HOME/fake/`.
