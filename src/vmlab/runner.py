"""Discover Scenarios and execute them on each Lab.

Each invocation writes one timestamped folder per Lab holding the reports of every
Scenario run there.

Lifecycle policies: a Regression suite (saved Scenarios) restores Clean state
once at its start and stops the Guests vmlab started; an Ad-hoc run (any
Scenario file outside the scenarios folder) keeps Guest state and leaves its
Guests running. A Scenario declaring FRESH, or --fresh, restores before a
Scenario. The Lab's app state paths are removed before every Run.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

from vmlab import __version__, report
from vmlab.config import ConfigError
from vmlab.deploy import build_if_stale, install, launch, prepare_run
from vmlab.home import StartedGuests
from vmlab.providers import provider_for
from vmlab.providers.base import GuestError
from vmlab.scenario import Guest, run_scenario


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


def run(project, lab_names, scenario_names, out, keep=False, fresh=False, stop_command="vmlab down"):
    """Run the chosen Scenarios on the chosen Labs. Returns the list of reports."""
    labs = project.select_labs(lab_names)
    scenarios, ad_hoc = discover(project, scenario_names)
    # Builds run first, one Lab at a time: Labs sharing an artifact build it once.
    runs = [_LabRun(project, lab) for lab in labs]
    for lab_run in runs:
        lab_run.build()
    reports, kept = [], []
    for lab_run in runs:
        data, still_ours = lab_run.run(scenarios, out, keep=keep or ad_hoc, ad_hoc=ad_hoc, fresh=fresh)
        reports.append(data)
        if still_ours:
            kept.append(lab_run.lab.name)
    if kept:
        out("Kept running: %s. Stop with: %s %s" % (", ".join(kept), stop_command, " ".join(kept)))
    return reports


def deploy(project, lab_names, out, stop_command="vmlab down"):
    """Build if stale, start, install, reset and launch the app on each Lab; leave the Guests running."""
    started_guests = StartedGuests()
    labs = project.select_labs(lab_names)
    for lab in labs:
        provider = provider_for(project, lab)
        built = build_if_stale(project, lab)
        if not provider.is_running():
            started_guests.add(guest_key(provider))
        provider.up()
        guest_artifact = install(provider, lab, built["artifact"]) if built else None
        prepare_run(provider, lab, guest_artifact, launch_app=bool(lab.app.launch))
        out("%s: deployed %s%s" % (lab.name, guest_artifact or "(no artifact)", " (rebuilt)" if built and built["built"] else ""))
    out("Kept running: %s. Stop with: %s %s" % (", ".join(l.name for l in labs), stop_command, " ".join(l.name for l in labs)))


def guest_key(provider):
    return "%s:%s" % (provider.lab.provider, provider.guest_id)


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

    def build(self):
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
        ours = key in started_guests or not provider.is_running()
        results = []
        if not self.error:
            try:
                if not provider.is_running():
                    started_guests.add(key)
                provider.up()
                results = self._scenarios(provider, scenarios, ad_hoc, fresh)
            except GuestError as exc:
                self.error = str(exc)
            finally:
                if ours and not keep:
                    provider.down()
                    started_guests.discard(key)

        data = {
            "vmlab_version": __version__,
            "lab": lab.name,
            "provider": lab.provider,
            "os": lab.os,
            "arch": lab.arch,
            "started_at": self.started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": round(time.time() - self.t0, 3),
            "status": "error" if self.error else report.overall_status(results),
            "error": self.error,
            "run_dir": str(self.run_dir),
            "deploy": self.built,
            "scenarios": results,
        }
        data["totals"] = report.totals(data)
        report.write(self.run_dir, data)
        _print_summary(data, out)
        return data, ours and keep and provider.is_running()

    def _scenarios(self, provider, scenarios, ad_hoc, fresh):
        lab = self.lab
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

        def launch_app(env):
            launch(provider, lab, state["guest_artifact"], env)

        return [run_scenario(path, Guest(lab, provider, self.run_dir, launch_app), prepare) for path in scenarios]


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
