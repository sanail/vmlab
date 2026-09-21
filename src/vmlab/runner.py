"""Discover Scenarios and execute them on each Lab.

Each invocation writes one timestamped folder per Lab holding the reports of every
Scenario run there.
"""

import time
from datetime import datetime, timezone

from vmlab import __version__, report
from vmlab.config import ConfigError
from vmlab.providers import provider_for
from vmlab.providers.base import GuestError
from vmlab.scenario import Guest, run_scenario


def discover(project, names):
    available = sorted(p for p in project.scenarios_dir.glob("*.py") if not p.name.startswith("_"))
    if not available:
        raise ConfigError(
            project.scenarios_dir,
            None,
            "no Scenarios found",
            "add a .py file there that defines scenario(g)",
        )
    if not names:
        return available
    by_name = {p.stem: p for p in available}
    chosen = []
    for name in names:
        stem = name[:-3] if name.endswith(".py") else name
        stem = stem.rsplit("/", 1)[-1]
        if stem not in by_name:
            raise ConfigError(
                project.scenarios_dir, None, "no Scenario named %r" % name, "use one of: %s" % ", ".join(by_name)
            )
        chosen.append(by_name[stem])
    return chosen


def run(project, lab_names, scenario_names, out):
    """Run the chosen Scenarios on the chosen Labs. Returns the list of reports."""
    labs = project.select_labs(lab_names)
    scenarios = discover(project, scenario_names)
    return [_run_lab(project, lab, scenarios, out) for lab in labs]


def _run_lab(project, lab, scenarios, out):
    provider = provider_for(project, lab)
    started = datetime.now(timezone.utc)
    run_dir = _new_run_dir(project, lab, started)
    t0 = time.time()
    started_by_us = not provider.is_running()
    results, error = [], None
    try:
        provider.up()
        results = [run_scenario(path, Guest(lab, provider, run_dir)) for path in scenarios]
    except GuestError as exc:
        error = str(exc)
    finally:
        if started_by_us:
            provider.down()

    data = {
        "vmlab_version": __version__,
        "lab": lab.name,
        "provider": lab.provider,
        "os": lab.os,
        "arch": lab.arch,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round(time.time() - t0, 3),
        "status": "error" if error else report.overall_status(results),
        "error": error,
        "run_dir": str(run_dir),
        "scenarios": results,
    }
    data["totals"] = report.totals(data)
    report.write(run_dir, data)
    _print_summary(data, out)
    return data


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
            out("ERROR %s/%s: %s" % (data["lab"], s["name"], s["error"].splitlines()[-1]))
        for c in s["checks"]:
            if not c["passed"]:
                evidence = ", ".join(s["screenshots"]) or "none"
                out(
                    "FAIL %s/%s: %s%s (screenshots: %s)"
                    % (data["lab"], s["name"], c["name"], ": %s" % c["detail"] if c["detail"] else "", evidence)
                )
    t = data["totals"]
    out(
        "%s %s: %d Scenario(s), %d Check(s), %d failed, %d error(s); report: %s"
        % (data["status"].upper(), data["lab"], t["scenarios"], t["checks"], t["failed_checks"], t["errors"], data["run_dir"])
    )
