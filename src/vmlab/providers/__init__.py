"""Providers: one adapter per hypervisor. See base.Provider for the interface."""

from vmlab.providers.fake import FakeProvider

PROVIDERS = {
    "fake": FakeProvider,
}


def provider_for(project, lab):
    return PROVIDERS[lab.provider](project, lab)
