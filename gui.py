#!/usr/bin/env python3
"""
RF Scanner Desktop GUI
Local control panel for the Raspberry Pi operator.
Displays live scan results, noise floor stats, and frequency alerts.
Requires: tkinter, matplotlib, requests
"""

# ── Dependency check — runs before the GUI window opens ──────────────────────
import sys
import os

def _parse_early_flags():
    skip  = "--skip-check" in sys.argv
    no_fix = "--no-fix"    in sys.argv
    sim   = "--simulate"   in sys.argv
    return skip, not no_fix, sim

_skip_check, _auto_fix, _sim = _parse_early_flags()

if not _skip_check:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from driver_check import run_checks_with_gui_splash
        _ok = run_checks_with_gui_splash(auto_fix=_auto_fix, simulate=_sim)
        if not _ok:
            # User clicked "Exit" on the splash failure screen
            sys.exit(1)
    except Exception as _e:
        print(f"[gui] Warning: dependency check error ({_e}). Continuing…")

# ── Now safe to import everything else ───────────────────────────────────────
import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from pathlib import Path

try:
    import requests
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.colors import Normalize
    from matplotlib import cm
except ImportError as e:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(); root.withdraw()
    messagebox.showerror(
        "Missing dependency",
        f"Required package not available: {e}\n\n"
        "Run:  pip3 install requests matplotlib --break-system-packages"
    )
    sys.exit(1)

import subprocess

API_BASE = "http://localhost:5000/api"
POLL_INTERVAL_MS = 10_000  # auto-refresh every 10s
CONFIG_PATH = Path("/home/pi/rf_scanner/config.json")

# ── Colour palette ─────────────────────────────────────────────────────────────
BG_DARK  = "#0d1117"
BG_MID   = "#161b22"
BG_CARD  = "#1c2230"
ACCENT   = "#00d4aa"
ACCENT2  = "#f0a500"
TEXT_PRI = "#e6edf3"
TEXT_SEC = "#8b949e"
ALERT    = "#ff4d4f"
GOOD     = "#3fb950"
BORDER   = "#30363d"

FONT_MONO = ("Courier New", 10)
FONT_BODY = ("Segoe UI", 10)
FONT_H1   = ("Segoe UI", 18, "bold")
FONT_H2   = ("Segoe UI", 13, "bold")
FONT_TINY = ("Segoe UI", 8)


class RFScannerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RF Scanner — Control Panel")
        self.geometry("1280x800")
        self.configure(bg=BG_DARK)
        self.resizable(True, True)

        self._scan_running = False
        self._freq_data    = {}
        self._sessions     = []
        self._selected_freq = None
        self._display_mode = tk.StringVar(value="amplitude")

        self._build_ui()
        self._refresh_data()
        self._schedule_refresh()

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        bar = tk.Frame(self, bg=BG_DARK, padx=16, pady=10)
        bar.pack(fill="x")
        tk.Label(bar, text="⚡ RF Scanner", font=FONT_H1,
                 bg=BG_DARK, fg=ACCENT).pack(side="left")

        self._status_lbl = tk.Label(bar, text="● connecting…",
                                    font=FONT_BODY, bg=BG_DARK, fg=TEXT_SEC)
        self._status_lbl.pack(side="left", padx=20)

        tk.Button(bar, text="⟳  Refresh", font=FONT_BODY, bg=BG_MID,
                  fg=TEXT_PRI, activebackground=ACCENT, relief="flat",
                  cursor="hand2", command=self._refresh_data,
                  padx=12, pady=4).pack(side="right", padx=4)
        tk.Button(bar, text="▶  Run Scan Now", font=FONT_BODY, bg=ACCENT,
                  fg=BG_DARK, activebackground="#00b898", relief="flat",
                  cursor="hand2", command=self._trigger_scan,
                  padx=12, pady=4).pack(side="right", padx=4)
        tk.Button(bar, text="⚙  Config", font=FONT_BODY, bg=BG_MID,
                  fg=TEXT_PRI, activebackground=BG_CARD, relief="flat",
                  cursor="hand2", command=self._open_config,
                  padx=12, pady=4).pack(side="right", padx=4)

        # Separator
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # Main paned layout
        pane = tk.PanedWindow(self, orient="horizontal",
                              bg=BG_DARK, sashwidth=4, sashrelief="flat")
        pane.pack(fill="both", expand=True, padx=0, pady=0)

        left  = self._build_left_panel(pane)
        right = self._build_right_panel(pane)
        pane.add(left,  minsize=320)
        pane.add(right, minsize=600)

        # Status bar
        sb = tk.Frame(self, bg=BG_MID, padx=12, pady=3)
        sb.pack(fill="x", side="bottom")
        self._sb_lbl = tk.Label(sb, text="Ready", font=FONT_TINY,
                                 bg=BG_MID, fg=TEXT_SEC)
        self._sb_lbl.pack(side="left")
        self._time_lbl = tk.Label(sb, text="", font=FONT_TINY,
                                   bg=BG_MID, fg=TEXT_SEC)
        self._time_lbl.pack(side="right")
        self._update_clock()

    def _build_left_panel(self, parent):
        frame = tk.Frame(parent, bg=BG_MID, width=340)

        # Noise floor card
        nf_card = self._card(frame, "📊 Noise Floor")
        self._nf_mean   = self._stat_row(nf_card, "Mean")
        self._nf_std    = self._stat_row(nf_card, "Std Dev")
        self._nf_median = self._stat_row(nf_card, "Median")
        self._nf_n      = self._stat_row(nf_card, "Samples")

        # Mode selector
        mode_card = self._card(frame, "🗺  Display Mode")
        for label, val in [("Amplitude (dBm)", "amplitude"), ("SNR (dB)", "snr")]:
            rb = tk.Radiobutton(mode_card, text=label, variable=self._display_mode,
                                value=val, bg=BG_CARD, fg=TEXT_PRI,
                                selectcolor=BG_DARK, activebackground=BG_CARD,
                                font=FONT_BODY, cursor="hand2",
                                command=self._on_mode_change)
            rb.pack(anchor="w", pady=2)

        # Tabbed notebook for left panel
        style = ttk.Style()
        style.configure("Left.TNotebook",     background=BG_MID, borderwidth=0)
        style.configure("Left.TNotebook.Tab", background=BG_CARD, foreground=TEXT_SEC,
                                              font=FONT_TINY, padding=[8, 4])
        style.map("Left.TNotebook.Tab",
                  background=[("selected", BG_DARK)],
                  foreground=[("selected", ACCENT)])

        left_nb = ttk.Notebook(frame, style="Left.TNotebook")
        left_nb.pack(fill="both", expand=True, padx=4, pady=4)

        # ── Tab A: Frequencies ────────────────────────────────────────────────
        freq_tab = tk.Frame(left_nb, bg=BG_DARK)
        left_nb.add(freq_tab, text=" 📡 Frequencies ")
        listframe = tk.Frame(freq_tab, bg=BG_DARK)
        listframe.pack(fill="both", expand=True, padx=4, pady=4)
        sb = tk.Scrollbar(listframe)
        sb.pack(side="right", fill="y")
        self._freq_list = tk.Listbox(
            listframe, bg=BG_DARK, fg=TEXT_PRI, font=FONT_MONO,
            selectbackground=ACCENT, selectforeground=BG_DARK,
            activestyle="none", relief="flat", yscrollcommand=sb.set,
            height=12, cursor="hand2",
        )
        self._freq_list.pack(fill="both", expand=True)
        sb.config(command=self._freq_list.yview)
        self._freq_list.bind("<<ListboxSelect>>", self._on_freq_select)

        # ── Tab B: Overlays ───────────────────────────────────────────────────
        ov_tab = tk.Frame(left_nb, bg=BG_DARK)
        left_nb.add(ov_tab, text=" 📂 Overlays ")
        self._build_overlay_panel(ov_tab)

        # ── Tab C: Alerts ─────────────────────────────────────────────────────
        alert_tab = tk.Frame(left_nb, bg=BG_DARK)
        left_nb.add(alert_tab, text=" 🚨 Alerts ")
        alert_toolbar = tk.Frame(alert_tab, bg=BG_DARK, pady=4)
        alert_toolbar.pack(fill="x", padx=4)
        tk.Button(alert_toolbar, text="⟳ Refresh Alerts", font=FONT_TINY,
                  bg=BG_MID, fg=TEXT_PRI, relief="flat", cursor="hand2",
                  command=self._refresh_alerts, padx=8, pady=3).pack(side="left")
        self._alert_badge = tk.Label(alert_toolbar, text="", font=FONT_TINY,
                                      bg=BG_DARK, fg=ALERT)
        self._alert_badge.pack(side="left", padx=6)

        alert_frame = tk.Frame(alert_tab, bg=BG_DARK)
        alert_frame.pack(fill="both", expand=True, padx=4)
        alert_sb = tk.Scrollbar(alert_frame)
        alert_sb.pack(side="right", fill="y")
        self._alert_text = tk.Text(alert_frame, bg=BG_DARK, fg=ALERT,
                                    font=FONT_MONO, relief="flat",
                                    state="disabled", wrap="word",
                                    yscrollcommand=alert_sb.set)
        self._alert_text.pack(fill="both", expand=True)
        alert_sb.config(command=self._alert_text.yview)
        self._alert_text.tag_config("header", foreground=ACCENT2, font=(FONT_MONO[0], FONT_MONO[1], "bold"))
        self._alert_text.tag_config("ok",     foreground=GOOD)

        return frame

    def _build_overlay_panel(self, parent):
        """Build the KMZ / overlay management panel inside the left notebook."""
        # Toolbar
        tb = tk.Frame(parent, bg=BG_DARK, pady=4)
        tb.pack(fill="x", padx=4)
        tk.Button(tb, text="📂 Import KMZ", font=FONT_TINY, bg=ACCENT2,
                  fg=BG_DARK, relief="flat", cursor="hand2",
                  command=self._import_kmz, padx=8, pady=3).pack(side="left", padx=2)
        tk.Button(tb, text="↓ Export KMZ", font=FONT_TINY, bg=BG_MID,
                  fg=TEXT_PRI, relief="flat", cursor="hand2",
                  command=self._export_overlay_kmz, padx=8, pady=3).pack(side="left", padx=2)
        tk.Button(tb, text="✕ Clear", font=FONT_TINY, bg=BG_MID,
                  fg=ALERT, relief="flat", cursor="hand2",
                  command=self._clear_overlays, padx=8, pady=3).pack(side="right", padx=2)

        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=4, pady=2)

        # Layer list
        lf = tk.Frame(parent, bg=BG_DARK)
        lf.pack(fill="both", expand=True, padx=4)
        lsb = tk.Scrollbar(lf)
        lsb.pack(side="right", fill="y")
        self._overlay_list = tk.Listbox(
            lf, bg=BG_DARK, fg=TEXT_PRI, font=FONT_MONO,
            selectbackground=ACCENT, selectforeground=BG_DARK,
            activestyle="none", relief="flat", yscrollcommand=lsb.set,
            height=10, cursor="hand2",
        )
        self._overlay_list.pack(fill="both", expand=True)
        lsb.config(command=self._overlay_list.yview)

        # Layer action buttons
        ab = tk.Frame(parent, bg=BG_DARK, pady=4)
        ab.pack(fill="x", padx=4)
        tk.Button(ab, text="👁 Toggle", font=FONT_TINY, bg=BG_MID,
                  fg=TEXT_PRI, relief="flat", cursor="hand2",
                  command=self._toggle_overlay_layer, padx=8, pady=3).pack(side="left", padx=2)
        tk.Button(ab, text="🗑 Delete", font=FONT_TINY, bg=BG_MID,
                  fg=ALERT, relief="flat", cursor="hand2",
                  command=self._delete_overlay_layer, padx=8, pady=3).pack(side="left", padx=2)

        # Internal overlay data store (mirrors server, for display)
        self._overlay_layers = []   # [{id, name, color, visible, count}]

    def _build_right_panel(self, parent):
        frame = tk.Frame(parent, bg=BG_DARK)

        # Notebook for chart tabs
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook",          background=BG_DARK, borderwidth=0)
        style.configure("TNotebook.Tab",      background=BG_MID, foreground=TEXT_SEC,
                                               font=FONT_BODY, padding=[12, 6])
        style.map("TNotebook.Tab",
                  background=[("selected", BG_CARD)],
                  foreground=[("selected", ACCENT)])

        nb = ttk.Notebook(frame)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        # Tab 1 — spectrum bar chart
        self._tab_spectrum = tk.Frame(nb, bg=BG_DARK)
        nb.add(self._tab_spectrum, text="  Spectrum  ")
        self._fig_spectrum, self._ax_spectrum = self._make_fig()
        self._canvas_spectrum = FigureCanvasTkAgg(self._fig_spectrum, self._tab_spectrum)
        self._canvas_spectrum.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self._canvas_spectrum, self._tab_spectrum)

        # Tab 2 — SNR chart
        self._tab_snr = tk.Frame(nb, bg=BG_DARK)
        nb.add(self._tab_snr, text="  SNR  ")
        self._fig_snr, self._ax_snr = self._make_fig()
        self._canvas_snr = FigureCanvasTkAgg(self._fig_snr, self._tab_snr)
        self._canvas_snr.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self._canvas_snr, self._tab_snr)

        # Tab 3 — session table
        self._tab_sessions = tk.Frame(nb, bg=BG_DARK)
        nb.add(self._tab_sessions, text="  Sessions  ")
        self._build_session_table(self._tab_sessions)

        # Detail bar below notebook
        detail = tk.Frame(frame, bg=BG_CARD, padx=12, pady=8)
        detail.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(detail, text="Selected Frequency Detail",
                 font=FONT_H2, bg=BG_CARD, fg=TEXT_PRI).pack(anchor="w")
        self._detail_lbl = tk.Label(detail, text="Click a frequency to see stats",
                                     font=FONT_BODY, bg=BG_CARD, fg=TEXT_SEC,
                                     justify="left")
        self._detail_lbl.pack(anchor="w")

        return frame

    def _build_session_table(self, parent):
        cols = ("id", "timestamp", "lat", "lon", "noise_mean", "n_noise", "label")
        tree = ttk.Treeview(parent, columns=cols, show="headings", height=20)
        for col, label, w in [
            ("id", "ID", 50), ("timestamp", "Time (UTC)", 160),
            ("lat", "Latitude", 90), ("lon", "Longitude", 90),
            ("noise_mean", "Noise (dBm)", 100), ("n_noise", "Noise N", 70),
            ("label", "Label", 120),
        ]:
            tree.heading(col, text=label)
            tree.column(col, width=w, anchor="center")
        style = ttk.Style()
        style.configure("Treeview", background=BG_DARK, foreground=TEXT_PRI,
                        fieldbackground=BG_DARK, font=FONT_BODY, rowheight=26)
        style.configure("Treeview.Heading", background=BG_MID,
                        foreground=ACCENT, font=FONT_BODY)
        sb = tk.Scrollbar(parent, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        self._session_tree = tree

    # ── Helper Widgets ─────────────────────────────────────────────────────────

    def _card(self, parent, title):
        outer = tk.Frame(parent, bg=BG_CARD, padx=10, pady=8)
        outer.pack(fill="x", padx=8, pady=4)
        tk.Label(outer, text=title, font=FONT_H2,
                 bg=BG_CARD, fg=TEXT_PRI).pack(anchor="w", pady=(0, 6))
        tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", pady=(0, 6))
        return outer

    def _stat_row(self, parent, label):
        row = tk.Frame(parent, bg=BG_CARD)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=label, font=FONT_BODY, bg=BG_CARD,
                 fg=TEXT_SEC, width=10, anchor="w").pack(side="left")
        val = tk.Label(row, text="—", font=FONT_MONO,
                       bg=BG_CARD, fg=ACCENT)
        val.pack(side="right")
        return val

    def _make_fig(self):
        fig = Figure(figsize=(8, 4), facecolor=BG_DARK)
        ax  = fig.add_subplot(111)
        ax.set_facecolor(BG_MID)
        for spine in ax.spines.values():
            spine.set_edgecolor(BORDER)
        ax.tick_params(colors=TEXT_SEC, labelsize=8)
        ax.yaxis.label.set_color(TEXT_SEC)
        ax.xaxis.label.set_color(TEXT_SEC)
        ax.title.set_color(TEXT_PRI)
        fig.tight_layout(pad=2)
        return fig, ax

    # ── Data Refresh ───────────────────────────────────────────────────────────

    def _refresh_data(self):
        threading.Thread(target=self._fetch_and_update, daemon=True).start()
        self._refresh_overlays()
        self._refresh_alerts()

    def _fetch_and_update(self):
        try:
            status = requests.get(f"{API_BASE}/status", timeout=5).json()
            freqs  = requests.get(f"{API_BASE}/frequencies", timeout=5).json()
            sessions = requests.get(f"{API_BASE}/sessions?limit=50", timeout=5).json()

            self._sessions = sessions
            self._freq_list_data = freqs

            # Latest noise floor from most recent session
            latest = status.get("latest_session") or {}

            self.after(0, lambda: self._update_ui(status, freqs, sessions, latest))
        except Exception as e:
            self.after(0, lambda: self._set_status(f"● offline — {e}", ALERT))

    def _update_ui(self, status, freqs, sessions, latest):
        n = status.get("n_sessions", 0)
        self._set_status(f"● online — {n} sessions recorded", GOOD)

        # Noise floor
        def fmt(v, unit=""):
            return f"{v:.1f}{unit}" if v is not None else "—"
        self._nf_mean  ["text"] = fmt(latest.get("noise_mean"),  " dBm")
        self._nf_std   ["text"] = fmt(latest.get("noise_std"),   " dB")
        self._nf_median["text"] = fmt(latest.get("noise_median"), " dBm")
        self._nf_n     ["text"] = str(latest.get("noise_n", "—"))

        # Frequency list
        self._freq_list.delete(0, "end")
        for f in freqs:
            mhz = f / 1e6
            if mhz >= 1000:
                label = f"{mhz/1000:.4f} GHz"
            elif mhz >= 1:
                label = f"{mhz:.3f} MHz"
            else:
                label = f"{f/1000:.1f} kHz"
            self._freq_list.insert("end", f"  {label}")
        self._freq_list_data = freqs

        # Session table
        for row in self._session_tree.get_children():
            self._session_tree.delete(row)
        for s in sessions:
            ts = s["timestamp"][:19].replace("T", " ") if s.get("timestamp") else "—"
            lat = f"{s['latitude']:.5f}" if s.get("latitude") else "—"
            lon = f"{s['longitude']:.5f}" if s.get("longitude") else "—"
            nm  = f"{s['noise_mean']:.1f}" if s.get("noise_mean") else "—"
            self._session_tree.insert("", "end", values=(
                s["id"], ts, lat, lon, nm, s.get("noise_n","—"),
                s.get("session_label","—"),
            ))

        # Draw charts from latest session
        self._draw_charts(sessions[:1])
        self._sb_lbl.config(text=f"Last refresh: {datetime.now().strftime('%H:%M:%S')}")

    def _draw_charts(self, sessions):
        if not sessions:
            return
        session_id = sessions[0]["id"]
        threading.Thread(target=self._fetch_session_charts,
                         args=(session_id,), daemon=True).start()

    def _fetch_session_charts(self, session_id):
        try:
            data = requests.get(f"{API_BASE}/sessions/{session_id}/detail",
                                timeout=10).json()
            self.after(0, lambda: self._render_charts(data))
        except Exception as e:
            print(f"Chart fetch error: {e}")

    def _render_charts(self, data):
        measurements = data.get("measurements", [])
        if not measurements:
            return

        freqs = [m["frequency_hz"] / 1e6 for m in measurements]
        amps  = [m["amplitude_dbm"] for m in measurements]
        snrs  = [m.get("snr_db") or 0 for m in measurements]
        flags = [m.get("is_outlier", 0) for m in measurements]

        # Spectrum chart
        ax = self._ax_spectrum
        ax.clear()
        ax.set_facecolor(BG_MID)
        colors = [ALERT if f else ACCENT for f in flags]
        bars = ax.bar(range(len(freqs)), amps, color=colors, edgecolor=BG_DARK, linewidth=0.5)
        ax.set_xticks(range(len(freqs)))
        ax.set_xticklabels([f"{f:.2f}" for f in freqs], rotation=45, ha="right",
                            fontsize=8, color=TEXT_SEC)
        ax.set_ylabel("Amplitude (dBm)", color=TEXT_SEC, fontsize=9)
        ax.set_title("Signal Amplitude — Latest Session", color=TEXT_PRI, fontsize=11)
        ax.tick_params(colors=TEXT_SEC)
        for spine in ax.spines.values():
            spine.set_edgecolor(BORDER)
        if data.get("session", {}).get("noise_mean"):
            ax.axhline(data["session"]["noise_mean"], color=ACCENT2,
                       linestyle="--", linewidth=1, label="Noise floor")
            ax.legend(facecolor=BG_MID, labelcolor=TEXT_PRI, fontsize=8)
        self._canvas_spectrum.draw()

        # SNR chart
        ax2 = self._ax_snr
        ax2.clear()
        ax2.set_facecolor(BG_MID)
        snr_colors = [ALERT if (f or s < 0) else GOOD for f, s in zip(flags, snrs)]
        ax2.bar(range(len(freqs)), snrs, color=snr_colors, edgecolor=BG_DARK, linewidth=0.5)
        ax2.axhline(0, color=ACCENT2, linestyle="--", linewidth=1)
        ax2.set_xticks(range(len(freqs)))
        ax2.set_xticklabels([f"{f:.2f}" for f in freqs], rotation=45,
                             ha="right", fontsize=8, color=TEXT_SEC)
        ax2.set_ylabel("SNR (dB)", color=TEXT_SEC, fontsize=9)
        ax2.set_title("Signal-to-Noise Ratio — Latest Session", color=TEXT_PRI, fontsize=11)
        ax2.tick_params(colors=TEXT_SEC)
        for spine in ax2.spines.values():
            spine.set_edgecolor(BORDER)
        self._canvas_snr.draw()

        # Alerts
        alerts = [m for m in measurements if m.get("is_outlier")]
        self._alert_text.config(state="normal")
        self._alert_text.delete("1.0", "end")
        if alerts:
            for m in alerts:
                self._alert_text.insert("end",
                    f"⚠ {m['frequency_hz']/1e6:.3f} MHz: "
                    f"{m['amplitude_dbm']:.1f} dBm  "
                    f"(SNR {m.get('snr_db',0):.1f} dB)\n"
                )
        else:
            self._alert_text.insert("end", "✓ No anomalies detected")
            self._alert_text.config(fg=GOOD)
        self._alert_text.config(state="disabled")

    # ── Event Handlers ─────────────────────────────────────────────────────────

    def _on_freq_select(self, event):
        sel = self._freq_list.curselection()
        if not sel:
            return
        idx  = sel[0]
        freq = self._freq_list_data[idx]
        self._selected_freq = freq
        threading.Thread(target=self._fetch_freq_detail,
                         args=(freq,), daemon=True).start()

    def _fetch_freq_detail(self, freq):
        try:
            data = requests.get(f"{API_BASE}/stats/frequency/{int(freq)}",
                                timeout=10).json()
            self.after(0, lambda: self._show_freq_detail(data))
        except Exception as e:
            print(f"Freq detail error: {e}")

    def _show_freq_detail(self, data):
        amp = data.get("amplitude_dbm", {})
        snr = data.get("snr_db", {})
        freq_mhz = data["frequency_hz"] / 1e6
        text = (
            f"{freq_mhz:.4f} MHz  │  "
            f"Amp: {amp.get('mean','—')} dBm avg  "
            f"(min {amp.get('min','—')} / max {amp.get('max','—')})  │  "
            f"SNR: {snr.get('mean','—')} dB avg  │  "
            f"{data.get('total_readings','—')} readings  "
            f"({data.get('outlier_count',0)} flagged)"
        )
        self._detail_lbl.config(text=text)

    def _on_mode_change(self):
        self._refresh_data()

    def _trigger_scan(self):
        if self._scan_running:
            messagebox.showinfo("Scan", "A scan is already running.")
            return
        if messagebox.askyesno("Run Scan", "Trigger an immediate scan now?"):
            self._scan_running = True
            threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        try:
            self.after(0, lambda: self._set_status("● scanning…", ACCENT2))
            subprocess.run(
                [sys.executable, "/home/pi/rf_scanner/scanner/rf_scanner.py",
                 "--once", "--simulate"],
                check=True, timeout=120,
            )
            self.after(0, self._refresh_data)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Scan Error", str(e)))
        finally:
            self._scan_running = False

    def _open_config(self):
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            cfg = {}
        win = tk.Toplevel(self)
        win.title("Scanner Configuration")
        win.geometry("540x480")
        win.configure(bg=BG_DARK)

        tk.Label(win, text="⚙  Configuration", font=FONT_H1,
                 bg=BG_DARK, fg=ACCENT).pack(padx=16, pady=12, anchor="w")

        lf = tk.Frame(win, bg=BG_CARD, padx=14, pady=10)
        lf.pack(fill="both", expand=True, padx=16, pady=4)

        entries = {}
        fields = [
            ("frequencies_hz (comma-separated)", "frequencies_hz"),
            ("Scan interval (seconds)",           "scan_interval_s"),
            ("Dwell time (ms)",                   "dwell_ms"),
            ("SDR Gain (auto or number)",          "sdr_gain"),
            ("SDR PPM correction",                 "sdr_ppm"),
            ("Session label",                      "session_label"),
        ]
        for label, key in fields:
            row = tk.Frame(lf, bg=BG_CARD)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=label, font=FONT_BODY, bg=BG_CARD,
                     fg=TEXT_SEC, width=32, anchor="w").pack(side="left")
            val = cfg.get(key, "")
            if key == "frequencies_hz" and isinstance(val, list):
                val = ", ".join(str(f) for f in val)
            e = tk.Entry(row, font=FONT_MONO, bg=BG_DARK, fg=ACCENT,
                         insertbackground=ACCENT, relief="flat")
            e.insert(0, str(val))
            e.pack(side="right", fill="x", expand=True)
            entries[key] = e

        def save():
            new_cfg = {}
            for _, key in fields:
                v = entries[key].get().strip()
                if key == "frequencies_hz":
                    new_cfg[key] = [int(f.strip()) for f in v.split(",") if f.strip()]
                elif key in ("scan_interval_s", "dwell_ms", "sdr_ppm"):
                    new_cfg[key] = int(v) if v else 0
                else:
                    new_cfg[key] = v
            CONFIG_PATH.write_text(json.dumps(new_cfg, indent=2))
            messagebox.showinfo("Config", "Configuration saved.")
            win.destroy()

        tk.Button(win, text="Save Configuration", font=FONT_BODY,
                  bg=ACCENT, fg=BG_DARK, relief="flat", cursor="hand2",
                  command=save, padx=16, pady=6).pack(pady=12)

    def _import_kmz(self):
        """Open a file dialog to import a KMZ or KML file, POST it to the server."""
        paths = filedialog.askopenfilenames(
            title="Import KMZ / KML overlay",
            filetypes=[("KMZ / KML files", "*.kmz *.kml"), ("All files", "*.*")],
        )
        if not paths:
            return
        for path in paths:
            threading.Thread(target=self._do_import_kmz, args=(path,), daemon=True).start()

    def _do_import_kmz(self, path: str):
        """Parse a KMZ/KML file locally and push each feature to the overlay API."""
        import zipfile, xml.etree.ElementTree as ET

        try:
            p = Path(path)
            if p.suffix.lower() == ".kmz":
                with zipfile.ZipFile(p) as zf:
                    kml_name = next((n for n in zf.namelist() if n.endswith(".kml")), None)
                    if not kml_name:
                        self.after(0, lambda: messagebox.showerror("Import Error", f"No KML found inside {p.name}"))
                        return
                    kml_text = zf.read(kml_name).decode("utf-8", errors="replace")
            else:
                kml_text = p.read_text(encoding="utf-8", errors="replace")

            root = ET.fromstring(kml_text)
            ns = {"k": "http://www.opengis.net/kml/2.2"}

            def txt(el, tag):
                t = el.find(tag, ns)
                return t.text.strip() if t is not None and t.text else ""

            def parse_coords(raw):
                pts = []
                for tok in raw.strip().split():
                    parts = tok.split(",")
                    if len(parts) >= 2:
                        try:
                            pts.append([float(parts[0]), float(parts[1])])
                        except ValueError:
                            pass
                return pts

            # Create one layer per KML folder, or one root layer
            layer_name = p.stem
            layer_resp = requests.post(f"{API_BASE}/overlays/layer", json={
                "name": layer_name, "color": "#f0a500", "type": "kml", "source": p.name
            }, timeout=10)
            if not layer_resp.ok:
                self.after(0, lambda: messagebox.showerror("Import Error", "Could not create layer on server"))
                return
            layer_id = layer_resp.json()["id"]
            count = 0

            for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
                name = txt(pm, "k:name") or "Unnamed"
                desc = txt(pm, "k:description")
                geom = None

                point = pm.find(".//k:Point/k:coordinates", ns)
                if point is not None and point.text:
                    parts = point.text.strip().split(",")
                    if len(parts) >= 2:
                        geom = {"type": "Point", "coordinates": [float(parts[0]), float(parts[1])]}

                line = pm.find(".//k:LineString/k:coordinates", ns)
                if line is not None and line.text:
                    pts = parse_coords(line.text)
                    if len(pts) >= 2:
                        geom = {"type": "LineString", "coordinates": pts}

                poly = pm.find(".//k:Polygon//k:LinearRing/k:coordinates", ns)
                if poly is not None and poly.text:
                    pts = parse_coords(poly.text)
                    if len(pts) >= 3:
                        geom = {"type": "Polygon", "coordinates": [pts]}

                if geom:
                    item_type = geom["type"].lower().replace("linestring", "path")
                    requests.post(f"{API_BASE}/overlays/item", json={
                        "layer_id": layer_id, "item_type": item_type,
                        "name": name, "description": desc, "geometry": geom,
                    }, timeout=10)
                    count += 1

            msg = f"Imported {count} features from {p.name}"
            self.after(0, lambda: self._refresh_overlays())
            self.after(0, lambda: self._sb_lbl.config(text=msg))

        except Exception as e:
            self.after(0, lambda err=e: messagebox.showerror("Import Error", str(err)))

    def _export_overlay_kmz(self):
        """Trigger a KMZ download from the server and save locally."""
        path = filedialog.asksaveasfilename(
            title="Save overlay KMZ",
            defaultextension=".kmz",
            filetypes=[("KMZ file", "*.kmz")],
            initialfile=f"rf_overlays_{datetime.utcnow().strftime('%Y%m%d')}.kmz",
        )
        if not path:
            return
        threading.Thread(target=self._do_export_kmz, args=(path,), daemon=True).start()

    def _do_export_kmz(self, path: str):
        try:
            resp = requests.get(f"{API_BASE.replace('/api','')}/api/export/overlays/kmz",
                                timeout=30, stream=True)
            if resp.ok:
                with open(path, "wb") as f:
                    for chunk in resp.iter_content(8192):
                        f.write(chunk)
                self.after(0, lambda: messagebox.showinfo("Export", f"Saved to {path}"))
            else:
                self.after(0, lambda: messagebox.showerror("Export Error", f"Server returned {resp.status_code}"))
        except Exception as e:
            self.after(0, lambda err=e: messagebox.showerror("Export Error", str(err)))

    def _refresh_overlays(self):
        """Fetch overlay layers from server and update the listbox."""
        threading.Thread(target=self._fetch_overlays, daemon=True).start()

    def _fetch_overlays(self):
        try:
            data = requests.get(f"{API_BASE}/overlays", timeout=10).json()
            self.after(0, lambda d=data: self._render_overlay_list(d))
        except Exception:
            pass

    def _render_overlay_list(self, layers):
        self._overlay_layers = layers
        self._overlay_list.delete(0, "end")
        for layer in layers:
            vis = "👁" if layer.get("visible", 1) else "○"
            n   = len(layer.get("items", []))
            self._overlay_list.insert("end", f"  {vis}  {layer['name']}  ({n} items)")

    def _toggle_overlay_layer(self):
        sel = self._overlay_list.curselection()
        if not sel:
            return
        layer = self._overlay_layers[sel[0]]
        new_vis = 0 if layer.get("visible", 1) else 1
        try:
            requests.patch(f"{API_BASE}/overlays/layer/{layer['id']}",
                           json={"visible": new_vis}, timeout=5)
            self._refresh_overlays()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _delete_overlay_layer(self):
        sel = self._overlay_list.curselection()
        if not sel:
            return
        layer = self._overlay_layers[sel[0]]
        if not messagebox.askyesno("Delete layer", f"Delete layer '{layer['name']}'?"):
            return
        try:
            requests.delete(f"{API_BASE}/overlays/layer/{layer['id']}", timeout=5)
            self._refresh_overlays()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _clear_overlays(self):
        if not self._overlay_layers:
            return
        if not messagebox.askyesno("Clear overlays", "Delete all overlay layers?"):
            return
        for layer in self._overlay_layers:
            try:
                requests.delete(f"{API_BASE}/overlays/layer/{layer['id']}", timeout=5)
            except Exception:
                pass
        self._refresh_overlays()

    def _refresh_alerts(self):
        threading.Thread(target=self._fetch_alerts, daemon=True).start()

    def _fetch_alerts(self):
        try:
            alerts = requests.get(f"{API_BASE}/alerts/recent", timeout=10).json()
            self.after(0, lambda a=alerts: self._render_alerts(a))
        except Exception:
            pass

    def _render_alerts(self, alerts):
        self._alert_text.config(state="normal")
        self._alert_text.delete("1.0", "end")
        if not alerts:
            self._alert_text.insert("end", "✓ No alerts in recent sessions", "ok")
            self._alert_badge.config(text="")
        else:
            self._alert_badge.config(text=f"  ⚠ {len(alerts)} alerts")
            # Group by session
            by_session = {}
            for a in alerts:
                sid = a["session_id"]
                by_session.setdefault(sid, []).append(a)
            for sid, items in sorted(by_session.items(), reverse=True):
                ts = (items[0].get("timestamp") or "")[:19].replace("T", " ")
                label = items[0].get("session_label", "")
                self._alert_text.insert("end", f"Session {sid}  {ts}  [{label}]\n", "header")
                for a in items:
                    freq_mhz = a["frequency_hz"] / 1e6
                    amp = a.get("amplitude_dbm", 0)
                    snr = a.get("snr_db", 0) or 0
                    self._alert_text.insert(
                        "end",
                        f"  ⚠  {freq_mhz:.3f} MHz  {amp:.1f} dBm  SNR {snr:.1f} dB\n"
                    )
                self._alert_text.insert("end", "\n")
        self._alert_text.config(state="disabled")

    # ── Utilities ──────────────────────────────────────────────────────────────

    def _set_status(self, text, color=TEXT_SEC):
        self._status_lbl.config(text=text, fg=color)

    def _update_clock(self):
        self._time_lbl.config(text=datetime.utcnow().strftime("UTC %Y-%m-%d %H:%M:%S"))
        self.after(1000, self._update_clock)

    def _schedule_refresh(self):
        self._refresh_data()
        self.after(POLL_INTERVAL_MS, self._schedule_refresh)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="RF Scanner Desktop GUI")
    p.add_argument("--api",        default="http://localhost:5000/api",
                   help="Base URL of the RF Scanner web server API")
    p.add_argument("--skip-check", action="store_true",
                   help="Skip startup dependency check")
    p.add_argument("--no-fix",     action="store_true",
                   help="Check dependencies but do not auto-install")
    p.add_argument("--simulate",   action="store_true",
                   help="Relax hardware requirements in dependency check")
    args = p.parse_args()

    if args.api:
        API_BASE = args.api

    app = RFScannerGUI()
    app.mainloop()
