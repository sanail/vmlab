#!/bin/bash
# Provision a macOS Base guest for vmlab. Idempotent: safe to re-run after a failure.
#
# Runs as the Guest user through `tart exec` (the Tart guest agent), which needs
# nothing set up yet. Arguments: vmlab's SSH public key, and the path of the UI
# helper's Swift source, already copied into the Guest.
#
# Security trade-off, Guest only: SIP must be off, because granting Accessibility,
# Screen Recording and Apple Events to automation without MDM means writing TCC.db
# directly. The Guest is a throwaway clone; the Host is never touched.
set -euo pipefail

PUBKEY="$1"
UI_SRC="$2"
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

say "desktop: no apps or windows restored at login"
defaults write NSGlobalDomain NSQuitAlwaysKeepsWindows -bool false
defaults write com.apple.loginwindow TALLogoutSavesState -bool false
# loginwindow relaunches whatever ran when the Guest was powered off (the image
# ships with Terminal). The per-host global domain turns that off; the plain
# global one does too, but makes AppKit log "ApplePersistence=NO" more widely.
defaults -currentHost write -g ApplePersistence -bool false
rm -rf ~/Library/Saved\ Application\ State/*.savedState
osascript -e 'quit app "Terminal"' >/dev/null 2>&1 || true

say "typing: no automatic capitalisation, spelling correction or smart punctuation"
# Otherwise apps rewrite what vmlab types ("typed" arrives as "Typed").
for key in NSAutomaticCapitalizationEnabled NSAutomaticSpellingCorrectionEnabled NSAutomaticPeriodSubstitutionEnabled \
  NSAutomaticQuoteSubstitutionEnabled NSAutomaticDashSubstitutionEnabled NSAutomaticTextCompletionEnabled \
  WebAutomaticSpellingCorrectionEnabled; do
  defaults write -g "$key" -bool false
done

say "UI helper: compile vmlab-ui (Accessibility + CGEvent)"
UI_DIR=/usr/local/vmlab
if ! xcrun --find swiftc >/dev/null 2>&1; then
  say "  no Swift compiler (Command Line Tools): UI commands will use the slower JXA fallback"
elif [ -x "$UI_DIR/bin/vmlab-ui" ] && cmp -s "$UI_SRC" "$UI_DIR/src/vmlab-ui.swift"; then
  say "  already built from this source"
else
  sudo -n mkdir -p "$UI_DIR/bin" "$UI_DIR/src"
  xcrun swiftc -O -o "$UI_SRC.bin" "$UI_SRC" || fail "compiling the UI helper failed (output above)"
  sudo -n install -m 755 "$UI_SRC.bin" "$UI_DIR/bin/vmlab-ui"
  sudo -n install -m 644 "$UI_SRC" "$UI_DIR/src/vmlab-ui.swift"
  rm -f "$UI_SRC.bin"
fi

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

say "Screen Recording: no consent alert for vmlab's Channels"
# Even with the TCC grant, macOS 15+ alerts ("... is requesting to bypass the system
# private window picker") when a client captures the screen without the picker: at its
# first capture, and again every 30 days. The alert covers whatever a Scenario is
# looking at. replayd keys its approvals by the client's executable path, even where
# the alert names a bundle id (com.apple.sshd-session for SSH sessions on macOS 26):
# sshd-keygen-wrapper for SSH, and the Tart guest agent. Without a recorded use an
# entry still gets the first-capture alert, so each records one; the next 30-day
# alert is dated 2100.
APPROVALS="$HOME/Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist"
mkdir -p "$(dirname "$APPROVALS")"
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{
  printf '<?xml version="1.0" encoding="UTF-8"?>\n'
  printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
  printf '<plist version="1.0"><dict>\n'
  for c in "${clients[@]}"; do
    [ "${c%% *}" = 1 ] || continue  # paths only
    printf '<key>%s</key><dict>' "${c#* }"
    printf '<key>kScreenCaptureAlertableUsageCount</key><integer>1</integer>'
    printf '<key>kScreenCaptureApprovalLastAlerted</key><date>%s</date>' "$now"
    printf '<key>kScreenCaptureApprovalLastUsed</key><date>%s</date>' "$now"
    printf '<key>kScreenCapturePrivacyHintDate</key><date>2100-01-01T00:00:00Z</date>'
    printf '<key>kScreenCapturePrivacyHintPolicy</key><integer>2592000</integer></dict>\n'
  done
  printf '</dict></plist>\n'
} > "$APPROVALS"
plutil -lint -s "$APPROVALS" || fail "wrote an invalid $APPROVALS"

say "done; tccd reads the grants after a reboot"
