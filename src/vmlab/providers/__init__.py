"""Providers: one adapter per hypervisor. See base.Provider for the interface."""

from vmlab.providers.fake import FakeProvider
from vmlab.providers.stubs import stub

PROVIDERS = {
    "fake": FakeProvider,
    "parallels": stub("parallels", "Parallels Desktop"),
    "utm": stub("utm", "UTM"),
}


def provider_for(project, lab):
    return PROVIDERS[lab.provider](project, lab)
