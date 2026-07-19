#!/usr/bin/env bash
# TactileSight board setup — run once on the Arduino UNO Q (arm64, Debian 13 trixie)
# SSH: arduino@10.221.208.1  password: vidhu123
#
# Usage:
#   scp linux/setup.sh linux/haptic_depth_server.py linux/haptic-demo.service \
#       arduino@10.221.208.1:~
#   ssh arduino@10.221.208.1 "bash ~/setup.sh"
#
# What this does:
#   1. Install system deps (unrar, pillow)
#   2. Download + extract OpenNI SDK (if not already present)
#   3. Install udev rules for Orbbec camera (non-root USB access)
#   4. Create /usr/local/bin/usb-role helper + sudoers entry
#   5. Install + enable usb-host-mode.service (keeps USB-C in host mode after boot)
#   6. Disable adbd.service (conflicts with host mode)
#   7. Deploy haptic server + systemd service + enable on boot
#
# Re-running is safe — each step is idempotent.

set -e
ARDUINO_HOME=/home/arduino
SDK_BASE="$ARDUINO_HOME/OpenNI_SDK"
SDK_DIR="$SDK_BASE/OpenNI_2.3.0.86_202210111155_4c8f5aa4_beta6_a311d"
NIDIR="$SDK_DIR/tools/NiViewer"
SDK_ZIP="OpenNI_2.3.0.86_202210111155_4c8f5aa4_beta6_arm64.zip"
SDK_URL="https://github.com/orbbec/OpenNI_SDK/releases/download/v2.3.0.86-beat6/$SDK_ZIP"
ROLE_SYSFS="/sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb/usb_role/4e00000.usb-role-switch/role"

echo "=== TactileSight board setup ==="

# ── 1. System dependencies ────────────────────────────────────────────────────
echo "[1/7] Installing system deps..."
sudo apt-get update -qq
sudo apt-get install -y unrar python3-pip python3-opencv python3-numpy
pip3 install --quiet pillow
pip3 install --break-system-packages --quiet websockets pypng pyserial

# ── 2. OpenNI SDK ─────────────────────────────────────────────────────────────
echo "[2/7] OpenNI SDK..."
if [ -f "$NIDIR/libOpenNI2.so" ]; then
    echo "  already installed at $NIDIR — skipping download"
else
    mkdir -p "$SDK_BASE"
    cd "$SDK_BASE"
    if [ ! -f "$SDK_ZIP" ]; then
        echo "  downloading SDK..."
        wget -q --show-progress "$SDK_URL"
    fi
    echo "  extracting zip..."
    unzip -q "$SDK_ZIP"
    RAR_FILE=$(ls *.rar 2>/dev/null | head -1)
    if [ -n "$RAR_FILE" ]; then
        echo "  extracting rar: $RAR_FILE"
        unrar x -y "$RAR_FILE"
    fi
    if [ ! -f "$NIDIR/libOpenNI2.so" ]; then
        echo "ERROR: libOpenNI2.so not found after extraction — check SDK directory"
        exit 1
    fi
    echo "  SDK ready at $NIDIR"
fi

# ── 3. udev rules ─────────────────────────────────────────────────────────────
echo "[3/7] udev rules..."
sudo tee /etc/udev/rules.d/558-orbbec-usb.rules > /dev/null <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="2bc5", MODE="0666", GROUP="plugdev"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
echo "  done"

# ── 4. USB role helper + sudoers ──────────────────────────────────────────────
echo "[4/7] USB role helper..."
sudo tee /usr/local/bin/usb-role > /dev/null <<EOF
#!/bin/sh
# Usage: usb-role host | usb-role device
case "\$1" in
  host|device) echo "\$1" > $ROLE_SYSFS ;;
  *) echo "Usage: usb-role host|device" >&2; exit 1 ;;
esac
EOF
sudo chmod +x /usr/local/bin/usb-role

sudo tee /etc/sudoers.d/usb-role-switch > /dev/null <<'EOF'
arduino ALL=(ALL) NOPASSWD: /usr/local/bin/usb-role
arduino ALL=(ALL) NOPASSWD: /bin/systemctl enable usb-host-mode.service
arduino ALL=(ALL) NOPASSWD: /bin/systemctl disable usb-host-mode.service
EOF
sudo chmod 0440 /etc/sudoers.d/usb-role-switch
echo "  done"

# ── 5. usb-host-mode.service ──────────────────────────────────────────────────
echo "[5/7] usb-host-mode.service..."
sudo tee /etc/systemd/system/usb-host-mode.service > /dev/null <<EOF
[Unit]
Description=Force USB-C into host mode so the Orbbec depth camera enumerates
After=sysinit.target
DefaultDependencies=no
Conflicts=adbd.service

[Service]
Type=oneshot
RemainAfterExit=yes
# The Qualcomm ADSP/OTG state machine tears down xHCI and re-enables the USB
# gadget at ~16s into boot. Wait 25s so this write lands after that and sticks;
# a 2-second delay is overwritten by the ADSP switch and has no effect.
# Disable this unit to boot into device mode instead.
ExecStart=/bin/sh -c 'sleep 25 && /usr/local/bin/usb-role host'

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable usb-host-mode.service
echo "  enabled"

# ── 6. Disable adbd ───────────────────────────────────────────────────────────
echo "[6/7] Disabling adbd..."
sudo systemctl disable adbd.service 2>/dev/null || true
sudo systemctl stop adbd.service    2>/dev/null || true
echo "  done"

# ── 7. Deploy haptic server ───────────────────────────────────────────────────
echo "[7/7] Deploying haptic server + rgb_worker..."
cp "$ARDUINO_HOME/haptic_depth_server.py" "$ARDUINO_HOME/haptic_depth_server.py.bak" 2>/dev/null || true
cp "$(dirname "$0")/haptic_depth_server.py" "$ARDUINO_HOME/haptic_depth_server.py" 2>/dev/null || true
cp "$(dirname "$0")/rgb_worker.py"          "$ARDUINO_HOME/rgb_worker.py"           2>/dev/null || true
mkdir -p /dev/shm/tactile

sudo cp "$ARDUINO_HOME/haptic-demo.service" /etc/systemd/system/haptic-demo.service
sudo systemctl daemon-reload
sudo systemctl enable haptic-demo.service
sudo systemctl restart haptic-demo.service
echo "  service started"

# ── Quick verification ────────────────────────────────────────────────────────
echo ""
echo "=== Verification ==="
echo -n "haptic-demo.service: "
systemctl is-active haptic-demo.service

echo -n "USB host mode:        "
cat "$ROLE_SYSFS" 2>/dev/null || echo "(check after reboot)"

echo -n "Camera on USB:        "
lsusb | grep 2bc5 | head -2 || echo "(not detected — may need reboot)"

echo ""
echo "=== Done ==="
echo "Web UI:    http://10.221.208.1:8081"
echo "Grid JSON: http://10.221.208.1:8081/grid"
echo "Depth cam: http://10.221.208.1:8081/depth.mjpg  (toggle in UI)"
echo "Capture WS: ws://10.221.208.1:8083"
echo ""
echo "If camera not detected, reboot the board:"
echo "  sudo reboot"
echo ""
echo "After reboot, verify with:"
echo "  lsusb | grep 2bc5   # should show two 2bc5 devices"
echo "  systemctl status haptic-demo.service"
