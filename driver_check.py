#!/usr/bin/env python3
"""
driver_check.py — Startup dependency & driver verification for RF Scanner.

Checks (and, where possible, auto-installs) everything the scanner needs:
  • System packages  : rtl-sdr, librtlsdr-dev, gpsd, python3-pip, python3-serial
  • Conflicting kernel modules that must be blacklisted / unloaded
  • Python packages  : pyserial, pynmea2, flask, flask-cors  (+ matplotlib/requests for GUI)
  • RTL-SDR USB device presence (vendor 0bda)
  • Serial GPS port  (/dev/ttyAMA0 or /dev/ttyUSB0)
  • udev rule for non-root SDR access

Each check returns a CheckResult.  run_all_checks() prints a formatted
table and returns True only if every required item passed (or was fixed).

Usage
-----
  from driver_check import run_all_checks
  if not run_all_checks(auto_fix=True, gui_mode=False):
      sys.exit(1)

Or stand-alone:
  python3 driver_check.py [--no-fix] [--gui]
"""

import os
import sys
import shutil
import subprocess
import importlib
import platform
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── ANSI colours (suppressed when not a tty) ─────────────────────────────────
def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

RED    = lambda t: _c("0;31", t)
GREEN  = lambda t: _c("0;32", t)
YELLOW = lambda t: _c("1;33", t)
CYAN   = lambda t: _c("0;36", t)
BOLD   = lambda t: _c("1",    t)

# ── Data types ────────────────────────────────────────────────────────────────
@dataclass
class CheckResult:
    name:     str
    ok:       bool
    message:  str
    fixed:    bool  = False   # True if auto-fix succeeded
    required: bool  = True    # False = warn only, don't block startup
    detail:   str   = ""      # extra diagnostic info shown on failure

@dataclass
class CheckGroup:
    title:   str
    results: list = field(default_factory=list)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _is_root() -> bool:
    return os.geteuid() == 0

def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"

def _apt_install(*packages: str) -> tuple[bool, str]:
    """Install system packages via apt-get.  Requires root or sudo."""
    if not packages:
        return True, ""
    prefix = [] if _is_root() else ["sudo", "-n"]  # -n = non-interactive
    rc, out, err = _run([*prefix, "apt-get", "install", "-y", "-qq", *packages], timeout=120)
    if rc != 0:
        # Try with sudo (will prompt if TTY, fail silently if not)
        if not _is_root():
            rc, out, err = _run(["sudo", "apt-get", "install", "-y", "-qq", *packages], timeout=120)
    return rc == 0, err if rc != 0 else ""

def _pip_install(*packages: str) -> tuple[bool, str]:
    """Install Python packages via pip into the current interpreter."""
    if not packages:
        return True, ""
    rc, out, err = _run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--break-system-packages", *packages],
        timeout=120,
    )
    return rc == 0, err if rc != 0 else ""

def _module_importable(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False

def _binary_exists(name: str) -> bool:
    return shutil.which(name) is not None

def _kernel_module_loaded(name: str) -> bool:
    rc, out, _ = _run(["lsmod"])
    return name in out

def _is_raspberry_pi() -> bool:
    model = Path("/proc/device-tree/model")
    if model.exists():
        return "raspberry pi" in model.read_text(errors="ignore").lower()
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        return "raspberry pi" in cpuinfo.read_text(errors="ignore").lower()
    return False

def _is_linux() -> bool:
    return platform.system() == "Linux"

# ── Individual check functions ────────────────────────────────────────────────

# -- Python version -----------------------------------------------------------
def check_python_version() -> CheckResult:
    major, minor = sys.version_info[:2]
    ok = (major == 3 and minor >= 9)
    return CheckResult(
        name="Python version",
        ok=ok,
        message=f"Python {major}.{minor} {'✓' if ok else '— need 3.9+'}",
        required=True,
        detail="" if ok else f"Upgrade Python: sudo apt-get install python3.11",
    )

# -- System packages ----------------------------------------------------------
def check_system_package(pkg: str, test_binary: Optional[str] = None,
                          auto_fix: bool = True) -> CheckResult:
    """Check if a system package is installed; auto-install if missing."""
    binary = test_binary or pkg
    present = _binary_exists(binary)

    if present:
        return CheckResult(name=f"pkg:{pkg}", ok=True, message=f"{pkg} installed ✓")

    if not auto_fix or not _is_linux():
        return CheckResult(
            name=f"pkg:{pkg}", ok=False,
            message=f"{pkg} not found",
            detail=f"Install manually: sudo apt-get install {pkg}",
        )

    print(f"  {YELLOW('→')} Installing {pkg}…", flush=True)
    ok, err = _apt_install(pkg)
    return CheckResult(
        name=f"pkg:{pkg}",
        ok=ok, fixed=ok,
        message=f"{pkg} {'installed ✓' if ok else 'install FAILED ✗'}",
        detail=err if not ok else "",
    )

# -- rtl-sdr suite ------------------------------------------------------------
def check_rtlsdr_tools(auto_fix: bool = True) -> CheckResult:
    """Check for rtl_power and rtl_test binaries (from rtl-sdr package)."""
    has_power = _binary_exists("rtl_power")
    has_test  = _binary_exists("rtl_test")

    if has_power and has_test:
        return CheckResult(name="rtl-sdr tools", ok=True, message="rtl_power + rtl_test found ✓")

    if not auto_fix or not _is_linux():
        return CheckResult(
            name="rtl-sdr tools", ok=False,
            message="rtl_power / rtl_test not found",
            detail="sudo apt-get install rtl-sdr",
        )

    print(f"  {YELLOW('→')} Installing rtl-sdr…", flush=True)
    ok, err = _apt_install("rtl-sdr", "librtlsdr-dev")
    return CheckResult(
        name="rtl-sdr tools", ok=ok, fixed=ok,
        message=f"rtl-sdr {'installed ✓' if ok else 'install FAILED ✗'}",
        detail=err if not ok else "",
    )

# -- Conflicting DVB kernel modules -------------------------------------------
def check_dvb_blacklist(auto_fix: bool = True) -> CheckResult:
    """
    The dvb_usb_rtl28xxu kernel module claims the RTL2838 chip before
    rtl-sdr can.  It must be blacklisted and unloaded.
    """
    BAD_MODS = ["dvb_usb_rtl28xxu", "rtl2832", "rtl2830"]
    BLACKLIST_FILE = Path("/etc/modprobe.d/blacklist-rtl.conf")

    loaded = [m for m in BAD_MODS if _kernel_module_loaded(m)]
    blacklisted = BLACKLIST_FILE.exists() and all(
        m in BLACKLIST_FILE.read_text() for m in BAD_MODS
    )

    if not loaded and blacklisted:
        return CheckResult(name="DVB module blacklist", ok=True,
                           message="Conflicting DVB modules blacklisted ✓")

    if not auto_fix or not _is_linux():
        detail = ""
        if loaded:
            detail = f"Loaded modules: {', '.join(loaded)}\n"
        detail += f"Add to {BLACKLIST_FILE}:\n" + "\n".join(f"  blacklist {m}" for m in BAD_MODS)
        return CheckResult(name="DVB module blacklist", ok=False,
                           message="Conflicting DVB modules present", detail=detail)

    # Write blacklist file
    print(f"  {YELLOW('→')} Blacklisting DVB kernel modules…", flush=True)
    blacklist_content = "\n".join(f"blacklist {m}" for m in BAD_MODS) + "\n"
    try:
        if _is_root():
            BLACKLIST_FILE.write_text(blacklist_content)
        else:
            _run(["sudo", "bash", "-c",
                  f"echo '{blacklist_content}' > {BLACKLIST_FILE}"])
    except Exception as e:
        return CheckResult(name="DVB module blacklist", ok=False,
                           message="Could not write blacklist file",
                           detail=str(e))

    # Unload currently loaded modules
    unload_ok = True
    for mod in loaded:
        rc, _, err = _run(["sudo", "rmmod", mod] if not _is_root() else ["rmmod", mod])
        if rc != 0 and "not currently loaded" not in err:
            unload_ok = False
            log.warning("Could not unload %s: %s", mod, err)

    fixed = True  # blacklist written; reboot will permanently fix it
    msg = "DVB modules blacklisted ✓"
    detail = ""
    if loaded and not unload_ok:
        msg += " (reboot recommended to fully unload)"
        detail = f"Still loaded: {', '.join(loaded)}"

    return CheckResult(name="DVB module blacklist", ok=True, fixed=fixed,
                       message=msg, detail=detail)

# -- udev rule ----------------------------------------------------------------
def check_udev_rule(auto_fix: bool = True) -> CheckResult:
    """Ensure non-root users can access the RTL-SDR dongle via udev."""
    UDEV_FILE = Path("/etc/udev/rules.d/20-rtlsdr.rules")
    RULE = (
        'SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", '
        'GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"\n'
        'SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", '
        'GROUP="plugdev", MODE="0666"\n'
    )

    if UDEV_FILE.exists() and "0bda" in UDEV_FILE.read_text():
        return CheckResult(name="udev RTL-SDR rule", ok=True,
                           message="udev rule present ✓")

    if not auto_fix or not _is_linux():
        return CheckResult(name="udev RTL-SDR rule", ok=False,
                           message="udev rule missing",
                           detail=f"Create {UDEV_FILE} with RTL-SDR rules",
                           required=False)

    print(f"  {YELLOW('→')} Installing udev rule…", flush=True)
    try:
        if _is_root():
            UDEV_FILE.write_text(RULE)
        else:
            _run(["sudo", "bash", "-c", f"cat > {UDEV_FILE} << 'EOF'\n{RULE}EOF"])
        _run(["sudo", "udevadm", "control", "--reload-rules"] if not _is_root()
             else ["udevadm", "control", "--reload-rules"])
        return CheckResult(name="udev RTL-SDR rule", ok=True, fixed=True,
                           message="udev rule installed ✓", required=False)
    except Exception as e:
        return CheckResult(name="udev RTL-SDR rule", ok=False,
                           message="udev rule install failed",
                           detail=str(e), required=False)

# -- RTL-SDR USB device -------------------------------------------------------
def check_rtlsdr_device() -> CheckResult:
    """Check if an RTL-SDR dongle is physically plugged in (USB VID 0bda)."""
    rc, out, _ = _run(["lsusb"])
    found = "0bda" in out  # Realtek VID used by all RTL2832-based dongles

    if found:
        # Extract the device line for the message
        line = next((l for l in out.splitlines() if "0bda" in l), "")
        return CheckResult(name="RTL-SDR USB device", ok=True,
                           message=f"RTL-SDR dongle detected ✓  [{line.strip()}]")

    return CheckResult(
        name="RTL-SDR USB device", ok=False,
        message="RTL-SDR dongle not detected",
        detail="Plug in the RTL-SDR USB dongle, then re-run",
        required=False,   # warn but don't block — simulate mode still works
    )

# -- GPS serial port ----------------------------------------------------------
def check_gps_port() -> CheckResult:
    """Check that a serial GPS port exists (/dev/ttyAMA0 or /dev/ttyUSB0)."""
    candidates = ["/dev/ttyAMA0", "/dev/ttyUSB0", "/dev/ttyS0"]
    found = [p for p in candidates if Path(p).exists()]

    if found:
        return CheckResult(name="GPS serial port", ok=True,
                           message=f"Serial port found: {', '.join(found)} ✓")

    return CheckResult(
        name="GPS serial port", ok=False,
        message="No serial GPS port found",
        detail=(
            "For NEO-7M on GPIO pins: enable UART in /boot/config.txt (enable_uart=1)\n"
            "For USB GPS: plug in the module\n"
            "Simulation mode bypasses this requirement"
        ),
        required=False,
    )

# -- Python packages ----------------------------------------------------------
def check_python_package(import_name: str, pip_name: Optional[str] = None,
                          auto_fix: bool = True) -> CheckResult:
    """Check that a Python package is importable; pip-install if not."""
    pkg = pip_name or import_name
    if _module_importable(import_name):
        return CheckResult(name=f"py:{import_name}", ok=True,
                           message=f"{import_name} importable ✓")

    if not auto_fix:
        return CheckResult(
            name=f"py:{import_name}", ok=False,
            message=f"{import_name} not installed",
            detail=f"pip install {pkg}",
        )

    print(f"  {YELLOW('→')} pip install {pkg}…", flush=True)
    ok, err = _pip_install(pkg)
    # Try import again after install
    importable = _module_importable(import_name) if ok else False
    return CheckResult(
        name=f"py:{import_name}",
        ok=importable,
        fixed=importable,
        message=f"{import_name} {'installed ✓' if importable else 'install FAILED ✗'}",
        detail=err if not importable else "",
    )

# -- gpsd ---------------------------------------------------------------------
def check_gpsd(auto_fix: bool = True) -> CheckResult:
    """Check gpsd is installed (helpful but not strictly required)."""
    if _binary_exists("gpsd"):
        return CheckResult(name="gpsd", ok=True, message="gpsd installed ✓",
                           required=False)
    if not auto_fix or not _is_linux():
        return CheckResult(name="gpsd", ok=False, message="gpsd not installed",
                           detail="sudo apt-get install gpsd gpsd-clients",
                           required=False)
    print(f"  {YELLOW('→')} Installing gpsd…", flush=True)
    ok, err = _apt_install("gpsd", "gpsd-clients")
    return CheckResult(name="gpsd", ok=ok, fixed=ok,
                       message=f"gpsd {'installed ✓' if ok else 'FAILED ✗'}",
                       detail=err if not ok else "", required=False)

# ── Master check runner ───────────────────────────────────────────────────────

def run_all_checks(auto_fix: bool = True, gui_mode: bool = False,
                   simulate: bool = False) -> bool:
    """
    Run all checks, print a formatted report, return True if startup is safe.

    Parameters
    ----------
    auto_fix  : attempt to install missing components automatically
    gui_mode  : also check GUI-specific packages (matplotlib, requests)
    simulate  : relax hardware requirements (device + GPS become optional)
    """
    on_pi    = _is_raspberry_pi()
    on_linux = _is_linux()

    print()
    print(BOLD("╔══════════════════════════════════════════════════════╗"))
    print(BOLD("║         RF Scanner — Startup Dependency Check        ║"))
    print(BOLD("╚══════════════════════════════════════════════════════╝"))
    print(f"  Platform : {platform.system()} {platform.machine()}"
          + (" (Raspberry Pi)" if on_pi else ""))
    print(f"  Python   : {sys.version.split()[0]}")
    print(f"  Auto-fix : {'enabled' if auto_fix else 'disabled'}")
    print(f"  Mode     : {'simulation' if simulate else 'hardware'}")
    print()

    groups: list[CheckGroup] = []

    # ── Group 1: Python runtime ───────────────────────────────────────────
    g1 = CheckGroup("Python runtime")
    g1.results.append(check_python_version())
    g1.results.append(check_python_package("serial",   "pyserial",   auto_fix))
    g1.results.append(check_python_package("pynmea2",  "pynmea2",    auto_fix))
    g1.results.append(check_python_package("flask",    "flask",       auto_fix))
    g1.results.append(check_python_package("flask_cors","flask-cors", auto_fix))
    if gui_mode:
        g1.results.append(check_python_package("matplotlib", "matplotlib", auto_fix))
        g1.results.append(check_python_package("requests",   "requests",   auto_fix))
    groups.append(g1)

    # ── Group 2: System tools (Linux only) ────────────────────────────────
    if on_linux:
        g2 = CheckGroup("System tools")
        g2.results.append(check_rtlsdr_tools(auto_fix))
        g2.results.append(check_gpsd(auto_fix))
        groups.append(g2)

        # ── Group 3: Kernel / udev (Linux only) ──────────────────────────
        g3 = CheckGroup("Kernel & device access")
        g3.results.append(check_dvb_blacklist(auto_fix))
        g3.results.append(check_udev_rule(auto_fix))
        groups.append(g3)

        # ── Group 4: Hardware presence ────────────────────────────────────
        g4 = CheckGroup("Hardware" + (" (relaxed — simulation mode)" if simulate else ""))
        r_sdr = check_rtlsdr_device()
        r_gps = check_gps_port()
        if simulate:
            # downgrade to warnings only in simulate mode
            r_sdr.required = False
            r_gps.required = False
        g4.results.append(r_sdr)
        g4.results.append(r_gps)
        groups.append(g4)

    # ── Print results ─────────────────────────────────────────────────────
    all_required_ok = True
    any_fixed       = False

    for group in groups:
        print(f"  {CYAN(BOLD(group.title))}")
        for r in group.results:
            if r.ok or r.fixed:
                icon = GREEN("✓")
            elif not r.required:
                icon = YELLOW("⚠")
            else:
                icon = RED("✗")
                all_required_ok = False

            fixed_tag = f" {YELLOW('[auto-fixed]')}" if r.fixed else ""
            warn_tag  = f" {YELLOW('[optional]')}"   if not r.required and not r.ok else ""
            print(f"    {icon}  {r.message}{fixed_tag}{warn_tag}")
            if r.detail and not r.ok:
                for line in r.detail.splitlines():
                    print(f"       {YELLOW('ℹ')}  {line}")
            if r.fixed:
                any_fixed = True
        print()

    # ── Summary ───────────────────────────────────────────────────────────
    if all_required_ok:
        print(GREEN(BOLD("  ✓  All required checks passed — RF Scanner ready.")))
    else:
        print(RED(BOLD("  ✗  One or more required checks failed.")))
        if not auto_fix:
            print(f"     Re-run with auto-fix enabled (default) to attempt repairs.")
        elif not _is_root() and not _is_linux():
            print(f"     Some fixes require elevated privileges.")
            print(f"     Try: sudo python3 scanner/rf_scanner.py")
        print()
        print("  Manual fix reference:")
        print("    sudo apt-get install rtl-sdr librtlsdr-dev gpsd python3-pip")
        print("    pip3 install pyserial pynmea2 flask flask-cors --break-system-packages")

    if any_fixed:
        print()
        print(YELLOW(BOLD("  ⚠  Some components were auto-installed.")))
        print(YELLOW("     A reboot is recommended if kernel modules were changed."))

    print()
    return all_required_ok


# ── GUI splash variant ────────────────────────────────────────────────────────

def run_checks_with_gui_splash(auto_fix: bool = True, simulate: bool = False) -> bool:
    """
    Run checks and show a tkinter splash/progress window while doing so.
    Falls back to console-only if tkinter is unavailable.
    Returns True if startup is safe.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return run_all_checks(auto_fix=auto_fix, gui_mode=True, simulate=simulate)

    # ── Build splash window ───────────────────────────────────────────────
    splash = tk.Tk()
    splash.title("RF Scanner — Starting up")
    splash.geometry("520x340")
    splash.configure(bg="#0d1117")
    splash.resizable(False, False)

    # Centre on screen
    splash.update_idletasks()
    sw = splash.winfo_screenwidth()
    sh = splash.winfo_screenheight()
    x  = (sw - 520) // 2
    y  = (sh - 340) // 2
    splash.geometry(f"520x340+{x}+{y}")

    BG   = "#0d1117"
    CARD = "#1c2230"
    ACC  = "#00d4aa"
    WARN = "#f0a500"
    ERR  = "#ff4d4f"
    GOOD_C = "#3fb950"
    MUT  = "#8b949e"
    TXT  = "#e6edf3"

    tk.Label(splash, text="⚡ RF Scanner", font=("Segoe UI", 20, "bold"),
             bg=BG, fg=ACC).pack(pady=(24, 2))
    tk.Label(splash, text="Checking system dependencies…", font=("Segoe UI", 10),
             bg=BG, fg=MUT).pack()

    tk.Frame(splash, bg="#30363d", height=1).pack(fill="x", padx=20, pady=12)

    # Scrollable log area
    log_frame = tk.Frame(splash, bg=CARD, bd=0)
    log_frame.pack(fill="both", expand=True, padx=20)
    log_text = tk.Text(log_frame, bg=CARD, fg=TXT, font=("Courier New", 9),
                       relief="flat", state="disabled", height=10, wrap="word",
                       insertbackground=ACC)
    log_text.pack(fill="both", expand=True, padx=6, pady=6)
    log_text.tag_config("ok",   foreground=GOOD_C)
    log_text.tag_config("warn", foreground=WARN)
    log_text.tag_config("err",  foreground=ERR)
    log_text.tag_config("fix",  foreground=ACC)
    log_text.tag_config("hdr",  foreground=ACC, font=("Segoe UI", 9, "bold"))

    # Progress bar
    style = ttk.Style()
    style.theme_use("default")
    style.configure("Scan.Horizontal.TProgressbar",
                    troughcolor=CARD, background=ACC, borderwidth=0)
    pbar = ttk.Progressbar(splash, style="Scan.Horizontal.TProgressbar",
                           mode="determinate", maximum=100)
    pbar.pack(fill="x", padx=20, pady=(8, 4))

    status_lbl = tk.Label(splash, text="", font=("Segoe UI", 9),
                          bg=BG, fg=MUT)
    status_lbl.pack(pady=(2, 8))

    result_holder = [True]  # mutable container for thread result

    def append_log(text: str, tag: str = ""):
        log_text.config(state="normal")
        log_text.insert("end", text + "\n", tag)
        log_text.see("end")
        log_text.config(state="disabled")

    def run_in_thread():
        """Run checks in a background thread, updating the splash."""
        on_linux = sys.platform.startswith("linux")
        simulate_mode = simulate

        STEPS = [
            ("Python runtime",     None),
            ("serial",             None),
            ("pynmea2",            None),
            ("flask",              None),
            ("flask_cors",         None),
            ("matplotlib",         None),
            ("requests",           None),
            ("rtl-sdr tools",      None),
            ("gpsd",               None),
            ("DVB blacklist",      None),
            ("udev rule",          None),
            ("RTL-SDR device",     None),
            ("GPS port",           None),
        ]
        total = len(STEPS)
        all_ok = True

        def step(i, label, result: CheckResult):
            nonlocal all_ok
            pct = int((i + 1) / total * 100)
            splash.after(0, lambda: pbar.configure(value=pct))
            splash.after(0, lambda: status_lbl.config(text=f"Checking: {label}"))

            if result.ok or result.fixed:
                tag = "ok"
                prefix = "  ✓"
            elif not result.required:
                tag = "warn"
                prefix = "  ⚠"
            else:
                tag = "err"
                prefix = "  ✗"
                all_ok = False

            msg = f"{prefix}  {result.message}"
            if result.fixed:
                msg += "  [auto-fixed]"
            splash.after(0, lambda m=msg, t=tag: append_log(m, t))

            if result.detail and not result.ok:
                for dl in result.detail.splitlines():
                    splash.after(0, lambda d=dl: append_log(f"       ℹ  {d}", "warn"))

        i = 0
        splash.after(0, lambda: append_log("── Python runtime ─────────────────", "hdr"))
        step(i, "Python version",  check_python_version());                          i+=1
        step(i, "pyserial",        check_python_package("serial",    "pyserial",    auto_fix)); i+=1
        step(i, "pynmea2",         check_python_package("pynmea2",   "pynmea2",    auto_fix)); i+=1
        step(i, "flask",           check_python_package("flask",     "flask",       auto_fix)); i+=1
        step(i, "flask-cors",      check_python_package("flask_cors","flask-cors",  auto_fix)); i+=1
        step(i, "matplotlib",      check_python_package("matplotlib","matplotlib",  auto_fix)); i+=1
        step(i, "requests",        check_python_package("requests",  "requests",    auto_fix)); i+=1

        if on_linux:
            splash.after(0, lambda: append_log("── System tools ────────────────────", "hdr"))
            step(i, "rtl-sdr",  check_rtlsdr_tools(auto_fix)); i+=1
            step(i, "gpsd",     check_gpsd(auto_fix));          i+=1

            splash.after(0, lambda: append_log("── Kernel & device access ──────────", "hdr"))
            step(i, "DVB blacklist", check_dvb_blacklist(auto_fix)); i+=1
            step(i, "udev rule",     check_udev_rule(auto_fix));     i+=1

            splash.after(0, lambda: append_log("── Hardware ────────────────────────", "hdr"))
            r_sdr = check_rtlsdr_device()
            r_gps = check_gps_port()
            if simulate_mode:
                r_sdr.required = False
                r_gps.required = False
            step(i, "RTL-SDR device", r_sdr); i+=1
            step(i, "GPS port",       r_gps); i+=1
        else:
            i = total  # non-Linux: skip hardware checks

        result_holder[0] = all_ok

        if all_ok:
            splash.after(0, lambda: status_lbl.config(
                text="All checks passed — launching…", fg=GOOD_C))
            splash.after(800, splash.destroy)
        else:
            splash.after(0, lambda: status_lbl.config(
                text="Some checks failed. See details above.", fg=ERR))
            # Show a continue/abort choice
            def make_buttons():
                btn_frame = tk.Frame(splash, bg=BG)
                btn_frame.pack(pady=4)
                def do_continue():
                    result_holder[0] = True  # user chose to proceed anyway
                    splash.destroy()
                def do_abort():
                    result_holder[0] = False
                    splash.destroy()
                tk.Button(btn_frame, text="Continue Anyway", font=("Segoe UI", 9),
                          bg=WARN, fg=BG, relief="flat", padx=12, pady=4,
                          cursor="hand2", command=do_continue).pack(side="left", padx=6)
                tk.Button(btn_frame, text="Exit", font=("Segoe UI", 9),
                          bg=ERR, fg=TXT, relief="flat", padx=12, pady=4,
                          cursor="hand2", command=do_abort).pack(side="left", padx=6)
            splash.after(0, make_buttons)

    import threading
    threading.Thread(target=run_in_thread, daemon=True).start()
    splash.mainloop()

    return result_holder[0]


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="RF Scanner dependency checker")
    p.add_argument("--no-fix",   action="store_true", help="Check only, do not auto-install")
    p.add_argument("--gui",      action="store_true", help="Also check GUI dependencies")
    p.add_argument("--simulate", action="store_true", help="Relax hardware requirements")
    args = p.parse_args()

    ok = run_all_checks(
        auto_fix = not args.no_fix,
        gui_mode = args.gui,
        simulate = args.simulate,
    )
    sys.exit(0 if ok else 1)
