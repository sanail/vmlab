"""Command-line entry point.

Exit codes: 0 success, 1 a Check failed or a Run errored, 2 usage or config error.
"""

import argparse
import json
import os
import sys

from vmlab import __version__, config, runner
from vmlab.config import ConfigError
from vmlab.providers import provider_for

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vmlab", description="Test desktop apps inside Guests.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sub.add_parser("version", help="print the vmlab version")

    p = sub.add_parser("run", help="run Scenarios on Labs and write reports")
    p.add_argument("scenarios", nargs="*", metavar="SCENARIO", help="Scenario names (default: all)")
    p.add_argument("--lab", action="append", dest="labs", metavar="LAB", help="Lab to run on (repeatable; default: all)")

    for name, help_text in (("up", "start Guests"), ("down", "stop Guests")):
        p = sub.add_parser(name, help=help_text + " (default: all Labs)")
        p.add_argument("labs", nargs="*", metavar="LAB")

    p = sub.add_parser("status", help="show whether each Lab's Guest is running")
    p.add_argument("--json", action="store_true", help="print JSON")

    args = parser.parse_args(argv)
    if args.command == "version":
        print("vmlab %s" % __version__)
        return EXIT_OK

    try:
        project = config.load(os.getcwd())
        if args.command == "run":
            return _run(project, args)
        if args.command in ("up", "down"):
            return _up_down(project, args.command, args.labs)
        if args.command == "status":
            return _status(project, args.json)
    except ConfigError as exc:
        print("vmlab: config error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


def _run(project, args):
    reports = runner.run(project, args.labs, args.scenarios, out=print)
    return EXIT_OK if all(r["status"] == "passed" for r in reports) else EXIT_FAILED


def _up_down(project, command, names):
    for lab in project.select_labs(names):
        getattr(provider_for(project, lab), command)()
        print("%s %s" % (lab.name, "running" if command == "up" else "stopped"))
    return EXIT_OK


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


def _zipapp_main():
    raise SystemExit(main())
