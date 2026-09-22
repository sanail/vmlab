"""Command-line entry point.

Exit codes: 0 success, 1 a Check failed or a Run errored, 2 usage or config error.
"""

import argparse
import functools
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from vmlab import __version__, bases, config, doctor, runner, ui, vendoring
from vmlab.config import ConfigError, UsageError
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

    p = sub.add_parser("base", help="Base guests: provisioned Guests that Labs clone, shared by all projects")
    base_sub = p.add_subparsers(dest="base_command", metavar="BASE_COMMAND")
    base_sub.required = True
    base_sub.add_parser("list", help="list this Host's Base guests")
    p = base_sub.add_parser("create", help="create and provision a Base guest (idempotent)")
    p.add_argument("name", metavar="NAME", help="e.g. %s" % ", ".join(sorted(bases.CATALOG)))
    p.add_argument("--image", help="image to create it from (default: the known image for NAME)")
    p.add_argument("--yes", action="store_true", help="allow downloading the image without asking")
    p.add_argument("--reprovision", action="store_true", help="provision again even if it is ready")

    p = sub.add_parser("self-update", help="replace the project's vendored vmlab.pyz with a newer one")
    p.add_argument("--from", dest="source", metavar="PYZ", help="vmlab.pyz to vendor (default: the skill's copy)")

    p = sub.add_parser("run", help="run Scenarios on Labs and write reports")
    p.add_argument("scenarios", nargs="*", metavar="SCENARIO", help="Scenario names (default: all)")
    p.add_argument("--lab", action="append", dest="labs", metavar="LAB", help="Lab to run on (repeatable; default: all)")
    p.add_argument("--keep", action="store_true", help="leave Guests vmlab started running")
    p.add_argument("--fresh", action="store_true", help="restore Clean state before every Scenario")
    p.add_argument("--parallel", action="store_true", help="run Labs concurrently as free Host memory allows")

    p = sub.add_parser("deploy", help="build if stale, install and launch the app; Guests stay running (default: all Labs)")
    p.add_argument("labs", nargs="*", metavar="LAB")

    for name, help_text in (("up", "start Guests"), ("down", "stop Guests")):
        p = sub.add_parser(name, help=help_text + " (default: all Labs)")
        p.add_argument("labs", nargs="*", metavar="LAB")

    p = sub.add_parser("doctor", help="check Providers, Guests and Channels (default: all Labs)")
    p.add_argument("labs", nargs="*", metavar="LAB")
    p.add_argument("--json", action="store_true", help="print JSON")

    _ui_parser(sub)

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
        if args.command == "base":
            return _base(args)
        project = config.load(os.getcwd())
        if args.command == "run":
            return _run(project, args)
        if args.command == "deploy":
            runner.deploy(project, args.labs, out=print, stop_command=_prog() + " down")
            return EXIT_OK
        if args.command in ("up", "down"):
            return _up_down(project, args.command, args.labs)
        if args.command == "status":
            return _status(project, args.json)
        if args.command == "doctor":
            return _doctor(project, args.labs, args.json)
        if args.command == "ui":
            return _ui(project, args)
    except (ConfigError, UsageError) as exc:
        print("vmlab: error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    except GuestError as exc:
        print("vmlab: error: %s" % exc, file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def _run(project, args):
    reports = runner.run(
        project, args.labs, args.scenarios, out=print, keep=args.keep, fresh=args.fresh, parallel=args.parallel, stop_command=_prog() + " down"
    )
    return EXIT_OK if all(r["status"] == "passed" for r in reports) else EXIT_FAILED


def _base(args):
    if args.base_command == "list":
        bases.render(print)
        return EXIT_OK
    progress = functools.partial(print, flush=True)  # it takes minutes: show each step as it happens
    bases.create(args.name, args.image, confirm=lambda question: args.yes or _ask(question), reprovision=args.reprovision, out=progress)
    return EXIT_OK


def _ask(question):
    """A yes/no question on the terminal; no terminal means no."""
    if not sys.stdin.isatty():
        return False
    return input("%s [y/N] " % question).strip().lower() in ("y", "yes")


def _ui_parser(sub):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lab", help="the Lab whose Guest to use (needed when the project has several)")
    element = argparse.ArgumentParser(add_help=False)
    element.add_argument("--text", help="name, value or description: exact matches win, else substring")
    element.add_argument("--role", help="cross-OS role (button, textfield, window, ...) or native role")
    element.add_argument("--app", help="only inside this application")

    p = sub.add_parser("ui", help="read and drive a running Guest's UI; prints JSON (the same shapes on every OS)")
    ui_sub = p.add_subparsers(dest="ui_command", metavar="UI_COMMAND")
    ui_sub.required = True
    p = ui_sub.add_parser("tree", parents=[common], help="the accessibility tree of the desktop or one app")
    p.add_argument("--app")
    ui_sub.add_parser("find", parents=[common, element], help="elements by text and/or role")
    p = ui_sub.add_parser("click", parents=[common, element], help="click an element's middle (or --at X Y)")
    p.add_argument("--index", type=int, default=0, help="which match to click (default: the first)")
    p.add_argument("--at", nargs=2, type=int, metavar=("X", "Y"), help="click this screen point instead")
    p = ui_sub.add_parser("press", parents=[common], help="press a key chord, e.g. cmd+shift+space")
    p.add_argument("chord")
    p = ui_sub.add_parser("type", parents=[common], help="type text into whatever has focus")
    p.add_argument("text")
    p = ui_sub.add_parser("wait-for", parents=[common, element], help="wait for one condition; exit 1 if it is not met in time")
    p.add_argument("--gone", action="store_true", help="wait for the element to disappear")
    p.add_argument("--process", help="a process of this name runs")
    p.add_argument("--file", help="this Guest path exists (~ is the Guest user's home)")
    p.add_argument("--log", help="this Guest file has a line matching --pattern")
    p.add_argument("--pattern", help="a Python regular expression")
    p.add_argument("--timeout", type=float, help="seconds (default: the Lab's step_timeout)")
    p = ui_sub.add_parser("focus", parents=[common], help="bring a running app (and one of its windows) to the front")
    p.add_argument("--app", required=True)
    p.add_argument("--window", help="raise the first window whose title contains this")
    p = ui_sub.add_parser("clipboard", parents=[common], help="read the clipboard (or --set it)")
    p.add_argument("--set", metavar="TEXT")
    p = ui_sub.add_parser("stage-text", parents=[common], help="open text in a third-party editor, select it all, then press --then; one Guest call")
    p.add_argument("text")
    p.add_argument("--app", help="the editor (default: TextEdit, Notepad or gedit)")
    p.add_argument("--then", metavar="CHORD", help="chord to press once the text is selected")
    p = ui_sub.add_parser("screenshot", parents=[common], help="save a PNG of the Guest's screen")
    p.add_argument("--out", help="where to save it (default: .vmlab/runs/<time>-<lab>-screenshot.png)")


def _ui(project, args):
    if args.lab:
        lab = project.lab(args.lab)
    elif len(project.labs) == 1:
        [lab] = project.labs.values()
    else:
        raise UsageError("the project has several Labs; pick one with --lab (%s)" % ", ".join(project.labs))
    provider = provider_for(project, lab)
    if not provider.is_running():
        raise GuestError("Guest %s is not running" % lab.name, "vmlab up %s   (or vmlab deploy %s)" % (lab.name, lab.name))
    contract = ui.UI(provider, lambda doing: lab.step_timeout)
    c = args.ui_command
    if c == "tree":
        result = contract.tree(args.app)
    elif c == "find":
        result = contract.find(ui.Query(args.text, args.role, args.app))
    elif c == "click":
        at = tuple(args.at) if args.at else None
        result = contract.click(ui.Query(args.text, args.role, args.app), index=args.index, at=at)
    elif c == "press":
        result = contract.press(args.chord)
    elif c == "type":
        result = contract.type(args.text)
    elif c == "wait-for":
        condition = ui.condition(args.text, args.role, args.app, args.gone, args.process, args.file, args.log, args.pattern)
        result = contract.wait_for(condition, timeout=args.timeout)
    elif c == "clipboard":
        result = contract.clipboard(set=args.set)
    elif c == "focus":
        result = contract.focus(args.app, window=args.window)
    elif c == "stage-text":
        result = contract.stage_text(args.text, app=args.app, then=args.then)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = Path(args.out) if args.out else project.runs_dir / ("%s-%s-screenshot.png" % (stamp, lab.name))
        dest.parent.mkdir(parents=True, exist_ok=True)
        provider.screenshot(dest)
        result = {"path": str(dest.resolve())}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return EXIT_FAILED if c == "wait-for" and not result["met"] else EXIT_OK


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
