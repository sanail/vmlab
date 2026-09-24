"""`vmlab doctor`: can this Host run each Lab, and what exactly is wrong if not?

Host first (hypervisors, vmlab's home, key and credentials, Fusion VMs with a
sound device, the Base guest registry), then per Lab: its arch, its Provider, what its Guest is made from
(Base guest, clone), and for a running Guest its Channels, the Provider's own
checks, screenshots and the UI helper. With bench, each Channel of a running
Guest is timed over several no-op calls.

Each finding is ok, info, warn or FAIL, with a fix for anything not ok. Only
FAIL makes doctor exit non-zero: a broken preferred Channel with a working
fallback still lets Runs pass, slower. A hung preferred Channel does not: a
call that times out never falls back (ADR 0003).
"""

import json
import platform
import shutil
import statistics
import struct
import tempfile
import time
from pathlib import Path

from vmlab import arch, bases, runner
from vmlab.config import host_arch
from vmlab.home import GuestLock, vmlab_home
from vmlab.memory import free_memory_gb
from vmlab.providers import PROVIDERS, fusion, provider_for
from vmlab.providers.base import FAIL, INFO, OK, WARN, GuestError, GuestTimeout

PROBE_TIMEOUT = 10
BENCH_CALLS = 10
SSH_TOOLS = ("ssh", "scp", "ssh-keygen")


def diagnose(project, labs, bench_calls=0):
    """Findings for the Host and each Lab; bench_calls > 0 also times each Channel that many times.

    Without a project (before `vmlab init`) only the Host is checked.
    """
    findings = _diagnose_host()
    if project is None:
        findings.append(_finding(None, "Project", INFO, "no .vmlab/vmlab.toml here or above", "run `vmlab init` in the project root, then declare its Labs"))
    for lab in labs:
        findings.extend(_diagnose_lab(provider_for(project, lab), lab, bench_calls))
    return findings


def failed(findings):
    return any(f["status"] == FAIL for f in findings)


def _finding(lab, check, status, detail, fix=None, **extra):
    return dict({"lab": lab, "check": check, "status": status, "detail": detail, "fix": fix}, **extra)


def _diagnose_host():
    findings = []

    def add(check, status, detail, fix=None):
        findings.append(_finding(None, check, status, detail, fix))

    add("Host", INFO, _host_detail())
    found = []
    for provider in PROVIDERS.values():
        if provider.HYPERVISOR and not provider.NOT_IMPLEMENTED:
            ok, detail, _ = provider.hypervisor()
            found.append(detail if ok else "%s not found" % provider.HYPERVISOR)
    add("Hypervisors", INFO, "; ".join(found))

    home = vmlab_home()
    if home.stat().st_mode & 0o077:
        add("vmlab home", WARN, "%s is open to other users; it holds vmlab's SSH key and Guest credentials" % home, "chmod 700 %s" % home)
    else:
        add("vmlab home", OK, str(home))

    missing = [tool for tool in SSH_TOOLS if not shutil.which(tool)]
    if missing:
        add("SSH client", WARN, "%s not found: ssh Channels cannot work" % ", ".join(missing), "install the OpenSSH client")
    else:
        add("SSH client", OK, ", ".join(SSH_TOOLS))

    key = home / "ssh" / "id_ed25519"
    if not key.exists():
        add("SSH key", INFO, "none yet; made when the first Base guest is created")
    elif key.stat().st_mode & 0o077:
        add("SSH key", WARN, "%s is readable by others, so ssh refuses to use it" % key, "chmod 600 %s" % key)
    else:
        add("SSH key", OK, str(key))

    credentials = fusion.credential_files()
    loose = [str(path) for path in credentials if path.stat().st_mode & 0o077]
    if loose:
        add("Guest credentials", WARN, "readable by others: %s" % ", ".join(loose), "chmod 600 %s" % " ".join(loose))
    elif credentials:
        add("Guest credentials", OK, "%d file(s), readable by you only" % len(credentials))
    for check, status, detail, fix in fusion.sound_findings():
        add(check, status, detail, fix)

    registry = bases.Registry()
    try:
        add("Base guest registry", OK, "%d Base guest(s): %s" % (len(registry.all()), registry.path))
    except GuestError as exc:
        add("Base guest registry", FAIL, exc.message, exc.fix)
    return findings


def _host_detail():
    system, machine = platform.system(), host_arch()
    if system != "Darwin":
        return "%s %s: no v1 Provider runs here; Labs need a Mac Host" % (system, machine)
    name = "macOS %s %s" % (platform.mac_ver()[0], machine)
    if machine == "arm64":
        return "%s: tests macOS (Tart), Linux and Windows (VMware Fusion) Guests" % name
    return "%s: tests Linux and Windows (VMware Fusion) Guests; macOS Guests need an Apple Silicon Mac" % name


def _diagnose_lab(provider, lab, bench_calls):
    findings = []

    def add(check, status, detail, fix=None, **extra):
        findings.append(_finding(lab.name, check, status, detail, fix, **extra))

    coverage = arch.coverage(lab)
    if coverage["mode"] == arch.UNCOVERED:  # nothing else matters for a Lab that cannot run here
        add("Architecture", WARN, coverage["detail"], arch.fix(lab))
        return findings
    add("Architecture", OK, coverage["detail"])
    present, detail, fix = provider.detect()
    add("Provider %s" % lab.provider, OK if present else FAIL, detail, fix)
    if not present:
        return findings
    try:
        checks = provider.diagnose()
    except GuestError as exc:
        checks = [("Provider %s" % lab.provider, FAIL, exc.message, exc.fix)]
    for check in checks:
        add(*check)
    if failed(findings):
        return findings  # the Guest cannot start

    holder = GuestLock(runner.guest_key(provider)).holder()
    if holder:
        add("Guest lock", WARN, "in use by another vmlab (%s)" % holder, "runs and deploys of Lab %s fail until it finishes" % lab.name)
    if not provider.is_running():
        _check_memory(lab, add)
        add("Guest", INFO, "stopped; Channels not checked", "vmlab up %s && vmlab doctor%s %s" % (lab.name, " --bench" if bench_calls else "", lab.name))
        return findings
    try:
        reachable = provider.is_reachable()
    except GuestError as exc:
        reachable = False
        add("Guest", FAIL, exc.message, exc.fix)
    else:
        if reachable:
            add("Guest", OK, "running (%s)" % provider.guest_id)
        else:
            add("Guest", FAIL, "running but not reachable", "wait for it to boot, or restart it: vmlab down %s && vmlab up %s" % (lab.name, lab.name))

    working, preferred_hangs = _check_channels(provider, lab, add)
    if not working:
        add("no Channel reaches the Guest", FAIL, "every Channel failed", "fix one of the Channels above")
        return findings
    if not reachable or preferred_hangs:
        return findings  # every further check would fail or time out the same way

    try:
        checks = provider.diagnose_guest()
    except GuestError as exc:
        checks = [("Guest", WARN, exc.message, exc.fix)]
    for check in checks:
        add(*check)
    _check_screenshot(provider, add)
    try:
        ok, detail = provider.ui_helper().describe(PROBE_TIMEOUT)
    except GuestError as exc:
        ok, detail = False, exc.message
    if ok is not None:
        fix = None if ok else (
            "restart the Guest (vmlab down %s && vmlab up %s), which gives the helper a fresh desktop session; "
            "if it still fails, re-provision the Lab's Base guest: vmlab base create NAME --reprovision (NAME from vmlab base list)" % (lab.name, lab.name)
        )
        add("UI helper", OK if ok else WARN, detail, fix)
    if bench_calls:
        _bench(provider, lab, bench_calls, add)
    return findings


def _check_memory(lab, add):
    free = free_memory_gb()
    if free is None:
        return
    if free < lab.memory_gb:
        add("Memory", WARN, "needs %s GB, %s GB free" % (_gb(lab.memory_gb), _gb(free)),
            "close apps or stop other Guests first; if the Guest needs less, lower labs.%s.memory_gb" % lab.name)  # fmt: skip
    else:
        add("Memory", OK, "needs %s GB, %s GB free" % (_gb(lab.memory_gb), _gb(free)))


def _gb(value):
    return ("%.1f" % value).rstrip("0").rstrip(".")


def _check_channels(provider, lab, add):
    """Probe each Channel once: (the names of those that work, whether the preferred one hangs)."""
    working, preferred_hangs = [], False
    for index, channel in enumerate(provider.channels()):
        check = "Channel %s" % channel.name
        started = time.time()
        try:
            result = channel.exec(provider.probe_argv(), PROBE_TIMEOUT, {})
        except GuestTimeout:
            restart = "restart the Guest: vmlab down %s && vmlab up %s" % (lab.name, lab.name)
            if index == 0:
                preferred_hangs = True
                add(check, FAIL, "hangs: a no-op did not finish within %ss; every call tries it first, times out and never falls back" % PROBE_TIMEOUT,
                    "%s; until it answers, list it last in labs.%s.%s.channels" % (restart, lab.name, lab.provider))  # fmt: skip
            else:
                add(check, WARN, "hangs: a no-op did not finish within %ss; calls that fall back to it time out" % PROBE_TIMEOUT, restart)
            continue
        except GuestError as exc:
            add(check, WARN, exc.message, exc.fix)
            continue
        ms = (time.time() - started) * 1000
        if result.ok:
            working.append(channel.name)
            add(check, OK, "%.0f ms" % ms)
        else:
            add(check, WARN, "probe %s exited %s: %s" % (result.argv, result.code, result.stderr.strip()), "check the Guest's shell and PATH")
    return working, preferred_hangs


def _check_screenshot(provider, add):
    with tempfile.TemporaryDirectory() as tmp:
        shot = Path(tmp) / "doctor.png"
        try:
            provider.screenshot(shot)
        except GuestError as exc:
            add("Screenshot", WARN, exc.message, exc.fix)
            return
        png = shot.read_bytes()
        if png[:8] != b"\x89PNG\r\n\x1a\n" or len(png) < 24:
            add("Screenshot", WARN, "the screenshot is not a PNG (%d bytes)" % len(png), "report it: the Provider wrote something else")
            return
        width, height = struct.unpack(">II", png[16:24])  # the IHDR chunk comes first
        add("Screenshot", OK, "%dx%d PNG" % (width, height))


def _bench(provider, lab, calls, add):
    """Time calls no-op calls over each Channel, after one call that warms it up (an SSH master
    connection, a helper sent once), and say whether the preferred Channel is the fastest."""
    medians = {}
    names = [channel.name for channel in provider.channels()]
    for channel in provider.channels():
        check = "Bench %s" % channel.name
        times = []
        try:
            for _ in range(calls + 1):
                started = time.perf_counter()
                result = channel.exec(provider.probe_argv(), PROBE_TIMEOUT, {})
                if not result.ok:
                    raise GuestError("the no-op exited %s: %s" % (result.code, result.stderr.strip()))
                times.append((time.perf_counter() - started) * 1000)
        except GuestError as exc:
            add(check, WARN, exc.message, exc.fix)
            continue
        times = times[1:]
        latency = {"calls": calls, "min": round(min(times)), "median": round(statistics.median(times)), "max": round(max(times))}
        medians[channel.name] = latency["median"]
        add(check, INFO, "median %(median)d ms (min %(min)d, max %(max)d; %(calls)d calls)" % latency, latency_ms=latency)
    preferred = names[0]
    if len(medians) < 2 or preferred not in medians:
        return
    fastest = min(medians, key=medians.get)
    if medians[fastest] >= medians[preferred]:
        add("Channel order", OK, "%s, the preferred Channel, is the fastest" % preferred)
    else:
        order = [fastest] + [name for name in names if name != fastest]
        add("Channel order", WARN, "%s is faster than %s, the preferred Channel (median %d ms against %d ms)" % (fastest, preferred, medians[fastest], medians[preferred]),
            "if it is as reliable, prefer it: channels = %s under [labs.%s.%s]" % (json.dumps(order), lab.name, lab.provider))  # fmt: skip


def render(findings, out):
    for f in findings:
        out("%-4s  %s: %s: %s" % (f["status"], f["lab"] or "Host", f["check"], f["detail"]))
        if f["fix"] and f["status"] != OK:
            out("      fix: %s" % f["fix"])
