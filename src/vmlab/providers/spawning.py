"""Background processes in a Guest (g.spawn): start one detached, ask if it runs, stop it.

A spawned process writes its stdout and stderr, together, to a log file in the
Guest. Stopping ends what it started too:

- POSIX: it leads its own process group (setsid, or perl's where there is no
  setsid(1), as on macOS); stop sends the group SIGTERM, then SIGKILL after
  TERM_SECONDS. Its start time, as ps prints it, guards against a reused pid.
- Windows: guest/windows/vmlab-spawn.ps1 starts it in a Job Object, which holds
  everything it starts, children whose parent has exited too; stop closes the
  windows of the job's processes, gives those that had one TERM_SECONDS, then
  ends the job. Its start time guards running() against a reused pid.

Every call rides on Provider.exec.
"""

import collections
import subprocess
import uuid

from vmlab.config import UsageError, is_argv
from vmlab.providers.base import GuestError, sh_expand_tilde
from vmlab.providers.windows import helper_call

NO_COMMAND = 127  # the start script's exit code when the Guest has no such command
GONE = 4  # the stop script's exit code when nothing of the process was left to stop
STOPPED, EXITED = "stopped", "exited"  # what stop() found
TERM_SECONDS = 5  # how long a process gets to end when asked, before it is killed

# A started process. started is its start time as the Guest tells it, to recognise it by (a
# pid may be reused); job is its Windows Job Object's name (None on POSIX).
Process = collections.namedtuple("Process", "pid log started job")

# POSIX: PID's start time, the same whatever the locale and time zone of the call's env.
_START_TIME = 'st() { set -- $(LC_ALL=C TZ=UTC0 ps -o lstart= -p "$1" 2>/dev/null); s="$*"; echo "${s:--}"; }; '


def validate_argv(argv):
    """argv as a list; UsageError unless it is a command (config.is_argv)."""
    if not is_argv(argv):
        raise UsageError("spawn needs a command: a non-empty argv list of strings, e.g. [\"python3\", \"-m\", \"http.server\"]")
    return list(argv)


class PosixSpawner:
    def __init__(self, provider, log_dir="/tmp"):
        self.provider = provider
        self.log_dir = log_dir  # ~ is the Guest user's home

    def start(self, argv, env, timeout):
        """Start argv detached, its log in log_dir."""
        log = "%s/vmlab-spawn-%s.log" % (self.log_dir, uuid.uuid4().hex[:12])
        script = "log=$1; shift; " + sh_expand_tilde("log") + _START_TIME + (
            'command -v "$1" >/dev/null 2>&1 || { echo "no such command: $1" >&2; exit %d; }; '
            "if command -v setsid >/dev/null 2>&1; then "
            'setsid "$@" </dev/null >"$log" 2>&1 & '
            "else "
            "perl -e 'use POSIX (); POSIX::setsid(); exec { $ARGV[0] } @ARGV or die \"cannot run $ARGV[0]: $!\\n\"' \"$@\" </dev/null >\"$log\" 2>&1 & "
            "fi; "
            'pid=$!; echo "$pid"; st "$pid"' % NO_COMMAND
        )
        result = self.provider.exec(["sh", "-c", script, "sh", log] + argv, timeout, env=env)
        pid, started = _parse(argv, result, 2)
        return Process(int(pid), log, started, None)

    def running(self, process, timeout):
        script = _START_TIME + 'kill -0 "$1" 2>/dev/null && [ "$(st "$1")" = "$2" ]'
        return self.provider.exec(["sh", "-c", script, "sh", str(process.pid), process.started], timeout).ok

    def stop(self, process, timeout):
        """End the process and its group: STOPPED, or EXITED if nothing of it was left. GuestError if it lives on.

        The group is its, unless its leader's pid now belongs to a process that started later: a
        group id is not reused while the group lives, and the leader may have exited before it."""
        polls = TERM_SECONDS * 10
        script = _START_TIME + (
            'g=-$1; kill -0 "$g" 2>/dev/null || exit %d; '
            'if kill -0 "$1" 2>/dev/null && [ "$(st "$1")" != "$2" ]; then exit %d; fi; '
            'kill -TERM "$g" 2>/dev/null; i=0; '
            'while kill -0 "$g" 2>/dev/null; do '
            '[ $i -eq %d ] && kill -KILL "$g" 2>/dev/null; [ $i -ge %d ] && exit 1; i=$((i+1)); sleep 0.1; '
            "done; exit 0" % (GONE, GONE, polls, polls + 20)
        )
        return _stopped(process, self.provider.exec(["sh", "-c", script, "sh", str(process.pid), process.started], timeout))


class WindowsSpawner:
    def __init__(self, provider):
        self.provider = provider

    def start(self, argv, env, timeout):
        """Start argv detached, in a Job Object of its own; its log is in %TEMP%."""
        if '"' in argv[0]:
            raise UsageError("spawn: %r is not a program name" % argv[0])
        name = "vmlab-spawn-%s" % uuid.uuid4().hex[:12]
        params = {
            "argv": argv,
            "line": subprocess.list2cmdline(argv),  # for CreateProcess
            "cmd_line": cmd_line(argv),  # for a batch file, which cmd.exe runs
            "log": "%%TEMP%%\\%s.log" % name,
            "job": name,
        }
        pid, started, log = _parse(argv, self._call("start", params, timeout, env), 3)
        return Process(int(pid), log, int(started), name)

    def running(self, process, timeout):
        script = (
            "$p = Get-Process -Id %d -ErrorAction SilentlyContinue; "
            "if ($p -and $p.StartTime.ToFileTimeUtc() -eq %d) { exit 0 } else { exit 1 }" % (process.pid, process.started)
        )
        return self.provider.exec(self.provider.shell_argv(script), timeout).ok

    def stop(self, process, timeout):
        return _stopped(process, self._call("stop", {"job": process.job, "grace_ms": TERM_SECONDS * 1000}, timeout))

    def _call(self, command, params, timeout, env=None):
        argv, stdin = helper_call("vmlab-spawn.ps1", command, params)
        return self.provider.exec(argv, timeout, env=env, stdin=stdin)


def cmd_line(argv):
    """argv as a cmd.exe command line that reaches the program unchanged.

    The program name is quoted plainly (cmd.exe finds the program from it); the arguments get
    the C runtime's quoting, then a ^ before each character cmd.exe would act on, quotes
    included, so none of them opens a quoted stretch where carets stop working."""
    args = "".join("^" + c if c in '()%!^"<>&|' else c for c in subprocess.list2cmdline(argv[1:]))
    return '"%s"%s' % (argv[0], " " + args if args else "")


def _parse(argv, result, fields):
    if result.code == NO_COMMAND:
        raise GuestError("spawn %s: the Guest has no such command" % argv, "check the command's name and that it is installed in the Guest")
    lines = result.stdout.strip().splitlines()
    if not result.ok or len(lines) != fields:
        detail = result.stderr.strip() or "exit %d, output %r" % (result.code, result.stdout[-200:])
        raise GuestError("starting %s in the Guest failed: %s" % (argv, detail))
    return [line.strip() for line in lines]


def _stopped(process, result):
    if result.code == GONE:
        return EXITED
    if not result.ok:
        raise GuestError(
            "spawned process %d (log %s) is still running after vmlab tried to stop it%s"
            % (process.pid, process.log, ": " + result.stderr.strip() if result.stderr.strip() else ""),
            "look at it in the Guest; it may ignore signals or belong to another user",
        )
    return STOPPED
