"""Providers that are planned but not written yet: selecting one fails at config load
with a pointer to the contributor guide."""

from vmlab.providers.base import Provider

GUIDE = "docs/adding-a-provider.md in the vmlab skill"


def stub(name, hypervisor):
    class StubProvider(Provider):
        NOT_IMPLEMENTED = (
            "use another Provider for now, or implement the %s Provider (%s): see %s" % (name, hypervisor, GUIDE)
        )

    StubProvider.__name__ = "%sStubProvider" % name.capitalize()
    return StubProvider
