"""Command-line entry point.

Exit codes: 0 success, 1 a Check failed or a Run errored, 2 usage or config error.
"""

import argparse
import functools
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from vmlab import __version__, arch, bases, config, doctor, runner, ui, vendoring
from vmlab.config import ConfigError, UsageError
from vmlab.home import StartedGuests
from vmlab.providers import provider_for
from vmlab.providers.base import GuestError

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
QUIET_HELP = "print no step lines (each step as it starts, and its time), only the results"


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
    p = base_sub.add_parser("create", help="create and provision a Base guest (idempotent); shuts a running Fusion one down")
    p.add_argument("name", metavar="NAME", help="e.g. %s" % ", ".join(sorted(bases.CATALOG)))
    p.add_argument("--image", help="image to create it from (default: the known image for NAME); windows-*: the Fusion VM to copy")
    p.add_argument("--yes", action="store_true", help="answer yes: download the image, delete earlier snapshots no Lab needs (Fusion)")
    p.add_argument("--reprovision", action="store_true", help="provision again even if it is ready")

    p = sub.add_parser("clean", help="delete what no known Lab needs: orphaned clones, stray files, earlier provisionings' snapshots, unused Base guests")
    p.add_argument("--yes", action="store_true", help="delete without asking")
    p.add_argument("--bases", action="store_true", help="also delete unused Base guests (re-creating one downloads its image)")

    p = sub.add_parser("self-update", help="replace the project's vendored vmlab.pyz with a newer one")
    p.add_argument("--from", dest="source", metavar="PYZ", help="vmlab.pyz to vendor (default: the skill's copy)")

    p = sub.add_parser("run", help="run Scenarios on Labs and write reports")
    p.add_argument("scenarios", nargs="*", metavar="SCENARIO", help="Scenario names (default: all)")
    p.add_argument("--lab", action="append", dest="labs", metavar="LAB", help="Lab to run on (repeatable; default: all)")
    p.add_argument("--keep", action="store_true", help="leave Guests vmlab started running")
    p.add_argument("--fresh", action="store_true", help="restore Clean state before every Scenario")
    p.add_argument("--parallel", action="store_true", help="run Labs concurrently as free Host memory allows")
    p.add_argument("--quiet", action="store_true", help=QUIET_HELP)

    p = sub.add_parser("deploy", help="build if stale, install and launch the app; Guests stay running (default: all Labs)")
    p.add_argument("labs", nargs="*", metavar="LAB")
    p.add_argument("--quiet", action="store_true", help=QUIET_HELP)

    for name, help_text in (("up", "start Guests"), ("down", "stop Guests")):
        p = sub.add_parser(name, help=help_text + " (default: all Labs)")
        p.add_argument("labs", nargs="*", metavar="LAB")

    p = sub.add_parser("doctor", help="check the Host, Providers, Base guests, Guests, Channels and UI helpers (default: all Labs)")
    p.add_argument("labs", nargs="*", metavar="LAB")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--bench", action="store_true", help="also time each Channel of running Guests")
    p.add_argument("--calls", type=int, default=doctor.BENCH_CALLS, metavar="N", help="calls per Channel for --bench (default: %(default)s)")

    p = sub.add_parser("exec", help="run one command in a running Guest; its output and exit code are vmlab's")
    p.add_argument("--lab", help="the Lab whose Guest to use (needed when the project has several)")
    p.add_argument("--timeout", type=float, help="seconds (default: the Lab's step_timeout)")
    p.add_argument("argv", nargs=argparse.REMAINDER, metavar="-- COMMAND ...")

    p = sub.add_parser("put", help="write a file into a running Guest from piped stdin (or --from); prints its absolute Guest path")
    p.add_argument("guest_path", metavar="GUEST_PATH", help="its folders are made; ~ is the Guest user's home, %%VARS%% expand on Windows")
    p.add_argument("--from", dest="source", metavar="HOSTFILE", help="copy this Host file instead of reading stdin")
    p.add_argument("--lab", help="the Lab whose Guest to use (needed when the project has several)")
    p = sub.add_parser("get", help="print a file of a running Guest to stdout, byte for byte")
    p.add_argument("guest_path", metavar="GUEST_PATH", help="~ is the Guest user's home, %%VARS%% expand on Windows")
    p.add_argument("--lab", help="the Lab whose Guest to use (needed when the project has several)")

    _ui_parser(sub)

    p = sub.add_parser("status", help="show whether each Lab's Guest is running")
    p.add_argument("--json", action="store_true", help="print JSON")

    args = parser.parse_args(_exec_dashes(sys.argv[1:] if argv is None else list(argv)))
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
        if args.command == "clean":
            return _clean(args)
        if args.command == "doctor":
            if args.calls != doctor.BENCH_CALLS and not args.bench:
                raise UsageError("--calls goes with --bench")
            if args.calls < 1:
                raise UsageError("--calls must be at least 1")
            if not args.labs and not config.has_config(os.getcwd()):
                if args.bench:
                    raise UsageError("--bench times the Channels of a project's running Guests; run it inside a project")
                return _doctor(None, [], args.json, 0)  # before `vmlab init`: what this Host can test
            project = config.load(os.getcwd())
            return _doctor(project, project.select_labs(args.labs), args.json, args.calls if args.bench else 0)
        project = config.load(os.getcwd())
        if args.command == "run":
            return _run(project, args)
        if args.command == "deploy":
            runner.deploy(project, args.labs, out=_flushed, stop_command=_prog() + " down", steps_out=_steps(args))
            return EXIT_OK
        if args.command in ("up", "down"):
            return _up_down(project, args.command, args.labs)
        if args.command == "status":
            return _status(project, args.json)
        if args.command == "ui":
            return _ui(project, args)
        if args.command == "exec":
            return _exec(project, args)
        if args.command == "put":
            return _put(project, args)
        if args.command == "get":
            return _get(project, args)
    except (ConfigError, UsageError) as exc:
        print("vmlab: error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    except GuestError as exc:
        print("vmlab: error: %s" % exc, file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def _exec_dashes(argv):
    """argv without the -- in `ui wait-for ... --exec -- COMMAND`: argparse keeps a command
    after an option's -- only from Python 3.12 on."""
    if argv[:2] == ["ui", "wait-for"] and "--exec" in argv:
        at = argv.index("--exec")
        if argv[at + 1 : at + 2] == ["--"]:
            return argv[: at + 1] + argv[at + 2 :]
    return argv


def _run(project, args):
    reports = runner.run(
        project, args.labs, args.scenarios, out=_flushed, keep=args.keep, fresh=args.fresh, parallel=args.parallel,
        stop_command=_prog() + " down", steps_out=_steps(args),
    )  # fmt: skip
    # a skipped Lab (arch not covered on this Host) warned and wrote its reports; it fails nothing
    return EXIT_OK if all(r["status"] in ("passed", "skipped") for r in reports) else EXIT_FAILED


def _flushed(line):
    """print, flushed at once: `run` and `deploy` take minutes, and their reader (often an agent,
    with no TTY) must see each line as it happens."""
    print(line, flush=True)


def _steps(args):
    """Where `run` and `deploy` print their step lines: stdout, or nowhere with --quiet."""
    return None if args.quiet else _flushed


def _base(args):
    if args.base_command == "list":
        bases.render(print)
        return EXIT_OK
    progress = functools.partial(print, flush=True)  # it takes minutes: show each step as it happens
    bases.create(args.name, args.image, prompt=Terminal(args.yes), reprovision=args.reprovision, out=progress)
    return EXIT_OK


class Terminal:
    """Questions to the person at the terminal. Without one, every answer is no (or none)."""

    def __init__(self, yes=False):
        self.yes = yes  # --yes: confirmations are answered yes

    @property
    def interactive(self):
        return sys.stdin.isatty()

    def confirm(self, question):
        return self.yes or _ask(question)

    def text(self, question):
        return input(question) if sys.stdin.isatty() else None

    def secret(self, question):
        import getpass

        return getpass.getpass(question) if sys.stdin.isatty() else None

    def pause(self, message):
        """Wait for Enter; False when there is no terminal to wait at."""
        if not sys.stdin.isatty():
            return False
        input("%s " % message)
        return True


def _clean(args):
    from vmlab import clean

    in_use = set()
    try:  # run inside a project, its Labs' Base guests count as used even before their first clone
        in_use = clean.bases_in_use(config.load(os.getcwd()))
    except ConfigError:
        pass
    found, warnings = clean.leftovers(in_use)
    for warning in warnings:
        print("warning: %s" % warning)
    if not found:
        print("nothing to clean")
        return EXIT_OK
    deletable = []
    for item in found:
        if item.kept:
            note = "kept: %s" % item.kept
        elif item.running and not item.stops_first:
            note = "running: left alone (%s)" % item.stop_hint
        elif item.needs_bases and not args.bases:
            note = "kept: pass --bases to delete it"
        else:
            note = "running: to stop and delete" if item.running else "to delete"
            deletable.append(item)
        print("%-6s %s\n       %s; %s" % (item.kind, item.name, item.reason, note))
    if not deletable:
        return EXIT_OK
    if not (args.yes or _ask("Delete %d of them?" % len(deletable))):
        print("Nothing deleted. Re-run with --yes to delete them, or in a terminal to be asked.")
        return EXIT_OK
    for item in deletable:
        item.remove()
        print("deleted %s %s" % (item.kind, item.name))
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
    p.add_argument("--timeout", type=float, help="seconds to wait for the element to be there and uncovered (default: fail at once; --at ignores it)")
    p = ui_sub.add_parser("press", parents=[common], help="press a key chord, e.g. cmd+shift+space")
    p.add_argument("chord")
    p = ui_sub.add_parser("type", parents=[common], help="type text into whatever has focus")
    p.add_argument("text")
    p = ui_sub.add_parser("wait-for", parents=[common, element], help="wait for one condition; exit 1 if it is not met in time")
    p.add_argument("--gone", action="store_true", help="wait for the condition to stop holding (an element, process or file gone, ...)")
    p.add_argument("--process", help="a process of this name runs")
    p.add_argument("--file", help="this Guest path exists (~ is the Guest user's home)")
    p.add_argument("--log", help="this Guest file has a line matching --pattern")
    p.add_argument("--pattern", help="a Python regular expression, for --log or --exec")
    p.add_argument("--notification", metavar="PATTERN", help="a Notification whose title or body matches this Python regular expression is posted (--app: by that app)")
    p.add_argument("--since", metavar="TIME", help="for --notification: count those posted at or after this Guest time, ISO 8601 (default: the call's start)")
    p.add_argument("--tray", metavar="APP", help="this app's Tray icon is there, as `ui tray` finds it (its menu is not opened)")
    p.add_argument("--timeout", type=float, help="seconds (default: the Lab's step_timeout)")
    # Last, as it takes everything after it: the command's own options stay its own.
    p.add_argument(
        "--exec", nargs=argparse.REMAINDER, metavar="ARG",
        help="this command exits 0 (with --pattern: its output matches); goes last: --exec [--] COMMAND ...",
    )  # fmt: skip
    p = ui_sub.add_parser("tray", parents=[common], help="read an app's Tray menu, or --choose an item of it")
    p.add_argument("--app", required=True, help="the app whose Tray icon to use")
    p.add_argument("--choose", action="append", metavar="LABEL", help="the item to choose; repeat it for each submenu level: --choose Settings --choose Advanced")
    p.add_argument("--timeout", type=float, help="seconds to wait for the Tray icon to appear (default: fail at once)")
    p = ui_sub.add_parser("notifications", parents=[common], help="the Notifications the Guest's OS recorded, oldest first")
    p.add_argument("--app", help="only this app's: the OS's id for it (macOS bundle id, Windows AppUserModelID, Linux app name); default: the Lab's app.notification_id, else every app's")
    p.add_argument("--text", metavar="PATTERN", help="only those whose title or body matches this Python regular expression")
    p.add_argument("--since", metavar="TIME", help="only those posted at or after this Guest time, ISO 8601 (default: all)")
    p = ui_sub.add_parser("focus", parents=[common], help="bring a running app (and one of its windows) to the front")
    p.add_argument("--app", required=True)
    p.add_argument("--window", help="raise the first window whose title contains this")
    p = ui_sub.add_parser("clipboard", parents=[common], help="read the clipboard (or --set it)")
    p.add_argument("--set", metavar="TEXT")
    p = ui_sub.add_parser("stage-text", parents=[common], help="open text in a third-party editor, select it all, then press --then; one Guest call")
    p.add_argument("text")
    p.add_argument("--app", help="the editor (default: TextEdit, Notepad or gnome-text-editor)")
    p.add_argument("--then", metavar="CHORD", help="chord to press once the text is selected")
    p = ui_sub.add_parser("close-staged", parents=[common], help="save and close the Staged document a stage-text opened")
    p.add_argument("--file", required=True, metavar="PATH", help='the "file" stage-text printed')
    p.add_argument("--app", help="the editor it is open in (default: TextEdit, Notepad or gnome-text-editor)")
    p = ui_sub.add_parser("screenshot", parents=[common], help="save a PNG of the Guest's screen")
    p.add_argument("--out", help="where to save it (default: .vmlab/runs/<time>-<lab>-screenshot.png)")


def _running_guest(project, lab_name):
    """(lab, provider) for --lab, or the only Lab; its Guest must be running."""
    if lab_name:
        lab = project.lab(lab_name)
    elif len(project.labs) == 1:
        [lab] = project.labs.values()
    else:
        raise UsageError("the project has several Labs; pick one with --lab (%s)" % ", ".join(project.labs))
    provider = provider_for(project, lab)
    if not provider.is_running():
        raise GuestError("Guest %s is not running" % lab.name, "vmlab up %s   (or vmlab deploy %s)" % (lab.name, lab.name))
    return lab, provider


def _exec(project, args):
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        raise UsageError("no command given; e.g. vmlab exec -- cat ~/app.log")
    if args.timeout is not None and args.timeout <= 0:
        raise UsageError("--timeout must be more than 0 seconds")
    lab, provider = _running_guest(project, args.lab)
    result = provider.exec(argv, timeout=lab.step_timeout if args.timeout is None else args.timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.code


def _put(project, args):
    if args.source is None and sys.stdin.isatty():
        raise UsageError(
            "no content for %s: pipe it in or name a Host file, e.g. `echo hi | vmlab put %s` or `vmlab put %s --from notes.txt`"
            % (args.guest_path, args.guest_path, args.guest_path)
        )
    lab, provider = _running_guest(project, args.lab)
    if args.source is None:
        data = sys.stdin.buffer.read()  # all of it; nothing piped in (< /dev/null) makes an empty file
    else:
        try:
            data = Path(args.source).read_bytes()
        except OSError as exc:
            raise UsageError("--from %s: %s" % (args.source, exc.strerror or exc))
    print(provider.put_file(args.guest_path, data, lab.step_timeout))
    return EXIT_OK


def _get(project, args):
    lab, provider = _running_guest(project, args.lab)
    data = provider.read_file(args.guest_path, lab.step_timeout)
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return EXIT_OK


def _ui(project, args):
    lab, provider = _running_guest(project, args.lab)
    contract = ui.UI(provider, lambda doing: lab.step_timeout)
    c = args.ui_command
    if c == "tree":
        result = contract.tree(args.app)
    elif c == "find":
        result = contract.find(ui.Query(args.text, args.role, args.app))
    elif c == "click":
        at = tuple(args.at) if args.at else None
        result = contract.click(ui.Query(args.text, args.role, args.app), index=args.index, at=at, timeout=args.timeout)
    elif c == "press":
        result = contract.press(args.chord)
    elif c == "type":
        result = contract.type(args.text)
    elif c == "wait-for":
        app, since = args.app, args.since
        if args.notification is not None:
            app = app or lab.app.notification_id
            since = since or ui.HostTime(time.time())
        condition = ui.condition(
            text=args.text, role=args.role, app=app, gone=args.gone, process=args.process, file=args.file,
            log=args.log, pattern=args.pattern, exec=args.exec, notification=args.notification, since=since, tray=args.tray,
        )  # fmt: skip
        result = contract.wait_for(condition, timeout=args.timeout)
    elif c == "notifications":
        result = contract.notifications(args.app or lab.app.notification_id, args.text, args.since)
    elif c == "tray":
        try:
            result = contract.tray(args.app, choose=args.choose, timeout=args.timeout)
        except ui.TrayError as exc:
            print(json.dumps(exc.result, indent=2, ensure_ascii=False))
            raise
    elif c == "clipboard":
        result = contract.clipboard(set=args.set)
    elif c == "focus":
        result = contract.focus(args.app, window=args.window)
    elif c == "stage-text":
        result = contract.stage_text(args.text, app=args.app, then=args.then)
    elif c == "close-staged":
        result = contract.close_staged(args.file, app=args.app)
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
        warning = arch.warning(lab) if command == "up" else None
        if warning:
            print("warning: %s: %s" % (lab.name, warning))
        provider = provider_for(project, lab)
        getattr(provider, command)()
        started_guests.discard(runner.guest_key(provider))  # the user now owns (or stopped) it
        print("%s %s" % (lab.name, "running" if command == "up" else "stopped"))
    return EXIT_OK


def _doctor(project, labs, as_json, bench_calls):
    findings = doctor.diagnose(project, labs, bench_calls)
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
