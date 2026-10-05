"""Free Host memory and the vCPU budget, for deciding how many Guests can run at once.

VMLAB_FREE_MEMORY_GB overrides the measurement (tests, or to hold memory back);
VMLAB_HOST_CPUS overrides the vCPU budget.
"""

import os
import platform
import re
import subprocess


def free_memory_gb():
    """Memory available to start Guests, in GB, or None if it cannot be measured."""
    override = os.environ.get("VMLAB_FREE_MEMORY_GB")
    if override:
        return float(override)
    try:
        if platform.system() == "Darwin":
            return _darwin()
        if platform.system() == "Linux":
            return _linux()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def vcpu_budget():
    """The vCPUs the Guests of one `vmlab run --parallel` may have at once: the Host's cores × 1.5, rounded down."""
    override = os.environ.get("VMLAB_HOST_CPUS")
    if override:
        return int(override)
    return (os.cpu_count() or 1) * 3 // 2


def _darwin():
    # Free, inactive and speculative pages can all be handed to a new process.
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10, check=True).stdout
    page = int(re.search(r"page size of (\d+) bytes", out).group(1))
    pages = 0
    for label in ("Pages free", "Pages inactive", "Pages speculative"):
        match = re.search(r"%s:\s+(\d+)" % label, out)
        if match:
            pages += int(match.group(1))
    return pages * page / 1024 ** 3


def _linux():
    with open("/proc/meminfo") as f:
        kb = int(re.search(r"MemAvailable:\s+(\d+) kB", f.read()).group(1))
    return kb / 1024 ** 2
