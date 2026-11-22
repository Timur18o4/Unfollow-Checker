#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo: sudo ./install.sh" >&2
  exit 1
fi
if [[ ! -f /etc/os-release ]] || ! grep -qiE 'debian|raspbian' /etc/os-release; then
  echo "This installer currently supports Raspberry Pi OS and Debian." >&2
  exit 1
fi

APP_USER="${SUDO_USER:-}"
if [[ -z "$APP_USER" || "$APP_USER" == root ]]; then
  echo "Run this from a normal Pi user account with sudo." >&2
  exit 1
fi
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
if [[ -z "$APP_HOME" || ! -d "$APP_HOME" ]]; then
  echo "Could not find the home directory for $APP_USER." >&2
  exit 1
fi

APP_DIR="/opt/unfollow-checker"
DATA_DIR="$APP_HOME/.config/unfollow-checker"
OLD_DIR="$APP_HOME/.config/twitch-follow-watch"
install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$APP_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$DATA_DIR"
install -o root -g root -m 0644 "$(dirname "$0")/app.py" "$APP_DIR/app.py"
install -o root -g root -m 0755 "$(dirname "$0")/reset-pin.sh" "$APP_DIR/reset-pin.sh"

# Migrate this project's earlier personal installation, preserving the existing baseline.
if [[ ! -f "$DATA_DIR/credentials.json" && -f "$OLD_DIR/credentials.json" ]]; then
  install -o "$APP_USER" -g "$APP_USER" -m 0600 "$OLD_DIR/credentials.json" "$DATA_DIR/credentials.json"
  python3 - "$OLD_DIR/config.json" "$DATA_DIR/config.json" <<'PY'
import json, os, sys
old_path, new_path = sys.argv[1:]
try:
    old = json.load(open(old_path, encoding="utf-8"))
except (OSError, ValueError):
    old = {}
config = {
    "client_id": old.get("client_id", "9bm0ou31egoqeobo3sl8a9cvpldcb3"),
    "channel_login": old.get("channel_login", ""),
    "bot_login": old.get("bot_login", ""),
    "mode": "separate",
}
if old.get("broadcaster_id"):
    config["broadcaster_id"] = old["broadcaster_id"]
with open(new_path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2)
os.chmod(new_path, 0o600)
PY
fi
if [[ ! -f "$DATA_DIR/state.sqlite3" && -f "$OLD_DIR/state.sqlite3" ]]; then
  install -o "$APP_USER" -g "$APP_USER" -m 0600 "$OLD_DIR/state.sqlite3" "$DATA_DIR/state.sqlite3"
fi

PIN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')"
printf '%s\n' "$PIN" > "$DATA_DIR/web.pin"
chown "$APP_USER:$APP_USER" "$DATA_DIR/web.pin"
chmod 0600 "$DATA_DIR/web.pin"

cat > /etc/systemd/system/unfollow-checker.service <<UNIT
[Unit]
Description=Unfollow Checker for Twitch
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=DATA_DIR=$DATA_DIR
Environment=HOST=0.0.0.0
Environment=PORT=8765
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $APP_DIR/app.py
Restart=always
RestartSec=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 /etc/systemd/system/unfollow-checker.service

# Stop the old personal service after migration to prevent duplicate chat messages.
if systemctl list-unit-files twitch-follow-watch.service --no-legend 2>/dev/null | grep -q twitch-follow-watch.service; then
  systemctl disable --now twitch-follow-watch.service || true
fi
systemctl daemon-reload
systemctl enable --now unfollow-checker.service
sleep 2

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Unfollow Checker is installed and starts automatically after reboot."
echo "Open on this Pi: http://localhost:8765"
echo "Open from another device on your home network: http://${IP:-raspberrypi.local}:8765"
echo "Setup PIN: $PIN"
echo "Keep the PIN private and use the page only on a trusted local network."
echo "Status: sudo systemctl status unfollow-checker.service"
