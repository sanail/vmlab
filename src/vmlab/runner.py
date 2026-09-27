"""Discover Scenarios and execute them on each Lab.

Each invocation writes one timestamped folder per Lab holding the reports of every
Scenario run there. --repeat N runs that Suite run N times on each Lab, one folder
each, with its Guest up throughout, and ends with how often each Check passed;
--until-fail stops every Lab at the first repetition that did not pass and keeps
the Guests running.

Lifecycle policies: a Regression suite (saved Scenarios) restores Clean state
once at its start and stops the Guests vmlab started; an Ad-hoc run (any
Scenario file outside the scenarios folder) keeps Guest state and leaves its
Guests running. A Scenario declaring FRESH, or --fresh, restores before a
Scenario. The Lab's app state paths are removed before every Run.

Ctrl-C ends every Lab's Suite run: what its Scenario staged and spawned ends, the
Guest policy applies, and vmlab says what it stopped and left. Under --parallel
the Labs get up to INTERRUPT_WAIT_S for that; a second Ctrl-C exits at once.

A Lab this Host does not cover (vmlab.arch) is skipped: no build, no Guest, a
warning, and reports with status "skipped".
"""

import collections
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from vmlab import __version__, arch, hostpower, report
from vmlab.config import ConfigError, UsageError
from vmlab.deploy import build_if_stale, install, launch, prepare_run, quit
from vmlab.home import GuestInUse, GuestLock, StartedGuests
from vmlab.memory import free_memory_gb
from vmlab.progress import Progress, duration
from vmlab.providers import provider_for
from vmlab.providers.base import BootTimeout, GuestError
from vmlab.scenario import FAIL, SKIP, STILL_OPEN, STILL_RUNNING, ChannelUse, Guest, Interrupted, outcome, run_scenario

INTERRUPT_WAIT_S = 60  # how long Ctrl-C in a parallel Run waits for the Labs to end theirs


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


def run(
    project, lab_names, scenario_names, out, keep=False, fresh=False, parallel=False, stop_command="vmlab down", steps_out=None,
    repeat=1, until_fail=False,
):  # fmt: skip
    """Run the chosen Scenarios on the chosen Labs, repeat times each. Returns the list of reports.

    steps_out, a print-like callable, gets each Lab's step lines (vmlab.progress); None: none."""
    labs = project.select_labs(lab_names)
    scenarios, ad_hoc = discover(project, scenario_names)
    lock = threading.Lock()

    def locked(write):
        """write, one line at a time across Labs; None stays None."""
        if write is None:
            return None

        def locked_write(line):
            with lock:
                write(line)

        return locked_write

    locked_out = locked(out)
    # Builds run first, one Lab at a time: Labs sharing an artifact build it once.
    runs = [_LabRun(project, lab, Progress(locked(steps_out), lab.name)) for lab in labs]
    for lab_run in runs:
        lab_run.build()

    interrupt = threading.Event()
    stop_repeating = threading.Event() if until_fail else None  # set at the first repetition that did not pass

    def work(lab_run):
        if stop_repeating and stop_repeating.is_set():
            locked_out("%s: not run: --until-fail stopped at a failure" % lab_run.lab.name)
            lab_run.drop_run_dir()
            return False
        return lab_run.run(
            scenarios, locked_out, keep=keep or ad_hoc, ad_hoc=ad_hoc, fresh=fresh, interrupt=interrupt, repeat=repeat,
            stop_repeating=stop_repeating,
        )  # fmt: skip

    try:
        outcomes = _run_parallel(runs, work, locked_out, interrupt) if parallel else [work(r) for r in runs]
    except KeyboardInterrupt:
        _print_tally(runs, repeat, locked_out)
        _say_what_was_left(runs, locked_out, stop_command)
        raise
    except Exception:
        _print_tally(runs, repeat, locked_out)  # what the Labs finished is not lost with the error
        raise
    _print_tally(runs, repeat, out)
    kept_running([r.lab.name for r, still_ours in zip(runs, outcomes) if still_ours], out, stop_command)
    return _reports(runs)


def _reports(runs):
    """Every report the Labs have written so far, Lab by Lab; a copy, as Labs may still be adding theirs."""
    return [data for r in runs for data in list(r.reports)]


def _print_tally(runs, repeat, out):
    """After --repeat N: how often each Scenario errored and each Check passed, over the repetitions each Lab finished."""
    if repeat > 1:
        for line in report.tally(_reports(runs)):
            out(line)


def _run_parallel(runs, work, out, interrupt):
    """Run work(lab_run) concurrently, starting a Lab only while its memory_gb fits in free
    Host memory. A Guest that is already running needs none. Returns outcomes in order.

    On Ctrl-C it sets interrupt, which ends each Lab's Suite run, waits up to INTERRUPT_WAIT_S for
    them, and raises KeyboardInterrupt; a second Ctrl-C raises it at once."""
    free = free_memory_gb()
    if free is None:
        out("warning: cannot measure free Host memory; starting all Labs at once")
        free = float("inf")
    pending = list(runs)
    running = {}  # lab_run -> GB reserved
    outcomes, errors, announced = {}, [], set()
    threads = []
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

    try:
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
                    threads.append(threading.Thread(target=target, args=(lab_run,), daemon=True))
                    threads[-1].start()
                done.wait()
    except KeyboardInterrupt:
        interrupt.set()
        out("Interrupted: ending the Suite run on every Lab, for up to %ds (Ctrl-C again exits at once)" % INTERRUPT_WAIT_S)
        deadline = hostpower.awake_time() + INTERRUPT_WAIT_S
        with done:  # a thread is not alive before start() nor once it has ended
            while any(t.is_alive() for t in threads) and hostpower.awake_time() < deadline:
                done.wait(0.2)
        raise
    if errors:
        raise errors[0]
    return [outcomes[r] for r in runs]


def _say_what_was_left(runs, out, stop_command):
    """After Ctrl-C: which Labs' Guests vmlab stopped, which Labs had not ended their Run, and
    which Guests vmlab started and left running."""
    stopped = [r.lab.name for r in runs if r.guest_stopped]
    if stopped:
        out("Guests stopped: %s" % ", ".join(stopped))
    for r in runs:
        if r.began and not r.done:
            out("warning: %s had not ended its Suite run: its Scenario may still be running, and what it spawned in the Guest" % r.lab.name)
    # A Lab still ending its Suite run may be stopping its Guest: it is named without asking the hypervisor.
    left = [r.lab.name for r in runs if r.ours and not r.guest_stopped and (not r.done or _is_running(provider_for(r.project, r.lab)))]
    kept_running(left, out, stop_command)


def deploy(project, lab_names, out, stop_command="vmlab down", steps_out=None):
    """Build if stale, start, install, reset and launch the app on each Lab; leave the Guests running.

    It stops at the first Lab that fails, still naming the Guests it left running. steps_out, a
    print-like callable, gets each Lab's step lines (vmlab.progress); None: none."""
    started_guests = StartedGuests()
    kept = []  # (Lab name, its provider) whose Guest vmlab started
    try:
        for lab in project.select_labs(lab_names):
            warning = arch.warning(lab)
            if warning:
                out("warning: %s: %s (skipped)" % (lab.name, warning))
                continue
            steps = Progress(steps_out, lab.name)
            provider = provider_for(project, lab)
            provider.progress = steps
            built = build_if_stale(project, lab, progress=steps)
            lock = guest_lock(provider)
            try:
                if not provider.is_running():
                    started_guests.add(guest_key(provider))
                if guest_key(provider) in started_guests:
                    kept.append((lab.name, provider))
                provider.up()
                guest_artifact = install(provider, lab, built["artifact"], progress=steps) if built else None
                prepare_run(provider, lab, guest_artifact, launch_app=bool(lab.app.launch), progress=steps)
            finally:
                lock.release()
            out("%s: deployed %s%s" % (lab.name, guest_artifact or "(no artifact)", " (rebuilt)" if built and built["built"] else ""))
    except BaseException:
        kept_running([name for name, provider in kept if _is_running(provider)], out, stop_command)
        raise
    kept_running([name for name, _ in kept], out, stop_command)


def kept_running(names, out, stop_command):
    """Tell the user which Labs' Guests vmlab started and left running, and how to stop them."""
    if names:
        out("Kept running: %s. Stop with: %s %s" % (", ".join(names), stop_command, " ".join(names)))


def _is_running(provider):
    """Is provider's Guest running? True when that cannot be told (the hypervisor fails): naming a
    stopped Guest as left running costs the user less than hiding a running one."""
    try:
        return provider.is_running()
    except Exception:
        return True


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


# What a Suite run passes on as it is, with its own exit code; anything else a Lab raises is a failure of vmlab's own.
EXPECTED = (ConfigError, UsageError, GuestInUse)


class _LabRun:
    """One Lab's part of an invocation: its build, and its Suite runs' folders and Scenarios."""

    def __init__(self, project, lab, progress):
        self.project = project
        self.lab = lab
        self.progress = progress
        self._new_repetition()
        self.built = None
        self.reports = []  # one per repetition that ended
        self.coverage = arch.coverage(lab)
        self.warnings = [w for w in [arch.warning(lab)] if w]
        self.skipped = bool(self.warnings)
        # What Ctrl-C needs to say what was stopped and left (see _say_what_was_left)
        self.began = self.done = False
        self.ours = False  # vmlab started this Lab's Guest, or holds it from an earlier Run
        self.guest_stopped = False

    def _new_repetition(self):
        """A new Suite run: its own start time and Run folder, no error yet."""
        self.started = datetime.now(timezone.utc)
        self.t0 = time.time()
        self.run_dir = _new_run_dir(self.project, self.lab, self.started)
        self.error = None

    def drop_run_dir(self):
        """Remove the Run folder of a Lab that will not run, unless its build left a log in it."""
        if not any(self.run_dir.iterdir()):
            self.run_dir.rmdir()

    def build(self):
        if self.skipped:
            return
        try:
            self.built = build_if_stale(self.project, self.lab, log_path=self.run_dir / "build.log", progress=self.progress)
        except GuestError as exc:
            self.error = str(exc)

    def run(self, scenarios, out, keep, ad_hoc, fresh, interrupt, repeat=1, stop_repeating=None):
        """Run the Suite run repeat times, adding each one's report to self.reports. Returns whether
        vmlab left a Guest it owns running. interrupt, once set, ends the Suite run as Ctrl-C does:
        before the next Scenario, or at the Scenario's next Guest call. stop_repeating (--until-fail),
        set by the first Lab whose repetition did not pass, ends the repetitions after the current one and keeps the Guest running."""
        self.began = True
        try:
            return self._run(scenarios, out, keep, ad_hoc, fresh, interrupt, repeat, stop_repeating)
        finally:
            self.done = True

    def _run(self, scenarios, out, keep, ad_hoc, fresh, interrupt, repeat, stop_repeating):
        lab = self.lab
        provider = provider_for(self.project, lab)
        provider.progress = self.progress
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
        ours = self.ours = lock is not None and (key in started_guests or not provider.is_running())
        repetition = 1
        if lock:
            try:
                stopped = not provider.is_running()
                if stopped:
                    started_guests.add(key)
                if not (stopped and (not ad_hoc or fresh)):
                    provider.up()  # otherwise the Suite run's first restore boots it, once, into Clean state
                while True:
                    if repeat > 1:
                        self.progress.say("repetition %d of %d" % (repetition, repeat))
                    results = []
                    try:
                        results = self._scenarios(provider, scenarios, ad_hoc, fresh, lab_calls, out, interrupt)
                    except GuestError as exc:
                        self.error = self._lab_error(provider, exc)
                    except EXPECTED:
                        raise
                    except Exception as exc:
                        self.error = self._vmlab_error(exc)
                    self._stop_repeating_if_failed(results, stop_repeating)
                    if repetition == repeat or stop_repeating and stop_repeating.is_set():
                        break
                    # the last repetition is reported once the Guest is down, as a single Suite run is
                    self._report(lab_calls, results, out, repetition, repeat)
                    self._new_repetition()
                    lab_calls, repetition = ChannelUse(), repetition + 1
            except GuestError as exc:
                self.error = self._lab_error(provider, exc)
            except EXPECTED:
                raise
            except Exception as exc:
                self.error = self._vmlab_error(exc)
            finally:
                keep = keep or bool(stop_repeating and stop_repeating.is_set())
                if ours and not keep:
                    provider.down()
                    started_guests.discard(key)
                    self.guest_stopped = True
                lock.release()
        self._stop_repeating_if_failed(results, stop_repeating)  # e.g. the build failed: no later Lab runs
        self._report(lab_calls, results, out, repetition, repeat)
        return ours and keep and provider.is_running()

    def _lab_error(self, provider, exc):
        """The Suite run's error; a Guest that did not come up leaves a screenshot of its screen."""
        if not isinstance(exc, BootTimeout) or not provider.HOST_SCREENSHOTS:
            return str(exc)
        shot = "screenshots/boot-timeout.png"
        try:
            (self.run_dir / shot).parent.mkdir(parents=True, exist_ok=True)
            provider.screenshot(self.run_dir / shot)
        except GuestError as shot_exc:
            return "%s\n  no screenshot of its screen: %s" % (exc, shot_exc.message)
        return "%s\n  its screen then: %s" % (exc, shot)

    def _vmlab_error(self, exc):
        """A failure of vmlab's own rather than the Guest's (a bug): it errors this Lab's Suite run
        alone, so the other Labs and the tally go on, and keeps the traceback in the Run folder."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        where = traceback.extract_tb(exc.__traceback__)[-1]
        return "vmlab failed: %s: %s (%s:%d); its traceback: traceback.txt in the Run folder, worth reporting" % (
            type(exc).__name__, exc, Path(where.filename).name, where.lineno,
        )  # fmt: skip

    def _stop_repeating_if_failed(self, results, stop_repeating):
        if stop_repeating and self._status(results) not in ("passed", "skipped"):
            stop_repeating.set()

    def _status(self, results):
        return "error" if self.error else "skipped" if self.skipped else report.overall_status(results)

    def _report(self, lab_calls, results, out, repetition, repeat):
        """Write and print the report of this Suite run, repetition of repeat."""
        lab = self.lab
        deploy = dict(self.built, built=False) if self.built and self.reports else self.built  # built once, before the first repetition
        data = {
            "vmlab_version": __version__,
            "lab": lab.name,
            "provider": lab.provider,
            "os": lab.os,
            "arch": lab.arch,
            "coverage": self.coverage,
            "started_at": self.started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": round(time.time() - self.t0, 3),
            "status": self._status(results),
            "error": self.error,
            "warnings": self.warnings,
            "run_dir": str(self.run_dir),
            "deploy": deploy,
            "channels": lab_calls.channels,
            "fallbacks": lab_calls.fallbacks,
            "scenarios": results,
        }
        data["totals"] = report.totals(data)
        report.write(self.run_dir, data)
        _print_summary(data, out, "" if repeat == 1 else " (repetition %d of %d)" % (repetition, repeat))
        self.reports.append(data)

    def _scenarios(self, provider, scenarios, ad_hoc, fresh, lab_calls, out, interrupt):
        lab = self.lab
        provider.on_exec = lab_calls.record
        artifact = self.built["artifact"] if self.built else None
        state = {"clean": False, "guest_artifact": None}

        def restore():
            with self.progress.step("restoring Clean state"):
                provider.restore()
            state["clean"] = True
            state["guest_artifact"] = install(provider, lab, artifact, progress=self.progress) if artifact else None

        if not ad_hoc or fresh:
            restore()
        elif artifact:
            state["guest_artifact"] = install(provider, lab, artifact, progress=self.progress)

        def prepare(scenario_fresh, launch_app):
            if (scenario_fresh or fresh) and not state["clean"]:
                restore()
            state["clean"] = False
            prepare_run(provider, lab, state["guest_artifact"], launch_app=launch_app and bool(lab.app.launch))

        def launch_app(env, call_timeout):
            launch(provider, lab, state["guest_artifact"], env, call_timeout)

        def quit_app(env, call_timeout):
            quit(provider, lab, state["guest_artifact"], env, call_timeout)

        results = []
        for path, shots_dir in zip(scenarios, _shots_dirs([p.stem for p in scenarios])):
            if interrupt.is_set():
                raise Interrupted()
            self.progress.say("scenario %s" % path.stem)
            guest = Guest(lab, provider, self.run_dir, shots_dir, launch_app, quit_app, interrupt)
            provider.on_exec = guest.channel_use.record

            def unreported(staged, spawned, name=path.stem):
                for line in report.unreported_lines(lab.name, name, staged, spawned):
                    out(line)

            result = run_scenario(path, guest, prepare, unreported)
            self.progress.say("scenario %s %s" % (path.stem, _ended(result)))
            results.append(result)
        return results


def _ended(result):
    """How a Scenario ended, for its progress line: 'passed in 41s (1 of 6 Checks skipped)'."""
    how = {"passed": "passed", "failed": "failed", "error": "errored"}[result["status"]]
    line = "%s in %s" % (how, duration(result["duration_s"]))
    checks = result["checks"]
    failed = sum(outcome(c) == FAIL for c in checks)
    skipped = sum(outcome(c) == SKIP for c in checks)
    if result["status"] == "failed":
        line += " (%d of %d Checks failed)" % (failed, len(checks))
    elif result["status"] == "passed" and skipped:
        line += " (%d of %d Checks skipped)" % (skipped, len(checks))
    # A fallback Channel may be much slower, and polls then see less: a result to read with that in mind.
    by_pair = collections.OrderedDict()
    for f in result["fallbacks"]:
        by_pair.setdefault((f["from"], f["to"]), []).append(f["reason"])
    calls = sum(result["channels"].values())
    for (source, target), reasons in by_pair.items():
        line += "; %d of %d Guest call(s) fell back from %s to %s: %s" % (len(reasons), calls, source, target, reasons[-1])
    return line


def _shots_dirs(names):
    """Each Scenario's screenshots folder in the Suite run's folder: screenshots/<name>, and
    <name>-2, -3, ... for a later Scenario of the same name (never another Scenario's name)."""
    folders, taken = [], set(names)
    for i, name in enumerate(names):
        folder, n = name, 1
        while folder in taken and name in names[:i]:
            n += 1
            folder = "%s-%d" % (name, n)
        taken.add(folder)
        folders.append("screenshots/" + folder)
    return folders


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


def _print_summary(data, out, repetition=""):
    if data["error"]:
        out("ERROR %s: %s" % (data["lab"], data["error"]))
    for s in data["scenarios"]:
        for d in s["staged"]:
            if d["ended"] == STILL_OPEN:
                out(report.still_open(data["lab"], s["name"], d))
        for p in s["spawned"]:
            if p["ended"] == STILL_RUNNING:
                out(report.still_running(data["lab"], s["name"], p))
        if s["status"] == "error":
            out("ERROR %s/%s: %s" % (data["lab"], s["name"], s["error"]))
        for c in s["checks"]:
            if outcome(c) == SKIP:
                out("SKIP %s/%s: %s: %s" % (data["lab"], s["name"], c["name"], c["detail"]))
            elif outcome(c) == FAIL:
                evidence = ", ".join(s["screenshots"]) or "none"
                out(
                    "FAIL %s/%s: %s%s (screenshots: %s)"
                    % (data["lab"], s["name"], c["name"], ": %s" % c["detail"] if c["detail"] else "", evidence)
                )
    t = data["totals"]
    out(
        "%s %s%s: %d Scenario(s), %d Check(s) (%d visual), %d failed, %d skipped, %d error(s); report: %s"
        % (
            data["status"].upper(),
            data["lab"],
            repetition,
            t["scenarios"],
            t["checks"],
            t["visual_checks"],
            t["failed_checks"],
            t["skipped_checks"],
            t["errors"],
            data["run_dir"],
        )
    )
