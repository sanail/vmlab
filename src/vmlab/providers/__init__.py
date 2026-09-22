"""Providers: one adapter per hypervisor. See base.Provider for the interface."""

from vmlab.providers.fake import FakeProvider
from vmlab.providers.stubs import stub
from vmlab.providers.tart import TartProvider

PROVIDERS = {
    "fake": FakeProvider,
    "parallels": stub("parallels", "Parallels Desktop"),
    "tart": TartProvider,
    "utm": stub("utm", "UTM"),
}


def provider_for(project, lab):
    return PROVIDERS[lab.provider](project, lab)
