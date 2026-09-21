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
# Where an agent installs the vmlab skill; the zipapp ships as scripts/vmlab.pyz inside it.
SKILL_DIRS = (".claude/skills/vmlab", ".cursor/skills/vmlab", ".agents/skills/vmlab", ".codex/skills/vmlab")

TEMPLATE = """\
# vmlab project config. Vocabulary: a Lab describes one Guest (a VM) that
# Scenarios in scenarios/*.py run on. vmlab never rewrites this file.
#
# Declare one table per Lab. Uncomment and adapt, e.g.:
#
# [labs.mac]
# provider = "fake"        # fake (tart, fusion: planned)
# os = "macos"             # macos | windows | linux
# arch = "arm64"           # arm64 | x86_64; defaults to the Host's
#
# [labs.mac.fake]          # options of the Lab's Provider
# ui_tree = "tree.json"    # scripted UI tree, relative to this file
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
    candidates = [base / d / "scripts" / PYZ_NAME for base in (project_root, Path.home()) for d in SKILL_DIRS]
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
