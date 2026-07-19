#!/usr/bin/env bash
# One command: make a venv if needed, install pyserial, run the bridge.
# Every argument is passed through, e.g.  ./run.sh --test  or  ./run.sh --port /dev/ttyACM0
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "creating venv..."
  python3 -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
exec ./.venv/bin/python haptic_serial_bridge.py "$@"
