#!/usr/bin/env python3
"""
Seed script — generates realistic demo scan data for development/testing.
Run this on the Pi (or any machine) to populate the database with
a multi-day, multi-location dataset you can visualize immediately.
"""

import math
import random
import sqlite3
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "scans.db"

# Leesburg, VA area bounding box (realistic test area)
LAT_C, LON_C = 39.1157, -77.5636
SPREAD = 0.04   # ~4 km spread

# Frequencies to model
TARGET_FREQS = [
    88_100_000,    # WAMU FM
    100_300_000,   # WTOP FM (strong signal)
    162_400_000,   # NOAA Weather Radio
    433_920_000,   # ISM 433 MHz
    462_562_500,   # FRS/GMRS channel 1
    915_000_000,   # ISM 915 MHz (LoRa etc.)
    1_090_000_000, # ADS-B aircraft transponders
]

# Known "hot spots" — simulate signal sources at specific coords
SIGNAL_SOURCES = {
    88_100_000:    [(39.113, -77.558, -55), (39.118, -77.570, -60)],  # FM tower direction
    100_300_000:   [(39.120, -77.550, -52), (39.115, -77.555, -58)],
    162_400_000:   [(39.108, -77.562, -65)],
    1_090_000_000: [(39.116, -77.548, -72), (39.122, -77.535, -68)],  # aircraft route
}

NOISE_FLOOR_BASE = -96.0   # dBm
NOISE_STD_BASE   = 2.5


def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            latitude    REAL,
            longitude   REAL,
            altitude_m  REAL,
            gps_quality INTEGER,
            hdop        REAL,
            noise_mean  REAL,
            noise_std   REAL,
            noise_median REAL,
            noise_n     INTEGER,
            session_label TEXT
        );
        CREATE TABLE IF NOT EXISTS frequency_measurements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL REFERENCES scan_sessions(id),
            frequency_hz    REAL NOT NULL,
            amplitude_dbm   REAL NOT NULL,
            snr_db          REAL,
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
    """)
    conn.commit()


def distance_km(lat1, lon1, lat2, lon2):
    """Rough flat-earth distance in km."""
    dlat = (lat2 - lat1) * 111.0
    dlon = (lon2 - lon1) * 111.0 * math.cos(math.radians(lat1))
    return math.sqrt(dlat**2 + dlon**2)


def model_amplitude(freq_hz: float, lat: float, lon: float,
                    noise_floor: float, time_of_day: int) -> float:
    """
    Physics-inspired amplitude model.
    Simulates path-loss from known transmitter locations + noise.
    """
    sources = SIGNAL_SOURCES.get(freq_hz, [])
    max_amp = None

    for src_lat, src_lon, src_power in sources:
        d = distance_km(lat, lon, src_lat, src_lon)
        d = max(d, 0.1)
        # Free-space path loss approximation
        freq_mhz = freq_hz / 1e6
        fspl_db = 20 * math.log10(d) + 20 * math.log10(freq_mhz) + 32.45
        amp = src_power - fspl_db + random.gauss(0, 2.5)
        if max_amp is None or amp > max_amp:
            max_amp = amp

    # Time-of-day variation (propagation changes, traffic)
    tod_offset = 3 * math.sin(2 * math.pi * time_of_day / 24)

    base = max_amp if max_amp else noise_floor + random.gauss(2, 1)
    return min(base + tod_offset + random.gauss(0, 1.5), -20.0)


def gen_noise_samples(freq_min, freq_max, target_freqs, noise_floor, n=30):
    samples = []
    while len(samples) < n:
        f = random.uniform(freq_min, freq_max)
        if any(abs(f - t) < 500_000 for t in target_freqs):
            continue
        # Occasionally inject noise outliers to test rejection
        if random.random() < 0.05:
            amp = noise_floor + random.uniform(8, 15)  # spurious spike
        else:
            amp = noise_floor + random.gauss(0, NOISE_STD_BASE)
        samples.append((f, amp))
    return samples


def iqr_clean(values):
    if len(values) < 4:
        return values, []
    q1 = statistics.quantiles(values, n=4)[0]
    q3 = statistics.quantiles(values, n=4)[2]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [v for v in values if lo <= v <= hi], [v for v in values if v < lo or v > hi]


def seed(n_sessions=80):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    create_tables(conn)

    # Clear existing seed data
    conn.execute("DELETE FROM noise_samples")
    conn.execute("DELETE FROM frequency_measurements")
    conn.execute("DELETE FROM scan_sessions")
    conn.commit()

    print(f"Seeding {n_sessions} sessions…")
    base_time = datetime.now(timezone.utc) - timedelta(days=7)

    # Simulate a mobile survey — meandering route
    theta = 0.0
    lat, lon = LAT_C, LON_C

    for i in range(n_sessions):
        # Wander the route
        theta += random.uniform(-0.3, 0.5)
        lat += math.cos(theta) * random.uniform(0.0005, 0.002)
        lon += math.sin(theta) * random.uniform(0.0005, 0.002)
        lat = max(LAT_C - SPREAD, min(LAT_C + SPREAD, lat))
        lon = max(LON_C - SPREAD, min(LON_C + SPREAD, lon))

        ts  = (base_time + timedelta(hours=i * 2.1)).isoformat()
        tod = ((base_time + timedelta(hours=i * 2.1)).hour)

        # Noise floor with slight spatial variation
        nf_base = NOISE_FLOOR_BASE + random.gauss(0, 1.5)
        noise_samples = gen_noise_samples(
            min(TARGET_FREQS), max(TARGET_FREQS), TARGET_FREQS, nf_base
        )
        raw_amps = [a for _, a in noise_samples]
        clean, outliers = iqr_clean(raw_amps)
        noise_mean   = statistics.mean(clean)
        noise_std    = statistics.stdev(clean) if len(clean) > 1 else 1.5
        noise_median = statistics.median(clean)

        # Insert session
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scan_sessions
              (timestamp, latitude, longitude, altitude_m, gps_quality, hdop,
               noise_mean, noise_std, noise_median, noise_n, session_label)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (ts, lat, lon, 120.0 + random.gauss(0, 5), 1, round(random.uniform(0.9,2.2),1),
              noise_mean, noise_std, noise_median, len(clean), "survey_day_" + str(i//10 + 1)))
        session_id = cur.lastrowid

        # Noise samples
        for nf, na in noise_samples:
            cur.execute("""INSERT INTO noise_samples (session_id, frequency_hz, amplitude_dbm, is_outlier)
                           VALUES (?,?,?,?)""",
                        (session_id, nf, na, 1 if na in outliers else 0))

        # Target measurements
        for freq in TARGET_FREQS:
            amp = model_amplitude(freq, lat, lon, noise_mean, tod)
            snr = amp - noise_mean
            is_outlier = 1 if snr > 15 else 0
            reason = ">2.5σ above noise floor" if is_outlier else None
            cur.execute("""INSERT INTO frequency_measurements
                             (session_id, frequency_hz, amplitude_dbm, snr_db, is_outlier, outlier_reason)
                           VALUES (?,?,?,?,?,?)""",
                        (session_id, freq, round(amp,2), round(snr,2), is_outlier, reason))

        conn.commit()
        if i % 10 == 0:
            print(f"  {i}/{n_sessions} sessions…")

    print(f"Done. Database at {DB_PATH}")
    print(f"  Sessions:     {n_sessions}")
    print(f"  Frequencies:  {len(TARGET_FREQS)}")
    print(f"  Measurements: {n_sessions * len(TARGET_FREQS)}")


if __name__ == "__main__":
    seed()
