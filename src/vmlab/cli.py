"""Command-line entry point.

Exit codes: 0 success, 1 a Check failed or a Run errored, 2 usage or config error.
"""

import argparse
import json
import os
import sys

from vmlab import __version__, config, doctor, runner, vendoring
from vmlab.config import ConfigError
from vmlab.home import StartedGuests
from vmlab.providers import provider_for
from vmlab.providers.base import GuestError

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vmlab", description="Test desktop apps inside Guests.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sub.add_parser("version", help="print the vmlab version")

    sub.add_parser("init", help="create .vmlab/ here: config template, scenarios, vendored vmlab.pyz")

    p = sub.add_parser("self-update", help="replace the project's vendored vmlab.pyz with a newer one")
    p.add_argument("--from", dest="source", metavar="PYZ", help="vmlab.pyz to vendor (default: the skill's copy)")

    p = sub.add_parser("run", help="run Scenarios on Labs and write reports")
    p.add_argument("scenarios", nargs="*", metavar="SCENARIO", help="Scenario names (default: all)")
    p.add_argument("--lab", action="append", dest="labs", metavar="LAB", help="Lab to run on (repeatable; default: all)")
    p.add_argument("--keep", action="store_true", help="leave Guests vmlab started running")
    p.add_argument("--fresh", action="store_true", help="restore Clean state before every Scenario")

    for name, help_text in (("up", "start Guests"), ("down", "stop Guests")):
        p = sub.add_parser(name, help=help_text + " (default: all Labs)")
        p.add_argument("labs", nargs="*", metavar="LAB")

    p = sub.add_parser("doctor", help="check Providers, Guests and Channels (default: all Labs)")
    p.add_argument("labs", nargs="*", metavar="LAB")
    p.add_argument("--json", action="store_true", help="print JSON")

    p = sub.add_parser("status", help="show whether each Lab's Guest is running")
    p.add_argument("--json", action="store_true", help="print JSON")

    args = parser.parse_args(argv)
    if args.command == "version":
        print("vmlab %s" % __version__)
        return EXIT_OK

    try:
        if args.command == "init":
            vendoring.init(os.getcwd(), out=print)
            return EXIT_OK
        if args.command == "self-update":
            vendoring.self_update(os.getcwd(), args.source, out=print)
            return EXIT_OK
        project = config.load(os.getcwd())
        if args.command == "run":
            return _run(project, args)
        if args.command in ("up", "down"):
            return _up_down(project, args.command, args.labs)
        if args.command == "status":
            return _status(project, args.json)
        if args.command == "doctor":
            return _doctor(project, args.labs, args.json)
    except ConfigError as exc:
        print("vmlab: error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    except GuestError as exc:
        print("vmlab: error: %s" % exc, file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def _run(project, args):
    reports = runner.run(
        project, args.labs, args.scenarios, out=print, keep=args.keep, fresh=args.fresh, stop_command=_prog() + " down"
    )
    return EXIT_OK if all(r["status"] == "passed" for r in reports) else EXIT_FAILED


def _up_down(project, command, names):
    started_guests = StartedGuests()
    for lab in project.select_labs(names):
        provider = provider_for(project, lab)
        getattr(provider, command)()
        started_guests.discard(runner.guest_key(provider))  # the user now owns (or stopped) it
        print("%s %s" % (lab.name, "running" if command == "up" else "stopped"))
    return EXIT_OK


def _doctor(project, names, as_json):
    findings = doctor.diagnose(project, project.select_labs(names))
    if as_json:
        print(json.dumps(findings, indent=2))
    else:
        doctor.render(findings, print)
    return EXIT_FAILED if doctor.failed(findings) else EXIT_OK


def _status(project, as_json):
    rows = []
    for lab in project.labs.values():
        provider = provider_for(project, lab)
        rows.append(
            {
                "lab": lab.name,
                "provider": lab.provider,
                "os": lab.os,
                "arch": lab.arch,
                "guest": provider.guest_id,
                "running": provider.is_running(),
            }
        )
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            state = "running" if r["running"] else "stopped"
            print("%-12s %-8s %s/%s via %s (%s)" % (r["lab"], state, r["os"], r["arch"], r["provider"], r["guest"]))
    return EXIT_OK


def _prog():
    """How the user invoked vmlab, for commands we print back."""
    argv0 = sys.argv[0]
    if argv0.endswith(".pyz"):
        return "python3 %s" % os.path.relpath(argv0)
    return "vmlab"


def _zipapp_main():
    raise SystemExit(main())
