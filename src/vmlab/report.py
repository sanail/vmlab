"""Write a Run folder's reports: report.json, junit.xml and summary.md."""

import json
import shlex
import xml.etree.ElementTree as ET

from vmlab.scenario import BY_ITSELF, BY_SCENARIO, OUTPUT_TAIL, STILL_RUNNING, VISUAL, WITH_RUN

STATUS_ORDER = ("passed", "failed", "error")  # of Scenarios; a Lab's Run may also be "skipped"
# How a spawned process ended: its report entry's "ended".
ENDED = {
    BY_SCENARIO: "stopped by the Scenario",
    WITH_RUN: "stopped at the end of the Run",
    BY_ITSELF: "had exited by itself",
    STILL_RUNNING: "still running: vmlab could not stop it",
}


def overall_status(scenarios):
    return max((s["status"] for s in scenarios), key=STATUS_ORDER.index, default="error")


def write(run_dir, report):
    (run_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _write_junit(run_dir / "junit.xml", report)
    (run_dir / "summary.md").write_text(_markdown(report), encoding="utf-8")


def totals(report):
    checks = [c for s in report["scenarios"] for c in s["checks"]]
    return {
        "scenarios": len(report["scenarios"]),
        "checks": len(checks),
        "failed_checks": sum(not c["passed"] for c in checks),
        "visual_checks": sum(c["kind"] == VISUAL for c in checks),
        "errors": sum(s["status"] == "error" for s in report["scenarios"]) + bool(report["error"]),
    }


def _write_junit(path, report):
    t = report["totals"]
    skipped = int(report["status"] == "skipped")
    suite = ET.Element(
        "testsuite",
        name="vmlab.%s" % report["lab"],
        tests=str(t["checks"] + t["errors"] + skipped),
        failures=str(t["failed_checks"]),
        errors=str(t["errors"]),
        skipped=str(skipped),
        time="%.3f" % report["duration_s"],
        timestamp=report["started_at"],
    )
    if skipped:
        case = ET.SubElement(suite, "testcase", classname=report["lab"], name="Architecture")
        ET.SubElement(case, "skipped", message=report["coverage"]["detail"])
    if report["error"]:
        case = ET.SubElement(suite, "testcase", classname=report["lab"], name="Guest setup")
        error = ET.SubElement(case, "error", message=report["error"].splitlines()[0])
        error.text = report["error"]
    for s in report["scenarios"]:
        classname = "%s.%s" % (report["lab"], s["name"])
        for c in s["checks"]:
            case = ET.SubElement(suite, "testcase", classname=classname, name=c["name"])
            if c["kind"] == VISUAL:
                ET.SubElement(ET.SubElement(case, "properties"), "property", name="kind", value=VISUAL)
            if not c["passed"]:
                failure = ET.SubElement(case, "failure", message=c["detail"] or "Check failed")
                failure.text = _evidence(s)
        if s["status"] == "error":
            case = ET.SubElement(suite, "testcase", classname=classname, name=s["name"])
            error = ET.SubElement(case, "error", message=s["error"].splitlines()[-1])
            error.text = s["error"]
    ET.ElementTree(suite).write(str(path), encoding="utf-8", xml_declaration=True)


def command_line(argv):
    return " ".join(shlex.quote(a) for a in argv)


def still_running(lab, scenario, entry):
    """The warning about a spawned process vmlab could not stop (its report entry)."""
    return "warning: %s/%s: spawned `%s` (pid %s) is still running in the Guest: %s" % (
        lab, scenario, command_line(entry["argv"]), entry["pid"], entry["stop_error"].splitlines()[0])


def _evidence(scenario):
    return "\n".join(["evidence:"] + scenario["screenshots"]) if scenario["screenshots"] else ""


def _markdown(report):
    t = report["totals"]
    lines = [
        "# vmlab Run: %s" % report["lab"],
        "",
        "**%s** · %s/%s via %s · %d Scenario(s), %d Check(s) (%d visual, unverified), %d failed, %d error(s) · started %s"
        % (
            report["status"].upper(),
            report["os"],
            report["arch"],
            report["provider"],
            t["scenarios"],
            t["checks"],
            t["visual_checks"],
            t["failed_checks"],
            t["errors"],
            report["started_at"],
        ),
        "",
        "Architecture: %s" % report["coverage"]["detail"],
    ]
    for warning in report["warnings"]:
        lines += ["", "> **warning:** %s" % warning]
    if report["deploy"]:
        d = report["deploy"]
        lines += ["", "Build artifact: %s (%s)" % (d["artifact"], "rebuilt" if d["built"] else "up to date")]
    if report["error"]:
        lines += ["", "```", report["error"], "```"]
    for s in report["scenarios"]:
        lines += ["", "## %s: %s" % (s["name"], s["status"]), ""]
        if s["channels"]:
            lines += ["Channels: %s" % ", ".join("%s (%d calls)" % kv for kv in sorted(s["channels"].items())), ""]
        for c in s["checks"]:
            mark = "PASS" if c["passed"] else "FAIL"
            visual = " (%s)" % VISUAL if c["kind"] == VISUAL else ""
            lines.append("- %s %s%s%s" % (mark, c["name"], visual, ": %s" % c["detail"] if c["detail"] else ""))
        for f in s["fallbacks"]:
            lines.append("- Channel %s failed (%s); %s served %s" % (f["from"], f["reason"], f["to"], f["argv"]))
        if s["error"]:
            lines += ["", "```", s["error"], "```"]
        for p in s["spawned"]:
            lines += ["", "- spawned `%s` (pid %s, output in %s): %s" % (command_line(p["argv"]), p["pid"], p["log"], ENDED[p["ended"]])]
            if p.get("stop_error"):
                lines.append("  %s" % p["stop_error"].splitlines()[0])
            if "output_tail" in p:
                if p["output_tail"] is None:
                    lines.append("  output unreadable: %s" % p["output_error"].splitlines()[0])
                else:
                    lines += ["", "  Its output%s:" % (" (the end)" if len(p["output_tail"]) >= OUTPUT_TAIL else ""), "", "  ```"]
                    lines += ["  " + line for line in p["output_tail"].splitlines()] + ["  ```"]
        for shot in s["screenshots"]:
            lines.append("- screenshot: [%s](%s)" % (shot, shot))
    return "\n".join(lines) + "\n"
