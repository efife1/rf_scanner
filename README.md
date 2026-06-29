# RF Scanner — Raspberry Pi Signal Survey System

A complete system for mobile RF signal surveying using a Raspberry Pi, RTL-SDR
dongle, and NEO-7M GPS module. Scans user-defined frequencies, measures signal
amplitude against a statistically-cleaned noise floor, and displays geo-tagged
heatmaps in a browser-based interface accessible from any device on the network.

---

## Hardware Requirements

| Component | Notes |
|-----------|-------|
| Raspberry Pi 3B+ / 4 / 5 | Pi 4 recommended for web server performance |
| RTL-SDR dongle | RTL2838 chipset (e.g. RTL-SDR Blog V3 ~$30) |
| NEO-7M GPS module | UART connection on /dev/ttyAMA0 |
| USB power bank | For mobile surveying |
| MicroSD ≥16 GB | Class 10 / A1 recommended |

### Wiring the NEO-7M GPS

```
NEO-7M     →   Raspberry Pi GPIO
VCC        →   Pin 1  (3.3 V)
GND        →   Pin 6  (GND)
TX         →   Pin 10 (GPIO 15 / UART RX)
RX         →   Pin 8  (GPIO 14 / UART TX)
```

---

## Directory Layout

```
rf_scanner/
├── scanner/
│   ├── rf_scanner.py     # Main scanner daemon (RTL-SDR + GPS + stats)
│   └── gui.py            # Desktop GUI (tkinter + matplotlib)
├── server/
│   ├── app.py            # Flask web server + REST API
│   └── export.py         # CSV / GeoJSON / KML export endpoints
├── templates/
│   └── index.html        # Browser map UI (Leaflet + Chart.js)
├── data/
│   ├── seed.py           # Demo data generator
│   └── scans.db          # SQLite database (auto-created)
├── config.json           # Scanner configuration
├── requirements-pi.txt   # Pi Python dependencies
├── requirements-gui.txt  # GUI workstation dependencies
├── rf-scanner.service    # Systemd unit — scanner daemon
├── rf-scanner-web.service # Systemd unit — web server
└── install.sh            # One-shot installer
```

---

## Quick Start

### 1. Install on Raspberry Pi

```bash
git clone https://github.com/efife1/rf_scanner /home/pi/rf_scanner
cd /home/pi/rf_scanner
sudo bash install.sh
```

The installer will:
- Install system packages (rtl-sdr, gpsd, python3)
- Blacklist conflicting kernel DVB drivers
- Configure the serial UART for GPS
- Create a Python virtual environment
- Install both systemd services
- Optionally seed the database with demo data

### 2. Configure your frequencies

Edit `/home/pi/rf_scanner/config.json`:

```json
{
  "frequencies_hz": [
    88100000,
    162400000,
    433920000,
    915000000
  ],
  "dwell_ms": 250,
  "scan_interval_s": 300,
  "sdr_gain": "auto",
  "sdr_ppm": 0,
  "session_label": "survey_run_1"
}
```

**Finding your dongle's PPM offset** (improves frequency accuracy):
```bash
rtl_test -p
# Run for ~5 minutes, note the reported PPM offset, set sdr_ppm accordingly
```

### 3. Access the web interface

From any browser on your network:
```
http://<raspberry-pi-ip>:5000
```

Find your Pi's IP:
```bash
hostname -I
```

### 4. Run the desktop GUI (on the Pi or remote machine)

```bash
pip3 install -r requirements-gui.txt
python3 scanner/gui.py
```

If running remotely, edit the `API_BASE` constant in `gui.py`:
```python
API_BASE = "http://192.168.1.42:5000/api"
```

---

## Test Without Hardware (Simulation Mode)

```bash
# Run one simulated scan
python3 scanner/rf_scanner.py --once --simulate

# Or set in config.json:
#   "simulate": true

# Seed 80 sessions of realistic demo data
python3 data/seed.py
```

---

## How It Works

### Scan Session Flow

```
1. GPS Fix
   └── Read NMEA GGA sentences from NEO-7M on /dev/ttyAMA0
       Timeout after 10s; session records null coords on failure

2. Noise Floor Sampling
   └── Sample 30 random frequencies between f_min and f_max
       (avoiding ±500 kHz of any target frequency)
   └── Remove outliers using Tukey IQR fence (1.5× IQR)
   └── Compute: mean, std, median from clean samples

3. Target Frequency Measurement
   └── For each user-defined frequency:
       - Measure amplitude via rtl_power (50 kHz window, peak value)
       - Compute SNR = amplitude − noise_floor_mean
       - Flag as outlier if amplitude > noise_mean + 2.5σ
       - Alert operator if flagged

4. Persist to SQLite
   └── scan_sessions — GPS, noise stats, timestamp
   └── frequency_measurements — amplitude, SNR, outlier flag
   └── noise_samples — raw noise readings with outlier flags
```

### Statistical Analysis

**Noise floor outlier removal (IQR method):**
```
Q1, Q3 = 25th and 75th percentiles of noise samples
IQR = Q3 - Q1
Valid range: [Q1 - 1.5×IQR, Q3 + 1.5×IQR]
Outliers outside this range are discarded
```

**Signal flagging (Z-score method):**
```
Z = (amplitude - noise_mean) / noise_std
If Z > 2.5 → frequency is flagged as statistically significant
```

### RTL-SDR Measurement

The scanner uses `rtl_power` (bundled with rtl-sdr tools) to measure amplitude:
- Scans a ±25 kHz window around each target frequency
- Step size: 5 kHz
- Returns peak dB value across all steps
- Dwell time configurable (default 250 ms per frequency)

### Database Schema

```sql
scan_sessions          -- One row per scan run
  id, timestamp, latitude, longitude, altitude_m,
  gps_quality, hdop, noise_mean, noise_std, noise_median, noise_n

frequency_measurements -- One row per frequency per session
  session_id, frequency_hz, amplitude_dbm, snr_db,
  is_outlier, outlier_reason

noise_samples          -- Raw noise data for audit/debug
  session_id, frequency_hz, amplitude_dbm, is_outlier
```

---

## REST API Reference

All endpoints return JSON. Base URL: `http://<pi-ip>:5000`

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Server health + latest session info |
| GET | `/api/frequencies` | List of all measured frequencies |
| GET | `/api/sessions?limit=N` | Recent scan sessions |
| GET | `/api/sessions/<id>/detail` | Full session data + measurements |
| GET | `/api/heatmap/average?mode=amplitude\|snr` | GeoJSON heatmap (all freqs avg) |
| GET | `/api/heatmap/frequency/<hz>?mode=amplitude\|snr` | GeoJSON heatmap (single freq) |
| GET | `/api/stats/frequency/<hz>` | Statistical summary + timeseries |
| GET | `/api/export/csv?days=N&freq_hz=N` | Download CSV |
| GET | `/api/export/geojson` | Download GeoJSON |
| GET | `/api/export/kml` | Download KML (Google Earth) |

---

## Web UI Features

- **Average heatmap** — all frequencies averaged, all sessions
- **Single frequency heatmap** — click any frequency in the sidebar
- **Amplitude / SNR toggle** — switch between raw signal strength and signal-to-noise ratio
- **Spectrum bar chart** — latest session amplitude for all frequencies
- **SNR bar chart** — latest session SNR with noise floor reference line
- **Time series chart** — amplitude and SNR history for selected frequency
- **Alert overlay** — frequencies above 2.5σ noise threshold shown as map badges
- **GPS status bar** — latest fix coordinates, altitude, HDOP
- **Export buttons** — CSV, GeoJSON, KML download
- **Auto-refresh** — every 30 seconds

---

## Service Management

```bash
# Status
sudo systemctl status rf-scanner
sudo systemctl status rf-scanner-web

# Live logs
sudo journalctl -u rf-scanner -f
sudo journalctl -u rf-scanner-web -f

# Restart
sudo systemctl restart rf-scanner
sudo systemctl restart rf-scanner-web

# Manual scan (one-shot)
python3 /home/pi/rf_scanner/scanner/rf_scanner.py --once
```

---

## Troubleshooting

**No RTL-SDR device found:**
```bash
rtl_test          # Should print device info
lsusb             # Look for "Realtek Semiconductor" entry
sudo rmmod dvb_usb_rtl28xxu   # Remove conflicting driver
```

**No GPS fix:**
```bash
cat /dev/ttyAMA0  # Should stream NMEA sentences
sudo systemctl status gpsd
```

**Web UI shows "offline":**
```bash
sudo systemctl status rf-scanner-web
curl http://localhost:5000/api/status
```

**PPM calibration (improves accuracy up to ~10 kHz):**
```bash
rtl_test -p       # Run 5 min, note "cumulative PPM"
# Add result to config.json → "sdr_ppm": 42
```

---

## License

MIT — free to use, modify, and distribute.
