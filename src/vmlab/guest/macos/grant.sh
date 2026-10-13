#!/bin/bash
# Grant a Lab's permissions ([labs.<name>] grant) to apps in a macOS Guest, or check that they hold.
#
# Arguments: apply|check, then pairs of a permission name and an app: an installed path (an .app
# bundle or a bare executable) or a bundle id. Runs as the Guest user, with passwordless sudo.
# check prints one line per grant that does not hold and exits 1 if any.
#
# tccd reads new TCC.db rows at once, so no reboot is needed. A running replayd keeps its approvals
# in memory and writes them back at its next capture, so it is restarted after the write.
# A grant is given to the responsible process, the app, also for a screencapture it starts.
set -uo pipefail

MODE="$1"; shift
SYS_DB="/Library/Application Support/com.apple.TCC/TCC.db"
USER_DB="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
APPROVALS="$HOME/Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals"
missing=0
restart_replayd=0
miss() { printf '%s\n' "$*"; missing=1; }

# Sets client, client_type (0 a bundle id, 1 a path) and keys, the names replayd may file the app's
# approval under: an .app's bundle id, its executable's path and its file URL (an ad-hoc signed
# app's), a bare executable's path (SSH's sshd-keygen-wrapper).
resolve() {
  local app="$1" plist="$1/Contents/Info.plist" exe url
  if [ "${app#/}" = "$app" ]; then
    client="$app" client_type=0 keys=("$app")
  elif [ -f "$plist" ]; then
    client=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$plist" 2>/dev/null) ||
      { problem="$plist has no CFBundleIdentifier"; return 1; }
    exe=$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$plist" 2>/dev/null)
    url=$(/usr/bin/osascript -l JavaScript -e 'function run(a) { return $.NSURL.fileURLWithPathIsDirectory(a[0], true).absoluteString.js }' "$app") ||
      { problem="no file URL for $app"; return 1; }
    client_type=0 keys=("$client" "$app/Contents/MacOS/$exe" "$url")
  elif [ -f "$app" ]; then
    client="$app" client_type=1 keys=("$app")
  else
    problem="no app at $app"; return 1
  fi
}

tcc() {  # tcc DB SERVICE
  local db="$1" service="$2" sq="'" q
  q="'${client//$sq/$sq$sq}'"
  local where="service='$2' AND client=$q AND client_type=$client_type"
  if [ "$MODE" = apply ]; then
    # A denial macOS wrote when the app asked earlier wins over a grant: it goes first.
    sudo -n sqlite3 "$db" "DELETE FROM access WHERE $where;
      INSERT INTO access (service, client, client_type, auth_value, auth_reason, auth_version, flags, last_modified)
      VALUES ('$service', $q, $client_type, 2, 4, 1, 0, CAST(strftime('%s','now') AS INTEGER));" || exit 1
  elif [ "$(sudo -n sqlite3 "$db" "SELECT auth_value FROM access WHERE $where;")" != 2 ]; then
    miss "$service is not granted to $client"
  fi
}

replayd() {  # without an approval, a capture with no picker raises "... is requesting to bypass the system private window picker"
  local key now
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  for key in "${keys[@]}"; do
    if [ "$MODE" = apply ]; then
      defaults write "$APPROVALS" "$key" "<dict>
        <key>kScreenCaptureAlertableUsageCount</key><integer>1</integer>
        <key>kScreenCaptureApprovalLastAlerted</key><date>$now</date>
        <key>kScreenCaptureApprovalLastUsed</key><date>$now</date>
        <key>kScreenCapturePrivacyHintDate</key><date>2100-01-01T00:00:00Z</date>
        <key>kScreenCapturePrivacyHintPolicy</key><integer>2592000</integer></dict>" || exit 1
      restart_replayd=1
    elif ! defaults read "$APPROVALS" "$key" >/dev/null 2>&1; then
      miss "replayd has no Screen Recording approval for $key, so a capture raises its alert"
    fi
  done
}

while [ $# -ge 2 ]; do
  name="$1" app="$2"; shift 2
  if ! resolve "$app"; then
    [ "$MODE" = check ] || { echo "$problem" >&2; exit 1; }
    miss "$name cannot be granted: $problem"; continue
  fi
  case "$name" in
    screen_capture) tcc "$SYS_DB" kTCCServiceScreenCapture; replayd ;;
    accessibility) tcc "$SYS_DB" kTCCServiceAccessibility; tcc "$SYS_DB" kTCCServicePostEvent ;;
    input_monitoring) tcc "$SYS_DB" kTCCServiceListenEvent ;;
    camera) tcc "$USER_DB" kTCCServiceCamera ;;
    microphone) tcc "$USER_DB" kTCCServiceMicrophone ;;
  esac
done
if [ $restart_replayd = 1 ]; then
  launchctl kickstart -k "gui/$(id -u)/com.apple.replayd" || exit 1
fi
exit $missing
