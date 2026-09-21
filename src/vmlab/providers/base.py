"""The Provider interface every hypervisor adapter implements."""

import hashlib
import re


class ExecResult:
    def __init__(self, argv, code, stdout, stderr):
        self.argv = argv
        self.code = code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self):
        return self.code == 0


class Provider:
    """Starts, stops and talks to the Guest that realises one Lab."""

    def __init__(self, project, lab):
        self.project = project
        self.lab = lab

    @classmethod
    def validate_options(cls, config_path, key, options):
        """Raise ConfigError if this Provider's [labs.<name>.<provider>] table is wrong."""

    @property
    def guest_id(self):
        """Stable per project and Lab, so projects never share a Guest by accident."""
        digest = hashlib.sha1(str(self.project.root).encode("utf-8")).hexdigest()[:8]
        slug = re.sub(r"[^a-z0-9]+", "-", self.project.root.name.lower()).strip("-") or "project"
        return "vmlab-%s-%s-%s" % (slug, digest, self.lab.name)

    def is_running(self):
        raise NotImplementedError

    def up(self):
        """Start the Guest if it is not running. Idempotent."""
        raise NotImplementedError

    def down(self):
        """Stop the Guest if it is running. Idempotent."""
        raise NotImplementedError

    def exec(self, argv, timeout):
        """Run argv inside the Guest and return an ExecResult."""
        raise NotImplementedError

    def screenshot(self, dest):
        """Write a PNG screenshot of the Guest's screen to dest."""
        raise NotImplementedError

    def ui_tree(self):
        """Return the accessibility tree of the Guest's desktop as JSON-able data."""
        raise NotImplementedError
