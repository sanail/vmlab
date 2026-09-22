"""`vmlab doctor`: is each Lab's Provider present, its Guest reachable, and which Channels work?

Each finding is ok, warn or FAIL, with a fix for anything not ok. Only FAIL
makes doctor exit non-zero: a broken preferred Channel with a working fallback
still lets Runs pass, slower.
"""

import time

from vmlab.providers import provider_for
from vmlab.providers.base import GuestError

PROBE_TIMEOUT = 10
OK, WARN, FAIL, INFO = "ok", "warn", "FAIL", "info"


def diagnose(project, labs):
    findings = []
    for lab in labs:
        findings.extend(_diagnose_lab(provider_for(project, lab), lab))
    return findings


def failed(findings):
    return any(f["status"] == FAIL for f in findings)


def _finding(lab, check, status, detail, fix=None):
    return {"lab": lab.name, "check": check, "status": status, "detail": detail, "fix": fix}


def _diagnose_lab(provider, lab):
    present, detail, fix = provider.detect()
    findings = [_finding(lab, "Provider %s" % lab.provider, OK if present else FAIL, detail, fix)]
    if not present:
        return findings

    if not provider.is_running():
        return findings + [
            _finding(lab, "Guest", INFO, "stopped; Channels not checked", "vmlab up %s && vmlab doctor %s" % (lab.name, lab.name))
        ]
    if not provider.is_reachable():
        return findings + [
            _finding(lab, "Guest", FAIL, "running but not reachable", "wait for it to boot, or restart it: vmlab down %s && vmlab up %s" % (lab.name, lab.name))
        ]
    findings.append(_finding(lab, "Guest", OK, "running (%s)" % provider.guest_id))

    working = 0
    for channel in provider.channels():
        started = time.time()
        try:
            result = channel.exec(provider.probe_argv(), PROBE_TIMEOUT, {})
        except GuestError as exc:
            findings.append(_finding(lab, "Channel %s" % channel.name, WARN, exc.message, exc.fix))
            continue
        ms = (time.time() - started) * 1000
        if result.ok:
            working += 1
            findings.append(_finding(lab, "Channel %s" % channel.name, OK, "%.0f ms" % ms))
        else:
            findings.append(
                _finding(lab, "Channel %s" % channel.name, WARN, "probe %s exited %s: %s" % (result.argv, result.code, result.stderr.strip()), "check the Guest's shell and PATH")
            )
    if not working:
        findings.append(_finding(lab, "no Channel reaches the Guest", FAIL, "every Channel failed", "fix one of the Channels above"))
        return findings
    try:
        ok, detail = provider.ui_helper().describe(PROBE_TIMEOUT)
    except GuestError as exc:
        ok, detail = False, exc.message
    if ok is not None:
        fix = None if ok else "re-provision the Lab's Base guest: vmlab base create NAME --reprovision (NAME from vmlab base list)"
        findings.append(_finding(lab, "UI helper", OK if ok else WARN, detail, fix))
    return findings


def render(findings, out):
    for f in findings:
        out("%-4s  %s: %s: %s" % (f["status"], f["lab"], f["check"], f["detail"]))
        if f["fix"] and f["status"] != OK:
            out("      fix: %s" % f["fix"])
