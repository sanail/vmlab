"""Base guests: one provisioned Guest per OS version and architecture, shared by
every project on the Host. Labs clone them; projects never run in them.

The registry, $VMLAB_HOME/bases.json, records each Base guest's Provider, VM,
image, Guest user and provisioning version (None until provisioning succeeds).
Its SSH host key is pinned in vmlab's known_hosts (vmlab.providers.ssh).
"""

import json
import re

from vmlab.home import vmlab_home
from vmlab.providers.base import GuestError

# Names `vmlab base create` knows an image for.
CATALOG = {
    "macos-tahoe": "ghcr.io/cirruslabs/macos-tahoe-base:latest",
    "macos-sequoia": "ghcr.io/cirruslabs/macos-sequoia-base:latest",
}
NAME = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


class UsageError(Exception):
    """The command line asked for something that cannot be done."""


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

    def put(self, name, record):
        records = self.all()
        records[name] = record
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def create(name, image, confirm, reprovision, out):
    """Create and provision the Base guest name; idempotent."""
    if not NAME.match(name):
        raise UsageError("invalid Base guest name %r: use lowercase letters, digits, '.' and '-'" % name)
    image = image or CATALOG.get(name)
    if not image:
        raise UsageError(
            "no image known for Base guest %r; known: %s. For another one, pass --image with a Tart image"
            % (name, ", ".join(sorted(CATALOG)))
        )
    if not name.startswith("macos-"):
        raise UsageError("only macOS Base guests (Tart) can be created so far; name it macos-<version>")
    from vmlab.providers import tart

    tart.create_base(name, image, confirm=confirm, reprovision=reprovision, out=out)


def render(out):
    records = Registry().all()
    if not records:
        out("No Base guests yet. Create one with: vmlab base create macos-tahoe")
        return
    for name, r in sorted(records.items()):
        state = "ready" if r.get("provisioned") else "not provisioned (re-run vmlab base create %s)" % name
        out("%-16s %-6s %s/%s  %s  from %s" % (name, r["provider"], r["os"], r["arch"], state, r["image"]))
