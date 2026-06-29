/**
 * rf_scanner/static/js/utils.js
 * Shared utilities for the RF Scanner web UI.
 * Loaded before app-specific JS in index.html.
 */

'use strict';

// ── Frequency formatting ───────────────────────────────────────────────────
function fmtFreq(hz) {
  if (hz >= 1e9) return (hz / 1e9).toFixed(4) + ' GHz';
  if (hz >= 1e6) return (hz / 1e6).toFixed(3) + ' MHz';
  return (hz / 1e3).toFixed(1) + ' kHz';
}

// ── dBm colour ramp (blue→cyan→green→yellow→orange→red) ──────────────────
function dbmColor(norm) {
  // norm = 0..1 where 0 = weakest, 1 = strongest
  const stops = [
    [0.00, '#0d47a1'],
    [0.20, '#0288d1'],
    [0.40, '#00bcd4'],
    [0.60, '#69ff47'],
    [0.80, '#ffff00'],
    [1.00, '#ff1744'],
  ];
  for (let i = 1; i < stops.length; i++) {
    if (norm <= stops[i][0]) {
      const t = (norm - stops[i-1][0]) / (stops[i][0] - stops[i-1][0]);
      return lerpColor(stops[i-1][1], stops[i][1], t);
    }
  }
  return stops[stops.length-1][1];
}

function lerpColor(a, b, t) {
  const ra = parseInt(a.slice(1,3),16), ga = parseInt(a.slice(3,5),16), ba = parseInt(a.slice(5,7),16);
  const rb = parseInt(b.slice(1,3),16), gb = parseInt(b.slice(3,5),16), bb = parseInt(b.slice(5,7),16);
  const r = Math.round(ra + (rb-ra)*t).toString(16).padStart(2,'0');
  const g = Math.round(ga + (gb-ga)*t).toString(16).padStart(2,'0');
  const bh = Math.round(ba + (bb-ba)*t).toString(16).padStart(2,'0');
  return `#${r}${g}${bh}`;
}

// ── Debounce ──────────────────────────────────────────────────────────────
function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ── Date helpers ──────────────────────────────────────────────────────────
function todayISO() {
  return new Date().toISOString().slice(0, 10);
}
function daysAgoISO(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}
function fmtUTC(isoStr) {
  if (!isoStr) return '—';
  return isoStr.slice(0, 19).replace('T', ' ') + ' UTC';
}

// ── Notification / toast helper ───────────────────────────────────────────
const TOAST_CONTAINER_ID = 'alerts-bar';

function showToast(message, type = 'danger', durationMs = 6000) {
  const bar = document.getElementById(TOAST_CONTAINER_ID);
  if (!bar) return;
  const el = document.createElement('div');
  el.className = 'toast';
  if (type === 'warning') {
    el.style.borderColor = 'var(--accent2)';
    el.style.color = 'var(--accent2)';
    el.style.background = 'rgba(240,165,0,.1)';
  } else if (type === 'good') {
    el.style.borderColor = 'var(--good)';
    el.style.color = 'var(--good)';
    el.style.background = 'rgba(63,185,80,.1)';
  }
  el.textContent = message;
  el.onclick = () => el.remove();
  bar.appendChild(el);
  if (durationMs > 0) setTimeout(() => el.remove(), durationMs);
  return el;
}

// ── KML/KMZ colour conversion ─────────────────────────────────────────────
function kmlColorToHex(kmlColor) {
  if (!kmlColor) return null;
  const s = kmlColor.replace(/\s/g, '');
  if (s.length === 8) {
    // AABBGGRR → #RRGGBB
    return `#${s.slice(6,8)}${s.slice(4,6)}${s.slice(2,4)}`;
  }
  if (s.length === 6) return `#${s}`;
  return null;
}

// ── KML coordinate parser ─────────────────────────────────────────────────
function parseKmlCoords(raw) {
  return raw.trim().split(/\s+/).map(c => {
    const parts = c.split(',').map(Number);
    return parts; // [lon, lat, alt?]
  }).filter(p => p.length >= 2 && !isNaN(p[0]) && !isNaN(p[1]));
}

// ── Get text content of first child element by tag ────────────────────────
function kmlText(el, tag) {
  const t = el.querySelector(tag);
  return t ? t.textContent.trim() : '';
}

// ── Extract colour from KML Style/StyleMap ────────────────────────────────
function getKmlColor(pm, doc) {
  try {
    let styleEl = pm.querySelector('Style');
    const styleUrl = kmlText(pm, 'styleUrl').replace('#', '');
    if (!styleEl && styleUrl) {
      styleEl = doc.querySelector(`Style[id="${styleUrl}"], StyleMap[id="${styleUrl}"]`);
      if (styleEl && styleEl.tagName === 'StyleMap') {
        const normal = [...styleEl.querySelectorAll('Pair')].find(p => kmlText(p,'key') === 'normal');
        if (normal) {
          const ref = kmlText(normal,'styleUrl').replace('#','');
          styleEl = doc.querySelector(`Style[id="${ref}"]`);
        }
      }
    }
    if (!styleEl) return null;
    const colorEl = styleEl.querySelector('IconStyle > color')
                 || styleEl.querySelector('LineStyle > color')
                 || styleEl.querySelector('PolyStyle > color');
    return colorEl ? kmlColorToHex(colorEl.textContent.trim()) : null;
  } catch (e) { return null; }
}

// ── SVG pin icon HTML for Leaflet divIcon ────────────────────────────────
function makePinHtml(color) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="26" height="34" viewBox="0 0 26 34">
    <path d="M13 0C5.8 0 0 5.8 0 13c0 8.8 13 21 13 21s13-12.2 13-21C26 5.8 20.2 0 13 0z"
          fill="${color}" stroke="#fff" stroke-width="1.4"/>
    <circle cx="13" cy="13" r="5" fill="white"/>
  </svg>`;
}

function makePinIcon(color) {
  return L.divIcon({
    html: makePinHtml(color),
    className: '',
    iconSize: [26, 34],
    iconAnchor: [13, 34],
    popupAnchor: [0, -36],
  });
}

// ── Local storage helpers for overlay persistence ─────────────────────────
const OVERLAY_STORAGE_KEY = 'rf_scanner_overlays_v1';

function saveOverlaysToStorage(layers) {
  try {
    // Serialise only the data, not Leaflet objects
    const serialisable = layers.map(l => ({
      id:    l.id,
      name:  l.name,
      color: l.color,
      type:  l.type,
      source: l.source,
      visible: l.visible,
      items: l.items || [],   // {type, latlng/latlngs, name, desc, color}
    }));
    localStorage.setItem(OVERLAY_STORAGE_KEY, JSON.stringify(serialisable));
  } catch (e) {
    console.warn('Could not save overlays to localStorage:', e);
  }
}

function loadOverlaysFromStorage() {
  try {
    const raw = localStorage.getItem(OVERLAY_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (e) {
    return [];
  }
}

function clearOverlayStorage() {
  localStorage.removeItem(OVERLAY_STORAGE_KEY);
}
