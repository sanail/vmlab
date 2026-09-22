#!/bin/bash
# Provision a macOS Base guest for vmlab. Idempotent: safe to re-run after a failure.
#
# Runs as the Guest user through `tart exec` (the Tart guest agent), which needs
# nothing set up yet. Argument: vmlab's SSH public key.
#
# Security trade-off, Guest only: SIP must be off, because granting Accessibility,
# Screen Recording and Apple Events to automation without MDM means writing TCC.db
# directly. The Guest is a throwaway clone; the Host is never touched.
set -euo pipefail

PUBKEY="$1"
say() { printf '  %s\n' "$*"; }
fail() { printf 'provision: %s\n' "$*" >&2; exit 1; }

sudo -n true 2>/dev/null || fail "the Guest user $(id -un) needs passwordless sudo (cirruslabs images have it)"
csrutil status | grep -q disabled ||
  fail "SIP is enabled, so TCC grants cannot be written; use a cirruslabs *-base image, or disable SIP in the Guest's recovery mode"

say "SSH: authorise vmlab's key"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qxF "$PUBKEY" ~/.ssh/authorized_keys || printf '%s\n' "$PUBKEY" >> ~/.ssh/authorized_keys

say "SSH: Remote Login on"
if ! nc -z -G 2 127.0.0.1 22 2>/dev/null; then
  sudo -n launchctl enable system/com.openssh.sshd
  sudo -n launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist 2>/dev/null || true
fi

say "power: never sleep, no screen saver"
sudo -n pmset -a sleep 0 displaysleep 0 disksleep 0 >/dev/null
defaults -currentHost write com.apple.screensaver idleTime -int 0

say "desktop: no windows restored at login"
defaults write NSGlobalDomain NSQuitAlwaysKeepsWindows -bool false
defaults write com.apple.loginwindow TALLogoutSavesState -bool false
rm -rf ~/Library/Saved\ Application\ State/*.savedState

say "TCC: grant automation to vmlab's Channels"
SYS_DB="/Library/Application Support/com.apple.TCC/TCC.db"
USER_DB="$HOME/Library/Application Support/com.apple.TCC/TCC.db"

# grant DB SERVICE CLIENT_TYPE CLIENT [TARGET_BUNDLE_ID]
# CLIENT_TYPE 0 is a bundle id, 1 a path. Apple Events are granted per target app.
grant() {
  local db="$1" service="$2" type="$3" client="$4" target="${5:-}" target_type=NULL
  [ -n "$target" ] && target_type=0 || target=UNUSED
  sudo -n sqlite3 "$db" "INSERT OR REPLACE INTO access
    (service, client, client_type, auth_value, auth_reason, auth_version,
     indirect_object_identifier_type, indirect_object_identifier, flags, last_modified)
    VALUES ('$service', '$client', $type, 2, 4, 1, $target_type, '$target', 0, CAST(strftime('%s','now') AS INTEGER));"
}

# Who asks: SSH sessions (a bundle id on macOS 26, a path before), and the Tart
# guest agent behind `tart exec`, under its invoked and its resolved path.
clients=("0 com.apple.sshd-session" "1 /usr/libexec/sshd-keygen-wrapper")
for agent in /opt/homebrew/bin/tart-guest-agent /usr/local/bin/tart-guest-agent; do
  [ -e "$agent" ] || continue
  clients+=("1 $agent" "1 $(realpath "$agent")")
done

for c in "${clients[@]}"; do
  type="${c%% *}" client="${c#* }"
  for service in kTCCServiceAccessibility kTCCServicePostEvent kTCCServiceListenEvent kTCCServiceScreenCapture; do
    grant "$SYS_DB" "$service" "$type" "$client"
  done
  for target in com.apple.systemevents com.apple.finder; do
    grant "$USER_DB" kTCCServiceAppleEvents "$type" "$client" "$target"
  done
done

say "done; tccd reads the grants after a reboot"
