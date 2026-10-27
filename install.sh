#!/usr/bin/env bash
# AirBridge installer for Raspberry Pi OS (Bookworm / Trixie), Pi 4 or Pi 5.
#
# Needs internet while installing (apt + pip). After that AirBridge needs no
# internet at all, so the Pi can go onto an airgapped network.
#
#   sudo ./install.sh                     install / update
#   sudo ./install.sh --ntp 192.168.1.1   also use a local NTP server once airgapped
set -euo pipefail

APP=/opt/airbridge
DATA=/var/lib/airbridge
SVC_USER=airbridge
SRC="$(cd "$(dirname "$0")" && pwd)"
PKGS="python3-venv python3-pip python3-lgpio libportaudio2 ffmpeg i2c-tools"
NTP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ntp) NTP="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then echo "Run with sudo"; exit 1; fi

echo "==> packages"
apt-get update -qq
apt-get install -y -qq $PKGS

echo "==> enable I2C"
if command -v raspi-config >/dev/null; then raspi-config nonint do_i2c 0 || true; fi

echo "==> K5TRA HID access (PTT/COS) for the audio group"
cat > /etc/udev/rules.d/90-airbridge-cm119.rules <<'RULE'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0d8c", GROUP="audio", MODE="0660"
RULE
udevadm control --reload-rules && udevadm trigger --subsystem-match=hidraw || true

echo "==> user and folders"
id -u $SVC_USER >/dev/null 2>&1 || useradd --system --home $DATA --shell /usr/sbin/nologin $SVC_USER
for g in audio gpio i2c; do getent group $g >/dev/null && usermod -aG $g $SVC_USER; done
mkdir -p $APP $DATA
# refresh the app folder (keeps the venv), without needing rsync
find $APP -mindepth 1 -maxdepth 1 ! -name venv -exec rm -rf {} +
tar -C "$SRC" --exclude=.git --exclude=venv --exclude=bundle --exclude=__pycache__ --exclude='*.tar.gz' -cf - . \
  | tar -C $APP -xf -
chown -R $SVC_USER:$SVC_USER $DATA

echo "==> python environment"
# system site packages so the apt-installed lgpio (Pi 5 GPIO) is visible
python3 -m venv --system-site-packages $APP/venv
$APP/venv/bin/pip install -q --upgrade pip
$APP/venv/bin/pip install -q -r $APP/requirements.txt

echo "==> clock"
# Once airgapped there's no public NTP (and a Pi 4 has no RTC). Use --ntp for a local
# time server, and/or let admins set the clock from the web page
# (Access -> System clock) through a tiny root helper that only accepts an epoch time.
cat > /usr/local/sbin/airbridge-setclock <<'SH'
#!/bin/sh
# usage: airbridge-setclock <unix-seconds>   (called by the AirBridge service via sudo)
case "$1" in ''|*[!0-9]*) echo "need unix seconds" >&2; exit 2 ;; esac
[ "$1" -gt 1700000000 ] && [ "$1" -lt 4102444800 ] || { echo "time out of range" >&2; exit 2; }
timedatectl set-ntp false 2>/dev/null || true
date -s "@$1" >/dev/null
command -v fake-hwclock >/dev/null && fake-hwclock save >/dev/null 2>&1
[ -e /dev/rtc0 ] && hwclock --systohc 2>/dev/null
[ -f /etc/systemd/timesyncd.conf.d/airbridge.conf ] && timedatectl set-ntp true 2>/dev/null
exit 0
SH
chmod 755 /usr/local/sbin/airbridge-setclock
echo "$SVC_USER ALL=(root) NOPASSWD: /usr/local/sbin/airbridge-setclock" > /etc/sudoers.d/airbridge-clock
chmod 440 /etc/sudoers.d/airbridge-clock
visudo -cf /etc/sudoers.d/airbridge-clock >/dev/null
if [[ -n "$NTP" ]]; then
  mkdir -p /etc/systemd/timesyncd.conf.d
  printf '[Time]\nNTP=%s\nFallbackNTP=\n' "$NTP" > /etc/systemd/timesyncd.conf.d/airbridge.conf
  timedatectl set-ntp true || true
  systemctl restart systemd-timesyncd || true
  echo "   using NTP server $NTP"
fi

echo "==> service"
cp $APP/airbridge.service /etc/systemd/system/airbridge.service
systemctl daemon-reload
systemctl enable airbridge
systemctl restart airbridge

IP=$(hostname -I | awk '{print $1}')
echo
echo "AirBridge is running: http://$IP:8080"
sleep 3
if [[ -f $DATA/setup-code.txt ]]; then
  echo
  echo "First-time setup: open http://$IP:8080/setup and create the admin account."
  echo "Setup code: $(cat $DATA/setup-code.txt)"
fi
echo "Clock: $(date)  (set it from Access -> System clock if this is wrong)"
echo "Lost the admin password?  sudo -u $SVC_USER AIRBRIDGE_DATA=$DATA $APP/venv/bin/python -m airbridge user passwd <name>"
echo "Logs: journalctl -u airbridge -f"
