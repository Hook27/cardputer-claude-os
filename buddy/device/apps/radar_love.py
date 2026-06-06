"""radar_love — WiFi + BLE radio scanner for the Cardputer-Adv.

A "what's on the air around me" tool. It scans 2.4 GHz WiFi access
points and BLE advertisers, sorts each list by signal strength, and
renders a little 4-bar signal meter per row, so you can sweep a room
and see what lights up.

### Pages

Three pages, cycled with the left / right arrow keys (``,`` and ``/``
on the Cardputer-Adv cluster, ``a`` / ``d`` also accepted):

  1. WiFi — SSID, channel, lock (secured), RSSI
  2. Chan — bar chart of WiFi channel occupancy (1-13), reusing the
            WiFi scan; busy channels read red, quiet ones green
  3. BLE  — device name (or MAC if unnamed), connectable flag, RSSI

On the list pages, up / down (``;`` / ``.``, or ``w`` / ``s``) scroll
and ``R`` re-runs the scan; a thin orange scrollbar shows position when
the list overflows the 6 visible rows, and the selected row gets an
orange accent. **Enter** opens a full-screen detail view for the
selected WiFi network (BSSID, channel/MHz, decoded auth, hidden flag)
or BLE device (full MAC, connectability, all decoded AD fields — TX
power, service UUIDs, manufacturer data); any key returns to the list
at the same position. The channel map has no scroll or detail; ``R``
there rescans WiFi (the two WiFi pages share one scan).

### Scan model

Scans are *manual + on-entry*: a page scans once when you first open
it and again whenever you press ``R``. Nothing auto-refreshes, which
keeps radio contention low — important because the ESP32-S3 shares one
2.4 GHz radio between WiFi and BLE through a software coexistence
arbiter, and this bundle has a documented history of NimBLE faults
when the two contend (see ``buddy_ble.py`` / ``ble_on_micropython.md``).
We never run both scans at once, and we never call ``BLE.active(True)``
ourselves — the launcher's ``_init_ble`` already brought NimBLE up at
boot, so we just reuse the active singleton for ``gap_scan``.

- **WiFi.** ``network.WLAN(STA_IF).scan()`` returns tuples of
  ``(ssid, bssid, channel, rssi, authmode, hidden)``. The scan briefly
  interrupts the launcher's WiFi connection; it reconnects after.
- **BLE.** ``bluetooth.BLE().gap_scan(...)`` with an IRQ handler for
  ``_IRQ_SCAN_RESULT`` (5) and ``_IRQ_SCAN_DONE`` (6). The IRQ body
  stays tiny per buddy_ble's rule: copy the address + adv payload,
  dedup by MAC, decode the Complete/Shortened Local Name AD field.
  All sorting/drawing happens back in the main loop.

If a radio is unavailable on a given build (no ``network`` /
``bluetooth``, or ``gap_scan`` missing) the page shows an "unavailable"
notice rather than crashing.

### Exit

Q or ESC returns cleanly to the launcher: any in-flight ``gap_scan`` is
stopped and the BLE irq cleared, ``run()`` returns, and the module-
level ``finally`` clears the screen and drops us from ``sys.modules``
so a re-selection re-runs us. No ``machine.reset()`` — same pattern as
pi_dashboard / snake / particle_life.

### Font / layout

DejaVu9, size 1 throughout. Same three-zone chrome as the rest of the
bundle: 20 px DARK header with an ORANGE hairline at y=20, a 6-row list
band, and an 18 px hint strip at the bottom. Widths measured with
``_LCD.textWidth(...)`` for centering and truncation.
"""

import sys
import time

import M5
from hardware import MatrixKeyboard


# Palette inlined from ui_theme — same colors the rest of the bundle
# uses, so the apps feel visually coherent.
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_RED = 0xCC4444
_GREEN = 0x4CAF50

_LCD = M5.Lcd

_W = 240
_H = 135

# BLE IRQ event ids. Version-dependent across MicroPython builds, but
# these are the standard 1.22+ values the rest of the bundle relies on
# (buddy_ble.py uses the same numbering family). Guarded defensively in
# the scan path so a mismatch degrades to "no devices" rather than a
# crash.
_IRQ_SCAN_RESULT = 5
_IRQ_SCAN_DONE = 6

# AD structure types. NAME ones are decoded for the list; the rest are
# decoded on demand in the BLE detail view (_fmt_ad).
_AD_FLAGS = 0x01
_AD_UUID16_INC = 0x02
_AD_UUID16_ALL = 0x03
_AD_UUID32_INC = 0x04
_AD_UUID32_ALL = 0x05
_AD_UUID128_INC = 0x06
_AD_UUID128_ALL = 0x07
_AD_SHORT_NAME = 0x08
_AD_COMPLETE_NAME = 0x09
_AD_TX_POWER = 0x0A
_AD_MANUFACTURER = 0xFF

# Page identity. Order: WiFi list -> Channel map (WiFi-derived) -> BLE.
_PAGE_LABELS = ("WiFi", "Chan", "BLE")
_N_PAGES = len(_PAGE_LABELS)
_PAGE_WIFI = 0
_PAGE_CHAN = 1
_PAGE_BLE = 2

# Authmode names, indexed by the integer network.WLAN.scan() returns.
_AUTH_NAMES = ("Open", "WEP", "WPA", "WPA2", "WPA/WPA2", "WPA2-Ent",
               "WPA3", "WPA2/WPA3", "WAPI")

# Channel-map geometry (page 2). 13 slots * 17 px = 221 px, centered.
_N_CH = 13
_SLOT_W = 17
_BAR_W = 14
_CH_BASE_Y = 102        # bar baseline; bars grow upward from here
_CH_LABEL_Y = 104       # channel-number row (just below the baseline)
_CH_MIN_TOP = 22        # never draw a bar/label above this (header ends y20)

# List geometry. Content band sits between the header hairline (y=20)
# and the hint strip (starts at _H-18). 6 rows * 15 px = 90 px fits.
_LIST_Y0 = 24
_ROW_H = 15
_MAX_VISIBLE = 6
_TEXT_X = 24            # left x of the primary (SSID / name) text
_RINFO_RIGHT = _W - 6   # right edge of the RSSI/info text
_SCROLLBAR_X = _W - 3

_TICK_MS = 40           # keyboard poll interval
_BLE_SCAN_MS = 4000     # BLE gap_scan window per scan


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        # Fall back silently rather than crashing on a build without FONTS.
        print("radar: setFont fallback:", e)


# ---- chrome ---------------------------------------------------------


def _count(results):
    """Row count for a results list (None = radio unavailable)."""
    return 0 if results is None else len(results)


def _draw_chrome(page, count):
    """Full repaint of header + hairline + hint strip for ``page``."""
    _LCD.fillScreen(_BLACK)

    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Radar Love", 6, 5)

    if page == _PAGE_CHAN:
        # The channel map shows RSSI as a sign-less magnitude (a full
        # "-62" won't fit a 14 px bar), so the header flags that the
        # numbers above the bars are |dBm|.
        title = "Chan |dBm| [{}/{}]".format(page + 1, _N_PAGES)
    else:
        title = "{}:{} [{}/{}]".format(
            _PAGE_LABELS[page], count, page + 1, _N_PAGES)
    _LCD.setTextColor(_CREAM, _DARK)
    _LCD.drawString(title, _W - _LCD.textWidth(title) - 6, 5)

    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    if page == _PAGE_CHAN:
        hint = "</> pg   R rescan   Q menu"
    else:
        hint = "</> pg  ;/. scrl  Ent info  R scan  Q"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _center(text, color, y):
    """Centered single-line message in the list band."""
    _LCD.setTextSize(1)
    _LCD.setTextColor(color, _BLACK)
    _LCD.drawString(text, (_W - _LCD.textWidth(text)) // 2, y)


def _draw_banner(page):
    """Clear the list band and show a 'Scanning...' notice while a scan
    runs (WiFi scan() blocks; BLE accumulates over a few seconds)."""
    _LCD.fillRect(0, 21, _W, (_H - 18) - 21, _BLACK)
    _center("Scanning {}...".format(_PAGE_LABELS[page]), _ORANGE, 60)


def _truncate(text, max_px):
    """Cut ``text`` so it fits in ``max_px`` pixels, appending '..' when
    shortened. Uses measured widths so it's correct regardless of font."""
    if _LCD.textWidth(text) <= max_px:
        return text
    ell = ".."
    while text and _LCD.textWidth(text + ell) > max_px:
        text = text[:-1]
    return text + ell if text else ell


# ---- signal meter ---------------------------------------------------


def _rssi_color(rssi):
    if rssi >= -60:
        return _GREEN
    if rssi >= -75:
        return _ORANGE
    return _GRAY_MID


def _draw_meter(x, y_center, rssi):
    """4-bar signal meter, ~16 px wide. Lit bars scale with RSSI and
    are tinted by strength; unlit bars are dim."""
    color = _rssi_color(rssi)
    level = 0
    if rssi >= -85:
        level = 1
    if rssi >= -75:
        level = 2
    if rssi >= -65:
        level = 3
    if rssi >= -55:
        level = 4
    base_y = y_center + 5
    for i in range(4):
        h = 3 + i * 2  # 3, 5, 7, 9
        bx = x + i * 4
        _LCD.fillRect(bx, base_y - h, 3, h, color if i < level else _DARK)


# ---- list rendering -------------------------------------------------


def _draw_row_common(screen_i, rssi, selected):
    """Paint the shared row furniture (accent + meter) and return the
    top y of this row so the caller can place text."""
    y = _LIST_Y0 + screen_i * _ROW_H
    if selected:
        _LCD.fillRect(0, y, 2, _ROW_H, _ORANGE)
    _draw_meter(3, y + _ROW_H // 2, rssi)
    return y


def _draw_wifi_row(screen_i, row, selected):
    y = _draw_row_common(screen_i, row["rssi"], selected)

    rinfo = "{} c{}{}".format(
        row["rssi"], row["ch"], " *" if row["sec"] else "")
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    rw = _LCD.textWidth(rinfo)
    _LCD.drawString(rinfo, _RINFO_RIGHT - rw, y + 3)

    ssid = row["ssid"] or "<hidden>"
    _LCD.setTextColor(_ORANGE if selected else _CREAM, _BLACK)
    max_px = (_RINFO_RIGHT - rw - 6) - _TEXT_X
    _LCD.drawString(_truncate(ssid, max_px), _TEXT_X, y + 3)


def _draw_ble_row(screen_i, row, selected):
    y = _draw_row_common(screen_i, row["rssi"], selected)

    rinfo = "{} {}".format(row["rssi"], "c" if row["conn"] else "-")
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    rw = _LCD.textWidth(rinfo)
    _LCD.drawString(rinfo, _RINFO_RIGHT - rw, y + 3)

    named = bool(row["name"])
    primary = row["name"] if named else row["mac"]
    if selected:
        color = _ORANGE
    else:
        color = _CREAM if named else _GRAY_MID
    _LCD.setTextColor(color, _BLACK)
    max_px = (_RINFO_RIGHT - rw - 6) - _TEXT_X
    _LCD.drawString(_truncate(primary, max_px), _TEXT_X, y + 3)


def _draw_scrollbar(scroll, total):
    """Thin orange scrollbar thumb on the right edge when the list
    overflows the viewport."""
    if total <= _MAX_VISIBLE:
        return
    track_h = _MAX_VISIBLE * _ROW_H
    _LCD.fillRect(_SCROLLBAR_X, _LIST_Y0, 2, track_h, _DARK)
    thumb_h = max(6, track_h * _MAX_VISIBLE // total)
    span = total - _MAX_VISIBLE
    thumb_y = _LIST_Y0 + (track_h - thumb_h) * scroll // span
    _LCD.fillRect(_SCROLLBAR_X, thumb_y, 2, thumb_h, _ORANGE)


def _draw_list(page, results, cursor, scroll):
    """Repaint the whole list band for the current page."""
    _LCD.fillRect(0, 21, _W, (_H - 18) - 21, _BLACK)

    if results is None:
        _center(_PAGE_LABELS[page] + " unavailable", _RED, 60)
        return
    if not results:
        what = "networks" if page == _PAGE_WIFI else "devices"
        _center("No " + what + " found", _GRAY_MID, 60)
        return

    visible = results[scroll:scroll + _MAX_VISIBLE]
    for i, row in enumerate(visible):
        selected = (scroll + i) == cursor
        if page == _PAGE_WIFI:
            _draw_wifi_row(i, row, selected)
        else:
            _draw_ble_row(i, row, selected)

    _draw_scrollbar(scroll, len(results))


def _render(page, results, cursor, scroll):
    _draw_chrome(page, _count(results))
    _draw_list(page, results, cursor, scroll)


def _render_page(page, results, cursor, scroll):
    """Full repaint for ``page``, dispatching on page type. The channel
    map has no results slot of its own — it reads the WiFi scan."""
    if page == _PAGE_CHAN:
        _draw_chrome(page, _count(results[_PAGE_WIFI]))
        _draw_chanmap(results[_PAGE_WIFI])
    else:
        _render(page, results[page], cursor[page], scroll[page])


# ---- channel map (page 2) -------------------------------------------


def _bar_height(rssi):
    """Map an RSSI to a bar height: -30 dBm -> 75 px, -90 dBm -> 4 px,
    clamped and linear between."""
    r = rssi
    if r > -30:
        r = -30
    elif r < -90:
        r = -90
    return 4 + (r + 90) * 71 // 60


def _chan_color(rssi):
    """Color a channel bar by congestion: busy channels read red, quiet
    ones green (inverse of the signal meter — here 'strong' means
    'crowded', which is what you want to avoid)."""
    if rssi > -60:
        return _RED
    if rssi >= -75:
        return _ORANGE
    return _GREEN


def _outline(x, y, w, h, color):
    """Draw a 1 px rectangle outline using fillRect (no drawRect
    dependency, matching the rest of the bundle's fillRect-only style)."""
    _LCD.fillRect(x, y, w, 1, color)
    _LCD.fillRect(x, y + h - 1, w, 1, color)
    _LCD.fillRect(x, y, 1, h, color)
    _LCD.fillRect(x + w - 1, y, 1, h, color)


def _draw_chanmap(wifi):
    """Bar chart of WiFi channel occupancy (channels 1-13), reusing the
    WiFi scan already in memory. Bar height = strongest RSSI on that
    channel; empty channels show a dim outline stub."""
    _LCD.fillRect(0, 21, _W, (_H - 18) - 21, _BLACK)

    if wifi is None:
        _center("WiFi unavailable", _RED, 60)
        return

    # Strongest RSSI seen per channel.
    best = {}
    for row in wifi:
        ch = row["ch"]
        if 1 <= ch <= _N_CH and (ch not in best or row["rssi"] > best[ch]):
            best[ch] = row["rssi"]

    x0 = (_W - _N_CH * _SLOT_W) // 2
    _LCD.setTextSize(1)
    for idx in range(_N_CH):
        ch = idx + 1
        bx = x0 + idx * _SLOT_W
        cx = bx + _BAR_W // 2
        rssi = best.get(ch)

        if rssi is None:
            # Empty channel: dim outline stub, gray number.
            _outline(bx, _CH_BASE_Y - 6, _BAR_W, 6, _GRAY_MID)
            num_color = _GRAY_MID
        else:
            h = _bar_height(rssi)
            top = _CH_BASE_Y - h
            color = _chan_color(rssi)
            _LCD.fillRect(bx, top, _BAR_W, h, color)
            num_color = _CREAM
            # Magnitude label above the bar, but only if it clears the
            # header band (very strong bars are too tall to label).
            if top - 11 >= _CH_MIN_TOP:
                mag = str(-rssi)
                _LCD.setTextColor(color, _BLACK)
                _LCD.drawString(mag, cx - _LCD.textWidth(mag) // 2, top - 11)

        label = str(ch)
        _LCD.setTextColor(num_color, _BLACK)
        _LCD.drawString(label, cx - _LCD.textWidth(label) // 2, _CH_LABEL_Y)


# ---- WiFi scan ------------------------------------------------------


def _scan_wifi():
    """Return a list of network dicts sorted by RSSI desc, or None if
    WiFi scanning isn't available on this build."""
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        if not sta.active():
            sta.active(True)
        nets = sta.scan()
    except Exception as e:
        print("radar: wifi scan err:", e)
        return None

    rows = []
    for t in nets:
        try:
            ssid = bytes(t[0]).decode()
        except Exception:
            ssid = "?"
        try:
            bssid = bytes(t[1])
        except Exception:
            bssid = b""
        rows.append({
            "ssid": ssid,
            "bssid": bssid,     # raw 6 bytes, formatted in the detail view
            "ch": t[2],
            "rssi": t[3],
            "auth": t[4],       # raw authmode, decoded in the detail view
            "sec": t[4] != 0,   # authmode 0 == open
            "hidden": t[5],
        })
    rows.sort(key=lambda r: r["rssi"], reverse=True)
    return rows


# ---- BLE scan -------------------------------------------------------


def _mac_str(addr):
    return ":".join("{:02X}".format(b) for b in addr)


def _decode_name(payload):
    """Pull a device name out of a raw advertising payload, or None.

    Payload is a sequence of [len][type][data...] AD structures; we look
    for the Complete (0x09) or Shortened (0x08) Local Name.
    """
    i = 0
    n = len(payload)
    while i + 1 < n:
        ln = payload[i]
        if ln == 0:
            break
        t = payload[i + 1]
        if t == _AD_COMPLETE_NAME or t == _AD_SHORT_NAME:
            try:
                return bytes(payload[i + 2:i + 1 + ln]).decode()
            except Exception:
                return None
        i += 1 + ln
    return None


def _parse_ad(payload):
    """Split a raw advertising payload into (type, data_bytes) AD
    structures for the BLE detail view."""
    out = []
    i = 0
    n = len(payload)
    while i + 1 < n:
        ln = payload[i]
        if ln == 0:
            break
        t = payload[i + 1]
        out.append((t, bytes(payload[i + 2:i + 1 + ln])))
        i += 1 + ln
    return out


def _get_ble():
    """Return the active BLE singleton, or None if BLE is unavailable.

    The launcher's _init_ble already brought NimBLE up at boot, so the
    common path is a no-op fast return. We only call active(True) if the
    controller somehow isn't up — and even then guardedly, because
    cold-starting BLE on a busy radio is the documented fault path.
    """
    try:
        import bluetooth
        ble = bluetooth.BLE()
        if not ble.active():
            ble.active(True)
        return ble
    except Exception as e:
        print("radar: ble unavailable:", e)
        return None


def _scan_ble(kb):
    """Run a BLE observer scan for ~_BLE_SCAN_MS and return a list of
    device dicts sorted by RSSI desc, or None if BLE is unavailable.

    Results accumulate in an IRQ handler (kept tiny per buddy_ble's
    rule); we poll the keyboard meanwhile so ESC can abort early.
    """
    ble = _get_ble()
    if ble is None:
        return None

    devices = {}     # mac -> [rssi, name, connectable, adv_payload]
    done = [False]

    def _irq(event, data):
        if event == _IRQ_SCAN_RESULT:
            addr_type, addr, adv_type, rssi, adv_data = data
            mac = _mac_str(bytes(addr))
            adv = bytes(adv_data)
            name = _decode_name(adv)
            d = devices.get(mac)
            if d is None:
                # adv_type 0/1/2 are connectable/scannable forms.
                devices[mac] = [rssi, name, adv_type in (0, 1, 2), adv]
            else:
                d[0] = rssi
                if name and not d[1]:
                    d[1] = name
                # Keep the richest payload seen (scan responses often
                # carry more AD fields than the initial advert).
                if len(adv) > len(d[3]):
                    d[3] = adv
        elif event == _IRQ_SCAN_DONE:
            done[0] = True

    try:
        ble.irq(_irq)
        # active=True so we get scan responses (and thus more names).
        ble.gap_scan(_BLE_SCAN_MS, 30000, 30000, True)
    except Exception as e:
        print("radar: gap_scan err:", e)
        try:
            ble.irq(None)
        except Exception:
            pass
        return None

    deadline = time.ticks_add(time.ticks_ms(), _BLE_SCAN_MS + 1500)
    while not done[0]:
        kb.tick()
        if _intent(kb.get_key()) == "exit":
            break
        if time.ticks_diff(time.ticks_ms(), deadline) >= 0:
            break
        time.sleep_ms(_TICK_MS)

    try:
        ble.gap_scan(None)   # stop in case we broke out early
    except Exception:
        pass
    try:
        ble.irq(None)
    except Exception:
        pass

    rows = [
        {"name": d[1], "mac": mac, "rssi": d[0], "conn": d[2], "adv": d[3]}
        for mac, d in devices.items()
    ]
    rows.sort(key=lambda r: r["rssi"], reverse=True)
    return rows


# ---- input ----------------------------------------------------------


def _intent(k):
    """Normalize a MatrixKeyboard return to an intent string or None.

    Arrow cluster reports unshifted ASCII (``,`` left, ``/`` right,
    ``;`` up, ``.`` down); WASD accepted too for muscle memory. Q/ESC
    exits, R rescans.
    """
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x1B:  # ESC
            return "exit"
        if k in (0x0A, 0x0D):  # Enter (0x0A on this firmware build)
            return "select"
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch == "q":
        return "exit"
    if ch in ("\r", "\n"):
        return "select"
    if ch in (",", "a"):
        return "left"
    if ch in ("/", "d"):
        return "right"
    if ch in (";", "w"):
        return "up"
    if ch in (".", "s"):
        return "down"
    if ch == "r":
        return "rescan"
    return None


# ---- detail views ---------------------------------------------------


def _rssi_label(rssi):
    """Plain-language signal quality for a detail screen."""
    if rssi >= -50:
        return "Excellent"
    if rssi >= -60:
        return "Good"
    if rssi >= -70:
        return "Fair"
    return "Weak"


def _auth_str(auth):
    """Human-readable WiFi authmode."""
    if 0 <= auth < len(_AUTH_NAMES):
        return _AUTH_NAMES[auth]
    return "Auth {}".format(auth)


def _wrap_px(text, max_px, max_lines):
    """Width-measured character wrap (SSIDs rarely have spaces, so word
    wrap is useless here). Truncates the last line with '..' if the text
    overflows ``max_lines``."""
    lines = []
    cur = ""
    i = 0
    n = len(text)
    while i < n and len(lines) < max_lines:
        ch = text[i]
        if _LCD.textWidth(cur + ch) <= max_px or cur == "":
            cur += ch
            i += 1
        else:
            lines.append(cur)
            cur = ""
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if i < n and lines:
        # More text than fits — mark the last line as truncated.
        lines[-1] = _truncate(lines[-1] + text[i:], max_px)
    return lines or [""]


def _hex(data, max_bytes):
    """Uppercase hex of ``data``, '..' suffix if truncated."""
    s = "".join("{:02X}".format(b) for b in data[:max_bytes])
    return s + ".." if len(data) > max_bytes else s


def _detail_chrome(title):
    """Header + 'any key: back' hint for a full-screen detail view."""
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString(title, 6, 5)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "any key: back"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _kv(y, label, value, vcolor=_CREAM):
    """Draw a gray label + value on one line; return the next y."""
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString(label, 6, y)
    vx = 6 + _LCD.textWidth(label) + 4
    _LCD.setTextColor(vcolor, _BLACK)
    _LCD.drawString(_truncate(value, _RINFO_RIGHT - vx), vx, y)
    return y + 13


def _wait_key(kb):
    """Block until any key, after debouncing the press that opened the
    detail view so it isn't read as the dismiss."""
    time.sleep_ms(300)
    kb.tick()
    kb.get_key()   # drain whatever's latched from the trigger press
    while True:
        kb.tick()
        if kb.get_key() is not None:
            break
        time.sleep_ms(_TICK_MS)
    # Debounce the dismiss press too, so it doesn't bleed back into the
    # caller's main loop as a page-switch / re-open.
    time.sleep_ms(200)
    kb.tick()
    kb.get_key()


def _wifi_detail(row, kb):
    """Full-screen detail for one WiFi network. Returns on any key; does
    not touch list cursor/scroll, so the caller repaints at the same
    position."""
    _detail_chrome("WiFi detail")
    _LCD.setTextSize(1)
    y = 24

    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString("SSID:", 6, y)
    sx = 6 + _LCD.textWidth("SSID:") + 4
    ssid = row["ssid"] or "<hidden>"
    _LCD.setTextColor(_CREAM, _BLACK)
    for line in _wrap_px(ssid, _RINFO_RIGHT - sx, 2):
        _LCD.drawString(line, sx, y)
        y += 13

    y = _kv(y, "BSSID:", _mac_str(row["bssid"]) if row["bssid"] else "?")
    freq = 2412 + (row["ch"] - 1) * 5
    y = _kv(y, "Channel:", "{} ({} MHz)".format(row["ch"], freq))
    rssi = row["rssi"]
    y = _kv(y, "RSSI:", "{} dBm ({})".format(rssi, _rssi_label(rssi)),
            _rssi_color(rssi))
    y = _kv(y, "Auth:", _auth_str(row["auth"]))
    y = _kv(y, "Hidden:", "yes" if row["hidden"] else "no")

    _wait_key(kb)


def _fmt_ad(t, data):
    """Render one AD structure for the BLE detail view, or None to skip
    (the device name is shown separately at the top)."""
    if t == _AD_COMPLETE_NAME or t == _AD_SHORT_NAME:
        return None
    if t == _AD_FLAGS:
        return "Flags: 0x{:02X}".format(data[0]) if data else "Flags: -"
    if t == _AD_TX_POWER:
        if not data:
            return "TX pwr: -"
        v = data[0] - 256 if data[0] > 127 else data[0]  # signed byte
        return "TX pwr: {} dBm".format(v)
    if t == _AD_UUID16_INC or t == _AD_UUID16_ALL:
        ids = ["{:04X}".format(data[k] | (data[k + 1] << 8))
               for k in range(0, len(data) - 1, 2)]
        return "UUID16: " + ",".join(ids) if ids else "UUID16: -"
    if t == _AD_UUID32_INC or t == _AD_UUID32_ALL:
        return "UUID32 x{}".format(len(data) // 4)
    if t == _AD_UUID128_INC or t == _AD_UUID128_ALL:
        return "UUID128 x{}".format(len(data) // 16)
    if t == _AD_MANUFACTURER:
        if len(data) >= 2:
            cid = data[0] | (data[1] << 8)
            return "Mfr {:04X}: {}".format(cid, _hex(data[2:], 6))
        return "Mfr: " + _hex(data, 6)
    return "0x{:02X}: {}".format(t, _hex(data, 6))


def _ble_detail(row, kb):
    """Full-screen detail for one BLE device, including all decodable AD
    fields. Returns on any key."""
    _detail_chrome("BLE detail")
    _LCD.setTextSize(1)
    y = 24

    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString("Name:", 6, y)
    nx = 6 + _LCD.textWidth("Name:") + 4
    named = bool(row["name"])
    _LCD.setTextColor(_CREAM if named else _GRAY_MID, _BLACK)
    _LCD.drawString(_truncate(row["name"] if named else "unnamed",
                              _RINFO_RIGHT - nx), nx, y)
    y += 13

    y = _kv(y, "MAC:", row["mac"])
    y = _kv(y, "Conn:", "yes" if row["conn"] else "no")
    rssi = row["rssi"]
    y = _kv(y, "RSSI:", "{} dBm ({})".format(rssi, _rssi_label(rssi)),
            _rssi_color(rssi))

    # Decoded AD fields, as many as fit above the hint strip; the last
    # usable line becomes a "+N more" marker if they overflow.
    fields = []
    for t, data in _parse_ad(row["adv"] or b""):
        s = _fmt_ad(t, data)
        if s is not None:
            fields.append(s)

    max_y = _H - 18 - 13            # top of the last usable text line
    avail = (max_y - y) // 13 + 1   # lines left from y down to max_y
    if avail < 1:
        avail = 0
    _LCD.setTextColor(_CREAM, _BLACK)
    if len(fields) <= avail:
        shown = fields
    else:
        shown = fields[:max(0, avail - 1)]
    for s in shown:
        _LCD.drawString(_truncate(s, _RINFO_RIGHT - 6), 6, y)
        y += 13
    if len(fields) > len(shown):
        _LCD.setTextColor(_GRAY_MID, _BLACK)
        _LCD.drawString("+{} more".format(len(fields) - len(shown)), 6, y)

    _wait_key(kb)


# ---- main loop ------------------------------------------------------


def _move(page, delta, results, cursor, scroll):
    """Move the cursor by ``delta`` (wrapping) and keep it on screen."""
    n = _count(results[page])
    if n == 0:
        return
    cursor[page] = (cursor[page] + delta) % n
    cur = cursor[page]
    top = scroll[page]
    if cur < top:
        scroll[page] = cur
    elif cur >= top + _MAX_VISIBLE:
        scroll[page] = cur - _MAX_VISIBLE + 1
    # On a wrap, snap the viewport to the relevant end.
    if cur == 0:
        scroll[page] = 0
    elif cur == n - 1:
        scroll[page] = max(0, n - _MAX_VISIBLE)


def _scan_into(page, results, cursor, scroll, scanned, kb):
    """Run the scan backing ``page``. The channel map shares the WiFi
    scan, so WiFi and Chan both refresh ``results[_PAGE_WIFI]`` and mark
    each other scanned — flipping between them never rescans."""
    if page == _PAGE_BLE:
        _draw_banner(_PAGE_BLE)
        results[_PAGE_BLE] = _scan_ble(kb)
        cursor[_PAGE_BLE] = 0
        scroll[_PAGE_BLE] = 0
        scanned[_PAGE_BLE] = True
    else:
        _draw_banner(_PAGE_WIFI)
        results[_PAGE_WIFI] = _scan_wifi()
        cursor[_PAGE_WIFI] = 0
        scroll[_PAGE_WIFI] = 0
        scanned[_PAGE_WIFI] = True
        scanned[_PAGE_CHAN] = True


def run():
    _set_font()

    kb = MatrixKeyboard()
    # Debounce the launch keypress (Enter from the launcher) so it
    # doesn't immediately register — same 400 ms the other apps use.
    time.sleep_ms(400)

    page = _PAGE_WIFI
    results = [None, None, None]
    cursor = [0, 0, 0]
    scroll = [0, 0, 0]
    scanned = [False, False, False]

    # Scan the opening page once so the user lands on data, not a blank.
    _scan_into(page, results, cursor, scroll, scanned, kb)
    _render_page(page, results, cursor, scroll)

    while True:
        kb.tick()
        intent = _intent(kb.get_key())

        if intent == "exit":
            return
        elif intent in ("left", "right"):
            page = (page + (1 if intent == "right" else -1)) % _N_PAGES
            if not scanned[page]:
                _scan_into(page, results, cursor, scroll, scanned, kb)
            _render_page(page, results, cursor, scroll)
        elif intent == "up" and page != _PAGE_CHAN:
            _move(page, -1, results, cursor, scroll)
            _draw_list(page, results[page], cursor[page], scroll[page])
        elif intent == "down" and page != _PAGE_CHAN:
            _move(page, 1, results, cursor, scroll)
            _draw_list(page, results[page], cursor[page], scroll[page])
        elif intent == "select":
            # Enter opens a detail view on the list pages only.
            if page == _PAGE_WIFI and results[_PAGE_WIFI]:
                _wifi_detail(results[_PAGE_WIFI][cursor[_PAGE_WIFI]], kb)
                _render_page(page, results, cursor, scroll)
            elif page == _PAGE_BLE and results[_PAGE_BLE]:
                _ble_detail(results[_PAGE_BLE][cursor[_PAGE_BLE]], kb)
                _render_page(page, results, cursor, scroll)
        elif intent == "rescan":
            _scan_into(page, results, cursor, scroll, scanned, kb)
            _render_page(page, results, cursor, scroll)

        time.sleep_ms(_TICK_MS)


# Run, then drop ourselves from sys.modules so the launcher can
# re-import (and thus re-run) us next time it's selected — without a
# machine.reset(). The launcher's _launch holds its own reference to
# the module object, so popping here is safe; when run() returns,
# _launch falls through and repaints the menu.
try:
    run()
finally:
    try:
        _LCD.fillScreen(_BLACK)
    except Exception:
        pass
    sys.modules.pop(__name__, None)
