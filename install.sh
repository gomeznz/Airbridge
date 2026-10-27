#!/usr/bin/env bash
# AirBridge installer for Raspberry Pi OS (Bookworm / Trixie), Pi 4 or Pi 5.
set -euo pipefail

APP=/opt/airbridge
DATA=/var/lib/airbridge
SVC_USER=airbridge

if [[ $EUID -ne 0 ]]; then echo "Run with sudo"; exit 1; fi

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip python3-lgpio libportaudio2 ffmpeg i2c-tools

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
rsync -a --delete --exclude .git --exclude venv "$(dirname "$0")/" $APP/
chown -R $SVC_USER:$SVC_USER $DATA

echo "==> python environment"
# system site packages so the apt-installed lgpio (Pi 5 GPIO) is visible
python3 -m venv --system-site-packages $APP/venv
$APP/venv/bin/pip install -q --upgrade pip
$APP/venv/bin/pip install -q -r $APP/requirements.txt

echo "==> service"
cp $APP/airbridge.service /etc/systemd/system/airbridge.service
systemctl daemon-reload
systemctl enable --now airbridge

IP=$(hostname -I | awk '{print $1}')
echo
echo "AirBridge is running: http://$IP:8080"
echo "Logs: journalctl -u airbridge -f"
