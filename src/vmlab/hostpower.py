"""The Host's sleep, which pauses its Guests: vmlab keeps the Host awake while it runs, and
times every wait on a Guest on a clock that stops while the Host sleeps anyway (on battery,
lid closed), so a Guest never looks slow because the Host slept. One way per Host OS;
where a Host has none, vmlab runs without it.
"""

import ctypes
import os
import platform
import subprocess
import time

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001  # SetThreadExecutionState


def keep_awake():
    """Keep the Host from sleeping until this process exits."""
    system = platform.system()
    try:
        if system == "Windows":
            # For as long as the calling thread lives: vmlab's main thread lives as long as vmlab.
            _kernel32().SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        elif system == "Linux":
            _hold([os.environ.get("VMLAB_SYSTEMD_INHIBIT") or "systemd-inhibit", "--what=idle:sleep", "--who=vmlab",
                   "--why=Guests are running", "--mode=block", "tail", "--pid=%d" % os.getpid(), "-f", "/dev/null"])  # fmt: skip
        else:
            # -i holds off idle sleep, but not the sleep that ends a dark wake (woken for
            # maintenance, display off); -s holds off any sleep while on AC power.
            _hold([os.environ.get("VMLAB_CAFFEINATE") or "caffeinate", "-i", "-s", "-w", str(os.getpid())])
    except (OSError, AttributeError):
        pass  # no such tool, or no such call, on this Host


def awake_time():
    """Seconds on a clock that stops while the Host sleeps, as its Guests do: for every deadline
    of a wait on a Guest. time.monotonic() is one on macOS (mach_absolute_time) and Linux
    (CLOCK_MONOTONIC); on Windows it counts sleep, QueryUnbiasedInterruptTime does not."""
    if platform.system() == "Windows":
        ticks = ctypes.c_ulonglong()
        if _kernel32().QueryUnbiasedInterruptTime(ctypes.byref(ticks)):
            return ticks.value / 1e7  # 100 ns units
    return time.monotonic()


def _hold(argv):
    """Start a process that holds the Host awake and exits with vmlab (it watches vmlab's pid)."""
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # Ctrl-C is vmlab's
    )  # fmt: skip


def _kernel32():
    return ctypes.windll.kernel32
