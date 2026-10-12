# One coordinate space for the UI on every Guest OS: Desktop pixels

Up to 1.x each Guest OS's helper took and reported coordinates in its own space: points on macOS,
physical pixels on Windows, X pixels on X11, GNOME stage coordinates on Wayland.
A Scenario could not turn what it saw in a screenshot into a click without branching per OS,
and on macOS (Retina 2x) a screenshot pixel was not even the point a click at the same numbers landed on.

From 2.0 every coordinate the UI contract takes or returns — element bounds, `click(at=)`, `drag`,
`pointer`, `displays()` — is in **Desktop pixels**: a pixel of the Guest's screenshot is the point a click at the same
coordinates lands on.
Each helper converts to its OS's own space (the macOS helper by the display's backing scale, the GNOME Shell extension
from stage coordinates when the scale is not 1).
A Scenario never sees a per-OS space.

A Guest has one Display.
`displays()` returns a list and Desktop pixels are defined over the whole desktop,
so a second Display can come later without changing either; it was left out because Tart gives a macOS Guest no second
display and it is unproven that Fusion shows one in a Guest without a window.

The cost: a major release, since macOS Scenarios that click by coordinates or compute from bounds move from points to
pixels (×2 at the default scale); the migration is in the skill's `references/migrating.md`.

## Considered options

- **Document each OS's space** and let `screen()` report bounds in both pixels and points:
  no change to existing Scenarios, but every Scenario that uses coordinates branches per OS,
  and an agent clicking what it sees in a screenshot is wrong on macOS.
- **Logical points everywhere**: stable across scale changes, but a screenshot pixel then needs dividing by a scale that
  differs per Lab, and on Windows and X11 the helpers would convert away from their native space for no gain.
