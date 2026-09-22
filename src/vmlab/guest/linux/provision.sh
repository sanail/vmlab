#!/bin/bash
# Provision an Ubuntu desktop Base guest for vmlab. Idempotent: safe to re-run after a failure.
#
# Runs as the Guest user over SSH, after the unattended install gave that user
# passwordless sudo, vmlab's SSH key and open-vm-tools. It prepares a desktop
# session that automation can drive: autologin, no screen lock or blanking, the
# accessibility bus on, input and clipboard tools, and nothing that pops up or
# takes the package lock in the middle of a Run.
set -euo pipefail

say() { printf '  %s\n' "$*"; }
fail() { printf 'provision: %s\n' "$*" >&2; exit 1; }

sudo -n true 2>/dev/null || fail "the Guest user $(id -un) needs passwordless sudo (the vmlab install sets it)"
USER_NAME=$(id -un)
export DEBIAN_FRONTEND=noninteractive

say "apt: no background updates (they hold the package lock and pop up dialogs during Runs)"
sudo -n systemctl disable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
printf 'APT::Periodic::Update-Package-Lists "0";\nAPT::Periodic::Unattended-Upgrade "0";\n' |
  sudo -n tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null
# Wait for an apt run that started at boot, instead of failing on its lock.
for _ in $(seq 1 300); do
  sudo -n fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break
  sleep 1
done

say "packages: accessibility bus, input and clipboard tools, VMware Tools"
# at-spi2-core + python3-pyatspi read the UI tree; xdotool (X11) and ydotool
# (Wayland, through /dev/uinput) send input; wl-clipboard and xclip reach the
# clipboard; open-vm-tools-desktop serves vmrun and resizes the screen.
PACKAGES="open-vm-tools-desktop openssh-server at-spi2-core python3-pyatspi gir1.2-atspi-2.0 xdotool ydotool wl-clipboard xclip wmctrl x11-utils psmisc"
missing=$(for p in $PACKAGES; do dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed" || echo "$p"; done)
if [ -n "$missing" ]; then
  sudo -n apt-get update -qq
  # shellcheck disable=SC2086
  sudo -n apt-get install -y -qq $missing >/dev/null
fi

say "power: the Guest never suspends (an idle GNOME would, and take VMware Tools with it)"
sudo -n systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1

say "no update notifier: its banners and dialogs cover the app under test"
mkdir -p ~/.config/autostart
printf '[Desktop Entry]\nType=Application\nName=update-notifier\nHidden=true\n' > ~/.config/autostart/update-notifier.desktop

say "no crash reporter: its dialogs steal focus"
sudo -n systemctl disable --now apport.service >/dev/null 2>&1 || true
[ -f /etc/default/apport ] && sudo -n sed -i 's/^enabled=1/enabled=0/' /etc/default/apport

say "desktop: log $USER_NAME in automatically"
sudo -n tee /etc/gdm3/custom.conf >/dev/null <<EOF
# Written by vmlab: the Guest boots straight into the desktop session.
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=$USER_NAME
EOF

say "desktop: no screen lock, blanking or sleep; accessibility on; no welcome tour"
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
EOF
# The session writes its own toolkit-accessibility=false at login; a lock keeps ours.
sudo -n mkdir -p /etc/dconf/db/local.d/locks
echo /org/gnome/desktop/interface/toolkit-accessibility | sudo -n tee /etc/dconf/db/local.d/locks/00-vmlab >/dev/null
sudo -n dconf update
# The welcome wizard runs at first login and, as a "what's new" tour, after every
# release upgrade; either covers the app under test.
sudo -n systemctl --global mask gnome-initial-setup-first-login.service gnome-initial-setup-upgrade-login.service >/dev/null 2>&1
mkdir -p ~/.config
echo yes > ~/.config/gnome-initial-setup-done

say "accessibility: Qt apps join the accessibility bus too"
sudo -n mkdir -p /etc/environment.d
printf 'QT_ACCESSIBILITY=1\nQT_LINUX_ACCESSIBILITY_ALWAYS_ON=1\n' | sudo -n tee /etc/environment.d/90-vmlab-a11y.conf >/dev/null

say "input: ydotool's daemon for Wayland input, with /dev/uinput (group input) for $USER_NAME"
sudo -n usermod -aG input "$USER_NAME"
sudo -n systemctl --global enable ydotool.service >/dev/null 2>&1

say "SSH: key logins only"
sudo -n systemctl enable ssh >/dev/null 2>&1 || true

sync
say "done"
