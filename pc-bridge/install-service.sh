#!/usr/bin/env bash
# Install the bridge as a user service so it starts at login and restarts on
# its own. No sudo: a --user unit needs none, and this only ever talks to a USB
# port the user can already open.
set -euo pipefail
cd "$(dirname "$0")"

[ -d .venv ] || { echo "creating venv..."; python3 -m venv .venv; }
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

mkdir -p ~/.config/systemd/user
cp tactilesight-band.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tactilesight-band.service

# Survive logout, so the band keeps working when the laptop screen locks.
loginctl enable-linger "$USER" 2>/dev/null || \
  echo "note: could not enable linger; the service stops at logout"

echo
systemctl --user --no-pager status tactilesight-band.service | head -12
echo
echo "logs:  journalctl --user -u tactilesight-band -f"
echo "stop:  systemctl --user stop tactilesight-band"
