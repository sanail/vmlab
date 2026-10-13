#!/bin/sh
# Grant a Lab's permissions ([labs.<name>] grant) to apps in a Linux Guest, or check that they hold.
#
# Arguments: apply|check, then pairs of a permission name and an app: an installed path or an app
# id. check prints one line per grant that does not hold and exits 1 if any.
#
# Only a Wayland session asks: screen_capture is a "yes" in the portal's permission store for a
# non-interactive Screenshot. An app the store knows by an id is granted by it; one started from a
# shell (the launch recipe) has no id, which the store keys as "". X11 asks for nothing.
MODE="$1"; shift
uid=$(id -u)
export XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus"
session=$(loginctl show-user "$uid" -p Display --value 2>/dev/null)
[ -n "$session" ] || { echo "no graphical session is logged in, so its permissions cannot be read or given"; exit 1; }
[ "$(loginctl show-session "$session" -p Type --value)" = wayland ] || exit 0

store() {
  gdbus call --session -d org.freedesktop.impl.portal.PermissionStore -o /org/freedesktop/impl/portal/PermissionStore \
    -m "org.freedesktop.impl.portal.PermissionStore.$@"
}

missing=0
while [ $# -ge 2 ]; do
  name="$1" app="$2"; shift 2
  [ "$name" = screen_capture ] || continue
  case "$app" in /*) id="" ;; *) id="$app" ;; esac
  if [ "$MODE" = apply ]; then
    store SetPermission screenshot true screenshot "$id" "['yes']" >/dev/null || exit 1
  elif ! store Lookup screenshot screenshot 2>/dev/null | grep -qF "'$id': ['yes'"; then
    echo "the portal's permission store does not let ${id:-an app with no app id} take screenshots"
    missing=1
  fi
done
exit $missing
