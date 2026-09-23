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
LAB_KEYS = ("provider", "os", "arch", "memory_gb", "boot_timeout", "step_timeout", "scenario_timeout", "app")
# ...plus one options table named after each Provider
APP_KEYS = ("artifact", "build", "inputs", "build_timeout", "install", "install_timeout", "quit", "launch", "ready", "ready_timeout", "env", "state")
# [labs.<name>.app] ready: one wait_for condition, in wait_for's keywords
READY_CONDITIONS = ("text", "role", "process", "file", "log", "exec")  # text and role make one: an element
READY_KEYS = READY_CONDITIONS + ("app", "pattern", "gone")
DEFAULT_BUILD_TIMEOUT = 1800
DEFAULT_INSTALL_TIMEOUT = 600
DEFAULT_MEMORY_GB = 4
DEFAULT_BOOT_TIMEOUT = 300
DEFAULT_STEP_TIMEOUT = 60
DEFAULT_SCENARIO_TIMEOUT = 600


class UsageError(Exception):
    """The command line (or a Scenario's call) asked for something malformed or impossible. Exit 2."""


class ConfigError(Exception):
    def __init__(self, path, key, problem, fix):
        self.path, self.key, self.problem, self.fix = path, key, problem, fix
        where = "%s: %s" % (path, key) if key else str(path)
        super().__init__("%s: %s\n  fix: %s" % (where, problem, fix))


class Lab:
    def __init__(self, name, provider, os, arch, options, **settings):
        self.name = name
        self.provider = provider
        self.os = os
        self.arch = arch
        self.options = options  # provider-specific table, e.g. [labs.<name>.fake]
        self.memory_gb = settings.get("memory_gb", DEFAULT_MEMORY_GB)  # Host RAM the Guest takes when running
        self.boot_timeout = settings.get("boot_timeout", DEFAULT_BOOT_TIMEOUT)  # s from power-on until reachable
        self.step_timeout = settings.get("step_timeout", DEFAULT_STEP_TIMEOUT)  # s per Guest call
        self.scenario_timeout = settings.get("scenario_timeout", DEFAULT_SCENARIO_TIMEOUT)  # s per Scenario
        self.app = settings.get("app") or App({})


class App:
    """The application under test on one Lab: [labs.<name>.app]."""

    def __init__(self, table):
        self.artifact = table.get("artifact")  # Host path or glob, relative to the project root
        self.build = table.get("build")  # Host shell command, run in the project root when stale
        self.inputs = table.get("inputs", [])  # Host paths whose changes make the artifact stale
        self.build_timeout = table.get("build_timeout", DEFAULT_BUILD_TIMEOUT)
        self.install = table.get("install")  # Guest shell commands; see vmlab.deploy for the env they get
        self.install_timeout = table.get("install_timeout", DEFAULT_INSTALL_TIMEOUT)
        self.quit = table.get("quit")
        self.launch = table.get("launch")
        self.ready = table.get("ready")  # a vmlab.ui condition that holds once the launched app is ready, or None
        self.ready_timeout = table.get("ready_timeout")  # s; None: the Lab's step_timeout
        self.env = table.get("env", {})
        self.state = table.get("state", [])  # Guest paths removed before each Run


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


def has_config(start):
    return _search_config(start) is not None


def find_config(start):
    found = _search_config(start)
    if found:
        return found
    raise ConfigError(
        Path(start).resolve() / CONFIG_DIR / CONFIG_NAME,
        None,
        "no vmlab config found here or in any parent directory",
        "run vmlab from inside the project, or run `vmlab init` in the project root to create %s/%s"
        % (CONFIG_DIR, CONFIG_NAME),
    )


def _search_config(start):
    start = Path(start).resolve()
    for directory in (start,) + tuple(start.parents):
        candidate = directory / CONFIG_DIR / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


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
        if PROVIDERS[provider].NOT_IMPLEMENTED:
            raise ConfigError(path, key + ".provider", "Provider %r is not implemented yet" % provider, PROVIDERS[provider].NOT_IMPLEMENTED)
        os_name = _required_choice(path, table, key, "os", OSES)
        supported = PROVIDERS[provider].SUPPORTED_OS
        if supported and os_name not in supported:
            raise ConfigError(
                path,
                key + ".os",
                "Provider %s runs os = %s only" % (provider, " or ".join(supported)),
                'macOS Labs use provider = "tart", Linux and Windows Labs provider = "fusion"; provider = "fake" tries vmlab without a hypervisor',
            )
        arch = table.get("arch", host_arch())
        if arch not in ARCHES:
            raise ConfigError(path, key + ".arch", "unknown arch %r" % arch, "use one of: %s" % ", ".join(ARCHES))
        options = table.get(provider, {})
        if not isinstance(options, dict):
            raise ConfigError(path, "%s.%s" % (key, provider), "must be a table", "write it as [%s.%s]" % (key, provider))
        PROVIDERS[provider].validate_options(path, "%s.%s" % (key, provider), options, os_name)
        app = _app(path, key + ".app", table.get("app", {}))
        labs[name] = Lab(
            name,
            provider,
            os_name,
            arch,
            options,
            memory_gb=_positive_number(path, table, key, "memory_gb", DEFAULT_MEMORY_GB, "GB"),
            boot_timeout=_positive_number(path, table, key, "boot_timeout", DEFAULT_BOOT_TIMEOUT),
            step_timeout=_positive_number(path, table, key, "step_timeout", DEFAULT_STEP_TIMEOUT),
            scenario_timeout=_positive_number(path, table, key, "scenario_timeout", DEFAULT_SCENARIO_TIMEOUT),
            app=app,
        )

    return Project(path.parent.parent, path, labs)


def _required_choice(path, table, key, field, choices):
    full = "%s.%s" % (key, field)
    if field not in table:
        raise ConfigError(path, full, "missing", '%s = "<one of: %s>"' % (field, ", ".join(choices)))
    value = table[field]
    if value not in choices:
        raise ConfigError(path, full, "unknown %s %r" % (field, value), "use one of: %s" % ", ".join(choices))
    return value


def _positive_number(path, table, key, field, default, unit="seconds"):
    value = table.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(path, "%s.%s" % (key, field), "must be a number of %s > 0" % unit, "e.g. %s = %s" % (field, default))
    return value


def _app(path, key, table):
    if not isinstance(table, dict):
        raise ConfigError(path, key, "must be a table", "write it as [%s]" % key)
    for k in sorted(set(table) - set(APP_KEYS)):
        raise ConfigError(path, "%s.%s" % (key, k), "unknown key", "remove it; allowed keys: %s" % ", ".join(APP_KEYS))
    for field in ("artifact", "build", "install", "quit", "launch"):
        if field in table and not (isinstance(table[field], str) and table[field].strip()):
            raise ConfigError(path, "%s.%s" % (key, field), "must be a non-empty string", "e.g. %s = \"...\"" % field)
    for field, example in (("inputs", '["src", "package.json"]'), ("state", '["~/Library/Application Support/MyApp"]')):
        value = table.get(field, [])
        if not (isinstance(value, list) and all(isinstance(v, str) and v for v in value)):
            raise ConfigError(path, "%s.%s" % (key, field), "must be a list of paths", "e.g. %s = %s" % (field, example))
    env = table.get("env", {})
    if not (isinstance(env, dict) and all(isinstance(v, str) for v in env.values())):
        raise ConfigError(path, key + ".env", "must be a table of strings", 'e.g. env = { RUST_LOG = "debug" }')
    for field in ("build", "install", "inputs"):
        if field in table and "artifact" not in table:
            raise ConfigError(path, key + ".artifact", "missing (needed by %s.%s)" % (key, field), 'e.g. artifact = "dist/MyApp.dmg"')
    _positive_number(path, table, key, "build_timeout", DEFAULT_BUILD_TIMEOUT)
    _positive_number(path, table, key, "install_timeout", DEFAULT_INSTALL_TIMEOUT)
    if "ready_timeout" in table:
        if "ready" not in table:
            raise ConfigError(path, key + ".ready", "missing (needed by %s.ready_timeout)" % key, READY_EXAMPLE)
        _positive_number(path, table, key, "ready_timeout", 30)
    if "ready" in table:
        if "launch" not in table:
            raise ConfigError(path, key + ".launch", "missing (needed by %s.ready)" % key, 'e.g. launch = "open -a MyApp"')
        table = dict(table, ready=_ready(path, key + ".ready", table["ready"]))
    return App(table)


READY_EXAMPLE = 'e.g. ready = { process = "MyApp" }, or an element: ready = { text = "MyApp", role = "menubaritem", app = "MyApp" }'


def _ready(path, key, ready):
    """The vmlab.ui condition a ready table describes; ConfigError with wait_for's own errors."""
    from vmlab import ui  # it imports this module

    if not isinstance(ready, dict):
        raise ConfigError(path, key, "must be a table holding one wait_for condition", READY_EXAMPLE)
    for k in sorted(set(ready) - set(READY_KEYS)):
        raise ConfigError(path, "%s.%s" % (key, k), "unknown key", "remove it; allowed keys: %s" % ", ".join(READY_KEYS))
    for k in READY_KEYS:
        if k not in ready:
            continue
        value = ready[k]
        if k == "gone":
            if not isinstance(value, bool):
                raise ConfigError(path, key + ".gone", "must be true or false", "e.g. gone = true")
        elif k == "exec":
            if not (isinstance(value, list) and value and all(isinstance(a, str) and a for a in value)):
                raise ConfigError(path, key + ".exec", "must be a command: a non-empty list of strings", 'e.g. exec = ["curl", "-fsS", "http://127.0.0.1:8080/health"]')
        elif not (isinstance(value, str) and value):
            raise ConfigError(path, "%s.%s" % (key, k), "must be a non-empty string", 'e.g. %s = "MyApp"' % k)
    found = []
    for k in READY_CONDITIONS:
        if k in ready and not (k == "role" and "text" in ready):
            found.append("an element (text/role)" if k in ("text", "role") else k)
    if not found:
        raise ConfigError(path, key, "holds no condition; it needs one of: text/role (an element), process, file, log, exec", READY_EXAMPLE)
    if len(found) > 1:
        raise ConfigError(
            path, key, "holds several conditions (%s); it takes one" % ", ".join(found), "keep the one that says the app is ready; a Scenario waits for the rest with g.wait_for"
        )
    try:
        return ui.condition(**ready)
    except UsageError as exc:
        raise ConfigError(path, key, str(exc), READY_EXAMPLE)
