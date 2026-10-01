# The skill carries a launcher that downloads its vmlab release

The vmlab zipapp is a build product and stays out of git, yet the skill must reach agents in one step: as a Claude Code plugin, through `npx skills`, or as a copied folder. So the skill ships `scripts/vmlab`, a small sh launcher that names one vmlab version; on first use it downloads `vmlab-X.Y.Z.pyz` from that GitHub Release into `~/.vmlab/dist/`, checks that it reports that version, and runs it. A tag `vX.Y.Z` makes CI publish the release and fast-forward the `stable` branch, which the plugin marketplace and `npx skills` install from, so an installed skill always names a release that exists.

A project pins the same way (ADR 0002): `.vmlab/vmlab` is the launcher with the project's version and the sha256 of its `vmlab.pyz`. It runs that file when git has it, and otherwise downloads the release and refuses one whose digest differs, so a release replaced after the project pinned it never runs. `VMLAB_PYZ` makes every launcher run a local build instead.

The cost: the first use of a skill, or of a project that keeps the zipapp out of git, needs github.com; and the skill's own download is checked only by the version it reports, since the digest exists only after the release is built.

## Considered options

- **Plugin as a release archive** (a zip with the zipapp inside, the marketplace entry pointing at it with its sha256): works offline once installed, but CI must commit each release's URL and digest back to `main`, and `npx skills` and copied folders, which take files from git, would still lack the zipapp.
- **The skill carries vmlab's source** and builds the zipapp locally: no download, but the skill grows by the whole package, and it rebuilds only its own version, not the one a project pinned.
- **Commit the zipapp** to the repository or to a release branch: simplest to install, but a binary in git history on every release.
