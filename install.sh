#!/usr/bin/env bash
# NasPicFrame installer. Run on the Pi as the desktop user, via sudo:
#     sudo ./install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo ./install.sh" >&2
    exit 1
fi
FRAME_USER="${SUDO_USER:-}"
if [ -z "$FRAME_USER" ] || [ "$FRAME_USER" = "root" ]; then
    echo "Run via sudo from the user that logs in to the desktop (not as root)." >&2
    exit 1
fi
FRAME_UID="$(id -u "$FRAME_USER")"
FRAME_GID="$(id -g "$FRAME_USER")"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing packages"
apt-get update
apt-get install -y python3-pygame python3-pil python3-paho-mqtt fonts-dejavu-core cifs-utils wlr-randr
apt-get install -y wlopm || echo "    (wlopm not available - will fall back to wlr-randr)"

echo "==> Installing application to /opt/picframe"
install -d /opt/picframe /etc/picframe
install -m 755 "$SRC/picframe.py" /opt/picframe/picframe.py
[ -f "$SRC/README.md" ] && install -m 644 "$SRC/README.md" /opt/picframe/README.md
# Files edited on Windows may have CRLF line endings; strip them.
sed -i 's/\r$//' /opt/picframe/picframe.py

# Pristine copy of the defaults, the template for config migration.
install -m 644 "$SRC/config.ini" /opt/picframe/config.default.ini
sed -i 's/\r$//' /opt/picframe/config.default.ini

if [ -f /etc/picframe/config.ini ]; then
    echo "    existing config: adding new options only, your values are kept"
    python3 /opt/picframe/picframe.py --config /etc/picframe/config.ini \
        --migrate-config /opt/picframe/config.default.ini
else
    install -m 644 "$SRC/config.ini" /etc/picframe/config.ini
    sed -i 's/\r$//' /etc/picframe/config.ini
fi

echo "==> Preparing mount point and credentials file"
install -d -m 755 /mnt/photos
if [ ! -f /etc/samba/nas-credentials ]; then
    install -d /etc/samba
    install -m 600 -o root -g root "$SRC/nas-credentials.example" /etc/samba/nas-credentials
    sed -i 's/\r$//' /etc/samba/nas-credentials
    echo "    created /etc/samba/nas-credentials - EDIT IT with your NAS username/password"
fi

echo "==> Installing systemd service"
sed -e "s/@USER@/$FRAME_USER/g" -e "s/@UID@/$FRAME_UID/g" -e 's/\r$//' \
    "$SRC/picframe.service" > /etc/systemd/system/picframe.service

echo "==> Boot settings"
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_boot_behaviour B4   # boot to desktop, auto-login
    raspi-config nonint do_blanking 1          # disable the OS's own screen blanking
fi
# Make network-online.target actually wait for a connection (NetworkManager).
systemctl enable NetworkManager-wait-online.service 2>/dev/null || true

systemctl daemon-reload
systemctl enable picframe.service
# Upgrade: pick up the new code now rather than at the next reboot.
if systemctl is-active --quiet picframe.service; then
    echo "==> Restarting running slideshow"
    systemctl restart picframe.service
fi

cat <<EOF

Done. Next steps:
  1. sudo nano /etc/samba/nas-credentials      (NAS username / password)
  2. sudo nano /etc/fstab                      (add the line from fstab.example,
                                                 uid=$FRAME_UID,gid=$FRAME_GID)
  3. sudo systemctl daemon-reload && ls /mnt/photos   (test the mount)
  4. sudo nano /etc/picframe/config.ini        (delay, wake/sleep times)
  5. sudo reboot
Logs: journalctl -u picframe -f
EOF
