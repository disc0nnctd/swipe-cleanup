#!/usr/bin/env bash
# Install both apps as systemd services and expose them on the tailnet.
#   deploy/install.sh /path/to/storage/gdrive-backup [python-with-google-libs]
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
BACKUP_DIR=${1:?usage: install.sh BACKUP_DIR [PYTHON]}
PY=${2:-python3}
CONFIG=$HOME/.config/google-storage-cleanup/config.env
[ -f "$CONFIG" ] || { echo "create $CONFIG from config.example.env first"; exit 1; }
tmp=$(mktemp -d)
for u in photo-swipe mail-swipe; do
  sed -e "s|@@USER@@|$USER|g; s|@@REPO@@|$REPO|g; s|@@CONFIG@@|$CONFIG|g; s|@@BACKUP_DIR@@|$BACKUP_DIR|g; s|@@PYTHON@@|$PY|g" \
      "$REPO/deploy/$u.service" > "$tmp/$u.service"
done
sudo cp "$tmp"/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now photo-swipe mail-swipe
sudo tailscale serve --bg --https=8091 http://127.0.0.1:8091
sudo tailscale serve --bg --https=8092 http://127.0.0.1:8092
host=$(tailscale status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
if command -v homepage-add >/dev/null || [ -x "$HOME/bin/homepage-add" ]; then
  "${HOME}/bin/homepage-add" "Photo Swipe" "https://$host:8091/" "Sort backup photos" || true
  "${HOME}/bin/homepage-add" "Mail Swipe" "https://$host:8092/" "Gmail cleanup" || true
fi
echo "Photo Swipe: https://$host:8091   Mail Swipe: https://$host:8092"
