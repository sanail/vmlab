"""Base guests: one provisioned Guest per OS version and architecture, shared by
every project on the Host. Labs clone them; projects never run in them.

The registry, $VMLAB_HOME/bases.json, records each Base guest's Provider, VM,
image, Guest user, provisioning version (None until provisioning succeeds) and
provisioned_id, new with every successful provisioning, so Lab clones made
before it can tell they are stale even when the version did not change.
Fusion Base guests also record their .vmx, whether the install finished, and
the snapshot Lab clones are made from; Windows ones, whose image is the VM
they were copied from, also whether the Guest elevates without asking.
Its SSH host key is pinned in vmlab's known_hosts (vmlab.providers.ssh).
"""

import json
import re

from vmlab.config import UsageError, host_arch
from vmlab.home import vmlab_home
from vmlab.providers.base import GuestError

# Names `vmlab base create` knows an image for: a Tart image for macos-*, an
# installer ISO per Host architecture for ubuntu-* (VMware Fusion). windows-*
# Base guests are copies of a VM made with Fusion: the wizard finds it.
CATALOG = {
    "macos-tahoe": "ghcr.io/cirruslabs/macos-tahoe-base:latest",
    "macos-sequoia": "ghcr.io/cirruslabs/macos-sequoia-base:latest",
    "ubuntu-26.04": {
        "arm64": "https://cdimage.ubuntu.com/ubuntu/releases/26.04/release/ubuntu-26.04.1-desktop-arm64.iso",
        "x86_64": "https://releases.ubuntu.com/26.04/ubuntu-26.04.1-desktop-amd64.iso",
    },
    "windows-11": None,
}
NAME = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def provisioning(record):
    """Identifies one provisioning of a Base guest (records from before provisioned_id: its version)."""
    return record.get("provisioned_id") or str(record["provisioned"])


def vm_name(name):
    """The hypervisor's name for a Base guest's VM."""
    return "vmlab-base-%s" % name


class Registry:
    def __init__(self):
        self.path = vmlab_home() / "bases.json"

    def all(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as exc:  # never overwrite it: that would forget every Base guest
            raise GuestError("Base guest registry %s is corrupt: %s" % (self.path, exc), "repair or delete it; Base guests are then re-registered by `vmlab base create`")

    def get(self, name):
        return self.all().get(name)

    def remove(self, name):
        records = self.all()
        if records.pop(name, None) is not None:
            self._write(records)

    def put(self, name, record):
        records = self.all()
        records[name] = record
        self._write(records)

    def _write(self, records):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def create(name, image, prompt, reprovision, out):
    """Create and provision the Base guest name; idempotent. prompt asks the person at the
    terminal (vmlab.cli.Terminal): confirmations, and a wizard's steps for Windows."""
    if not NAME.match(name):
        raise UsageError("invalid Base guest name %r: use lowercase letters, digits, '.' and '-'" % name)
    if not name.startswith(("macos-", "ubuntu-", "windows-")):
        raise UsageError(
            "vmlab creates macOS (Tart), Ubuntu and Windows (VMware Fusion) Base guests; name it macos-<version>, ubuntu-<version> or windows-<version>"
        )
    if name.startswith("windows-"):
        from vmlab.providers import fusion_windows

        fusion_windows.create_base(name, image, prompt=prompt, reprovision=reprovision, out=out)
        return
    image = image or CATALOG.get(name)
    if isinstance(image, dict):
        image = image[host_arch()]
    if not image:
        raise UsageError(
            "no image known for Base guest %r; known: %s. For another one, pass --image with a Tart image (macos-*) "
            "or an Ubuntu desktop ISO (ubuntu-*)" % (name, ", ".join(sorted(CATALOG)))
        )
    if name.startswith("macos-"):
        from vmlab.providers import tart

        tart.create_base(name, image, confirm=prompt.confirm, reprovision=reprovision, out=out)
    else:
        from vmlab.providers import fusion

        fusion.create_base(name, image, confirm=prompt.confirm, reprovision=reprovision, out=out)


def render(out):
    records = Registry().all()
    if not records:
        out("No Base guests yet. Create one with: vmlab base create macos-tahoe (or ubuntu-26.04, windows-11)")
        return
    for name, r in sorted(records.items()):
        state = "ready" if r.get("provisioned") else "not provisioned (re-run vmlab base create %s)" % name
        out("%-16s %-6s %s/%s  %s  from %s" % (name, r["provider"], r["os"], r["arch"], state, r["image"]))
