#!/usr/bin/env bash
# =============================================================================
#  RF Scanner — Raspberry Pi Install Script
#  Run as root:  sudo bash install.sh
# =============================================================================
set -euo pipefail

INSTALL_DIR="/home/pi/rf_scanner"
SERVICE_DIR="/etc/systemd/system"
LOG_DIR="/var/log"
PI_USER="pi"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Please run as root: sudo bash install.sh"

# ── 1. System packages ────────────────────────────────────────────────────────
info "Updating package lists…"
apt-get update -qq

info "Installing system dependencies…"
apt-get install -y -qq \
    python3 python3-pip python3-venv python3-tk \
    rtl-sdr librtlsdr-dev \
    gpsd gpsd-clients \
    git curl

# ── 2. Block kernel DVB driver (conflicts with rtl-sdr) ──────────────────────
info "Blacklisting DVB kernel modules…"
cat > /etc/modprobe.d/blacklist-rtl.conf << 'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
rmmod dvb_usb_rtl28xxu rtl2832 rtl2830 2>/dev/null || true

# ── 3. udev rule for RTL-SDR dongle ──────────────────────────────────────────
info "Installing udev rule for RTL-SDR…"
cat > /etc/udev/rules.d/20-rtlsdr.rules << 'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666"
EOF
udevadm control --reload-rules

# ── 4. Enable serial UART for GPS (NEO-7M on /dev/ttyAMA0) ──────────────────
info "Configuring serial port for GPS…"
# Disable serial console, keep hardware UART
if grep -q "enable_uart" /boot/config.txt; then
    sed -i 's/enable_uart=.*/enable_uart=1/' /boot/config.txt
else
    echo "enable_uart=1" >> /boot/config.txt
fi
# Remove serial console from cmdline
if grep -q "console=serial0" /boot/cmdline.txt; then
    sed -i 's/console=serial0,[0-9]* //' /boot/cmdline.txt
    warn "Serial console removed from /boot/cmdline.txt — reboot required"
fi
# Add pi user to dialout for serial access
usermod -aG dialout,plugdev "$PI_USER"

# ── 5. Python virtual environment ─────────────────────────────────────────────
info "Creating Python virtual environment…"
VENV_DIR="$INSTALL_DIR/venv"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements-pi.txt" -q

# ── 6. Directory permissions ──────────────────────────────────────────────────
info "Setting permissions…"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
chown -R "$PI_USER:$PI_USER" "$INSTALL_DIR"
touch "$LOG_DIR/rf_scanner.log" "$LOG_DIR/rf_scanner_web.log"
chown "$PI_USER:$PI_USER" "$LOG_DIR/rf_scanner.log" "$LOG_DIR/rf_scanner_web.log"

# ── 7. Systemd services ───────────────────────────────────────────────────────
info "Installing systemd services…"
# Patch ExecStart to use venv python
sed "s|/usr/bin/python3|$VENV_DIR/bin/python3|g" \
    "$INSTALL_DIR/rf-scanner.service" > "$SERVICE_DIR/rf-scanner.service"
sed "s|/usr/bin/python3|$VENV_DIR/bin/python3|g" \
    "$INSTALL_DIR/rf-scanner-web.service" > "$SERVICE_DIR/rf-scanner-web.service"

systemctl daemon-reload
systemctl enable rf-scanner.service
systemctl enable rf-scanner-web.service

# ── 8. Seed database (optional demo data) ─────────────────────────────────────
read -rp "Seed database with demo data? [y/N] " seed_answer
if [[ "${seed_answer,,}" == "y" ]]; then
    info "Seeding database…"
    sudo -u "$PI_USER" "$VENV_DIR/bin/python3" "$INSTALL_DIR/data/seed.py"
fi

# ── 9. Start services ─────────────────────────────────────────────────────────
info "Starting services…"
systemctl start rf-scanner-web.service
systemctl start rf-scanner.service

# ── 10. Summary ───────────────────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  RF Scanner installed successfully!${NC}"
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo ""
echo "  Web UI:       http://${IP}:5000"
echo "  Desktop GUI:  python3 $INSTALL_DIR/scanner/gui.py"
echo "  Config:       $INSTALL_DIR/config.json"
echo "  Database:     $INSTALL_DIR/data/scans.db"
echo "  Scan log:     /var/log/rf_scanner.log"
echo "  Web log:      /var/log/rf_scanner_web.log"
echo ""
echo "  Service commands:"
echo "    sudo systemctl status rf-scanner"
echo "    sudo systemctl status rf-scanner-web"
echo "    sudo journalctl -u rf-scanner -f"
echo ""
warn "A reboot is recommended to activate UART changes."
read -rp "Reboot now? [y/N] " reboot_answer
[[ "${reboot_answer,,}" == "y" ]] && reboot
