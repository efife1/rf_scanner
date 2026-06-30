#!/usr/bin/env bash
# =============================================================================
#  RF Scanner — Raspberry Pi Install Script
#  Run as root:  sudo bash install.sh
#
#  Works whether files are:
#    • Extracted from the tar.gz  (rf_scanner/ tree already present)
#    • Copied flat into a directory alongside install.sh
#    • Run from any working directory
#
#  Tested on: Raspberry Pi OS Bookworm / Bullseye (32-bit & 64-bit)
# =============================================================================
set -uo pipefail

SERVICE_DIR="/etc/systemd/system"
LOG_DIR="/var/log"
PI_USER="pi"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
step()    { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }

# ── Privilege check ───────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    error "Please run as root:  sudo bash install.sh"
    exit 1
fi

# ── Parse CLI flags ────────────────────────────────────────────────────────────
SEED_DEMO="${SEED_DEMO:-0}"
ASSUME_NO=0
for arg in "$@"; do
    case "$arg" in
        --seed-demo)   SEED_DEMO=1 ;;
        --yes|-y)      ASSUME_NO=0 ;;
        --no-reboot)   ASSUME_NO=1 ;;
        *) ;;
    esac
done

# ── Detect actual non-root user ───────────────────────────────────────────────
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    PI_USER="$SUDO_USER"
fi
PI_HOME=$(getent passwd "$PI_USER" | cut -d: -f6)
INSTALL_DIR="$PI_HOME/rf_scanner"
VENV_DIR="$INSTALL_DIR/venv"
DB_PATH="$INSTALL_DIR/data/scans.db"

# ── Locate the directory containing this script ───────────────────────────────
# SCRIPT_DIR is the canonical path to whichever folder install.sh lives in,
# regardless of where the user ran it from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info "Running installer from : $SCRIPT_DIR"
info "Installing for user    : $PI_USER  (home: $PI_HOME)"
info "Install directory      : $INSTALL_DIR"

# ── Helper: run a command; print output only on failure ──────────────────────
ERRORS=0
run() {
    local desc="$1"; shift
    if "$@" >"$LOG_DIR/rf_install_step.log" 2>&1; then
        success "$desc"
        return 0
    else
        error "$desc — FAILED (output below)"
        cat "$LOG_DIR/rf_install_step.log"
        ((ERRORS++)) || true
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
step "0 · Locate and arrange project files"
# ─────────────────────────────────────────────────────────────────────────────
# Expected subdirectories and the files that belong in each.
# If a file is found flat next to install.sh we move it into place.
declare -A FILE_MAP=(
    ["scanner/rf_scanner.py"]="rf_scanner.py"
    ["scanner/gui.py"]="gui.py"
    ["scanner/driver_check.py"]="driver_check.py"
    ["server/app.py"]="app.py"
    ["server/export.py"]="export.py"
    ["templates/index.html"]="index.html"
    ["static/css/base.css"]="base.css"
    ["static/js/utils.js"]="utils.js"
    ["data/seed.py"]="seed.py"
)

# Create all subdirectories in INSTALL_DIR
for subdir in scanner server templates static/css static/js data logs; do
    mkdir -p "$INSTALL_DIR/$subdir"
done

# Copy or move files from SCRIPT_DIR into INSTALL_DIR with correct paths.
# Priority:  SCRIPT_DIR/<subpath>  >  SCRIPT_DIR/<flatname>  >  already in place
for dest_rel in "${!FILE_MAP[@]}"; do
    flat_name="${FILE_MAP[$dest_rel]}"
    dest_full="$INSTALL_DIR/$dest_rel"
    src_subpath="$SCRIPT_DIR/$dest_rel"
    src_flat="$SCRIPT_DIR/$flat_name"

    if [[ -f "$src_subpath" ]]; then
        # Already in the right subdirectory tree — copy to INSTALL_DIR
        cp "$src_subpath" "$dest_full" && success "Copied $dest_rel"
    elif [[ -f "$src_flat" ]]; then
        # Found flat next to install.sh — put it in the right place
        cp "$src_flat" "$dest_full" && success "Placed $flat_name → $dest_rel"
    elif [[ -f "$dest_full" ]]; then
        # Already in INSTALL_DIR from a previous install — leave it alone
        success "$dest_rel already present"
    else
        error "Cannot find $dest_rel (also tried $src_flat)"
        ((ERRORS++)) || true
    fi
done

# Copy top-level support files
for f in config.json requirements-pi.txt requirements-gui.txt \
          rf-scanner.service rf-scanner-web.service README.md; do
    src=""
    [[ -f "$SCRIPT_DIR/$f" ]]             && src="$SCRIPT_DIR/$f"
    [[ -z "$src" && -f "$INSTALL_DIR/$f" ]] && src="$INSTALL_DIR/$f"  # already there
    if [[ -n "$src" ]]; then
        cp "$src" "$INSTALL_DIR/$f" && success "Copied $f"
    else
        warn "$f not found — skipping (non-critical)"
    fi
done

# Verify the seed script is now in place — this was the original failure point
SEED_SCRIPT="$INSTALL_DIR/data/seed.py"
if [[ ! -f "$SEED_SCRIPT" ]]; then
    error "seed.py still missing after file layout step — cannot seed database"
    error "Expected it at: $SEED_SCRIPT"
    ((ERRORS++)) || true
fi

# ─────────────────────────────────────────────────────────────────────────────
step "1 · System packages"
# ─────────────────────────────────────────────────────────────────────────────
run "apt-get update" apt-get update -qq

PKGS=(python3 python3-pip python3-venv python3-tk
      rtl-sdr librtlsdr-dev gpsd gpsd-clients git curl)
for pkg in "${PKGS[@]}"; do
    if dpkg -s "$pkg" &>/dev/null; then
        success "$pkg already installed"
    else
        run "Installing $pkg" apt-get install -y -qq "$pkg"
    fi
done

# ─────────────────────────────────────────────────────────────────────────────
step "2 · Blacklist conflicting DVB kernel modules"
# ─────────────────────────────────────────────────────────────────────────────
cat > /etc/modprobe.d/blacklist-rtl.conf << 'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
success "Blacklist file written"

for mod in dvb_usb_rtl28xxu rtl2832 rtl2830; do
    if lsmod 2>/dev/null | grep -q "^${mod}"; then
        rmmod "$mod" 2>/dev/null \
            && success "Unloaded $mod" \
            || warn "Could not unload $mod (harmless — gone after reboot)"
    fi
done

# ─────────────────────────────────────────────────────────────────────────────
step "3 · udev rule for RTL-SDR"
# ─────────────────────────────────────────────────────────────────────────────
cat > /etc/udev/rules.d/20-rtlsdr.rules << 'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666"
EOF
udevadm control --reload-rules 2>/dev/null || true
success "udev rule installed"

# ─────────────────────────────────────────────────────────────────────────────
step "4 · Serial UART for GPS (NEO-7M on /dev/ttyAMA0)"
# ─────────────────────────────────────────────────────────────────────────────
BOOT_CONFIG=""
for f in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$f" ]] && BOOT_CONFIG="$f" && break
done

if [[ -n "$BOOT_CONFIG" ]]; then
    if grep -q "enable_uart" "$BOOT_CONFIG"; then
        sed -i 's/enable_uart=.*/enable_uart=1/' "$BOOT_CONFIG"
    else
        echo "enable_uart=1" >> "$BOOT_CONFIG"
    fi
    success "enable_uart=1 set in $BOOT_CONFIG"
else
    warn "Could not find /boot/config.txt — set enable_uart=1 manually"
fi

for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [[ -f "$f" ]] && grep -q "console=serial0" "$f"; then
        sed -i 's/console=serial0,[0-9]* //' "$f"
        warn "Removed serial console from $f — reboot required"
        break
    fi
done

usermod -aG dialout,plugdev "$PI_USER" 2>/dev/null \
    && success "User $PI_USER added to dialout, plugdev" || true

# ─────────────────────────────────────────────────────────────────────────────
step "5 · Python virtual environment"
# ─────────────────────────────────────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
    run "Creating venv at $VENV_DIR" python3 -m venv "$VENV_DIR"
else
    success "venv already exists — reusing"
fi

run "Upgrading pip" "$VENV_DIR/bin/pip" install --upgrade pip -q

REQ="$INSTALL_DIR/requirements-pi.txt"
if [[ -f "$REQ" ]]; then
    run "Installing Python packages" "$VENV_DIR/bin/pip" install -r "$REQ" -q
else
    warn "requirements-pi.txt not found — installing packages directly"
    run "Installing packages" \
        "$VENV_DIR/bin/pip" install -q \
            pyserial pynmea2 flask flask-cors python-dotenv
fi

# ─────────────────────────────────────────────────────────────────────────────
step "6 · Directory permissions"
# ─────────────────────────────────────────────────────────────────────────────
chown -R "$PI_USER:$PI_USER" "$INSTALL_DIR"
touch "$LOG_DIR/rf_scanner.log" "$LOG_DIR/rf_scanner_web.log"
chown "$PI_USER:$PI_USER" \
    "$LOG_DIR/rf_scanner.log" "$LOG_DIR/rf_scanner_web.log"
chmod 644 "$LOG_DIR/rf_scanner.log" "$LOG_DIR/rf_scanner_web.log"
success "Permissions set"

# ─────────────────────────────────────────────────────────────────────────────
step "7 · Systemd services"
# ─────────────────────────────────────────────────────────────────────────────
for svc in rf-scanner rf-scanner-web; do
    SRC="$INSTALL_DIR/${svc}.service"
    DST="$SERVICE_DIR/${svc}.service"
    if [[ ! -f "$SRC" ]]; then
        error "Service file missing: $SRC — skipping"
        ((ERRORS++)) || true
        continue
    fi
    # Substitute venv python path AND home directory
    sed \
        -e "s|/usr/bin/python3|$VENV_DIR/bin/python3|g" \
        -e "s|/home/pi|$PI_HOME|g" \
        "$SRC" > "$DST"
    success "Installed $DST"
done

systemctl daemon-reload
systemctl enable rf-scanner.service     2>/dev/null && success "rf-scanner enabled"    || warn "Could not enable rf-scanner"
systemctl enable rf-scanner-web.service 2>/dev/null && success "rf-scanner-web enabled" || warn "Could not enable rf-scanner-web"

# ─────────────────────────────────────────────────────────────────────────────
step "8 · Initialize database (demo data skipped by default)"
# ─────────────────────────────────────────────────────────────────────────────
# Demo seeding is now OPT-IN ONLY via --seed-demo, and never blocks install.
# This guarantees a stable, predictable launch every time.
mkdir -p "$(dirname "$DB_PATH")"

# Always create an empty database with the correct schema so the app
# has something valid to read on first launch, even with zero seed data.
info "Creating empty database with schema at $DB_PATH"
sudo -u "$PI_USER" "$VENV_DIR/bin/python3" - "$DB_PATH" << 'PYEOF' || warn "Schema init had a non-fatal issue"
import sqlite3, sys
db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
conn.executescript("""
    CREATE TABLE IF NOT EXISTS scan_sessions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp    TEXT    NOT NULL,
        latitude     REAL, longitude REAL, altitude_m REAL,
        gps_quality  INTEGER, hdop REAL,
        noise_mean   REAL, noise_std REAL, noise_median REAL,
        noise_n      INTEGER, session_label TEXT
    );
    CREATE TABLE IF NOT EXISTS frequency_measurements (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id    INTEGER NOT NULL REFERENCES scan_sessions(id),
        frequency_hz  REAL    NOT NULL,
        amplitude_dbm REAL    NOT NULL,
        snr_db        REAL,
        is_outlier    INTEGER DEFAULT 0,
        outlier_reason TEXT
    );
    CREATE TABLE IF NOT EXISTS noise_samples (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id    INTEGER NOT NULL REFERENCES scan_sessions(id),
        frequency_hz  REAL    NOT NULL,
        amplitude_dbm REAL    NOT NULL,
        is_outlier    INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS overlay_layers (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL,
        color      TEXT    NOT NULL DEFAULT '#f0a500',
        type       TEXT    NOT NULL DEFAULT 'drawn',
        source     TEXT,
        visible    INTEGER NOT NULL DEFAULT 1,
        created_at TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS overlay_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        layer_id    INTEGER NOT NULL REFERENCES overlay_layers(id) ON DELETE CASCADE,
        item_type   TEXT    NOT NULL,
        name        TEXT,
        description TEXT,
        color       TEXT,
        geometry    TEXT    NOT NULL,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_fm_session ON frequency_measurements(session_id);
    CREATE INDEX IF NOT EXISTS idx_ns_session ON noise_samples(session_id);
    CREATE INDEX IF NOT EXISTS idx_ss_time    ON scan_sessions(timestamp);
    CREATE INDEX IF NOT EXISTS idx_oi_layer   ON overlay_items(layer_id);
""")
conn.commit()
conn.close()
print(f"Schema OK: {db_path}")
PYEOF
success "Empty database ready — app will launch with zero sessions"

if [[ "${SEED_DEMO:-0}" == "1" ]]; then
    if [[ ! -f "$SEED_SCRIPT" ]]; then
        warn "Demo seed requested but seed.py not found at $SEED_SCRIPT — skipping (non-fatal)"
    else
        info "Demo data requested via --seed-demo — attempting in background-safe mode"
        if timeout 60 sudo -u "$PI_USER" \
               "$VENV_DIR/bin/python3" "$SEED_SCRIPT" \
               --db "$DB_PATH" \
               --sessions 80; then
            success "Demo data seeded at $DB_PATH"
        else
            warn "Demo seed failed or timed out — continuing with empty database (non-fatal)"
            warn "Retry manually any time with:"
            warn "  sudo -u $PI_USER $VENV_DIR/bin/python3 $SEED_SCRIPT --db $DB_PATH"
        fi
    fi
else
    info "Demo data skipped (default). To include it next time, run:"
    info "  sudo SEED_DEMO=1 bash install.sh"
    info "Or seed manually any time after install:"
    info "  sudo -u $PI_USER $VENV_DIR/bin/python3 $SEED_SCRIPT --db $DB_PATH"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "9 · Start services"
# ─────────────────────────────────────────────────────────────────────────────
if systemctl start rf-scanner-web.service 2>/dev/null; then
    success "rf-scanner-web started"
else
    warn "rf-scanner-web failed to start"
    warn "  Check: sudo journalctl -u rf-scanner-web -n 40"
    ((ERRORS++)) || true
fi

if systemctl start rf-scanner.service 2>/dev/null; then
    success "rf-scanner started"
else
    warn "rf-scanner failed to start (hardware may not be connected yet)"
    warn "  Check: sudo journalctl -u rf-scanner -n 40"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "10 · Summary"
# ─────────────────────────────────────────────────────────────────────────────
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")
echo ""
if [[ $ERRORS -eq 0 ]]; then
    echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║     RF Scanner installed successfully! ✓         ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
else
    echo -e "${YELLOW}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║  RF Scanner installed with $ERRORS error(s)             ║${NC}"
    echo -e "${YELLOW}╚══════════════════════════════════════════════════╝${NC}"
fi

cat << SUMMARY

  Web UI       →  http://${IP}:5000
  Desktop GUI  →  $VENV_DIR/bin/python3 $INSTALL_DIR/scanner/gui.py
  Config       →  $INSTALL_DIR/config.json
  Database     →  $DB_PATH
  Logs         →  $LOG_DIR/rf_scanner.log
               →  $LOG_DIR/rf_scanner_web.log

  Service status:
    sudo systemctl status rf-scanner
    sudo systemctl status rf-scanner-web
    sudo journalctl -u rf-scanner -f

  Seed database manually later:
    sudo -u $PI_USER $VENV_DIR/bin/python3 $SEED_SCRIPT --db $DB_PATH

  File layout in $INSTALL_DIR:
SUMMARY

find "$INSTALL_DIR" -not -path "$VENV_DIR/*" -type f 2>/dev/null \
    | sed "s|$INSTALL_DIR/||" | sort | sed 's/^/    /'

echo ""
warn "A reboot is recommended to activate UART and kernel module changes."
if [[ -t 0 && "$ASSUME_NO" -eq 0 ]]; then
    read -rp "  Reboot now? [y/N] " reboot_answer
    [[ "${reboot_answer,,}" == "y" ]] && reboot || echo "  Run 'sudo reboot' when ready."
else
    info "Non-interactive shell detected — skipping reboot prompt."
    info "Run 'sudo reboot' manually when ready."
fi
