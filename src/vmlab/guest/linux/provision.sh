#!/bin/bash
# Provision an Ubuntu desktop Base guest for vmlab. Idempotent: safe to re-run after a failure.
#
# Runs as the Guest user over SSH, after the unattended install gave that user
# passwordless sudo, vmlab's SSH key and open-vm-tools. It prepares desktop
# sessions that automation can drive, GNOME on Wayland (the default) and Xfce
# on X11: autologin, no screen lock or blanking, the accessibility bus on,
# input and clipboard tools, vmlab's GNOME Shell extension, and nothing that
# pops up or takes the package lock in the middle of a Run.
#
# $1: a folder holding the Shell extension's files and the Notification recorder
# (vmlab copies them there first).
set -euo pipefail
EXTENSION_SRC=${1:?usage: provision.sh EXTENSION_DIR}
EXTENSION_UUID=vmlab-ui@vmlab
RECORDER=/usr/local/lib/vmlab/notification-recorder.py
RECORDER_UNIT=vmlab-notifications.service

say() { printf '  %s\n' "$*"; }
fail() { printf 'provision: %s\n' "$*" >&2; exit 1; }

sudo -n true 2>/dev/null || fail "the Guest user $(id -un) needs passwordless sudo (the vmlab install sets it)"
USER_NAME=$(id -un)
export DEBIAN_FRONTEND=noninteractive

say "apt: no background updates (they hold the package lock and pop up dialogs during Runs)"
sudo -n systemctl disable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
printf 'APT::Periodic::Update-Package-Lists "0";\nAPT::Periodic::Unattended-Upgrade "0";\n' |
  sudo -n tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null
say "snaps: no background refreshes (they download hundreds of MB into every clone and restart apps during Runs)"
sudo -n snap refresh --hold >/dev/null || fail "cannot hold snap refreshes"
# Wait for an apt run that started at boot, instead of failing on its lock.
for _ in $(seq 1 300); do
  sudo -n fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break
  sleep 1
done

say "packages: accessibility bus, input and clipboard tools, VMware Tools, an X11 session"
# at-spi2-core + python3-pyatspi read the UI tree (vmlab-ui.py); in X11 sessions
# xdotool sends input, python3-xlib asks the window manager, xclip reaches the
# clipboard and wmctrl tells that the window manager is up; open-vm-tools-desktop
# serves vmrun and resizes the screen; gnome-text-editor is where stage-text
# stages text.
PACKAGES="open-vm-tools-desktop openssh-server at-spi2-core python3-pyatspi gir1.2-atspi-2.0 python3-xlib xdotool xclip wmctrl x11-utils psmisc gnome-text-editor"
# GNOME 50 has no X11 session any more: Xfce provides one, on the same GDM. Without
# recommends it stays small (no screen saver, power manager or extra apps), but
# keeps a notification server: without xfce4-notifyd nothing answers apps' notifications.
X11_PACKAGES="xfce4-session xfwm4 xfce4-panel xfdesktop4 xfce4-settings xfce4-notifyd xserver-xorg-core xserver-xorg-input-libinput xserver-xorg-legacy dbus-x11"
missing() { for p in "$@"; do dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed" || echo "$p"; done; }
# shellcheck disable=SC2086
missing_packages=$(missing $PACKAGES) missing_x11=$(missing $X11_PACKAGES)
if [ -n "$missing_packages$missing_x11" ]; then
  sudo -n apt-get update -qq
  # shellcheck disable=SC2086
  [ -z "$missing_packages" ] || sudo -n apt-get install -y -qq $missing_packages >/dev/null
  # shellcheck disable=SC2086
  [ -z "$missing_x11" ] || sudo -n apt-get install -y -qq --no-install-recommends $missing_x11 >/dev/null
fi

say "power: the Guest never suspends (an idle GNOME would, and take VMware Tools with it)"
sudo -n systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1

say "no update notifier: its banners and dialogs cover the app under test"
mkdir -p ~/.config/autostart
printf '[Desktop Entry]\nType=Application\nName=update-notifier\nHidden=true\n' > ~/.config/autostart/update-notifier.desktop

say "no crash reporter: its dialogs steal focus"
sudo -n systemctl disable --now apport.service >/dev/null 2>&1 || true
[ -f /etc/default/apport ] && sudo -n sed -i 's/^enabled=1/enabled=0/' /etc/default/apport

say "desktop: log $USER_NAME in automatically, into GNOME on Wayland"
sudo -n tee /etc/gdm3/custom.conf >/dev/null <<EOF
# Written by vmlab: the Guest boots straight into the desktop session.
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=$USER_NAME
EOF
# GDM starts the session AccountsService names; Labs with session = "x11" switch their clone to xfce.
sudo -n python3 - "/var/lib/AccountsService/users/$USER_NAME" <<'EOF'
import configparser, sys
config = configparser.ConfigParser()
config.optionxform = str
config.read(sys.argv[1])
if not config.has_section("User"):
    config.add_section("User")
config["User"].update(Session="ubuntu", XSession="ubuntu", SessionType="wayland")
with open(sys.argv[1], "w") as f:
    config.write(f, space_around_delimiters=False)
EOF

say "desktop: no screen lock, blanking or sleep; accessibility on; no welcome tour; no recent files"
sudo -n mkdir -p /etc/dconf/profile /etc/dconf/db/local.d
printf 'user-db:user\nsystem-db:local\n' | sudo -n tee /etc/dconf/profile/user >/dev/null
sudo -n tee /etc/dconf/db/local.d/00-vmlab >/dev/null <<'EOF'
[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/desktop/lockdown]
disable-lock-screen=true

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
idle-dim=false

[org/gnome/desktop/interface]
toolkit-accessibility=true

[org/gnome/desktop/a11y]
always-show-universal-access-status=false

[org/gnome/shell]
welcome-dialog-last-shown-version='999'

[org/gnome/software]
download-updates=false
allow-updates=false

# Every Run opens files (Staged documents): a list of recent files would grow with each one.
# gnome-text-editor keeps its own, which this turns off too.
[org/gnome/desktop/privacy]
remember-recent-files=false

# The stock editor reopens no documents an earlier Run left open.
[org/gnome/TextEditor]
restore-session=false

[org/gnome/shell]
enabled-extensions=['vmlab-ui@vmlab']
disable-extension-version-validation=true
disable-user-extensions=false
EOF
# The session writes its own toolkit-accessibility=false at login; a lock keeps ours.
# GNOME Shell sets disable-user-extensions when it stops within its first minute, which it
# takes for a crash (a Guest shut down while its session starts is one): the lock keeps
# vmlab's extension on. This Guest has no other extensions to protect.
sudo -n mkdir -p /etc/dconf/db/local.d/locks
printf '%s\n' /org/gnome/desktop/interface/toolkit-accessibility /org/gnome/shell/disable-user-extensions | sudo -n tee /etc/dconf/db/local.d/locks/00-vmlab >/dev/null
sudo -n dconf update
# The welcome wizard runs at first login and, as a "what's new" tour, after every
# release upgrade; either covers the app under test.
sudo -n systemctl --global mask gnome-initial-setup-first-login.service gnome-initial-setup-upgrade-login.service >/dev/null 2>&1
mkdir -p ~/.config
echo yes > ~/.config/gnome-initial-setup-done

say "UI: vmlab's GNOME Shell extension (window geometry, focus, input and clipboard on Wayland)"
sudo -n rm -rf "/usr/share/gnome-shell/extensions/$EXTENSION_UUID"
sudo -n mkdir -p "/usr/share/gnome-shell/extensions/$EXTENSION_UUID"
sudo -n cp "$EXTENSION_SRC"/metadata.json "$EXTENSION_SRC"/extension.js "/usr/share/gnome-shell/extensions/$EXTENSION_UUID/"

say "UI: vmlab's Notification recorder (Linux keeps no history of Notifications), for both Desktop sessions"
# A user unit, started with the user's systemd manager at login: whichever session starts,
# the session bus it records is already there (dbus-user-session).
sudo -n install -D -m 644 "$EXTENSION_SRC/notification-recorder.py" "$RECORDER"
sudo -n tee "/etc/systemd/user/$RECORDER_UNIT" >/dev/null <<EOF
# Written by vmlab: records the Notifications apps post, for vmlab ui notifications.
[Unit]
Description=vmlab Notification recorder

[Service]
ExecStart=/usr/bin/python3 $RECORDER
Restart=always
RestartSec=1

[Install]
WantedBy=default.target
EOF
sudo -n systemctl --global enable "$RECORDER_UNIT" >/dev/null
rm -rf "$EXTENSION_SRC"

say "session environment: Qt apps join the accessibility bus; no peer-to-peer AT-SPI; WebKitGTK draws without DMA-BUF"
# WebKitGTK's DMA-BUF renderer paints a window once and then never again on the Guest's
# software GL (Fusion passes no 3D to arm64 Linux): screenshots would freeze on the first frame.
# ATSPI_DISABLE_P2P: GTK 3 apps (atk-bridge) otherwise take peer-to-peer connections from every
# AT-SPI client, and libdbus keeps each client's pidfd after it disconnects: every UI call, a
# process of its own, left 3 in each app, and the Xfce session ran out of fds after a few dozen
# Runs. Over the accessibility bus nothing is left behind.
sudo -n mkdir -p /etc/environment.d
printf 'QT_ACCESSIBILITY=1\nQT_LINUX_ACCESSIBILITY_ALWAYS_ON=1\nATSPI_DISABLE_P2P=1\nWEBKIT_DISABLE_DMABUF_RENDERER=1\n' | sudo -n tee /etc/environment.d/90-vmlab-a11y.conf >/dev/null

say "X11: no screen blanking"
sudo -n mkdir -p /etc/X11/xorg.conf.d
sudo -n tee /etc/X11/xorg.conf.d/10-vmlab-no-blanking.conf >/dev/null <<'EOF'
# Written by vmlab: a blank screen hides the app under test from screenshots.
Section "ServerFlags"
    Option "BlankTime" "0"
    Option "StandbyTime" "0"
    Option "SuspendTime" "0"
    Option "OffTime" "0"
EndSection
EOF

say "SSH: key logins only"
sudo -n systemctl enable ssh >/dev/null 2>&1 || true

sync
say "done"
