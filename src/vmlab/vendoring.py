"""`vmlab init` and `vmlab self-update`: the project's pinned copy of the CLI (ADR 0002).

The vendored copy is .vmlab/vmlab.pyz; the zipapp itself carries its version,
read by running `<pyz> version`. vmlab never rewrites an existing config.
"""

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from vmlab import __version__
from vmlab.config import CONFIG_DIR, CONFIG_NAME, ConfigError

PYZ_NAME = "vmlab.pyz"
RUNS_IGNORE = "runs/"
RUN_NAME = "run"
# Where agents install the vmlab skill; the zipapp ships as scripts/vmlab.pyz inside it.
PROJECT_SKILL_DIRS = tuple(
    "%s/skills/vmlab" % d for d in (".claude", ".cursor", ".agents", ".codex", ".opencode", ".kilo")
)
HOME_SKILL_DIRS = PROJECT_SKILL_DIRS + (".config/opencode/skills/vmlab",)  # OpenCode's own home folder

RUN_SCRIPT = """\
#!/bin/sh
# The Regression suite: every saved Scenario on every Lab, or what the arguments name.
# Takes what `vmlab run` takes (see `--help`). Created by `vmlab init`.
exec python3 "$(dirname "$0")/vmlab.pyz" run "$@"
"""

TEMPLATE = """\
# vmlab project config. A Lab describes one Guest (a virtual machine running one
# OS) that the Scenarios in scenarios/*.py run on. vmlab never rewrites this file.
#
# Declare one table per Lab. Uncomment and adapt, e.g.:
#
# [labs.mac]
# provider = "tart"        # tart: macOS on Apple Silicon; fusion: Linux and Windows (VMware Fusion); fake: no hypervisor (utm, parallels: stubs)
# os = "macos"             # macos | windows | linux
# arch = "arm64"           # arm64 | x86_64; defaults to the Host's
# memory_gb = 4            # Host RAM the Guest takes; `run --parallel` queues Labs that don't fit
# boot_timeout = 300       # seconds from power-on until the Guest must be reachable
# step_timeout = 60        # default seconds per Guest call
# scenario_timeout = 600   # default seconds per Scenario
#
# [labs.mac.app]           # the application under test; every key is optional
# artifact = "dist/MyApp.app"          # Build artifact on the Host (glob: newest match), relative to the project root
# build = "npm run build"              # Host command run in the project root when the artifact is stale
# inputs = ["src", "package.json"]     # the artifact is stale when older than any of these
# install = 'cp -R "$VMLAB_ARTIFACT" /Applications/'   # Guest shell; $VMLAB_ARTIFACT is the delivered copy
# quit = "pkill -x MyApp"              # before every Run (exit code ignored)
# launch = "open -a MyApp"             # before every Run, after the state paths are removed
# env = { RUST_LOG = "debug" }         # extra environment for install, quit and launch
# state = ["~/Library/Application Support/MyApp"]  # Guest paths removed before every Run
#
# [labs.mac.tart]          # options of the Lab's Provider
# base = "macos-tahoe"     # Base guest to clone; create it once with `vmlab base create macos-tahoe`
# cpu = 4
# display = "1920x1080"
#
# [labs.linux]
# provider = "fusion"
# os = "linux"
#
# [labs.linux.fusion]
# base = "ubuntu-26.04"    # Base guest to clone; create it once with `vmlab base create ubuntu-26.04`
# cpu = 4
# session = "wayland"      # GNOME on Wayland; "x11": Xfce on X11
#
# [labs.win]
# provider = "fusion"
# os = "windows"
#
# [labs.win.fusion]
# base = "windows-11"      # Base guest to copy; create it once with `vmlab base create windows-11`
# cpu = 4
"""


def init(root, out):
    """Create or complete root/.vmlab. Never overwrites existing files."""
    vmlab_dir = Path(root) / CONFIG_DIR
    (vmlab_dir / "scenarios").mkdir(parents=True, exist_ok=True)

    config_path = vmlab_dir / CONFIG_NAME
    if config_path.exists():
        out("kept      %s" % config_path)
    else:
        config_path.write_text(TEMPLATE, encoding="utf-8")
        out("created   %s" % config_path)

    gitignore = vmlab_dir / ".gitignore"
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    if RUNS_IGNORE not in lines:
        gitignore.write_text("\n".join(lines + [RUNS_IGNORE]) + "\n", encoding="utf-8")
        out("ignored   %s (in %s)" % (RUNS_IGNORE, gitignore))

    run = vmlab_dir / RUN_NAME
    if run.exists():
        out("kept      %s" % run)
    else:
        run.write_text(RUN_SCRIPT, encoding="utf-8")
        run.chmod(0o755)
        out("created   %s" % run)

    pyz = vmlab_dir / PYZ_NAME
    if pyz.exists():
        out("kept      %s (%s); move it to another version with `vmlab self-update`" % (pyz, _version_of(pyz)))
    else:
        _install(_running_zipapp(), pyz)
        out("vendored  %s (vmlab %s)" % (pyz, __version__))


def self_update(start, source, out):
    """Replace the project's vendored copy with source, or with the newest skill copy found."""
    vmlab_dir = _find_vmlab_dir(start)
    pyz = vmlab_dir / PYZ_NAME
    source = Path(source).resolve() if source else _default_source(pyz, vmlab_dir.parent)

    old = _version_of(pyz) if pyz.exists() else None
    new = _version_of(source)
    if old and _parse(new) < _parse(old):
        raise ConfigError(
            source,
            None,
            "is vmlab %s, older than the vendored %s" % (new, old),
            "pass --from with a vmlab.pyz of %s or newer" % old,
        )
    if source != pyz.resolve():
        _install(source, pyz)
    out("%s: %s -> %s (from %s)" % (pyz, old or "none", new, source))


def _install(source, pyz):
    """Copy then rename, so a copy that is running (updating itself) never sees a half-written file."""
    tmp = pyz.with_name(pyz.name + ".tmp")
    shutil.copyfile(str(source), str(tmp))
    tmp.chmod(0o755)
    os.replace(str(tmp), str(pyz))


def _find_vmlab_dir(start):
    start = Path(start).resolve()
    for directory in (start,) + tuple(start.parents):
        candidate = directory / CONFIG_DIR
        if (candidate / CONFIG_NAME).is_file() or (candidate / PYZ_NAME).is_file():
            return candidate
    raise ConfigError(start / CONFIG_DIR, None, "not a vmlab project", "run `vmlab init` in the project root first")


def _default_source(pyz, project_root):
    running = _running_zipapp(required=False)
    if running and (not pyz.exists() or running != pyz.resolve()):
        return running
    candidates = [project_root / d / "scripts" / PYZ_NAME for d in PROJECT_SKILL_DIRS]
    candidates += [Path.home() / d / "scripts" / PYZ_NAME for d in HOME_SKILL_DIRS]
    found = [c for c in candidates if c.is_file()]
    if not found:
        raise ConfigError(
            pyz,
            None,
            "no vmlab skill copy found to update from; looked in:\n    " + "\n    ".join(map(str, candidates)),
            "install or update the vmlab skill, or pass --from PATH/TO/vmlab.pyz",
        )
    return max(found, key=lambda p: _parse(_version_of(p)))


def _running_zipapp(required=True):
    path = Path(sys.argv[0]).resolve()
    if path.is_file() and zipfile.is_zipfile(str(path)):
        return path
    if required:
        raise ConfigError(
            path,
            None,
            "vmlab is not running from a zipapp, so there is nothing to vendor",
            "build one with `python3 tools/build.py` and run dist/vmlab.pyz",
        )
    return None


def _version_of(pyz):
    proc = subprocess.run([sys.executable, str(pyz), "version"], capture_output=True, text=True, timeout=60)
    match = re.match(r"vmlab (\d+(?:\.\d+)*)\s*$", proc.stdout)
    if proc.returncode != 0 or not match:
        raise ConfigError(pyz, None, "is not a working vmlab zipapp", "replace it with a vmlab.pyz from the skill")
    return match.group(1)


def _parse(version):
    return tuple(int(n) for n in version.split("."))
