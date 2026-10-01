"""`vmlab init` and `vmlab self-update`: the project's pinned copy of the CLI (ADR 0002, ADR 0004).

The pinned copy is .vmlab/vmlab.pyz, run through the .vmlab/vmlab launcher, which names its
version and sha256 and downloads that release when the project leaves the zipapp out of git.
The zipapp itself carries its version, read by running `<pyz> version`. vmlab never rewrites
an existing config.
"""

import hashlib
import os
import pkgutil
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from vmlab import __version__
from vmlab.config import CONFIG_DIR, CONFIG_NAME, ConfigError

PYZ_NAME = "vmlab.pyz"
LAUNCHER_NAME = "vmlab"
RUNS_IGNORE = "runs/"
RUN_NAME = "run"

RUN_SCRIPT = """\
#!/bin/sh
# The Regression suite: every saved Scenario on every Lab, or what the arguments name.
# Takes what `vmlab run` takes (see `--help`). Created by `vmlab init`.
exec "$(dirname "$0")/vmlab" run "$@"
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
# language = "en-US"       # the Lab language (ll-RR): system element names come in it; en-US by default
# memory_gb = 4            # Host RAM the Guest takes; `run --parallel` queues Labs that don't fit
# boot_timeout = 300       # seconds from power-on until the Guest must be reachable (the Host's sleep does not count)
# step_timeout = 60        # default seconds per Guest call
# scenario_timeout = 600   # default seconds per Scenario
#
# [labs.mac.app]           # the application under test; every key is optional
# artifact = "dist/MyApp.app"          # Build artifact on the Host (glob: newest match), relative to the project root
# build = "npm run build"              # Host command run in the project root when the artifact is stale
# inputs = ["src", "package.json"]     # the artifact is stale when older than any of these
# install = 'cp -R "$VMLAB_ARTIFACT" /Applications/'   # Guest shell; $VMLAB_ARTIFACT is the delivered copy
# quit = "pkill -x MyApp"              # before every Run (exit code ignored), and in g.quit()
# process = "MyApp"                    # the app's process: every quit waits for it to go (default: ready's process, if any)
# quit_timeout = 30                    # seconds to wait for it to go (default: step_timeout; needs a process)
# launch = "open -a MyApp"             # before every Run, after the state paths are removed
# ready = { process = "MyApp" }        # optional (needs launch): one wait_for condition that says the launched app is ready
# ready_timeout = 30                   # seconds to wait for ready (default: step_timeout; needs ready)
# env = { RUST_LOG = "debug" }         # extra environment for install, quit and launch
# state = ["~/Library/Application Support/MyApp"]  # Guest paths removed before every Run
# notification_id = "com.example.myapp"  # the app as its Notifications' sender (macOS bundle id, Windows AppUserModelID, Linux app name)
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

    launcher = vmlab_dir / LAUNCHER_NAME
    if launcher.exists():
        out("kept      %s" % launcher)
    else:
        out("created   %s (vmlab %s)" % (launcher, _pin(launcher, pyz)))


def self_update(start, source, out):
    """Pin the project to source, or to the vmlab running this command (the skill's, or VMLAB_PYZ)."""
    vmlab_dir = _find_vmlab_dir(start)
    pyz, launcher = vmlab_dir / PYZ_NAME, vmlab_dir / LAUNCHER_NAME
    if source:
        source = Path(source).resolve()
    else:
        source = _running_zipapp()
        if pyz.exists() and source == pyz.resolve():
            raise ConfigError(
                pyz,
                None,
                "is the project's own copy, which self-update replaces",
                "run it from the skill: `SKILL_DIR/scripts/vmlab self-update`, or pass --from PATH/TO/vmlab.pyz",
            )

    template = _launcher_template()  # before _install: the running zipapp may be the file it replaces
    old = _version_of(pyz) if pyz.exists() else _pinned_version(launcher)
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
    _pin(launcher, pyz, template)
    out("%s: %s -> %s (from %s)" % (pyz, old or "none", new, source))


def _launcher_template():
    return pkgutil.get_data("vmlab", "launcher").decode("utf-8")


def _pin(launcher, pyz, template=None):
    """Write the launcher that runs pyz, or downloads its release with its digest when pyz is gone; returns the version."""
    version = _version_of(pyz)
    text = re.sub(r"^VERSION=.*$", "VERSION=" + version, template or _launcher_template(), count=1, flags=re.M)
    text = re.sub(r"^SHA256=.*$", "SHA256=" + hashlib.sha256(pyz.read_bytes()).hexdigest(), text, count=1, flags=re.M)
    tmp = launcher.with_name(launcher.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o755)
    os.replace(str(tmp), str(launcher))
    return version


def _pinned_version(launcher):
    match = launcher.exists() and re.search(r"^VERSION=(\S+)$", launcher.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else None


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
        if any((candidate / name).is_file() for name in (CONFIG_NAME, PYZ_NAME, LAUNCHER_NAME)):
            return candidate
    raise ConfigError(start / CONFIG_DIR, None, "not a vmlab project", "run `vmlab init` in the project root first")


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
        raise ConfigError(pyz, None, "is not a working vmlab zipapp", "delete it, then run `SKILL_DIR/scripts/vmlab self-update` from the skill")
    return match.group(1)


def _parse(version):
    return tuple(int(n) for n in version.split("."))
