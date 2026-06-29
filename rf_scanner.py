#!/usr/bin/env python3
"""
RF Scanner Daemon for Raspberry Pi
Hardware: RTL-SDR dongle + NEO-7M GPS module
Scans user-defined frequencies, records amplitude, noise floor,
GPS location, and timestamps. Saves data for web visualization.
"""

# ── Dependency check runs FIRST, before any optional imports ─────────────────
# This block is intentionally at the top so missing packages are caught and
# installed before we try to import them further down.
import sys
import os
import argparse as _argparse

def _early_args():
    """Peek at CLI args before full argparse setup, to pass simulate/no-fix flags."""
    simulate  = "--simulate" in sys.argv
    no_fix    = "--no-fix"   in sys.argv
    skip_check = "--skip-check" in sys.argv
    return simulate, not no_fix, skip_check

_simulate_early, _auto_fix, _skip_check = _early_args()

if not _skip_check:
    # driver_check.py lives in the same directory as this file
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from driver_check import run_all_checks
        _deps_ok = run_all_checks(
            auto_fix = _auto_fix,
            gui_mode = False,
            simulate = _simulate_early,
        )
        if not _deps_ok:
            print("\n[rf_scanner] Critical dependencies missing — exiting.")
            print("  Fix the issues above, or run with --simulate to test without hardware.")
            sys.exit(1)
    except Exception as _e:
        # If driver_check itself is broken, warn and continue best-effort
        print(f"[rf_scanner] Warning: dependency check failed ({_e}). Continuing…")

# ── Now safe to import optional packages ─────────────────────────────────────
import time
import json
import math
import random
import logging
import sqlite3
import subprocess
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import serial       # pyserial
    import pynmea2      # GPS NMEA parsing
except ImportError as e:
    print(f"[rf_scanner] Required package still missing after auto-install: {e}")
    print("  Try: pip3 install pyserial pynmea2 --break-system-packages")
    sys.exit(1)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/var/log/rf_scanner.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
DB_PATH = Path("/home/pi/rf_scanner/data/scans.db")
CONFIG_PATH = Path("/home/pi/rf_scanner/config.json")
GPS_PORT = "/dev/ttyAMA0"
GPS_BAUD = 9600
GPS_TIMEOUT = 10  # seconds to wait for GPS fix
NOISE_SAMPLES = 30          # random noise frequency samples per scan session
IQR_MULTIPLIER = 1.5        # IQR fence for outlier detection
Z_SCORE_THRESHOLD = 2.5     # Z-score threshold for outlier flagging
MIN_NOISE_SAMPLES = 8       # minimum noise samples after outlier removal


# ── Database Setup ────────────────────────────────────────────────────────────
def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS scan_sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            latitude    REAL,
            longitude   REAL,
            altitude_m  REAL,
            gps_quality INTEGER,           -- 0=no fix, 1=GPS, 2=DGPS
            hdop        REAL,              -- horizontal dilution of precision
            noise_mean  REAL,
            noise_std   REAL,
            noise_median REAL,
            noise_n     INTEGER,           -- samples after outlier removal
            session_label TEXT
        );

        CREATE TABLE IF NOT EXISTS frequency_measurements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL REFERENCES scan_sessions(id),
            frequency_hz    REAL NOT NULL,
            amplitude_dbm   REAL NOT NULL,
            snr_db          REAL,          -- amplitude - noise_floor
            is_outlier      INTEGER DEFAULT 0,
            outlier_reason  TEXT
        );

        CREATE TABLE IF NOT EXISTS noise_samples (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL REFERENCES scan_sessions(id),
            frequency_hz    REAL NOT NULL,
            amplitude_dbm   REAL NOT NULL,
            is_outlier      INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_freq_session ON frequency_measurements(session_id);
        CREATE INDEX IF NOT EXISTS idx_noise_session ON noise_samples(session_id);
        CREATE INDEX IF NOT EXISTS idx_session_time  ON scan_sessions(timestamp);
    """)
    conn.commit()
    log.info("Database initialized at %s", db_path)
    return conn


# ── GPS Reader ────────────────────────────────────────────────────────────────
def read_gps(port: str = GPS_PORT, baud: int = GPS_BAUD,
             timeout: int = GPS_TIMEOUT) -> dict:
    """
    Read a GPS fix from the NEO-7M via serial.
    Returns dict with lat, lon, alt, quality, hdop or None values on failure.
    """
    result = {"latitude": None, "longitude": None, "altitude_m": None,
              "gps_quality": 0, "hdop": None}
    deadline = time.time() + timeout
    try:
        with serial.Serial(port, baud, timeout=1) as ser:
            while time.time() < deadline:
                line = ser.readline().decode("ascii", errors="replace").strip()
                if not line.startswith("$"):
                    continue
                try:
                    msg = pynmea2.parse(line)
                    if isinstance(msg, pynmea2.types.talker.GGA):
                        if msg.gps_qual and int(msg.gps_qual) > 0:
                            result["latitude"]   = msg.latitude
                            result["longitude"]  = msg.longitude
                            result["altitude_m"] = float(msg.altitude) if msg.altitude else None
                            result["gps_quality"] = int(msg.gps_qual)
                            result["hdop"]       = float(msg.horizontal_dil) if msg.horizontal_dil else None
                            log.info("GPS fix: %.6f, %.6f (quality=%s, HDOP=%s)",
                                     msg.latitude, msg.longitude,
                                     msg.gps_qual, msg.horizontal_dil)
                            return result
                except pynmea2.ParseError:
                    continue
    except serial.SerialException as e:
        log.warning("GPS serial error: %s — using last known position", e)
    log.warning("No GPS fix within %ds", timeout)
    return result


# ── RTL-SDR Amplitude Measurement ─────────────────────────────────────────────
def measure_amplitude(frequency_hz: float, dwell_ms: int = 200,
                      gain: str = "auto", ppm: int = 0) -> Optional[float]:
    """
    Use rtl_power (bundled with rtl-sdr) to measure signal amplitude at
    a single frequency.  Returns peak amplitude in dBm, or None on error.

    rtl_power -f START:STOP:STEP -i INTERVAL -1 -g GAIN -p PPM -
    We scan a narrow 50 kHz window around the target and take the max.
    """
    bw = 25_000  # ±25 kHz window
    start = int(frequency_hz - bw)
    stop  = int(frequency_hz + bw)
    step  = 5_000
    interval = max(1, dwell_ms // 1000)

    cmd = [
        "rtl_power",
        "-f", f"{start}:{stop}:{step}",
        "-i", str(interval),
        "-1",                      # one shot
        "-g", gain,
        "-p", str(ppm),
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.debug("rtl_power stderr: %s", result.stderr.strip())
            return None

        # rtl_power CSV: date, time, Hz low, Hz high, Hz step, samples, dB...
        max_db = None
        for line in result.stdout.strip().splitlines():
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                db_values = [float(x) for x in parts[6:] if x.strip()]
                peak = max(db_values)
                if max_db is None or peak > max_db:
                    max_db = peak
            except ValueError:
                continue
        return max_db
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("rtl_power error: %s", e)
        return None


def measure_amplitude_simulated(frequency_hz: float) -> float:
    """
    Simulation fallback when no SDR hardware is present.
    Models a realistic-ish RF environment for development/testing.
    """
    # Base noise floor around -95 dBm with thermal noise variation
    noise_floor = -95.0 + random.gauss(0, 2)

    # Simulate known signal sources
    known_signals = {
        88.1e6: -55,   # FM radio
        100.3e6: -62,
        162.400e6: -70,  # NOAA weather
        433.920e6: -80,  # ISM band activity
        915.0e6: -72,
    }
    for known_freq, sig_strength in known_signals.items():
        if abs(frequency_hz - known_freq) < 500_000:
            offset = abs(frequency_hz - known_freq) / 500_000
            return sig_strength + random.gauss(0, 3) + offset * 10

    return noise_floor + random.gauss(0, 1.5)


# ── Statistical Analysis ──────────────────────────────────────────────────────
def remove_outliers_iqr(values: list[float]) -> tuple[list[float], list[float]]:
    """
    Remove outliers using Tukey's IQR fence method.
    Returns (clean_values, outlier_values).
    """
    if len(values) < 4:
        return values, []
    sorted_v = sorted(values)
    q1 = statistics.quantiles(sorted_v, n=4)[0]
    q3 = statistics.quantiles(sorted_v, n=4)[2]
    iqr = q3 - q1
    lo = q1 - IQR_MULTIPLIER * iqr
    hi = q3 + IQR_MULTIPLIER * iqr
    clean    = [v for v in values if lo <= v <= hi]
    outliers = [v for v in values if v < lo or v > hi]
    return clean, outliers


def z_score_flag(value: float, mean: float, std: float) -> bool:
    """Return True if value is an outlier by Z-score."""
    if std == 0:
        return False
    return abs((value - mean) / std) > Z_SCORE_THRESHOLD


def analyze_noise(raw_amplitudes: list[float]) -> dict:
    """
    Full noise floor analysis.
    Returns statistics dict usable for SNR calculation.
    """
    if not raw_amplitudes:
        return {"mean": None, "std": None, "median": None, "n": 0, "outliers_removed": 0}

    clean, outliers = remove_outliers_iqr(raw_amplitudes)
    if len(clean) < MIN_NOISE_SAMPLES:
        log.warning("Only %d clean noise samples (min %d) — using all", len(clean), MIN_NOISE_SAMPLES)
        clean = raw_amplitudes

    mean   = statistics.mean(clean)
    std    = statistics.stdev(clean) if len(clean) > 1 else 0.0
    median = statistics.median(clean)

    log.info("Noise floor: mean=%.1f dBm  std=%.1f  median=%.1f  n=%d  outliers_removed=%d",
             mean, std, median, len(clean), len(outliers))

    return {
        "mean":             mean,
        "std":              std,
        "median":           median,
        "n":                len(clean),
        "outliers_removed": len(outliers),
    }


def analyze_signal(amplitude_dbm: float, noise_stats: dict,
                   freq_hz: float) -> tuple[Optional[float], bool, Optional[str]]:
    """
    Compute SNR and flag if the amplitude is statistically significant.
    Returns (snr_db, is_outlier_high, reason).
    """
    if noise_stats["mean"] is None:
        return None, False, None

    snr = amplitude_dbm - noise_stats["mean"]
    noise_mean = noise_stats["mean"]
    noise_std  = noise_stats["std"] or 1.0

    # A signal is a "positive outlier" if it's well above the noise
    high_threshold = noise_mean + Z_SCORE_THRESHOLD * noise_std
    is_high = amplitude_dbm > high_threshold
    reason  = f">{Z_SCORE_THRESHOLD}σ above noise floor" if is_high else None

    return snr, is_high, reason


# ── Config ────────────────────────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    """Load scanner configuration from JSON file."""
    if not path.exists():
        default = {
            "frequencies_hz": [
                88100000, 100300000, 162400000,
                433920000, 915000000
            ],
            "dwell_ms": 200,
            "scan_interval_s": 300,
            "sdr_gain": "auto",
            "sdr_ppm": 0,
            "simulate": False,
            "session_label": "default",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default, indent=2))
        log.info("Created default config at %s", path)
        return default
    with open(path) as f:
        cfg = json.load(f)
    log.info("Loaded config: %d target frequencies", len(cfg.get("frequencies_hz", [])))
    return cfg


# ── Core Scan Loop ────────────────────────────────────────────────────────────
def run_scan(conn: sqlite3.Connection, cfg: dict, simulate: bool = False):
    """Execute one full scan session and persist results."""
    freqs = sorted(cfg["frequencies_hz"])
    if len(freqs) < 2:
        log.error("Need at least 2 target frequencies")
        return

    f_min, f_max = freqs[0], freqs[-1]
    dwell = cfg.get("dwell_ms", 200)
    measure = measure_amplitude_simulated if simulate else (
        lambda f: measure_amplitude(f, dwell, cfg.get("sdr_gain","auto"), cfg.get("sdr_ppm",0))
    )

    log.info("=== Scan session start ===")
    ts = datetime.now(timezone.utc).isoformat()

    # 1. GPS fix
    gps = read_gps() if not simulate else {
        "latitude": 39.1157 + random.uniform(-0.01, 0.01),
        "longitude": -77.5636 + random.uniform(-0.01, 0.01),
        "altitude_m": 120.0,
        "gps_quality": 1,
        "hdop": 1.2,
    }

    # 2. Sample noise floor (random frequencies in band)
    log.info("Sampling noise floor (%d random frequencies)…", NOISE_SAMPLES)
    noise_raw = []
    noise_freqs = []
    for _ in range(NOISE_SAMPLES):
        nf = random.uniform(f_min, f_max)
        # avoid being too close to target frequencies
        if any(abs(nf - tf) < 500_000 for tf in freqs):
            continue
        amp = measure(nf)
        if amp is not None:
            noise_raw.append(amp)
            noise_freqs.append(nf)

    noise_stats = analyze_noise(noise_raw)

    # 3. Insert session record
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO scan_sessions
          (timestamp, latitude, longitude, altitude_m, gps_quality, hdop,
           noise_mean, noise_std, noise_median, noise_n, session_label)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ts,
        gps["latitude"], gps["longitude"], gps["altitude_m"],
        gps["gps_quality"], gps["hdop"],
        noise_stats["mean"], noise_stats["std"], noise_stats["median"],
        noise_stats["n"], cfg.get("session_label", "default"),
    ))
    session_id = cur.lastrowid

    # Insert noise samples
    clean_noise, outlier_noise = remove_outliers_iqr(noise_raw)
    for i, (nf, na) in enumerate(zip(noise_freqs, noise_raw)):
        cur.execute("""
            INSERT INTO noise_samples (session_id, frequency_hz, amplitude_dbm, is_outlier)
            VALUES (?,?,?,?)
        """, (session_id, nf, na, 1 if na in outlier_noise else 0))

    # 4. Measure target frequencies
    log.info("Measuring %d target frequencies…", len(freqs))
    alerts = []
    for freq in freqs:
        amp = measure(freq)
        if amp is None:
            log.warning("No reading for %.3f MHz", freq/1e6)
            continue
        snr, is_outlier, reason = analyze_signal(amp, noise_stats, freq)
        cur.execute("""
            INSERT INTO frequency_measurements
              (session_id, frequency_hz, amplitude_dbm, snr_db, is_outlier, outlier_reason)
            VALUES (?,?,?,?,?,?)
        """, (session_id, freq, amp, snr, 1 if is_outlier else 0, reason))

        if is_outlier:
            msg = f"⚠  {freq/1e6:.3f} MHz — amplitude {amp:.1f} dBm is {reason}"
            alerts.append(msg)
            log.warning(msg)

    conn.commit()
    log.info("Session %d saved. Alerts: %d", session_id, len(alerts))

    if alerts:
        print("\n" + "="*60)
        print("SIGNAL ALERTS — frequencies above noise threshold:")
        for a in alerts:
            print("  " + a)
        print("="*60 + "\n")

    return session_id


# ── CLI Entry Point ───────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="RF Scanner Daemon")
    parser.add_argument("--config",  default=str(CONFIG_PATH))
    parser.add_argument("--db",      default=str(DB_PATH))
    parser.add_argument("--once",    action="store_true", help="Run one scan and exit")
    parser.add_argument("--simulate", action="store_true",
                        help="Simulate SDR and GPS (for testing without hardware)")
    # Dependency-check flags (consumed early above; kept here for --help display)
    parser.add_argument("--skip-check", action="store_true",
                        help="Skip startup dependency check (not recommended)")
    parser.add_argument("--no-fix",    action="store_true",
                        help="Check dependencies but do not auto-install missing items")
    args = parser.parse_args()

    cfg  = load_config(Path(args.config))
    conn = init_db(Path(args.db))
    simulate = args.simulate or cfg.get("simulate", False)

    if simulate:
        log.info("*** SIMULATION MODE — no real hardware required ***")

    interval = cfg.get("scan_interval_s", 300)

    if args.once:
        run_scan(conn, cfg, simulate)
    else:
        log.info("Starting continuous scan loop (interval=%ds)", interval)
        while True:
            try:
                run_scan(conn, cfg, simulate)
            except Exception as e:
                log.error("Scan error: %s", e, exc_info=True)
            log.info("Sleeping %ds until next scan…", interval)
            time.sleep(interval)


if __name__ == "__main__":
    main()
