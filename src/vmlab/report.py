"""Write a Run folder's reports: report.json, junit.xml and summary.md."""

import json
import xml.etree.ElementTree as ET

from vmlab.scenario import VISUAL

STATUS_ORDER = ("passed", "failed", "error")


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
    suite = ET.Element(
        "testsuite",
        name="vmlab.%s" % report["lab"],
        tests=str(t["checks"] + t["errors"]),
        failures=str(t["failed_checks"]),
        errors=str(t["errors"]),
        time="%.3f" % report["duration_s"],
        timestamp=report["started_at"],
    )
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
    ]
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
        for shot in s["screenshots"]:
            lines.append("- screenshot: [%s](%s)" % (shot, shot))
    return "\n".join(lines) + "\n"
