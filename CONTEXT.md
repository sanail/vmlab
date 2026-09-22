# vmlab

An agent skill for testing desktop applications inside virtual machines running macOS, Windows and Linux, driven from a single host machine.

## Language

**Host**:
The machine where the agent and the hypervisor run.
_Avoid_: local machine, runner

**Guest**:
One virtual machine running one operating system, in which the application is tested.
_Avoid_: VM (in prose), box, target

**Provider**:
An adapter for one hypervisor (e.g. Tart, VMware Fusion, UTM) that knows how to start, stop, reset and talk to its **Guests**.
_Avoid_: backend, driver, hypervisor (when meaning the adapter)

**Lab**:
A project's description of one **Guest**: which **Provider**, which base image, how it is provisioned, and which state counts as clean.
_Avoid_: environment, config

**Channel**:
The way the **Host** executes commands inside a **Guest** (e.g. SSH, the **Provider**'s own guest-exec). A **Guest** has a preferred **Channel** and may fall back to another.
_Avoid_: transport, connection

**Clean state**:
The state a **Guest** is restored to before isolated work (a snapshot or clone of the provisioned **Guest**).
_Avoid_: golden (in prose), baseline, fresh image

**Base guest**:
A provisioned **Guest** for one OS version and architecture, shared by all projects on the **Host**. Projects never run in it directly; they work in clones of it.
_Avoid_: golden image, template

**Build artifact**:
The packaged application (e.g. `.app`/`.dmg`, `.msi`/`.exe`, `.AppImage`/`.deb`) that is delivered into a **Guest**.
_Avoid_: binary, package, build

**Desktop session**:
The graphical login session inside a Linux **Guest** (GNOME on Wayland, or Xfce on X11) whose screen the UI contract drives. A **Lab** chooses one.
_Avoid_: session (alone, which could mean a **Run**)

**Scenario**:
An ordered sequence of steps performed against the application in a **Guest**, containing **Checks**.
_Avoid_: test case, script, flow

**Check**:
A single assertion inside a **Scenario** that yields pass or fail.
_Avoid_: assertion, expectation

**Run**:
One execution of one **Scenario** on one **Guest**, producing a report, screenshots and logs.
_Avoid_: session, job

**Ad-hoc run**:
A **Run** of a **Scenario** that is not saved in the project — a one-off request from the user.

**Regression suite**:
The set of **Scenarios** saved in the project so they can be run repeatedly without the agent.
_Avoid_: test suite, smoke tests

## Relationships

- A project declares one or more **Labs**; each **Lab** is realised by exactly one **Provider** as one **Guest**
- A **Lab** clones one **Base guest**; many projects' **Labs** may clone the same **Base guest**
- A **Lab** covers exactly one OS version and architecture; testing several versions means declaring several **Labs**
- A **Scenario** contains one or more **Checks**
- A **Run** executes one **Scenario** on one **Guest** against one **Build artifact**
- An **Ad-hoc run** and a **Regression suite** use the same **Scenario** format; they differ only in whether the **Scenario** is saved
- A **Regression suite** restores **Clean state** once at its start; a **Scenario** may demand its own restore
- A **Guest**'s architecture follows the **Host**'s; the other architecture is covered only where the guest OS emulates it

## Flagged ambiguities

- "VM" was used for both the running machine and its definition — resolved: the running machine is a **Guest**, its project-level definition is a **Lab**.
