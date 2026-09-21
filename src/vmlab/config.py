"""Load and validate a project's vmlab config.

Every error names the file, the offending key and how to fix it.
"""

import platform
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    from vmlab._vendor import tomli as tomllib

CONFIG_DIR = ".vmlab"
CONFIG_NAME = "vmlab.toml"

OSES = ("macos", "windows", "linux")
ARCHES = ("arm64", "x86_64")
LAB_KEYS = ("provider", "os", "arch")  # plus one options table named after each Provider


class ConfigError(Exception):
    def __init__(self, path, key, problem, fix):
        self.path, self.key, self.problem, self.fix = path, key, problem, fix
        where = "%s: %s" % (path, key) if key else str(path)
        super().__init__("%s: %s\n  fix: %s" % (where, problem, fix))


class Lab:
    def __init__(self, name, provider, os, arch, options):
        self.name = name
        self.provider = provider
        self.os = os
        self.arch = arch
        self.options = options  # provider-specific table, e.g. [labs.<name>.fake]


class Project:
    def __init__(self, root, config_path, labs):
        self.root = root
        self.config_path = config_path
        self.labs = labs

    @property
    def vmlab_dir(self):
        return self.config_path.parent

    @property
    def scenarios_dir(self):
        return self.vmlab_dir / "scenarios"

    @property
    def runs_dir(self):
        return self.vmlab_dir / "runs"

    def lab(self, name):
        if name not in self.labs:
            raise ConfigError(
                self.config_path,
                "labs.%s" % name,
                "no such Lab",
                "use one of: %s" % ", ".join(self.labs),
            )
        return self.labs[name]

    def select_labs(self, names):
        """The named Labs, or all Labs when names is empty."""
        return [self.lab(n) for n in names] if names else list(self.labs.values())


def host_arch():
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "x86_64"


def find_config(start):
    start = Path(start).resolve()
    for directory in (start,) + tuple(start.parents):
        candidate = directory / CONFIG_DIR / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise ConfigError(
        start / CONFIG_DIR / CONFIG_NAME,
        None,
        "no vmlab config found here or in any parent directory",
        "run vmlab from inside the project, or create %s/%s declaring at least one [labs.<name>]"
        % (CONFIG_DIR, CONFIG_NAME),
    )


def load(start):
    from vmlab.providers import PROVIDERS

    path = find_config(start)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, None, "invalid TOML: %s" % exc, "correct the syntax at that line")

    unknown = sorted(set(data) - {"labs"})
    if unknown:
        raise ConfigError(path, unknown[0], "unknown key", "remove it; top-level tables are: [labs.<name>]")

    labs_table = data.get("labs")
    if not isinstance(labs_table, dict) or not labs_table:
        raise ConfigError(
            path,
            "labs",
            "no Labs declared",
            'add a Lab, e.g.\n    [labs.mac]\n    provider = "fake"\n    os = "macos"',
        )

    labs = {}
    for name, table in labs_table.items():
        key = "labs.%s" % name
        if not isinstance(table, dict):
            raise ConfigError(path, key, "must be a table", "write it as [%s] with provider and os keys" % key)
        allowed = LAB_KEYS + tuple(sorted(PROVIDERS))
        for k in sorted(set(table) - set(allowed)):
            raise ConfigError(path, "%s.%s" % (key, k), "unknown key", "remove it; allowed keys: %s" % ", ".join(allowed))
        provider = _required_choice(path, table, key, "provider", sorted(PROVIDERS))
        os_name = _required_choice(path, table, key, "os", OSES)
        arch = table.get("arch", host_arch())
        if arch not in ARCHES:
            raise ConfigError(path, key + ".arch", "unknown arch %r" % arch, "use one of: %s" % ", ".join(ARCHES))
        options = table.get(provider, {})
        if not isinstance(options, dict):
            raise ConfigError(path, "%s.%s" % (key, provider), "must be a table", "write it as [%s.%s]" % (key, provider))
        PROVIDERS[provider].validate_options(path, "%s.%s" % (key, provider), options)
        labs[name] = Lab(name, provider, os_name, arch, options)

    return Project(path.parent.parent, path, labs)


def _required_choice(path, table, key, field, choices):
    full = "%s.%s" % (key, field)
    if field not in table:
        raise ConfigError(path, full, "missing", '%s = "<one of: %s>"' % (field, ", ".join(choices)))
    value = table[field]
    if value not in choices:
        raise ConfigError(path, full, "unknown %s %r" % (field, value), "use one of: %s" % ", ".join(choices))
    return value
