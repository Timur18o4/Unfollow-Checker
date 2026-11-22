#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo /opt/unfollow-checker/reset-pin.sh" >&2
  exit 1
fi
UNIT=/etc/systemd/system/unfollow-checker.service
APP_USER="$(sed -n 's/^User=//p' "$UNIT" | head -1)"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
DATA_DIR="$APP_HOME/.config/unfollow-checker"
PIN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')"
printf '%s\n' "$PIN" > "$DATA_DIR/web.pin"
chown "$APP_USER:$APP_USER" "$DATA_DIR/web.pin"
chmod 0600 "$DATA_DIR/web.pin"
systemctl restart unfollow-checker.service
echo "New setup PIN: $PIN"
