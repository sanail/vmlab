"""Run a process on the Host with a timeout that really kills it.

subprocess.run(timeout=...) kills only the direct child; a grandchild (e.g. the
`sleep` under `sh -c`) keeps the pipes open and run() then waits for it anyway.
Here the child leads its own process group and the whole group is killed.
"""

import os
import signal
import subprocess


def run(argv, timeout, cwd=None, env=None, stdin_data=None):
    """Return (code, stdout, stderr); raise subprocess.TimeoutExpired after killing the process group."""
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    return proc.returncode, out, err
