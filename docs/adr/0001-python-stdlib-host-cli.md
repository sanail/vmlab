# Host-side orchestration is a stdlib-only Python CLI, not bash

Hand-written per-OS VM test labs of the kind this skill replaces are typically written in bash. We chose a single Python 3 CLI (`vmlab`, standard library only) for everything that runs on the **Host**, while code inside **Guests** stays OS-native (zsh/osascript on macOS, PowerShell on Windows, bash on Linux).

Reasons: the skill must later run on Linux and Windows hosts, where bash is absent or awkward; such bash labs tend to show fragile nested quoting (bash → vmrun → PowerShell), copy-pasted libraries per lab, and no structured output. Python gives portable process/timeout handling, one shared **Provider** abstraction, and JSON/JUnit reports. Stdlib-only keeps installation to "have python3", which every supported agent environment already assumes.

## Considered options

- **Bash** — matches typical hand-written labs and is simplest on a Mac host, but blocks Windows hosts and repeats the quoting and duplication problems.
- **Node/TypeScript** — portable, but adds a runtime and package install step unrelated to most desktop app projects.
