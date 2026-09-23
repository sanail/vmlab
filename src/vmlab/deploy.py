"""Deploy the Build artifact per the Lab's [labs.<name>.app] recipe.

- build: on the Host, in the project root, only when the artifact is missing or
  older than one of its inputs. The hook gets VMLAB_LAB, VMLAB_OS and VMLAB_ARCH.
- deliver + install: once per suite, and again after any restore. The artifact
  is copied to a uniquely named Guest folder (shared-folder caches never serve
  a stale copy), then the install recipe runs.
- before every Run: quit (exit code ignored), remove the app state paths, launch,
  then wait for the app's ready condition, if it has one.

Guest recipes run in the Guest's shell with the Lab's app.env plus VMLAB_LAB,
VMLAB_OS, VMLAB_ARCH and VMLAB_ARTIFACT (the Guest path of the delivered copy).
"""

import glob
import json
import os
import subprocess
import time
from pathlib import Path

from vmlab import hostproc, ui
from vmlab.providers.base import GuestError

GUEST_ARTIFACTS = "~/vmlab/artifacts"
LOG_TAIL_LINES = 20
ANSWER_CHARS = 1000  # of the last poll's answer in an unmet ready's error


class DeployError(GuestError):
    """The Build artifact could not be built, found, installed or launched."""


def build_if_stale(project, lab, log_path=None):
    """Return {"artifact": Host path, "built": bool}, or None when the Lab has no artifact."""
    app = lab.app
    if not app.artifact:
        return None
    key = "labs.%s.app" % lab.name
    artifact = _resolve(project.root, app.artifact)
    if artifact and (not app.build or not _stale(artifact, [project.root / i for i in app.inputs])):
        return {"artifact": str(artifact), "built": False}
    if not app.build:
        raise DeployError(
            "Build artifact %s not found in %s" % (app.artifact, project.root),
            "build it, or set %s.build to a command that does" % key,
        )

    env = dict(os.environ, VMLAB_LAB=lab.name, VMLAB_OS=lab.os, VMLAB_ARCH=lab.arch)
    try:
        code, out, err = hostproc.run(["/bin/sh", "-c", app.build], app.build_timeout, cwd=str(project.root), env=env)
    except subprocess.TimeoutExpired:
        raise DeployError(
            "build hook %s.build timed out after %ss and was killed" % (key, app.build_timeout),
            "raise %s.build_timeout if the build is just slow" % key,
        )
    if log_path:
        log_path.write_text("$ %s\n%s%s" % (app.build, out, err), encoding="utf-8")
    if code != 0:
        raise DeployError(
            "build hook %s.build exited %s:\n%s" % (key, code, _tail(out + err)),
            "fix the build, or run it by hand in %s: %s" % (project.root, app.build),
        )
    artifact = _resolve(project.root, app.artifact)
    if not artifact:
        raise DeployError(
            "Build artifact %s not found after the build hook ran" % app.artifact,
            "make %s.build produce it, or correct %s.artifact" % (key, key),
        )
    return {"artifact": str(artifact), "built": True}


def install(provider, lab, host_artifact):
    """Deliver the artifact into the Guest and run the install recipe. Returns its Guest path."""
    provider.remove_paths([GUEST_ARTIFACTS], timeout=lab.step_timeout)
    unique = "%s-%d" % (time.strftime("%Y%m%dT%H%M%S"), os.getpid())
    guest_artifact = provider.copy_in(Path(host_artifact), "%s/%s" % (GUEST_ARTIFACTS, unique))
    if lab.app.install:
        _recipe(provider, lab, "install", guest_artifact, lab.app.install_timeout)
    return guest_artifact


def prepare_run(provider, lab, guest_artifact, launch_app=True):
    """Before a Run: quit the app, reset its state, and launch it."""
    if lab.app.quit:
        _recipe(provider, lab, "quit", guest_artifact, lab.step_timeout, check=False)
    provider.remove_paths(lab.app.state, timeout=lab.step_timeout)
    if launch_app:
        launch(provider, lab, guest_artifact)


def launch(provider, lab, guest_artifact, env=None, call_timeout=None):
    """Run the launch recipe, then wait until the app is ready.

    call_timeout(doing) bounds each Guest call of the wait (see vmlab.ui.UI); default: step_timeout.
    """
    if not lab.app.launch:
        raise DeployError("Lab %s has no launch recipe" % lab.name, "set labs.%s.app.launch" % lab.name)
    _recipe(provider, lab, "launch", guest_artifact, lab.step_timeout, extra_env=env)
    if lab.app.ready is not None:
        _wait_ready(provider, lab, call_timeout or (lambda doing: lab.step_timeout))


def _wait_ready(provider, lab, call_timeout):
    timeout = lab.app.ready_timeout or lab.step_timeout
    result = ui.UI(provider, call_timeout).wait_for(lab.app.ready, timeout=timeout)
    if not result["met"]:
        key = "labs.%s.app" % lab.name
        answer = json.dumps({k: v for k, v in result.items() if k not in ("met", "waited_s", "condition")})
        if len(answer) > ANSWER_CHARS:
            answer = answer[:ANSWER_CHARS] + "..."
        raise DeployError(
            "the app is not ready: %s.ready %s not met within %ss of its launch; last answer: %s" % (key, json.dumps(result["condition"]), timeout, answer),
            "check that the launch recipe starts the app and what the condition waits for (try it with `vmlab ui wait-for`), or raise %s.ready_timeout" % key,
        )


def _recipe(provider, lab, step, guest_artifact, timeout, check=True, extra_env=None):
    command = getattr(lab.app, step)
    env = dict(lab.app.env, VMLAB_LAB=lab.name, VMLAB_OS=lab.os, VMLAB_ARCH=lab.arch, VMLAB_ARTIFACT=guest_artifact or "")
    env.update(extra_env or {})
    result = provider.exec(provider.shell_argv(command), timeout, env=env)
    if check and not result.ok:
        raise DeployError(
            "%s recipe labs.%s.app.%s exited %s:\n%s" % (step, lab.name, step, result.code, _tail(result.stdout + result.stderr)),
            "try it in the Guest by hand: %s" % command,
        )
    return result


def _resolve(root, pattern):
    """The newest Host path matching pattern (relative to root), or None."""
    matches = [Path(m) for m in glob.glob(str(root / pattern))]
    return max(matches, key=_newest_mtime) if matches else None


def _stale(artifact, inputs):
    built = _newest_mtime(artifact)
    return any(_newest_mtime(i) > built for i in inputs if i.exists())


def _newest_mtime(path):
    """mtime of a file, or of the newest entry in a folder (bundles like .app are folders)."""
    newest = path.stat().st_mtime
    if path.is_dir():
        for folder, dirs, files in os.walk(str(path)):
            for name in dirs + files:
                try:
                    newest = max(newest, os.lstat(os.path.join(folder, name)).st_mtime)
                except OSError:
                    pass
    return newest


def _tail(text):
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-LOG_TAIL_LINES:]) or "(no output)"
