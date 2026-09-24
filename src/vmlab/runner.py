"""Discover Scenarios and execute them on each Lab.

Each invocation writes one timestamped folder per Lab holding the reports of every
Scenario run there.

Lifecycle policies: a Regression suite (saved Scenarios) restores Clean state
once at its start and stops the Guests vmlab started; an Ad-hoc run (any
Scenario file outside the scenarios folder) keeps Guest state and leaves its
Guests running. A Scenario declaring FRESH, or --fresh, restores before a
Scenario. The Lab's app state paths are removed before every Run.

A Lab this Host does not cover (vmlab.arch) is skipped: no build, no Guest, a
warning, and reports with status "skipped".
"""

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from vmlab import __version__, arch, report
from vmlab.config import ConfigError
from vmlab.deploy import build_if_stale, install, launch, prepare_run
from vmlab.home import GuestInUse, GuestLock, StartedGuests
from vmlab.memory import free_memory_gb
from vmlab.providers import provider_for
from vmlab.providers.base import GuestError
from vmlab.scenario import STILL_RUNNING, ChannelUse, Guest, run_scenario


def discover(project, names):
    """(Scenario paths, ad_hoc): names are saved Scenario names or paths to Scenario files."""
    scenarios_dir = project.scenarios_dir.resolve()
    files = [Path(n).resolve() for n in names if Path(n).is_file()]
    ad_hoc = any(scenarios_dir not in f.parents for f in files)
    available = sorted(p for p in project.scenarios_dir.glob("*.py") if not p.name.startswith("_"))
    only_files = bool(names) and len(files) == len(names)
    if not available and not only_files:
        raise ConfigError(
            project.scenarios_dir,
            None,
            "no Scenarios found",
            "add a .py file there that defines scenario(g)",
        )
    if not names:
        return available, False
    by_name = {p.stem: p for p in available}
    chosen = []
    for name in names:
        if Path(name).is_file():
            chosen.append(Path(name).resolve())
            continue
        stem = name[:-3] if name.endswith(".py") else name
        stem = stem.rsplit("/", 1)[-1]
        if stem not in by_name:
            raise ConfigError(
                project.scenarios_dir, None, "no Scenario named %r" % name, "use one of: %s" % ", ".join(by_name)
            )
        chosen.append(by_name[stem])
    return chosen, ad_hoc


def run(project, lab_names, scenario_names, out, keep=False, fresh=False, parallel=False, stop_command="vmlab down"):
    """Run the chosen Scenarios on the chosen Labs. Returns the list of reports."""
    labs = project.select_labs(lab_names)
    scenarios, ad_hoc = discover(project, scenario_names)
    # Builds run first, one Lab at a time: Labs sharing an artifact build it once.
    runs = [_LabRun(project, lab) for lab in labs]
    for lab_run in runs:
        lab_run.build()

    lock = threading.Lock()

    def locked_out(line):
        with lock:
            out(line)

    def work(lab_run):
        return lab_run.run(scenarios, locked_out, keep=keep or ad_hoc, ad_hoc=ad_hoc, fresh=fresh)

    outcomes = _run_parallel(runs, work, locked_out) if parallel else [work(r) for r in runs]
    reports, kept = [], []
    for lab_run, (data, still_ours) in zip(runs, outcomes):
        reports.append(data)
        if still_ours:
            kept.append(lab_run.lab.name)
    if kept:
        out("Kept running: %s. Stop with: %s %s" % (", ".join(kept), stop_command, " ".join(kept)))
    return reports


def _run_parallel(runs, work, out):
    """Run work(lab_run) concurrently, starting a Lab only while its memory_gb fits in free
    Host memory. A Guest that is already running needs none. Returns outcomes in order."""
    free = free_memory_gb()
    if free is None:
        out("warning: cannot measure free Host memory; starting all Labs at once")
        free = float("inf")
    pending = list(runs)
    running = {}  # lab_run -> GB reserved
    outcomes, errors, announced = {}, [], set()
    done = threading.Condition()

    def target(lab_run):
        try:
            outcomes[lab_run] = work(lab_run)
        except BaseException as exc:  # re-raised below, after every Lab has finished
            errors.append(exc)
        finally:
            with done:
                del running[lab_run]
                done.notify_all()

    with done:
        while pending or running:
            for lab_run in list(pending):
                lab = lab_run.lab
                need = 0 if lab_run.skipped or provider_for(lab_run.project, lab).is_running() else lab.memory_gb
                available = free - sum(running.values())
                if need > available and running:
                    if lab.name not in announced:
                        announced.add(lab.name)
                        out("queued %s: needs %g GB, %.1f GB free while %s run" % (lab.name, need, available, ", ".join(r.lab.name for r in running)))
                    continue
                if need > available:
                    out("warning: %s needs %g GB, more memory than is free (%.1f GB); running it alone" % (lab.name, need, available))
                pending.remove(lab_run)
                running[lab_run] = need
                threading.Thread(target=target, args=(lab_run,), daemon=True).start()
            done.wait()
    if errors:
        raise errors[0]
    return [outcomes[r] for r in runs]


def deploy(project, lab_names, out, stop_command="vmlab down"):
    """Build if stale, start, install, reset and launch the app on each Lab; leave the Guests running."""
    started_guests = StartedGuests()
    kept = []
    for lab in project.select_labs(lab_names):
        warning = arch.warning(lab)
        if warning:
            out("warning: %s: %s (skipped)" % (lab.name, warning))
            continue
        provider = provider_for(project, lab)
        built = build_if_stale(project, lab)
        lock = guest_lock(provider)
        try:
            if not provider.is_running():
                started_guests.add(guest_key(provider))
            if guest_key(provider) in started_guests:
                kept.append(lab.name)
            provider.up()
            guest_artifact = install(provider, lab, built["artifact"]) if built else None
            prepare_run(provider, lab, guest_artifact, launch_app=bool(lab.app.launch))
        finally:
            lock.release()
        out("%s: deployed %s%s" % (lab.name, guest_artifact or "(no artifact)", " (rebuilt)" if built and built["built"] else ""))
    if kept:
        out("Kept running: %s. Stop with: %s %s" % (", ".join(kept), stop_command, " ".join(kept)))


def guest_key(provider):
    return "%s:%s" % (provider.lab.provider, provider.guest_id)


def guest_lock(provider):
    """The acquired GuestLock of provider's Guest; GuestError while another vmlab process holds it."""
    lock = GuestLock(guest_key(provider))
    try:
        lock.acquire()
    except GuestInUse as exc:
        raise GuestError(
            "Guest %s of Lab %s is in use by another vmlab (%s)" % (provider.guest_id, provider.lab.name, exc),
            "wait for that one to finish, or stop it",
        )
    return lock


class _LabRun:
    """One Lab's part of an invocation: its run folder, build and Scenarios."""

    def __init__(self, project, lab):
        self.project = project
        self.lab = lab
        self.started = datetime.now(timezone.utc)
        self.t0 = time.time()
        self.run_dir = _new_run_dir(project, lab, self.started)
        self.built = None
        self.error = None
        self.coverage = arch.coverage(lab)
        self.warnings = [w for w in [arch.warning(lab)] if w]
        self.skipped = bool(self.warnings)

    def build(self):
        if self.skipped:
            return
        try:
            self.built = build_if_stale(self.project, self.lab, log_path=self.run_dir / "build.log")
        except GuestError as exc:
            self.error = str(exc)

    def run(self, scenarios, out, keep, ad_hoc, fresh):
        """Returns (report, whether vmlab left a Guest it owns running)."""
        lab = self.lab
        provider = provider_for(self.project, lab)
        started_guests = StartedGuests()
        key = guest_key(provider)
        results = []
        lab_calls = ChannelUse()  # calls outside any Run: suite restore and install
        lock = None
        for warning in self.warnings:
            out("warning: %s: %s" % (lab.name, warning))
        if not self.error and not self.skipped:
            try:
                lock = guest_lock(provider)
            except GuestError as exc:
                self.error = str(exc)
        ours = lock is not None and (key in started_guests or not provider.is_running())
        if lock:
            try:
                if not provider.is_running():
                    started_guests.add(key)
                provider.up()
                results = self._scenarios(provider, scenarios, ad_hoc, fresh, lab_calls, out)
            except GuestError as exc:
                self.error = str(exc)
            finally:
                if ours and not keep:
                    provider.down()
                    started_guests.discard(key)
                lock.release()

        data = {
            "vmlab_version": __version__,
            "lab": lab.name,
            "provider": lab.provider,
            "os": lab.os,
            "arch": lab.arch,
            "coverage": self.coverage,
            "started_at": self.started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": round(time.time() - self.t0, 3),
            "status": "error" if self.error else "skipped" if self.skipped else report.overall_status(results),
            "error": self.error,
            "warnings": self.warnings,
            "run_dir": str(self.run_dir),
            "deploy": self.built,
            "channels": lab_calls.channels,
            "fallbacks": lab_calls.fallbacks,
            "scenarios": results,
        }
        data["totals"] = report.totals(data)
        report.write(self.run_dir, data)
        _print_summary(data, out)
        return data, ours and keep and provider.is_running()

    def _scenarios(self, provider, scenarios, ad_hoc, fresh, lab_calls, out):
        lab = self.lab
        provider.on_exec = lab_calls.record
        artifact = self.built["artifact"] if self.built else None
        state = {"clean": False, "guest_artifact": None}

        def restore():
            provider.restore()
            state["clean"] = True
            state["guest_artifact"] = install(provider, lab, artifact) if artifact else None

        if not ad_hoc or fresh:
            restore()
        elif artifact:
            state["guest_artifact"] = install(provider, lab, artifact)

        def prepare(scenario_fresh, launch_app):
            if (scenario_fresh or fresh) and not state["clean"]:
                restore()
            state["clean"] = False
            prepare_run(provider, lab, state["guest_artifact"], launch_app=launch_app and bool(lab.app.launch))

        def launch_app(env, call_timeout):
            launch(provider, lab, state["guest_artifact"], env, call_timeout)

        results = []
        for path in scenarios:
            guest = Guest(lab, provider, self.run_dir, launch_app)
            provider.on_exec = guest.channel_use.record
            still_running = lambda entry: out(report.still_running(lab.name, path.stem, entry))  # noqa: E731
            results.append(run_scenario(path, guest, prepare, still_running))
        return results


def _new_run_dir(project, lab, started):
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    project.runs_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        name = "%s-%s" % (stamp, lab.name) if n == 1 else "%s.%d-%s" % (stamp, n, lab.name)
        try:
            (project.runs_dir / name).mkdir()
            return project.runs_dir / name
        except FileExistsError:
            n += 1


def _print_summary(data, out):
    if data["error"]:
        out("ERROR %s: %s" % (data["lab"], data["error"]))
    for s in data["scenarios"]:
        for p in s["spawned"]:
            if p["ended"] == STILL_RUNNING:
                out(report.still_running(data["lab"], s["name"], p))
        if s["status"] == "error":
            out("ERROR %s/%s: %s" % (data["lab"], s["name"], s["error"]))
        for c in s["checks"]:
            if not c["passed"]:
                evidence = ", ".join(s["screenshots"]) or "none"
                out(
                    "FAIL %s/%s: %s%s (screenshots: %s)"
                    % (data["lab"], s["name"], c["name"], ": %s" % c["detail"] if c["detail"] else "", evidence)
                )
    t = data["totals"]
    out(
        "%s %s: %d Scenario(s), %d Check(s) (%d visual), %d failed, %d error(s); report: %s"
        % (
            data["status"].upper(),
            data["lab"],
            t["scenarios"],
            t["checks"],
            t["visual_checks"],
            t["failed_checks"],
            t["errors"],
            data["run_dir"],
        )
    )
