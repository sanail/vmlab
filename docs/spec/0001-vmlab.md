# Spec 0001 — vmlab: test desktop apps in macOS, Windows and Linux Guests

Status: ready-for-agent (local; move to the issue tracker once one is configured)

Vocabulary follows `CONTEXT.md`. Decisions respect ADR 0001 (stdlib Python host CLI), ADR 0002 (vendored zipapp) and ADR 0003 (Channel per Provider with fallback).

## Problem Statement

A developer of a cross-platform desktop application works on one machine but ships to macOS, Windows and Linux. Checking that the app actually works on each OS means hand-building virtual machines, remembering hypervisor-specific incantations, copying builds in, clicking through the UI, and eyeballing the result. When they do automate it, each OS typically gets its own copy-pasted bash harness with hardcoded paths, personal SSH keys, fixed sleeps, fragile nested quoting, state leaking between runs, and no machine-readable results. Their coding agent could do this work, but has no reusable, agent-agnostic knowledge of how to set up **Guests**, drive the app inside them, or turn a one-off check into a repeatable **Regression suite**.

## Solution

An agent skill, `vmlab`, following the open SKILL.md standard, plus a host CLI of the same name that the skill vendors into each project.

- **Setup**: the agent checks the **Host**, installs or guides installation of a hypervisor, creates and provisions a shared **Base guest** per OS, and declares **Labs** for the project — automatically where legal and technically possible, via an interactive wizard where a human must act (vendor accounts, licensed downloads). Every host-level install asks for confirmation first.
- **Ad-hoc run**: the user describes steps in natural language ("install the latest build on Windows, open a file in Notepad, press the app's hotkey, check its window appears, take a screenshot"). The agent explores the app through CLI primitives, crystallises the working steps into a **Scenario**, runs it cleanly, reports with evidence, and offers to save it.
- **Regression suite**: saved **Scenarios** in the project that run from a terminal, cron or CI without any agent, producing JSON, JUnit and Markdown reports and a non-zero exit code on failure.

## User Stories

### Setup — host and Base guests

1. As a developer, I want the agent to tell me which OSes I can test on this Host, so that I know up front that macOS Guests need a Mac Host.
2. As a developer, I want the agent to detect which hypervisors are already installed, so that it reuses what I have instead of installing duplicates.
3. As a developer on Apple Silicon, I want Tart installed for me after I confirm, so that I get a macOS Guest without manual steps.
4. As a developer, I want a step-by-step wizard for installing VMware Fusion, so that I can complete the vendor-account download that the agent cannot do.
5. As a developer, I want the wizard to verify each step it asked me to perform, so that I don't discover a missed step later during a Run.
6. As a developer, I want Windows 11 obtained through Fusion's built-in "Get Windows from Microsoft" flow, so that I don't have to hunt for an ISO.
7. As a developer, I want Ubuntu installed unattended, so that the Linux Guest needs no clicking.
8. As a developer, I want each Base guest provisioned by an idempotent script, so that re-running provisioning after a failure is safe.
9. As a developer, I want provisioning to install the accessibility and input helpers each OS needs, so that Scenarios can read and drive the UI.
10. As a developer, I want the macOS Swift helper compiled during provisioning, so that the first Run is not slowed or broken by compilation.
11. As a developer, I want provisioning to configure passwordless sudo/elevation in the Guest, so that passwords never appear on command lines or in process lists.
12. As a developer, I want a dedicated SSH key and known_hosts for vmlab, so that my personal key is never used and reused Guest IPs don't break my own known_hosts.
13. As a developer, I want Guest credentials stored outside the project (vmlab's home directory with strict permissions, or Keychain), so that they are never committed.
14. As a developer, I want one Base guest per OS version and architecture shared by all my projects, so that I don't store 60 GB per OS per project.
15. As a developer, I want each project to work in its own clone of a Base guest, so that projects don't contaminate each other.
16. As a developer, I want a lock on a shared Guest when cloning isn't possible, so that two Runs never fight over one Guest.
17. As a developer, I want the Guest architecture to follow my Host's by default, so that Guests run natively and fast.
18. As a developer on arm64, I want the option to test x64 Windows builds via Windows' built-in emulation, so that the builds my users actually download get some coverage.
19. As a developer, I want an explicit warning when an architecture cannot be tested on this Host, so that I know the coverage gap exists.
20. As a developer, I want to declare several Labs for different OS versions, so that I can test on e.g. two macOS releases.
21. As a developer, I want the SIP-off / TCC-edit trade-off on macOS Guests explained, so that I understand it only affects the throwaway Guest.

### Setup — project

22. As a developer, I want the agent to create the project's vmlab folder with a commented config, so that I can read and edit Lab definitions by hand.
23. As a developer, I want the config to describe how the Build artifact is found, installed and launched, and where the app keeps its state, so that deploy and reset are automatic.
24. As a developer, I want an optional build hook per Lab, so that the Build artifact is rebuilt when it is stale without me remembering the command.
25. As a developer, I want the agent to help write the build hook for my stack (Tauri, Electron, Qt, .NET, …), including cross-compilation advice, so that I don't research toolchains myself.
26. As a developer, I want a documented "build inside the Guest" recipe, so that stacks that are painful to cross-compile still work.
27. As a developer, I want the vmlab CLI vendored into my project at a pinned version, so that saved Scenarios always match the API next to them.
28. As a developer, I want a self-update command, so that I can move the project to a newer vmlab deliberately.
29. As a developer, I want run outputs git-ignored automatically, so that screenshots and logs don't pollute the repo.
30. As a developer, I want `doctor` to check Host, Provider, Guest reachability, Channels and helpers and say exactly what is wrong, so that I can fix setup problems quickly.
31. As a developer, I want `doctor --bench` to measure each Channel's latency on my machine, so that defaults can be tuned with data.
32. As a developer, I want the agent to route me to setup automatically when `doctor` fails, so that I never have to know which workflow to invoke.

### Ad-hoc run

33. As a developer, I want to describe steps in natural language, so that I can check a behaviour without writing test code.
34. As a developer, I want the latest Build artifact delivered and installed in the Guest, so that I test what I just built.
35. As a developer, I want the app's state reset before each Run, so that stale settings don't produce false results.
36. As a developer, I want to ask for a completely fresh Guest, so that I can rule out Guest-level leftovers.
37. As an agent, I want CLI primitives to find UI elements, click, press key chords, type text and take screenshots, all returning JSON, so that I can explore an unknown app step by step.
38. As an agent, I want the same primitive names and JSON shapes on all three OSes, so that my exploration skills transfer between Guests.
39. As an agent, I want to wait for a condition (element appears, text matches, process running, file exists, log line) with a timeout, so that I never rely on fixed sleeps.
40. As an agent, I want to stage test data in a third-party app (e.g. selected text in an editor) and trigger the app in one Guest call, so that focus cannot be stolen in between.
41. As an agent, I want to read the app's logs and files from the Guest, so that I can check behaviour that has no UI.
42. As an agent, I want to look at screenshots myself and judge them, so that I can check visual things accessibility can't express.
43. As a developer, I want visual judgements marked as "visual, unverified" in the report, so that I know which conclusions are non-deterministic.
44. As an agent, I want to turn the exploration into a Scenario and run it cleanly before reporting, so that "it works" rests on a reproducible Run.
45. As a developer, I want the report to include screenshots and logs as evidence, so that I can verify the agent's conclusion.
46. As a developer, I want to be offered to save the Scenario into the Regression suite, so that one-off checks become permanent coverage.
47. As a developer, I want the Guest kept running after an Ad-hoc run with a reminder of how to stop it, so that iteration is fast and nothing is silently left in memory.
48. As a developer, I want to run the same Scenario on several Labs, so that I can compare behaviour across OSes.

### Regression suite

49. As a developer, I want Scenarios as Python files using a cross-OS API, so that one Scenario covers all OSes with branches only where platforms differ.
50. As a developer, I want to run the whole suite, one Lab, or one Scenario from the command line, so that I can scope Runs.
51. As a developer, I want the suite to restore Clean state once at its start, so that it is deterministic without paying the restore cost per Scenario.
52. As a developer, I want a Scenario to declare that it needs its own restore, so that isolation-sensitive Scenarios are isolated.
53. As a developer, I want Guests that vmlab started to be stopped at the end of the suite by default, so that VMs don't linger in memory.
54. As a developer, I want Guests I started myself to be left alone, so that vmlab never kills my working session.
55. As a developer, I want a keep flag, so that I can inspect Guests after a failing suite.
56. As a developer, I want Labs run sequentially by default, so that my Host isn't starved of memory.
57. As a developer, I want an opt-in parallel mode that checks free memory and queues when short, so that nightly runs on a dedicated machine are faster.
58. As a developer, I want only deterministic Checks (accessibility, logs, files, processes) in the Regression suite, so that results don't flake.
59. As a developer, I want screenshots captured as artifacts even in regression, so that failures are debuggable.
60. As a CI system, I want a JUnit report and a non-zero exit code on any failed Check, so that I can gate on the suite.
61. As a developer, I want a JSON report, so that other tools and agents can consume results.
62. As a developer, I want a Markdown summary, so that I can read results quickly.
63. As a developer, I want each Run's outputs in a timestamped folder per Lab, so that history is kept and comparable.
64. As a developer, I want the suite runnable with only python3 installed, including the macOS system Python, so that no extra tooling is needed.
65. As a developer, I want a Scenario to fail with a clear message naming the Check, Lab and evidence, so that I know where to look.
66. As a developer, I want a way to prove a new Check goes red against a build without the fix, so that I trust it catches the regression.

### Channels and Providers

67. As a developer, I want each Guest to use its fastest reliable Channel automatically, so that Runs are quick.
68. As a developer, I want an automatic fallback to the other Channel when the preferred one fails, so that transient Channel problems don't fail the Run.
69. As a developer, I want Windows UI commands to reach the logged-in desktop, so that UI automation works despite SSH sessions being non-interactive.
70. As a developer, I want every Channel call to have a timeout, so that a hung Guest can't hang the suite.
71. As a contributor, I want a documented Provider interface and stub Providers for UTM and Parallels, so that I can add a hypervisor without touching Scenarios.
72. As a future Linux or Windows Host user, I want the same Scenarios to run once a Provider for my hypervisor exists, so that my suite is Host-independent.

### Skill and agents

73. As a Claude Code user, I want the skill to trigger on requests like "test my app on Windows", so that I don't need to remember its name.
74. As a Cursor user, I want install instructions for the skill, so that I can use it there too.
75. As a user of another agent, I want the skill to follow the open SKILL.md standard with plain-text references and shell-invokable tools, so that it works without an agent-specific adapter.
76. As an agent, I want a short router in SKILL.md that sends me to exactly one workflow (setup, ad-hoc run, regression authoring), so that I load only the context I need.
77. As an agent, I want documented traps per OS (TCC, lazy accessibility trees, focus stealing, keyboard layouts, Wayland input), so that I don't rediscover them.
78. As a developer, I want all host-level installs and large downloads to require my confirmation, so that the agent never changes my machine unexpectedly.

## Implementation Decisions

- **Skill layout**: one skill named `vmlab`. SKILL.md is a short router; three workflow documents (setup, ad-hoc run, regression authoring) plus per-OS and per-Provider reference notes are loaded on demand. Everything is in English; the agent converses in the user's language.
- **Host CLI**: a single Python CLI, standard library only, compatible with Python 3.9+. A pure-Python TOML parser is bundled inside the zipapp and used when the standard one is unavailable. Distributed as a zipapp that the skill copies into the project at a pinned version, with a self-update command (ADR 0001, ADR 0002).
- **Project contract**: a project-level vmlab folder containing a human-editable TOML config (Labs, Build artifact location, optional build hook, install/launch/reset recipe, app state paths), a scenarios folder, and a git-ignored runs folder. vmlab edits the config from templates and never rewrites user comments. Secrets and machine-specific values live in vmlab's home directory (strict permissions) or Keychain, never in the project.
- **Host state**: vmlab's home directory holds the dedicated SSH key, a dedicated known_hosts, Guest credentials, a registry of Base guests, and a record of which Guests vmlab started (used for stop-only-what-we-started).
- **Modules** (deep, small interfaces):
  - *Config* — load and validate project config; errors name the file, key and fix.
  - *Provider* — interface: detect/install-guidance, create Base guest, clone for project, up, down, is-running, snapshot/restore Clean state, IP, host-side screenshot where supported, copy in/out, native exec where supported. v1 implementations: Tart, VMware Fusion, and a Fake Provider used by tests. Stubs with guidance: UTM, Parallels.
  - *Channel* — interface: exec (argv, timeout, env, interactive-desktop flag) returning exit code/stdout/stderr, and file copy. Implementations: SSH (multiplexed), vmrun interactive exec (unique output file per call), SSH + interactive Scheduled Task (Windows fallback). Each Guest has an ordered preference; fallback on Channel failure, not on command failure (ADR 0003).
  - *Guest UI contract* — cross-OS commands: tree, find (by text/role), click, press chord, type, wait-for, screenshot, clipboard read/write, focus/stage-text. JSON output with identical shapes on all OSes. Native implementations: macOS Swift helper (AX, CGEvent) with JXA fallback; Windows PowerShell with UI Automation and SendInput; Linux Python AT-SPI with xdotool (X11) / a vmlab GNOME Shell extension (Wayland: window geometry, focus, exact pointer, input-method typing; ydotool was measured to misplace the pointer, and Shell Introspect refuses unknown callers).
  - *Deploy* — run build hook if the Build artifact is stale, deliver, install, reset app state, launch with environment.
  - *Runner* — discover and execute Scenario files per Lab; apply restore policy (once per suite, per Scenario when declared fresh); enforce timeouts; stop Guests vmlab started unless keep is set; sequential by default, parallel opt-in with a free-memory check and queueing.
  - *Scenario API* — the Python object Scenarios receive: Guest OS/arch, deploy/launch, UI contract methods, wait-for with timeout, log/file/process reads, screenshot, and Check recording (deterministic vs visual).
  - *Report* — per Run folder named by timestamp and Lab containing JSON report, JUnit XML, Markdown summary, screenshots and logs. Exit code non-zero if any Check failed or a Run errored.
  - *Doctor* — Host/Provider/Guest/Channel/helper diagnosis with actionable output; bench mode measures Channel latency.
- **Channel defaults**: macOS/Tart → SSH (tart exec for bootstrap and fallback); Linux/Fusion → SSH (vmrun fallback); Windows/Fusion → vmrun interactive (SSH + Scheduled Task fallback). On Providers without native exec, SSH is primary everywhere.
- **Clean state**: Tart via APFS clones of the provisioned Guest; Fusion via snapshot revert, with linked clones for per-project copies where supported and a lock-file-guarded shared Guest otherwise.
- **Architecture**: Guest architecture follows the Host. On arm64 Hosts, x64 Windows builds may run under Windows' built-in emulation; on x86_64 Hosts, arm64 is covered only where the guest OS emulates it. Uncoverable combinations produce an explicit warning.
- **macOS Guest security**: SIP disabled and TCC grants written directly in the Guest, the only non-MDM way to grant Accessibility, Apple Events and Screen Recording to automation. Documented as a Guest-only trade-off.
- **Windows acquisition**: Fusion's "Get Windows from Microsoft" plus Fusion's installer, then scripted provisioning over vmrun; an unattended-answer-file path is kept for future Providers.
- **Linux acquisition**: Ubuntu desktop with autoinstall; provisioning installs a desktop session suitable for automation, accessibility bus, input tools and autologin.
- **Waiting**: no fixed sleeps in vmlab or generated Scenarios; every wait is condition-plus-timeout.
- **Items to verify during implementation**: whether Fusion's "encrypt only TPM files" option lets vmrun work without a VM password; whether tart exec runs with GUI-session and TCC access (if so and faster, it may become the macOS primary Channel); whether Fusion supports linked clones of encrypted VMs.

## Testing Decisions

- **Good tests** exercise external behaviour only: they invoke the CLI as a user or CI would and assert on exit codes, report contents, and observable Guest lifecycle — never on internal functions or module structure. A refactor that keeps behaviour must not break a test.
- **Seam 1 — the CLI as a black box (primary, every commit)**: tests run the vendored CLI as a subprocess against a temp project whose config declares Labs backed by the Fake Provider. The Fake Provider is a real, shipped Provider that emulates a Guest in a temp directory: it records commands, serves scripted UI trees, writes placeholder screenshots, and can inject Channel failures, hangs and slow boots. Covered behaviour: config validation errors; up/down and stop-only-what-we-started; keep flag; restore once per suite and per fresh Scenario; app-state reset before each Run; Channel fallback on Channel failure but not command failure; timeouts; sequential vs parallel with memory check; report JSON/JUnit/Markdown contents and exit codes; deterministic vs visual Check marking; build hook staleness.
- **Seam 2 — Provider/Channel contract against real Guests (manual or scheduled)**: one generic contract suite (up, exec, copy, screenshot, restore, down, and every UI contract command) run against each real Provider/Lab: Tart+macOS, Fusion+Linux (X11 and Wayland sessions), Fusion+Windows. Proves what the Fake cannot: that vmrun, SSH, TCC grants and accessibility helpers actually work.
- **No unit tests on internals** (config parser, registries, report writers): they are covered through Seam 1.
- **Acceptance**: take a real cross-platform desktop app (tray app with a global hotkey, notifications and first-run flow) that already has hand-written VM labs, port its key checks into Scenarios and get them green on all three Labs; verify the skill triggers and routes correctly in Claude Code (Cursor optional).
- **Prior art**: hand-written per-OS bash VM labs — their "measure" pattern (run a Check against a build without the fix to prove it goes red) is carried over as a documented practice. The repo itself has no tests yet.

## Out of Scope

- Full support for Linux and Windows Hosts (only the Provider abstraction and documentation in v1).
- Real Providers for UTM, Parallels, Hyper-V, KVM/libvirt, VirtualBox (stubs and guidance only).
- Full x86_64 emulation of Guests on arm64 Hosts (and vice versa) beyond what the guest OS itself provides.
- An MCP server wrapping vmlab.
- Pixel-diff screenshot comparison as a default Check (possible later as opt-in).
- Running the suite on a schedule (launchd, self-hosted or cloud CI runners), including guidance for it.
- Acquiring Windows media outside Fusion's built-in flow, and any licence handling.
- Verification in Codex or other agents beyond Claude Code (and optionally Cursor), though the skill follows the open standard.
- Mobile platforms and web apps.

## Further Notes

- Common flaws of hand-written VM labs that this design deliberately fixes: copy-pasted per-OS libraries, hardcoded user paths and static IPs, personal SSH key and shared known_hosts, passwords visible in Guest process lists, a single shared output file racing between calls, fixed sleeps, no automatic restore between runs, stderr-only reporting, and docs contradicting code.
- The shipped x86_64 Windows and Linux builds of a typical app remain untested on an arm64 Host except for Windows via emulation; the skill surfaces this gap rather than hiding it.
- Suggested delivery order: CLI core with Fake Provider → Tart+macOS → Fusion+Linux → Fusion+Windows → skill texts → port of an existing app's labs.
