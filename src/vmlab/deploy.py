"""Deploy the Build artifact per the Lab's [labs.<name>.app] recipe.

- build: on the Host, in the project root, only when the artifact is missing or
  older than one of its inputs. The hook gets VMLAB_LAB, VMLAB_OS, VMLAB_ARCH and
  VMLAB_LANGUAGE (the Lab language, e.g. ru-RU).
- deliver + install: once per suite, and again after any restore. The artifact
  is copied to a uniquely named Guest folder (shared-folder caches never serve
  a stale copy), then the install recipe runs.
- before every Run: quit (exit code ignored) and wait for the app's process to go, if the Lab
  names one, remove the app state paths, launch, then wait for the app's ready condition, if
  it has one.

Guest recipes run in the Guest's shell with the Lab's app.env plus VMLAB_LAB,
VMLAB_OS, VMLAB_ARCH, VMLAB_LANGUAGE and VMLAB_ARTIFACT (the Guest path of the
delivered copy).
"""

import glob
import json
import os
import subprocess
import time
from pathlib import Path

from vmlab import hostproc, ui
from vmlab.progress import QUIET
from vmlab.providers.base import GuestError, GuestTimeout

GUEST_ARTIFACTS = "~/vmlab/artifacts"
LOG_TAIL_LINES = 20
ANSWER_CHARS = 1000  # of the last poll's answer in an unmet ready's error


class DeployError(GuestError):
    """The Build artifact could not be built, found, installed or launched."""


def build_if_stale(project, lab, log_path=None, progress=QUIET):
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

    with progress.step("building", log_path and "(log: %s)" % log_path):
        _build(project, lab, log_path)
    artifact = _resolve(project.root, app.artifact)
    if not artifact:
        raise DeployError(
            "Build artifact %s not found after the build hook ran" % app.artifact,
            "make %s.build produce it, or correct %s.artifact" % (key, key),
        )
    return {"artifact": str(artifact), "built": True}


def _build(project, lab, log_path):
    app, key = lab.app, "labs.%s.app" % lab.name
    env = dict(os.environ, VMLAB_LAB=lab.name, VMLAB_OS=lab.os, VMLAB_ARCH=lab.arch, VMLAB_LANGUAGE=lab.language)
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


def install(provider, lab, host_artifact, progress=QUIET):
    """Deliver the artifact into the Guest and run the install recipe. Returns its Guest path."""
    with progress.step("delivering"):
        provider.remove_paths([GUEST_ARTIFACTS], timeout=lab.step_timeout)
        unique = "%s-%d" % (time.strftime("%Y%m%dT%H%M%S"), os.getpid())
        guest_artifact = provider.copy_in(Path(host_artifact), "%s/%s" % (GUEST_ARTIFACTS, unique))
    if lab.app.install:
        with progress.step("installing"):
            _recipe(provider, lab, "install", guest_artifact, lab.app.install_timeout)
    return guest_artifact


def prepare_run(provider, lab, guest_artifact, launch_app=True, progress=QUIET):
    """Before a Run: quit the app, reset its state once it has gone, and launch it."""
    if lab.app.quit:
        with progress.step("quitting"):
            quit(provider, lab, guest_artifact, check=False)
    provider.remove_paths(lab.app.state, timeout=lab.step_timeout)
    if launch_app:
        launch(provider, lab, guest_artifact, progress=progress)


def quit(provider, lab, guest_artifact, env=None, call_timeout=None, check=True):
    """Run the quit recipe, then wait until the app's process has gone; with no process to wait
    for, a failing recipe raises instead, unless check is False.

    call_timeout(doing) bounds each Guest call (see vmlab.ui.UI); default: step_timeout.
    """
    if not lab.app.quit:
        raise DeployError("Lab %s has no quit recipe" % lab.name, "set labs.%s.app.quit" % lab.name)
    call_timeout = call_timeout or (lambda doing: lab.step_timeout)
    process = lab.app.quit_process
    _recipe(provider, lab, "quit", guest_artifact, call_timeout("quit"), check=check and process is None, extra_env=env)
    if process:
        _wait_gone(provider, lab, call_timeout)


def launch(provider, lab, guest_artifact, env=None, call_timeout=None, progress=QUIET):
    """Run the launch recipe, then wait until the app is ready.

    call_timeout(doing) bounds each Guest call of the wait (see vmlab.ui.UI); default: step_timeout.
    """
    if not lab.app.launch:
        raise DeployError("Lab %s has no launch recipe" % lab.name, "set labs.%s.app.launch" % lab.name)
    with progress.step("launching"):
        _recipe(provider, lab, "launch", guest_artifact, lab.step_timeout, extra_env=env)
    if lab.app.ready is not None:
        with progress.step("waiting for ready", json.dumps(lab.app.ready.describe())):
            _wait_ready(provider, lab, call_timeout or (lambda doing: lab.step_timeout))


def _wait_ready(provider, lab, call_timeout):
    timeout = lab.app.ready_timeout or lab.step_timeout
    key = "labs.%s.app" % lab.name
    fix = "check that the launch recipe starts the app and what the condition waits for (try it with `vmlab ui wait-for`), or raise %s.ready_timeout" % key
    _wait(provider, call_timeout, lab.app.ready, timeout, key + ".ready", "the app to be ready", "the app is not ready within %ss of its launch" % timeout, fix)


def _wait_gone(provider, lab, call_timeout):
    """Wait until the Lab's quit process has gone, up to quit_timeout."""
    timeout = lab.app.quit_timeout or lab.step_timeout
    key = "labs.%s.app" % lab.name
    named_by = key + (".process" if lab.app.process else ".ready")
    fix = "check that %s.quit quits the app (try it with `vmlab exec`), or raise %s.quit_timeout" % (key, key)
    condition = ui.condition(process=lab.app.quit_process, gone=True)
    what = "the app's process %s has not gone within %ss of its quit" % (lab.app.quit_process, timeout)
    _wait(provider, call_timeout, condition, timeout, named_by, "the app's process to go", what, fix)


def _wait(provider, call_timeout, condition, timeout, named_by, waiting_for, unmet_problem, fix):
    try:
        result = ui.UI(provider, call_timeout).wait_for(condition, timeout=timeout)
    except GuestTimeout as exc:
        if getattr(exc, "unmet", None) is None:
            raise
        # The caller's clock (a Scenario's, in g.launch or g.quit) ran out before the Lab's
        # timeout: the same error ends it, saying what it was waiting for.
        raise type(exc)("%s, waiting for %s: %s" % (exc.message, waiting_for, _unmet(named_by, exc.unmet)))
    if not result["met"]:
        raise DeployError("%s: %s" % (unmet_problem, _unmet(named_by, result)), fix)


def _unmet(named_by, result):
    """'{named_by} {condition} not met; last answer: {...}', the answer cut to ANSWER_CHARS."""
    answer = json.dumps({k: v for k, v in result.items() if k not in ("met", "waited_s", "condition")})
    if len(answer) > ANSWER_CHARS:
        answer = answer[:ANSWER_CHARS] + "..."
    return "%s %s not met; last answer: %s" % (named_by, json.dumps(result["condition"]), answer)


def _recipe(provider, lab, step, guest_artifact, timeout, check=True, extra_env=None):
    command = getattr(lab.app, step)
    env = dict(lab.app.env, VMLAB_LAB=lab.name, VMLAB_OS=lab.os, VMLAB_ARCH=lab.arch, VMLAB_LANGUAGE=lab.language, VMLAB_ARTIFACT=guest_artifact or "")
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
