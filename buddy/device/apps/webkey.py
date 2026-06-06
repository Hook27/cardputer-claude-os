"""webkey — a WiFi-hosted remote USB keyboard for the Cardputer-Adv.

Plug the Cardputer into a target computer ("Computer A") over USB-C. The
app joins WiFi, brings the Cardputer up as a USB HID keyboard, and starts
a tiny HTTP server. Open the IP it shows on screen from any phone or
laptop on the same network ("Computer B"): you get a single-line text
field with a live ASCII preview, a Send button (Enter in the field also
sends), dedicated Enter / Backspace / Tab / Esc and arrow buttons, and
Ctrl / Alt / Shift / Gui modifier toggles. Whatever you send is injected
as real USB keystrokes into Computer A.

### Text translation + preview

A USB HID keyboard can only emit US-layout keycodes, so the browser folds
non-ASCII input to its closest ASCII form (e.g. "Knäckebröd" -> "Knackebrod",
ß -> ss) and shows the result live in a read-only preview. The Send button
transmits exactly what the preview shows; characters with no ASCII
equivalent (e.g. the euro sign) are dropped and simply don't appear in the
preview. Case is preserved — only accents are stripped — and uppercase is
sent the proper HID way (Shift + base key).

### Why HTTP (not WebSocket) and asyncio (not easysocket)

The firmware's frozen ``websocket`` module is codec-only — it wraps an
already-upgraded socket and does NOT do the HTTP Upgrade handshake, so a
real WebSocket server would mean hand-rolling Sec-WebSocket-Accept on the
MCU. Not worth it for v1. ``easysocket`` is blocking/``select``-based and
would fight the event loop. ``asyncio`` (confirmed present, with
``start_server``) lets the listener, the per-connection handlers and the
LCD/keyboard task all share one loop. Each keystroke is one small POST to
``/k``; on a LAN that's plenty responsive. WebSocket stays a future
upgrade if the byte counter ever says we need it.

### The USB-HID / serial caveat (learned the hard way)

``usb.device.get().init(kbd)`` reconfigures the USB stack. With the default
``builtin_driver=False`` it REMOVES the CDC serial interface and the REPL
vanishes (the device re-enumerates; on ESP32-S3 native USB there is no
DTR/RTS recovery — only a physical reset). We pass ``builtin_driver=True``
to keep serial alongside HID, but ``init`` still triggers a brief USB
re-enumeration. Because there is no clean software path back to plain
serial once HID is up, exiting AFTER HID was activated does a
``machine.reset()`` (the launcher boots straight back from that). If the
HID switch fails we never touched USB, so we exit the normal way (return +
``sys.modules.pop``) into a display-only fallback.

### HID API (confirmed live on this firmware, MicroPython 1.27)

``usb.device.keyboard`` here is the text-oriented M5 variant — no
``KeyCode`` class; instead ``char_to_hid_key(ch) -> (needs_shift, hid_code)``.
We send every character/key the same way: stage modifiers with
``set_modifiers(left_control=, left_shift=, left_alt=, left_gui=)`` (note
``left_control``, NOT ``left_ctrl``), then ``send_key(code)`` — which
emits a press+release carrying the staged modifier. ``send_keypresses``
is NOT used: it wants a list of int keycodes (not a str) and applies no
modifiers, so it can't type uppercase or shifted symbols.

``char_to_hid_key`` is also unreliable for punctuation (returns code 0 for
the double-quote and backslash, the wrong code for ';', drops the shift on
':'), so for printable ASCII we use our own ``_ASCII_HID`` US-layout table
first and fall back to ``char_to_hid_key`` only for anything outside it.

### Screen / controls

Same three-zone chrome as the rest of the bundle (20px DARK header +
ORANGE hairline at y=20, body, 18px hint strip). Body shows the URL, WiFi
SSID, USB-HID status, request count, last keystroke and a byte counter.
``Q`` / ``ESC`` on the Cardputer stops the server and exits.

### Limitations to know

- LAN-only, NO auth: anyone on the WiFi who knows the IP can type into the
  host. Treat as a trusted-network tool (the page says so too).
- US host layout assumed; on a non-US layout the symbol keys can produce
  different glyphs (a host-side, not device-side, constraint).
- Translation is lossy by design (accents stripped); characters with no
  ASCII equivalent are dropped.
- Send is one HTTP POST per Send action (not WebSocket).
"""

import sys
import time

import M5
import machine
import asyncio
from hardware import MatrixKeyboard


# Palette inlined from ui_theme — same colors the rest of the bundle uses.
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

_TICK_MS = 40

# HID usage codes for the special keys the web page exposes. (Letters /
# digits / punctuation are resolved per-character by char_to_hid_key.)
_KEYS = {
    "ENTER": 40, "ESC": 41, "BSPACE": 42, "TAB": 43, "SPACE": 44,
    "RIGHT": 79, "LEFT": 80, "DOWN": 81, "UP": 82,
    "DEL": 76, "HOME": 74, "END": 77, "PGUP": 75, "PGDN": 78,
}
# HID modifier bitmask bits (left-side).
_MODBIT = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "gui": 0x08}

# US-layout ASCII -> (needs_shift, hid_usage_code), built once at import.
# This is the authoritative mapping for printable ASCII: the firmware's
# char_to_hid_key() is unreliable for several punctuation marks (it returns
# code 0 for '"' and '\\', the wrong code for ';', and drops the shift on
# ':'), which is what made '"' silently disappear. We consult this table
# first and only fall back to char_to_hid_key for anything not in it.
_ASCII_HID = {}


def _build_ascii_hid():
    m = _ASCII_HID
    for i in range(26):
        m[chr(ord("a") + i)] = (False, 4 + i)   # a..z -> 4..29
        m[chr(ord("A") + i)] = (True, 4 + i)    # A..Z -> shift + 4..29
    for i, d in enumerate("1234567890"):
        m[d] = (False, 30 + i)                  # 1..9,0 -> 30..39
    for i, c in enumerate("!@#$%^&*()"):
        m[c] = (True, 30 + i)                   # shifted number row
    # (unshifted, shifted, code) for the punctuation keys.
    for lo, hi, code in (
        ("-", "_", 45), ("=", "+", 46), ("[", "{", 47), ("]", "}", 48),
        ("\\", "|", 49), (";", ":", 51), ("'", '"', 52), ("`", "~", 53),
        (",", "<", 54), (".", ">", 55), ("/", "?", 56),
    ):
        m[lo] = (False, code)
        m[hi] = (True, code)
    m[" "] = (False, 44)
    m["\t"] = (False, 43)
    m["\n"] = (False, 40)


_build_ascii_hid()


# ---- module state ---------------------------------------------------

_kb = None            # MatrixKeyboard, for the local Q/ESC exit
_kbd = None           # usb.device.keyboard.Keyboard instance (or None)
_char_to_hid = None   # char_to_hid_key function (or None)
_hid_ok = False

_server = None
_port = 80
_stop = asyncio.Event()
_dirty = True         # repaint the status body on the next UI tick

_state = {
    "url": "starting...",
    "ssid": "?",
    "hid": False,
    "reqs": 0,
    "last": "-",
    "bytes": 0,
}


# ---- the web page (single self-contained string) --------------------

_PAGE = (
    "<!DOCTYPE html><html><head>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<meta charset=utf-8><title>WebKey</title><style>"
    "body{font-family:sans-serif;margin:12px;background:#111;color:#eee}"
    "h2{color:#CC785C;margin:.2em 0}"
    ".warn{color:#e0a070;font-size:.8em}"
    "input.t{width:100%;box-sizing:border-box;font-size:1.1em;padding:6px;"
    "background:#000;color:#eee;border:1px solid #555}"
    "input.p{width:100%;box-sizing:border-box;font-size:1em;padding:6px;"
    "background:#181818;color:#9ad;border:1px solid #444}"
    ".lbl{font-size:.72em;color:#888;margin:8px 0 2px}"
    "button{font-size:1em;margin:3px;padding:8px 10px;background:#222;"
    "color:#eee;border:1px solid #555;border-radius:6px}"
    "button.on{background:#CC785C;color:#000}"
    ".row{margin-top:6px}</style></head><body>"
    "<h2>WebKey</h2>"
    "<p class=warn>LAN only - anyone on this WiFi can type into the host.</p>"
    "<div class=lbl>Type here (Enter sends):</div>"
    "<input id=t class=t autofocus autocomplete=off>"
    "<div class=lbl>Preview (sent as):</div>"
    "<input id=p class=p readonly>"
    "<div class=row><button onclick=sendText()>Send</button></div>"
    "<div class=row>"
    "<button onclick=\"k('ENTER')\">Enter</button>"
    "<button onclick=\"k('BSPACE')\">Bksp</button>"
    "<button onclick=\"k('TAB')\">Tab</button>"
    "<button onclick=\"k('ESC')\">Esc</button></div>"
    "<div class=row>"
    "<button onclick=\"k('UP')\">&uarr;</button>"
    "<button onclick=\"k('DOWN')\">&darr;</button>"
    "<button onclick=\"k('LEFT')\">&larr;</button>"
    "<button onclick=\"k('RIGHT')\">&rarr;</button></div>"
    "<div class=row>"
    "<button id=mctrl onclick=\"tm('ctrl')\">Ctrl</button>"
    "<button id=malt onclick=\"tm('alt')\">Alt</button>"
    "<button id=mshift onclick=\"tm('shift')\">Shift</button>"
    "<button id=mgui onclick=\"tm('gui')\">Gui</button></div>"
    "<script>"
    # Diacritic -> ASCII fold table, grouped by target letter.
    "var TR={};"
    "[['a','àáâãäå'],['e','èéêë'],"
    "['i','ìíîï'],['o','òóôõö'],"
    "['u','ùúûü'],['c','ç'],['n','ñ'],['y','ÿ'],"
    "['A','ÀÁÂÃÄÅ'],['E','ÈÉÊË'],"
    "['I','ÌÍÎÏ'],['O','ÒÓÔÕÖ'],"
    "['U','ÙÚÛÜ'],['C','Ç'],['N','Ñ']]"
    ".forEach(function(p){for(var i=0;i<p[1].length;i++){TR[p[1][i]]=p[0];}});"
    "TR['ß']='ss';"
    # Fold each char: mapped diacritic -> ASCII; pass through other ASCII;
    # drop anything else (no ASCII equivalent, e.g. euro) silently.
    "function translate(s){var o='';for(var i=0;i<s.length;i++){var c=s[i];"
    "if(TR[c]!==undefined){o+=TR[c];}else if(c.charCodeAt(0)<128){o+=c;}}return o;}"
    "var T=document.getElementById('t'),P=document.getElementById('p');"
    "T.addEventListener('input',function(){P.value=translate(T.value);});"
    "var mods={ctrl:0,alt:0,shift:0,gui:0};"
    "function tm(m){mods[m]^=1;"
    "document.getElementById('m'+m).classList.toggle('on',!!mods[m]);}"
    "function cur(){return Object.keys(mods).filter(function(x){return mods[x];});}"
    "function clr(){for(var m in mods){mods[m]=0;"
    "document.getElementById('m'+m).classList.remove('on');}}"
    "function post(o){return fetch('/k',{method:'POST',body:JSON.stringify(o)});}"
    "function k(n){post({t:'key',k:n,m:cur()}).then(clr);}"
    # Send the TRANSLATED (preview) text; clear both fields on success.
    "function sendText(){var v=translate(T.value);"
    "if(v){post({t:'text',d:v}).then(function(){T.value='';P.value='';});}}"
    # Enter in the field sends instead of doing nothing (Bug 1).
    "T.addEventListener('keydown',function(e){"
    "if(e.key==='Enter'){e.preventDefault();sendText();}});"
    "</script></body></html>"
).encode()


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("webkey: setFont fallback:", e)


# ---- WiFi -----------------------------------------------------------


def _ensure_wifi():
    """Return the STA IP string, or None if we couldn't get online.

    The launcher usually connects at boot; we reuse that link and only
    actively connect if it's down.
    """
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        if not sta.active():
            sta.active(True)
        if not sta.isconnected():
            try:
                import wifi_event
                wifi_event.connect()
            except Exception as e:
                print("webkey: wifi_event connect err:", e)
        if sta.isconnected():
            try:
                import wifi_event
                _state["ssid"] = wifi_event.SSID or "?"
            except Exception:
                pass
            return sta.ifconfig()[0]
    except Exception as e:
        print("webkey: wifi err:", e)
    return None


# ---- USB HID --------------------------------------------------------


def _init_hid():
    """Bring the Cardputer up as a USB HID keyboard.

    Passes builtin_driver=True so the CDC serial survives (see module
    docstring). Sets _hid_ok / _kbd / _char_to_hid; on any failure leaves
    _hid_ok False so the app runs display-only.
    """
    global _kbd, _char_to_hid, _hid_ok
    try:
        import usb.device
        from usb.device.keyboard import Keyboard, char_to_hid_key
        _kbd = Keyboard()
        usb.device.get().init(_kbd, builtin_driver=True)
        _char_to_hid = char_to_hid_key
        # Let the host finish re-enumerating the new HID interface before
        # we start accepting keystrokes.
        time.sleep_ms(800)
        _hid_ok = True
    except Exception as e:
        print("webkey: HID init failed (display-only):", e)
        _kbd = None
        _hid_ok = False
    _state["hid"] = _hid_ok


def _inject_key(code, modmask):
    """Press (and release) one HID key code, optionally with modifiers.

    The firmware's set_modifiers() uses the keyword names left_control /
    left_shift / left_alt / left_gui (NOT left_ctrl), and only *stages*
    the modifier — it emits no report on its own. send_key() then sends a
    press+release that carries the staged modifier. We confirmed this
    live by capturing the generated reports: e.g. shift+code 4 -> report
    [0x02, 0, 4, ...]. After the key we clear the modifiers and, when one
    was held, send a neutral [0,0,0] release so the modifier doesn't stay
    logically pressed on the host until the next keystroke.

    (The earlier version called set_modifiers(left_ctrl=...), which raised
    "unexpected keyword argument 'left_ctrl'"; the exception was swallowed
    so every uppercase / shifted-symbol key silently sent nothing.)
    """
    if not _hid_ok or _kbd is None:
        return
    try:
        _kbd.set_modifiers(
            left_control=bool(modmask & 0x01),
            left_shift=bool(modmask & 0x02),
            left_alt=bool(modmask & 0x04),
            left_gui=bool(modmask & 0x08),
        )
        _kbd.send_key(code)
        _kbd.set_modifiers()
        if modmask:
            # Neutral report so the held modifier is released on the host.
            _kbd.send_key(0)
    except Exception as e:
        print("webkey: inject_key err:", e)


def _inject_text(s):
    """Type a string, one character at a time.

    Resolution order per character:
      1. _ASCII_HID (our authoritative US-layout table) — correct for all
         printable ASCII, including the marks the firmware botches ('"',
         '\\', ';', ':').
      2. char_to_hid_key() as a fallback for anything else.
    A (needs_shift, code) pair with code 0 means "unsupported" and is
    skipped (typing it would send nothing). The browser already folds
    non-ASCII to ASCII before sending, so step 1 normally covers everything.

    We do NOT use send_keypresses(): it wants a list of int keycodes (not a
    str) and applies no modifiers, so it can't produce uppercase or shifted
    symbols."""
    if not _hid_ok or _kbd is None:
        return
    for ch in s:
        hid = _ASCII_HID.get(ch)
        if hid is None and _char_to_hid is not None:
            try:
                hid = _char_to_hid(ch)
            except Exception:
                hid = None
        if not hid:
            continue
        shift, code = hid
        if not code:
            # Unsupported character (no usable HID usage code).
            continue
        _inject_key(code, 0x02 if shift else 0)


# ---- request handling -----------------------------------------------


def _apply(body):
    """Parse a /k JSON body and perform the keystroke. Returns True on a
    well-formed request (even in display-only mode — the screen still
    reflects it). Updates _state and marks the UI dirty."""
    global _dirty
    try:
        import json
        msg = json.loads(body)
    except Exception:
        return False
    kind = msg.get("t")
    if kind == "text":
        data = msg.get("d", "")
        if not isinstance(data, str):
            return False
        _inject_text(data)
        _state["bytes"] += len(data)
        show = data if len(data) <= 14 else data[:13] + "…"
        _state["last"] = "txt: " + show
    elif kind == "key":
        name = msg.get("k", "")
        code = _KEYS.get(name)
        if code is None:
            return False
        modmask = 0
        for m in msg.get("m", []) or []:
            modmask |= _MODBIT.get(m, 0)
        _inject_key(code, modmask)
        _state["bytes"] += 1
        pre = "+".join(msg.get("m", []) or [])
        _state["last"] = (pre + "+" + name) if pre else name
    else:
        return False
    _dirty = True
    return True


async def _respond(writer, status, ctype, body):
    if isinstance(body, str):
        body = body.encode()
    hdr = (b"HTTP/1.0 " + status + b"\r\nContent-Type: " + ctype +
           b"\r\nContent-Length: " + str(len(body)).encode() +
           b"\r\nConnection: close\r\n\r\n")
    writer.write(hdr)
    writer.write(body)
    await writer.drain()


async def _handle(reader, writer):
    global _dirty
    try:
        line = await reader.readline()
        if not line:
            return
        parts = line.split()
        method = parts[0] if parts else b""
        path = parts[1] if len(parts) > 1 else b"/"

        clen = 0
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            if h.lower().startswith(b"content-length:"):
                try:
                    clen = int(h.split(b":", 1)[1].strip())
                except Exception:
                    clen = 0

        body = b""
        while len(body) < clen:
            chunk = await reader.read(clen - len(body))
            if not chunk:
                break
            body += chunk

        _state["reqs"] += 1
        _dirty = True

        if method == b"GET" and path == b"/":
            await _respond(writer, b"200 OK", b"text/html; charset=utf-8", _PAGE)
        elif method == b"POST" and path == b"/k":
            ok = _apply(body)
            await _respond(writer, b"200 OK" if ok else b"400 Bad Request",
                           b"text/plain", b"ok" if ok else b"err")
        else:
            await _respond(writer, b"404 Not Found", b"text/plain", b"404")
    except Exception as e:
        print("webkey: handler err:", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ---- screen ---------------------------------------------------------


def _draw_static():
    """Header + hairline + hint strip (painted once)."""
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("WebKey", 6, 5)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "Q/ESC exit"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _kv(y, label, value, vcolor=_CREAM):
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString(label, 6, y)
    vx = 6 + _LCD.textWidth(label) + 4
    text = value
    while text and _LCD.textWidth(text) > (_W - 6 - vx):
        text = text[:-1]
    _LCD.setTextColor(vcolor, _BLACK)
    _LCD.drawString(text, vx, y)
    return y + 14


def _draw_status():
    """Repaint the dynamic body region from _state."""
    _LCD.fillRect(0, 21, _W, (_H - 18) - 21, _BLACK)
    _LCD.setTextSize(1)
    url = _state["url"]
    _LCD.setTextColor(_ORANGE, _BLACK)
    _LCD.drawString(url, (_W - _LCD.textWidth(url)) // 2, 25)
    y = 44
    y = _kv(y, "WiFi:", _state["ssid"])
    y = _kv(y, "USB HID:", "ready" if _state["hid"] else "UNAVAILABLE",
            _GREEN if _state["hid"] else _RED)
    y = _kv(y, "Reqs:", str(_state["reqs"]))
    y = _kv(y, "Last:", _state["last"])
    y = _kv(y, "Bytes:", str(_state["bytes"]))


def _intent(k):
    """Local exit only: Q or ESC."""
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x1B:
            return "exit"
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if isinstance(k, str) and k.lower() == "q":
        return "exit"
    return None


# ---- async main -----------------------------------------------------


async def _ui_task():
    global _dirty
    while not _stop.is_set():
        _kb.tick()
        if _intent(_kb.get_key()) == "exit":
            _stop.set()
            break
        if _dirty:
            _draw_status()
            _dirty = False
        await asyncio.sleep_ms(120)


async def _amain(ip):
    global _server, _port, _dirty
    last_err = None
    for p in (80, 8080):
        try:
            _server = await asyncio.start_server(_handle, "0.0.0.0", p)
            _port = p
            break
        except Exception as e:
            last_err = e
            _server = None
    if _server is None:
        _state["url"] = "server bind failed"
        _state["last"] = str(last_err)[:14]
    else:
        _state["url"] = "http://{}{}".format(
            ip, "" if _port == 80 else ":" + str(_port))
    _dirty = True

    ui = asyncio.create_task(_ui_task())
    await _stop.wait()

    if _server is not None:
        _server.close()
        try:
            await _server.wait_closed()
        except Exception:
            pass
    ui.cancel()


# ---- entrypoint -----------------------------------------------------


def run():
    global _kb
    _set_font()

    ip = _ensure_wifi()
    if ip is None:
        # No network -> no server. Show a notice and wait for Q/ESC.
        _draw_static()
        _LCD.setTextColor(_RED, _BLACK)
        msg = "WiFi offline"
        _LCD.drawString(msg, (_W - _LCD.textWidth(msg)) // 2, 50)
        _LCD.setTextColor(_GRAY_MID, _BLACK)
        sub = "connect WiFi, relaunch"
        _LCD.drawString(sub, (_W - _LCD.textWidth(sub)) // 2, 70)
        kb = MatrixKeyboard()
        time.sleep_ms(400)
        while True:
            kb.tick()
            if _intent(kb.get_key()) == "exit":
                return
            time.sleep_ms(_TICK_MS)

    # HID before the server, so we're ready to inject the moment a
    # browser connects. May re-enumerate USB (see module docstring).
    _init_hid()

    _kb = MatrixKeyboard()
    # Debounce the Enter that launched us so it isn't read as an exit.
    time.sleep_ms(400)

    _draw_static()
    try:
        asyncio.run(_amain(ip))
    finally:
        try:
            _LCD.fillScreen(_BLACK)
        except Exception:
            pass

    if _hid_ok:
        # USB is in HID mode and there's no clean software path back to
        # plain CDC serial on ESP32-S3 native USB. Reboot: the launcher
        # boots straight back (boot_option=2 -> main.py) with serial
        # restored. This intentionally deviates from the bundle's newer
        # no-reset return convention.
        try:
            _LCD.fillScreen(_BLACK)
            _LCD.setTextColor(_CREAM, _BLACK)
            note = "Rebooting..."
            _LCD.drawString(note, (_W - _LCD.textWidth(note)) // 2, 60)
        except Exception:
            pass
        time.sleep_ms(400)
        machine.reset()
    # Display-only path (HID never activated): fall through and let the
    # module-level finally drop us from sys.modules so the launcher
    # repaints its menu.


# Run, then drop ourselves from sys.modules so the launcher can re-import
# (and thus re-run) us next time it's selected. The HID path above
# reboots instead and never reaches here. Same pattern as the rest of the
# bundle for the non-HID case.
try:
    run()
finally:
    try:
        _LCD.fillScreen(_BLACK)
    except Exception:
        pass
    sys.modules.pop(__name__, None)
