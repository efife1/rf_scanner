#!/usr/bin/env python3
"""
RF Scanner Web Server
Serves the interactive heat-map web UI and REST API.
Run on the Raspberry Pi; accessible from any browser on the network.
"""

import json
import math
import sqlite3
import statistics
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from export import export_bp

# ── Config ────────────────────────────────────────────────────────────────────
import os as _os

DB_PATH      = Path(_os.environ.get("RF_SCANNER_DB",
                    str(Path(__file__).parent.parent / "data" / "scans.db")))
STATIC_DIR   = Path(__file__).parent.parent / "static"
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

# Share resolved path with export blueprint via environment
_os.environ.setdefault("RF_SCANNER_DB", str(DB_PATH))

app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(TEMPLATE_DIR))
CORS(app)  # allow remote browser access
app.register_blueprint(export_bp)


def init_db_schema():
    """
    Ensure ALL tables exist on startup — safe to call even when the DB was
    seeded before the overlay feature was added.
    """
    if not DB_PATH.exists():
        return  # will be created on first real request
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
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
    except Exception as e:
        print(f"[WARNING] Schema init error: {e}")


# Run schema check at import time so the DB is always ready
with app.app_context():
    init_db_schema()


# ── DB Helper ─────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def row_to_dict(row):
    return dict(row) if row else None


def ensure_overlay_tables(conn):
    """Create overlay tables if they don't exist yet."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS overlay_layers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            color       TEXT    NOT NULL DEFAULT '#f0a500',
            type        TEXT    NOT NULL DEFAULT 'drawn',
            source      TEXT,
            visible     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS overlay_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            layer_id    INTEGER NOT NULL REFERENCES overlay_layers(id) ON DELETE CASCADE,
            item_type   TEXT    NOT NULL,   -- 'placemark' | 'path' | 'polygon'
            name        TEXT,
            description TEXT,
            color       TEXT,
            geometry    TEXT    NOT NULL,   -- JSON: {type, coordinates}
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_oi_layer ON overlay_items(layer_id);
    """)
    conn.commit()


# ── Overlay API ───────────────────────────────────────────────────────────────

@app.route("/api/overlays", methods=["GET"])
def api_overlays_list():
    """Return all overlay layers with their items."""
    db = get_db()
    ensure_overlay_tables(db)
    layers = db.execute("SELECT * FROM overlay_layers ORDER BY id").fetchall()
    result = []
    for layer in layers:
        items = db.execute(
            "SELECT * FROM overlay_items WHERE layer_id=? ORDER BY id",
            (layer["id"],)
        ).fetchall()
        layer_dict = row_to_dict(layer)
        layer_dict["items"] = []
        for item in items:
            d = row_to_dict(item)
            d["geometry"] = json.loads(d["geometry"])
            layer_dict["items"].append(d)
        result.append(layer_dict)
    return jsonify(result)


@app.route("/api/overlays/layer", methods=["POST"])
def api_overlay_layer_create():
    """Create a new overlay layer. Body: {name, color, type, source, visible}"""
    data = request.get_json(force=True)
    if not data or not data.get("name"):
        abort(400, "name required")
    db = get_db()
    ensure_overlay_tables(db)
    cur = db.cursor()
    cur.execute("""
        INSERT INTO overlay_layers (name, color, type, source, visible)
        VALUES (?, ?, ?, ?, ?)
    """, (
        data["name"],
        data.get("color", "#f0a500"),
        data.get("type", "drawn"),
        data.get("source"),
        1 if data.get("visible", True) else 0,
    ))
    db.commit()
    layer = db.execute("SELECT * FROM overlay_layers WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(row_to_dict(layer)), 201


@app.route("/api/overlays/layer/<int:layer_id>", methods=["PATCH"])
def api_overlay_layer_update(layer_id):
    """Update layer name, color, or visibility."""
    data = request.get_json(force=True)
    db = get_db()
    ensure_overlay_tables(db)
    layer = db.execute("SELECT * FROM overlay_layers WHERE id=?", (layer_id,)).fetchone()
    if not layer:
        abort(404)
    fields, vals = [], []
    for col in ("name", "color", "visible"):
        if col in data:
            fields.append(f"{col}=?")
            vals.append(data[col])
    if fields:
        vals.append(layer_id)
        db.execute(f"UPDATE overlay_layers SET {','.join(fields)} WHERE id=?", vals)
        db.commit()
    updated = db.execute("SELECT * FROM overlay_layers WHERE id=?", (layer_id,)).fetchone()
    return jsonify(row_to_dict(updated))


@app.route("/api/overlays/layer/<int:layer_id>", methods=["DELETE"])
def api_overlay_layer_delete(layer_id):
    """Delete a layer and all its items (CASCADE)."""
    db = get_db()
    ensure_overlay_tables(db)
    db.execute("DELETE FROM overlay_layers WHERE id=?", (layer_id,))
    db.commit()
    return jsonify({"deleted": layer_id})


@app.route("/api/overlays/item", methods=["POST"])
def api_overlay_item_create():
    """
    Add an item to a layer.
    Body: {layer_id, item_type, name, description, color, geometry}
    geometry = GeoJSON-style {type: 'Point'|'LineString'|'Polygon', coordinates: [...]}
    """
    data = request.get_json(force=True)
    required = ("layer_id", "item_type", "geometry")
    for r in required:
        if r not in data:
            abort(400, f"{r} required")
    db = get_db()
    ensure_overlay_tables(db)
    layer = db.execute("SELECT id FROM overlay_layers WHERE id=?", (data["layer_id"],)).fetchone()
    if not layer:
        abort(404, "layer not found")
    cur = db.cursor()
    cur.execute("""
        INSERT INTO overlay_items (layer_id, item_type, name, description, color, geometry)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        data["layer_id"],
        data["item_type"],
        data.get("name"),
        data.get("description"),
        data.get("color"),
        json.dumps(data["geometry"]),
    ))
    db.commit()
    item = db.execute("SELECT * FROM overlay_items WHERE id=?", (cur.lastrowid,)).fetchone()
    d = row_to_dict(item)
    d["geometry"] = json.loads(d["geometry"])
    return jsonify(d), 201


@app.route("/api/overlays/item/<int:item_id>", methods=["PATCH"])
def api_overlay_item_update(item_id):
    """Update item name or description."""
    data = request.get_json(force=True)
    db = get_db()
    ensure_overlay_tables(db)
    item = db.execute("SELECT * FROM overlay_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        abort(404)
    fields, vals = [], []
    for col in ("name", "description", "color"):
        if col in data:
            fields.append(f"{col}=?")
            vals.append(data[col])
    if fields:
        vals.append(item_id)
        db.execute(f"UPDATE overlay_items SET {','.join(fields)} WHERE id=?", vals)
        db.commit()
    updated = db.execute("SELECT * FROM overlay_items WHERE id=?", (item_id,)).fetchone()
    d = row_to_dict(updated)
    d["geometry"] = json.loads(d["geometry"])
    return jsonify(d)


@app.route("/api/overlays/item/<int:item_id>", methods=["DELETE"])
def api_overlay_item_delete(item_id):
    db = get_db()
    ensure_overlay_tables(db)
    db.execute("DELETE FROM overlay_items WHERE id=?", (item_id,))
    db.commit()
    return jsonify({"deleted": item_id})


# ── Session filter endpoint ───────────────────────────────────────────────────

@app.route("/api/heatmap/filtered")
def api_heatmap_filtered():
    """
    Filtered heatmap with date range and optional frequency.
    Query params:
      mode         = 'amplitude' | 'snr'
      date_from    = YYYY-MM-DD
      date_to      = YYYY-MM-DD
      freq_hz      = integer (optional, filters to single frequency ±50kHz)
      label        = session_label substring (optional)
    """
    mode      = request.args.get("mode", "amplitude")
    date_from = request.args.get("date_from")
    date_to   = request.args.get("date_to")
    freq_hz   = request.args.get("freq_hz", type=int)
    label     = request.args.get("label")
    tol       = 50_000

    db = get_db()
    where = ["s.latitude IS NOT NULL", "s.longitude IS NOT NULL"]
    params = []

    if date_from:
        where.append("s.timestamp >= ?")
        params.append(date_from)
    if date_to:
        where.append("s.timestamp <= ?")
        params.append(date_to + "T23:59:59")
    if freq_hz:
        where.append("m.frequency_hz BETWEEN ? AND ?")
        params += [freq_hz - tol, freq_hz + tol]
    if label:
        where.append("s.session_label LIKE ?")
        params.append(f"%{label}%")

    where_clause = "WHERE " + " AND ".join(where)
    agg = "m.snr_db" if mode == "snr" else "m.amplitude_dbm"

    rows = db.execute(f"""
        SELECT s.latitude, s.longitude, s.timestamp, s.noise_mean,
               AVG({agg}) AS value, COUNT(m.id) AS n_meas,
               SUM(m.is_outlier) AS n_alerts
        FROM scan_sessions s
        JOIN frequency_measurements m ON m.session_id = s.id
        {where_clause}
        GROUP BY s.id
        ORDER BY s.timestamp
    """, params).fetchall()

    features = []
    for r in rows:
        if r["value"] is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
            "properties": {
                "value":       round(r["value"], 2),
                "mode":        mode,
                "timestamp":   r["timestamp"],
                "noise_floor": r["noise_mean"],
                "n_meas":      r["n_meas"],
                "n_alerts":    r["n_alerts"],
            }
        })
    return jsonify({"type": "FeatureCollection", "features": features})


# ── Alert polling endpoint ────────────────────────────────────────────────────

@app.route("/api/alerts/recent")
def api_alerts_recent():
    """
    Return the most recent flagged frequency measurements.
    Query params:
      since_session = session id (default: last 5 sessions)
      limit         = max alerts (default: 50)
    """
    since = request.args.get("since_session", type=int)
    limit = min(int(request.args.get("limit", 50)), 200)
    db    = get_db()

    if since:
        rows = db.execute("""
            SELECT m.frequency_hz, m.amplitude_dbm, m.snr_db, m.outlier_reason,
                   s.id AS session_id, s.timestamp, s.latitude, s.longitude,
                   s.session_label
            FROM frequency_measurements m
            JOIN scan_sessions s ON s.id = m.session_id
            WHERE m.is_outlier = 1 AND s.id > ?
            ORDER BY s.id DESC LIMIT ?
        """, (since, limit)).fetchall()
    else:
        # Last 5 sessions
        rows = db.execute("""
            SELECT m.frequency_hz, m.amplitude_dbm, m.snr_db, m.outlier_reason,
                   s.id AS session_id, s.timestamp, s.latitude, s.longitude,
                   s.session_label
            FROM frequency_measurements m
            JOIN scan_sessions s ON s.id = m.session_id
            WHERE m.is_outlier = 1
              AND s.id >= (SELECT MAX(id)-4 FROM scan_sessions)
            ORDER BY s.id DESC LIMIT ?
        """, (limit,)).fetchall()

    return jsonify([row_to_dict(r) for r in rows])


# ── REST API ──────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """Health check + latest session info."""
    try:
        db = get_db()
        latest = db.execute(
            "SELECT * FROM scan_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        n_sessions = db.execute("SELECT COUNT(*) FROM scan_sessions").fetchone()[0]
        n_meas     = db.execute("SELECT COUNT(*) FROM frequency_measurements").fetchone()[0]
        return jsonify({
            "status": "ok",
            "n_sessions": n_sessions,
            "n_measurements": n_meas,
            "latest_session": row_to_dict(latest),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/frequencies")
def api_frequencies():
    """List all unique target frequencies in the database."""
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT frequency_hz
        FROM frequency_measurements
        ORDER BY frequency_hz
    """).fetchall()
    return jsonify([r["frequency_hz"] for r in rows])


@app.route("/api/sessions")
def api_sessions():
    """List scan sessions with optional date filter."""
    limit = min(int(request.args.get("limit", 200)), 1000)
    db = get_db()
    rows = db.execute("""
        SELECT * FROM scan_sessions
        ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/heatmap/average")
def api_heatmap_average():
    """
    Average amplitude OR SNR heatmap across ALL sessions, all frequencies.
    Query params:
      mode = "amplitude" | "snr"  (default: amplitude)
    Returns GeoJSON FeatureCollection with measurement points.
    """
    mode = request.args.get("mode", "amplitude")
    db   = get_db()

    rows = db.execute("""
        SELECT
            s.latitude, s.longitude, s.timestamp,
            AVG(m.amplitude_dbm) AS avg_amplitude,
            AVG(m.snr_db)        AS avg_snr,
            s.noise_mean
        FROM scan_sessions s
        JOIN frequency_measurements m ON m.session_id = s.id
        WHERE s.latitude IS NOT NULL
          AND s.longitude IS NOT NULL
        GROUP BY s.id
        ORDER BY s.timestamp
    """).fetchall()

    features = []
    for r in rows:
        value = r["avg_snr"] if mode == "snr" else r["avg_amplitude"]
        if value is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
            "properties": {
                "value": round(value, 2),
                "mode": mode,
                "timestamp": r["timestamp"],
                "noise_floor": r["noise_mean"],
            }
        })

    return jsonify({"type": "FeatureCollection", "features": features})


@app.route("/api/heatmap/frequency/<int:freq_hz>")
def api_heatmap_frequency(freq_hz: int):
    """
    Amplitude or SNR heatmap for a single frequency.
    Uses session-level noise floor for SNR even in per-frequency view.
    """
    mode = request.args.get("mode", "amplitude")
    tol  = int(request.args.get("tolerance_hz", 50_000))
    db   = get_db()

    rows = db.execute("""
        SELECT
            s.latitude, s.longitude, s.timestamp,
            m.amplitude_dbm, m.snr_db, m.is_outlier,
            s.noise_mean
        FROM scan_sessions s
        JOIN frequency_measurements m ON m.session_id = s.id
        WHERE s.latitude IS NOT NULL
          AND s.longitude IS NOT NULL
          AND m.frequency_hz BETWEEN ? AND ?
        ORDER BY s.timestamp
    """, (freq_hz - tol, freq_hz + tol)).fetchall()

    features = []
    for r in rows:
        value = r["snr_db"] if mode == "snr" else r["amplitude_dbm"]
        if value is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
            "properties": {
                "value":       round(value, 2),
                "mode":        mode,
                "is_outlier":  bool(r["is_outlier"]),
                "timestamp":   r["timestamp"],
                "noise_floor": r["noise_mean"],
                "frequency_hz": freq_hz,
            }
        })

    return jsonify({"type": "FeatureCollection", "features": features})


@app.route("/api/sessions/<int:session_id>/detail")
def api_session_detail(session_id: int):
    """Full detail for a single session including all measurements."""
    db = get_db()
    session = db.execute("SELECT * FROM scan_sessions WHERE id=?", (session_id,)).fetchone()
    if not session:
        abort(404)

    measurements = db.execute("""
        SELECT * FROM frequency_measurements WHERE session_id=? ORDER BY frequency_hz
    """, (session_id,)).fetchall()

    noise_samples = db.execute("""
        SELECT * FROM noise_samples WHERE session_id=? ORDER BY frequency_hz
    """, (session_id,)).fetchall()

    return jsonify({
        "session":      row_to_dict(session),
        "measurements": [row_to_dict(m) for m in measurements],
        "noise_samples": [row_to_dict(n) for n in noise_samples],
    })


@app.route("/api/stats/frequency/<int:freq_hz>")
def api_freq_stats(freq_hz: int):
    """Statistical summary across all sessions for a given frequency."""
    tol = int(request.args.get("tolerance_hz", 50_000))
    db  = get_db()
    rows = db.execute("""
        SELECT m.amplitude_dbm, m.snr_db, m.is_outlier, s.timestamp
        FROM frequency_measurements m
        JOIN scan_sessions s ON s.id = m.session_id
        WHERE m.frequency_hz BETWEEN ? AND ?
        ORDER BY s.timestamp
    """, (freq_hz - tol, freq_hz + tol)).fetchall()

    amps = [r["amplitude_dbm"] for r in rows if not r["is_outlier"]]
    snrs = [r["snr_db"] for r in rows if r["snr_db"] is not None and not r["is_outlier"]]

    def safe_stats(values):
        if not values:
            return {}
        return {
            "min":    round(min(values), 2),
            "max":    round(max(values), 2),
            "mean":   round(statistics.mean(values), 2),
            "median": round(statistics.median(values), 2),
            "std":    round(statistics.stdev(values), 2) if len(values) > 1 else 0,
            "n":      len(values),
        }

    return jsonify({
        "frequency_hz":  freq_hz,
        "amplitude_dbm": safe_stats(amps),
        "snr_db":        safe_stats(snrs),
        "total_readings": len(rows),
        "outlier_count":  sum(1 for r in rows if r["is_outlier"]),
        "timeseries": [
            {"t": r["timestamp"], "amp": r["amplitude_dbm"], "snr": r["snr_db"]}
            for r in rows
        ],
    })


# ── Static / Frontend ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(str(TEMPLATE_DIR), "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    """Serve CSS, JS, and other static assets from the static/ directory."""
    return send_from_directory(str(STATIC_DIR), filename)


@app.errorhandler(404)
def not_found(e):
    # For unknown routes that aren't /api/*, serve index.html (SPA fallback)
    if not request.path.startswith("/api/"):
        return send_from_directory(str(TEMPLATE_DIR), "index.html")
    return jsonify({"error": "Not found", "path": request.path}), 404


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    print(f"RF Scanner server starting on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)
