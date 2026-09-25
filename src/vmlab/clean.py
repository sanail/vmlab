"""`vmlab clean`: what vmlab left on this Host that no known Lab needs any more.

Every Provider that keeps VMs on the Host (Tart, Fusion) describes them with a
HostVMs inventory: its VMs and whether they run, how to delete one, its
service files, all named <VM><suffix> in one folder, and the snapshots of
earlier provisionings its VMs keep. Each Lab clone has a
record there, <clone>.json, naming the project, Lab and Base guest it serves;
that is how an orphan is recognised. The rest is the same for every Provider
and lives here.
"""

import json
from pathlib import Path

from vmlab import bases, config
from vmlab.providers.base import GuestError

RECORD = ".json"


class Leftover:
    """Something of vmlab's on this Host that no known Lab needs any more."""

    def __init__(self, kind, name, reason, remove, stop_hint=None, running=False, needs_bases=False, kept=None, stops_first=False):
        self.kind, self.name, self.reason, self.remove = kind, name, reason, remove
        self.stop_hint = stop_hint  # how to stop it by hand
        self.running = running  # never deleted while running, unless stops_first
        self.stops_first = stops_first  # remove() stops it first: no one but vmlab can
        self.needs_bases = needs_bases  # a Base guest: deleted only with --bases (re-creating one downloads its image)
        self.kept = kept  # why it stays after all (a Lab clone still needs it), or None


def inventories():
    from vmlab.providers import fusion, tart

    return [tart.HostVMs(), fusion.HostVMs()]


def bases_in_use(project):
    """The Base guests project's Labs use, even before their first clone."""
    found = set()
    for inventory in inventories():
        found |= {(inventory.provider, lab.options.get("base", inventory.default_base(lab))) for lab in project.labs.values() if lab.provider == inventory.provider}
    return found


def leftovers(in_use=()):
    """(leftovers, warnings) across every inventory. in_use: (provider, Base guest) pairs
    the current project needs. A hypervisor that cannot be asked is skipped with a warning,
    unless vmlab has nothing of its own there."""
    found, warnings = [], []
    registry = bases.Registry().all()
    for inventory in inventories():
        records = {name: r for name, r in registry.items() if r.get("provider", "tart") == inventory.provider}
        try:
            vms = inventory.vms()
        except GuestError as exc:
            if records or _service_names(inventory):
                warnings.append("%s: skipped, cannot list its VMs: %s" % (inventory.provider, exc.message))
            continue
        found += _leftovers(inventory, vms, records, {base for provider, base in in_use if provider == inventory.provider})
    return found, warnings


def _leftovers(inventory, vms, records, used):
    found = []
    for vm, running in sorted(vms.items()):
        if not vm.startswith("vmlab-") or vm.startswith("vmlab-base-"):
            continue  # not vmlab's, or a Base guest
        reason, base = _clone_orphan_reason(inventory, vm)
        if reason is None:
            used.add(base)
        else:
            # No Lab needs it, so no `vmlab down` can stop it: deleting it stops it first.
            found.append(Leftover("clone", vm, reason, lambda vm=vm: _delete_clone(inventory, vm), running=running, stops_first=True))
    for name, record in sorted(records.items()):
        vm = record.get("vm") or bases.vm_name(name)
        if name not in used and vm in vms:
            found.append(
                Leftover("base", vm, "Base guest %s: no known Lab uses it" % name, lambda name=name, vm=vm: _delete_base(inventory, name, vm),
                         inventory.stop_hint(vm), vms[vm], needs_bases=True)
            )  # fmt: skip
    whole = {item.name for item in found}  # orphaned clones and unused Base guests: they go whole
    for old in inventory.old_snapshots(vms):
        if old.vm.name not in whole:
            found.append(Leftover("snapshot", "%s %s" % (old.vm.name, old.name), "%s: from an earlier provisioning" % old.owner, old.delete,
                                  old.stop_hint, old.running, kept=old.why_kept() or None))  # fmt: skip
    for vm in sorted(_service_names(inventory) - set(vms)):
        found.append(Leftover("files", vm, "service files of a VM that is gone", lambda vm=vm: _remove_files(inventory, vm)))
    return found


def _clone_orphan_reason(inventory, vm):
    """(why the clone vm is not needed, or None; the Base guest it serves)."""
    from vmlab.providers import provider_for

    try:
        record = json.loads((inventory.service_dir / (vm + RECORD)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "made by an older vmlab or elsewhere: its project is unknown", None
    root = Path(record["project"])
    if not (root / config.CONFIG_DIR / config.CONFIG_NAME).is_file():
        return "its project %s no longer exists" % root, record["base"]
    try:
        project = config.load(str(root))
    except config.ConfigError:
        return None, record["base"]  # cannot tell: keep it
    lab = project.labs.get(record["lab"])
    if lab is None or lab.provider != inventory.provider:
        return "project %s has no Lab %s on %s any more" % (root, record["lab"], inventory.provider.capitalize()), record["base"]
    if provider_for(project, lab).guest_id != vm:
        return "project %s's Lab %s uses another clone now" % (root, record["lab"]), record["base"]
    return None, record["base"]


def _service_names(inventory):
    """VM names that have service files; the longest matching suffix wins (x.credentials.json is x's)."""
    if not inventory.service_dir.is_dir():
        return set()
    names = set()
    for path in inventory.service_dir.iterdir():
        suffixes = [s for s in inventory.service_suffixes if path.is_file() and path.name.endswith(s)]
        if suffixes:
            names.add(path.name[: -len(max(suffixes, key=len))])
    return names


def _remove_files(inventory, vm):
    for suffix in inventory.service_suffixes:
        path = inventory.service_dir / (vm + suffix)
        if path.is_file():
            path.unlink()


def _delete_clone(inventory, vm):
    inventory.delete_vm(vm)
    _remove_files(inventory, vm)


def _delete_base(inventory, name, vm):
    from vmlab.providers.ssh import unpin_host_key

    inventory.delete_vm(vm)
    _remove_files(inventory, vm)
    bases.Registry().remove(name)
    unpin_host_key(vm)
