#!/usr/bin/env python3
"""
RF Scanner — Export Utilities
Adds CSV export to the Flask app and a standalone KML/GeoJSON exporter
for use with Google Earth or GIS tools.
Append the blueprint registration to server/app.py, or run standalone.
"""

import csv
import io
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, request, jsonify

DB_PATH = Path("/home/pi/rf_scanner/data/scans.db")

export_bp = Blueprint("export", __name__)


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ── CSV export ────────────────────────────────────────────────────────────────
@export_bp.route("/api/export/csv")
def export_csv():
    """
    Download all measurements as a flat CSV.
    Query params:
      freq_hz   — filter to single frequency (optional)
      days      — limit to last N days (default: all)
    """
    freq_filter = request.args.get("freq_hz")
    days        = request.args.get("days")

    db = get_db()
    where_clauses = []
    params = []

    if freq_filter:
        where_clauses.append("m.frequency_hz BETWEEN ? AND ?")
        params += [float(freq_filter) - 50000, float(freq_filter) + 50000]
    if days:
        where_clauses.append("s.timestamp >= datetime('now', ?)")
        params.append(f"-{days} days")

    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    rows = db.execute(f"""
        SELECT
            s.id            AS session_id,
            s.timestamp,
            s.latitude,
            s.longitude,
            s.altitude_m,
            s.gps_quality,
            s.hdop,
            s.noise_mean,
            s.noise_std,
            s.noise_median,
            s.noise_n,
            s.session_label,
            m.frequency_hz,
            m.amplitude_dbm,
            m.snr_db,
            m.is_outlier,
            m.outlier_reason
        FROM scan_sessions s
        JOIN frequency_measurements m ON m.session_id = s.id
        {where}
        ORDER BY s.timestamp, m.frequency_hz
    """, params).fetchall()

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "session_id", "timestamp", "latitude", "longitude", "altitude_m",
            "gps_quality", "hdop", "noise_mean_dbm", "noise_std_db",
            "noise_median_dbm", "noise_n", "session_label",
            "frequency_hz", "amplitude_dbm", "snr_db", "is_outlier", "outlier_reason"
        ])
        yield buf.getvalue(); buf.seek(0); buf.truncate()

        for row in rows:
            writer.writerow(list(row))
            yield buf.getvalue(); buf.seek(0); buf.truncate()

    fname = f"rf_scan_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ── GeoJSON export ────────────────────────────────────────────────────────────
@export_bp.route("/api/export/geojson")
def export_geojson():
    """Full GeoJSON FeatureCollection of all sessions (one point per session)."""
    db = get_db()
    sessions = db.execute("""
        SELECT s.*,
               AVG(m.amplitude_dbm) AS avg_amp,
               AVG(m.snr_db)        AS avg_snr,
               COUNT(m.id)          AS n_meas,
               SUM(m.is_outlier)    AS n_alerts
        FROM scan_sessions s
        JOIN frequency_measurements m ON m.session_id = s.id
        WHERE s.latitude IS NOT NULL
        GROUP BY s.id
        ORDER BY s.timestamp
    """).fetchall()

    features = []
    for s in sessions:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [s["longitude"], s["latitude"], s["altitude_m"] or 0]
            },
            "properties": {
                "session_id":    s["id"],
                "timestamp":     s["timestamp"],
                "noise_mean":    s["noise_mean"],
                "noise_std":     s["noise_std"],
                "avg_amplitude": round(s["avg_amp"], 2) if s["avg_amp"] else None,
                "avg_snr":       round(s["avg_snr"], 2) if s["avg_snr"] else None,
                "n_measurements": s["n_meas"],
                "n_alerts":       s["n_alerts"],
                "gps_quality":    s["gps_quality"],
                "hdop":           s["hdop"],
                "session_label":  s["session_label"],
            }
        })

    fc = {"type": "FeatureCollection", "features": features}
    fname = f"rf_scan_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.geojson"
    return Response(
        json.dumps(fc, indent=2),
        mimetype="application/geo+json",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ── KML export ────────────────────────────────────────────────────────────────
@export_bp.route("/api/export/kml")
def export_kml():
    """KML export compatible with Google Earth."""
    db = get_db()
    sessions = db.execute("""
        SELECT s.*,
               AVG(m.amplitude_dbm) AS avg_amp,
               AVG(m.snr_db)        AS avg_snr
        FROM scan_sessions s
        JOIN frequency_measurements m ON m.session_id = s.id
        WHERE s.latitude IS NOT NULL
        GROUP BY s.id
        ORDER BY s.timestamp
    """).fetchall()

    placemarks = []
    for s in sessions:
        alerts = db.execute("""
            SELECT frequency_hz, amplitude_dbm, snr_db
            FROM frequency_measurements
            WHERE session_id = ? AND is_outlier = 1
        """, (s["id"],)).fetchall()

        alert_lines = "".join(
            f"  {r['frequency_hz']/1e6:.3f} MHz: {r['amplitude_dbm']:.1f} dBm (SNR {r['snr_db']:.1f} dB)\n"
            for r in alerts
        )
        desc = (
            f"Time: {s['timestamp']}\n"
            f"Avg Amplitude: {s['avg_amp']:.1f} dBm\n"
            f"Avg SNR: {s['avg_snr']:.1f} dB\n"
            f"Noise floor: {s['noise_mean']:.1f} dBm\n"
            f"GPS quality: {s['gps_quality']}  HDOP: {s['hdop']}\n"
            + (f"Alerts:\n{alert_lines}" if alert_lines else "No alerts")
        )
        placemarks.append(f"""
    <Placemark>
      <name>Session {s['id']} — {(s['timestamp'] or '')[:10]}</name>
      <description><![CDATA[<pre>{desc}</pre>]]></description>
      <Point>
        <coordinates>{s['longitude']},{s['latitude']},{s['altitude_m'] or 0}</coordinates>
      </Point>
    </Placemark>""")

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>RF Scanner Export {datetime.utcnow().strftime('%Y-%m-%d')}</name>
    {''.join(placemarks)}
  </Document>
</kml>"""

    fname = f"rf_scan_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.kml"
    return Response(
        kml,
        mimetype="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ── KMZ export of user overlays ──────────────────────────────────────────────
@export_bp.route("/api/export/overlays/kmz")
def export_overlays_kmz():
    """
    Export all saved overlay layers (placemarks, paths, polygons) as a .kmz file.
    KMZ = ZIP containing doc.kml.
    """
    import zipfile
    db = get_db()

    # Ensure tables exist (may be first call on fresh DB)
    try:
        layers = db.execute("SELECT * FROM overlay_layers ORDER BY id").fetchall()
    except Exception:
        layers = []

    def hex_to_kml(hexcol):
        """Convert #RRGGBB → FFBBGGRR (KML AABBGGRR format, alpha=FF)."""
        h = hexcol.lstrip("#")
        if len(h) == 6:
            r, g, b = h[0:2], h[2:4], h[4:6]
            return f"FF{b}{g}{r}".upper()
        return "FFF0A500"

    folders = []
    for layer in layers:
        items = db.execute(
            "SELECT * FROM overlay_items WHERE layer_id=? ORDER BY id",
            (layer["id"],)
        ).fetchall()
        kml_color = hex_to_kml(layer["color"] or "#f0a500")
        pms = []
        for item in items:
            geom = json.loads(item["geometry"])
            name = item["name"] or "Unnamed"
            desc = item["description"] or ""
            ic   = hex_to_kml(item["color"] or layer["color"] or "#f0a500")

            style = f"""<Style>
        <IconStyle><color>{ic}</color><scale>1.1</scale>
          <Icon><href>http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png</href></Icon>
        </IconStyle>
        <LineStyle><color>{ic}</color><width>3</width></LineStyle>
        <PolyStyle><color>40{ic[2:]}</color></PolyStyle>
      </Style>"""

            coords_str = ""
            gtype = geom.get("type", "")
            coords = geom.get("coordinates", [])

            if gtype == "Point":
                lng, lat = coords[0], coords[1]
                alt = coords[2] if len(coords) > 2 else 0
                coords_str = f"<Point><coordinates>{lng},{lat},{alt}</coordinates></Point>"

            elif gtype == "LineString":
                pts = " ".join(f"{c[0]},{c[1]},{c[2] if len(c)>2 else 0}" for c in coords)
                coords_str = f"<LineString><tessellate>1</tessellate><coordinates>{pts}</coordinates></LineString>"

            elif gtype == "Polygon":
                # coords[0] = outer ring
                ring = coords[0] if coords else []
                pts = " ".join(f"{c[0]},{c[1]},{c[2] if len(c)>2 else 0}" for c in ring)
                coords_str = (
                    f"<Polygon><outerBoundaryIs><LinearRing>"
                    f"<coordinates>{pts}</coordinates>"
                    f"</LinearRing></outerBoundaryIs></Polygon>"
                )

            if coords_str:
                pms.append(f"""    <Placemark>
      <name>{name}</name>
      <description><![CDATA[{desc}]]></description>
      {style}
      {coords_str}
    </Placemark>""")

        if pms:
            folders.append(
                f"  <Folder><name>{layer['name']}</name>\n"
                + "\n".join(pms)
                + "\n  </Folder>"
            )

    kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>RF Scanner Overlays {datetime.utcnow().strftime('%Y-%m-%d')}</name>
{chr(10).join(folders)}
  </Document>
</kml>"""

    # Pack into a ZIP (KMZ)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml_content)
    buf.seek(0)

    fname = f"rf_overlays_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.kmz"
    return Response(
        buf.read(),
        mimetype="application/vnd.google-earth.kmz",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@export_bp.route("/api/export/combined/kmz")
def export_combined_kmz():
    """
    Export everything — scan session points + user overlays — as a single KMZ.
    """
    import zipfile
    db = get_db()

    # --- Scan session points ---
    sessions = db.execute("""
        SELECT s.*, AVG(m.amplitude_dbm) AS avg_amp, AVG(m.snr_db) AS avg_snr,
               SUM(m.is_outlier) AS n_alerts
        FROM scan_sessions s
        JOIN frequency_measurements m ON m.session_id = s.id
        WHERE s.latitude IS NOT NULL
        GROUP BY s.id ORDER BY s.timestamp
    """).fetchall()

    scan_pms = []
    for s in sessions:
        icon_color = "FF00D4AA" if not s["n_alerts"] else "FF4D4FFF"
        scan_pms.append(f"""    <Placemark>
      <name>Session {s['id']}</name>
      <description><![CDATA[
        Time: {s['timestamp']}<br/>
        Avg amp: {s['avg_amp']:.1f} dBm<br/>
        Avg SNR: {s['avg_snr']:.1f} dB<br/>
        Noise: {s['noise_mean']:.1f} dBm<br/>
        Alerts: {s['n_alerts'] or 0}
      ]]></description>
      <Style><IconStyle><color>{icon_color}</color><scale>0.8</scale></IconStyle></Style>
      <Point><coordinates>{s['longitude']},{s['latitude']},{s['altitude_m'] or 0}</coordinates></Point>
    </Placemark>""")

    # --- Overlay layers (reuse logic from overlay export) ---
    try:
        overlay_layers = db.execute("SELECT * FROM overlay_layers ORDER BY id").fetchall()
    except Exception:
        overlay_layers = []

    overlay_folders = []
    for layer in overlay_layers:
        items = db.execute("SELECT * FROM overlay_items WHERE layer_id=?", (layer["id"],)).fetchall()
        pms = []
        for item in items:
            geom = json.loads(item["geometry"])
            name = item["name"] or "Unnamed"
            gtype = geom.get("type", "")
            coords = geom.get("coordinates", [])
            coords_str = ""
            if gtype == "Point":
                coords_str = f"<Point><coordinates>{coords[0]},{coords[1]},0</coordinates></Point>"
            elif gtype == "LineString":
                pts = " ".join(f"{c[0]},{c[1]},0" for c in coords)
                coords_str = f"<LineString><coordinates>{pts}</coordinates></LineString>"
            elif gtype == "Polygon":
                ring = coords[0] if coords else []
                pts = " ".join(f"{c[0]},{c[1]},0" for c in ring)
                coords_str = f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{pts}</coordinates></LinearRing></outerBoundaryIs></Polygon>"
            if coords_str:
                pms.append(f"    <Placemark><name>{name}</name>{coords_str}</Placemark>")
        if pms:
            overlay_folders.append(f"  <Folder><name>{layer['name']}</name>\n" + "\n".join(pms) + "\n  </Folder>")

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>RF Scanner Complete Export {datetime.utcnow().strftime('%Y-%m-%d')}</name>
  <Folder>
    <name>Scan Sessions</name>
    {''.join(scan_pms)}
  </Folder>
  {''.join(overlay_folders)}
  </Document>
</kml>"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml)
    buf.seek(0)

    fname = f"rf_scanner_complete_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.kmz"
    return Response(
        buf.read(),
        mimetype="application/vnd.google-earth.kmz",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ── Standalone CLI exporter ───────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse, sys

    parser = argparse.ArgumentParser(description="Export RF scan data")
    parser.add_argument("--format", choices=["csv", "geojson", "kml"], default="csv")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--out", help="Output file (default: stdout)")
    parser.add_argument("--days", type=int, help="Limit to last N days")
    args = parser.parse_args()

    DB_PATH = Path(args.db)
    db = get_db()

    if args.format == "csv":
        rows = db.execute("""
            SELECT s.id, s.timestamp, s.latitude, s.longitude, s.altitude_m,
                   s.noise_mean, m.frequency_hz, m.amplitude_dbm, m.snr_db,
                   m.is_outlier
            FROM scan_sessions s
            JOIN frequency_measurements m ON m.session_id = s.id
            ORDER BY s.timestamp, m.frequency_hz
        """).fetchall()
        out = open(args.out, "w", newline="") if args.out else sys.stdout
        w = csv.writer(out)
        w.writerow(["session_id","timestamp","lat","lon","alt_m","noise_mean_dbm",
                    "frequency_hz","amplitude_dbm","snr_db","is_outlier"])
        for r in rows:
            w.writerow(list(r))
        if args.out:
            out.close()
            print(f"Wrote {len(rows)} rows to {args.out}")

    elif args.format == "geojson":
        # Reuse the logic above without Flask context
        sessions = db.execute("""
            SELECT s.*, AVG(m.amplitude_dbm) AS avg_amp, AVG(m.snr_db) AS avg_snr
            FROM scan_sessions s
            JOIN frequency_measurements m ON m.session_id = s.id
            WHERE s.latitude IS NOT NULL GROUP BY s.id
        """).fetchall()
        features = [{"type":"Feature",
                     "geometry":{"type":"Point","coordinates":[s["longitude"],s["latitude"]]},
                     "properties":{"session_id":s["id"],"timestamp":s["timestamp"],
                                   "avg_amp":s["avg_amp"],"avg_snr":s["avg_snr"]}}
                    for s in sessions]
        payload = json.dumps({"type":"FeatureCollection","features":features}, indent=2)
        if args.out:
            Path(args.out).write_text(payload)
            print(f"Wrote {len(features)} features to {args.out}")
        else:
            print(payload)
