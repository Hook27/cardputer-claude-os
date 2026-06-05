"""pi_dashboard — two-Pi status dashboard for Cardputer-Adv.

Polls two Raspberry Pi ``pi_dshbrd_server`` endpoints every 5 seconds
and shows their status across three auto-rotating pages: CPU
temperature, uptime, and pending-updates + free disk. Leave it on the
desk as a glanceable health panel for the two Pi 5s.

Each Pi runs the companion ``pi_dshbrd_server.py`` (in the CardPuter ADV
repo), which serves on :8081/temp:

    {"hostname": ..., "temp_c": ..., "uptime": ...,
     "updates": ..., "disk_free_gb": ...}

### Layout

Two cards side by side, same three-zone chrome as the rest of the
bundle (hello_cardputer / snake / claude_buddy): a 20 px DARK header
with an ORANGE hairline at y=20, the two cards below, and a hint strip
at the bottom. **NC-Pi5** (192.168.178.45) is the LEFT card,
**TaSc-Pi5** (192.168.178.122) the RIGHT.

### Pages

Three pages auto-rotate every 5 s and are also switchable with the
left / right arrow keys (``,`` and ``/`` on the Cardputer-Adv cluster):

  1. CPU temperature  — green normally, red at >= 70 C
  2. Uptime
  3. Updates + free disk

If either Pi reports ``updates > 0`` we chirp the speaker once (edge-
triggered, so it sounds when the condition first appears rather than
nagging every poll).

### Exit

Q or ESC returns cleanly to the launcher menu — we simply let ``run()``
return and drop the module from ``sys.modules`` so it can be relaunched.
No ``machine.reset()``: the launcher's ``_launch`` repaints its menu as
soon as our import completes.

### Port notes

- **Network.** WiFi via ``network.WLAN(STA_IF)`` + the ``wifi_event``
  helper (the launcher has usually already connected by the time we
  run). HTTP uses the firmware ``requests`` module, polled sequentially.

- **Cadence.** 5 s poll and 5 s page-rotate deadlines tracked with
  ``time.ticks_ms``, while the keyboard is polled at ~40 ms so the
  arrows and Q/ESC stay responsive instead of being blocked inside a
  long sleep.

- **Font.** DejaVu9, size 1 for labels / hints, size 2 for the headline
  temperature number. Widths measured with ``_LCD.textWidth(...)`` for
  centering.
"""

import sys
import time

import M5
import network
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
_YELLOW = 0xE0B341

_LCD = M5.Lcd

_W = 240
_H = 135

# Load endpoints from config.py (gitignored).
try:
    from apps.config import PI_ENDPOINTS
    _PIS = (
        {"name": "NC-Pi5",   "url": PI_ENDPOINTS[0]},
        {"name": "TaSc-Pi5", "url": PI_ENDPOINTS[1]},
    )
except Exception:
    _PIS = (
        {"name": "NC-Pi5",   "url": ""},
        {"name": "TaSc-Pi5", "url": ""},
    )

# Page identifiers and their header labels.
_PAGE_TITLES = ("CPU temp", "Uptime", "Updates / disk")
_N_PAGES = len(_PAGE_TITLES)

# Cadences, all in milliseconds.
_POLL_MS = 5000   # network poll interval
_PAGE_MS = 5000   # auto-rotate interval
_TICK_MS = 40     # keyboard poll interval

# Above this many degrees C we tint the temperature red as a "running
# hot" cue. 70 C is below the Pi's 80 C throttle point but high enough
# to mean something.
_HOT_C = 70.0


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        # Fall back silently rather than crashing on a build without FONTS.
        print("pidash: setFont fallback:", e)


def _beep():
    """Short two-note chirp. Defensive: M5.Speaker isn't guaranteed on
    every build, and its API has been observed to vary, so any failure
    falls through silently — the on-screen update count is the primary
    channel anyway."""
    try:
        spk = M5.Speaker
    except Exception:
        return
    try:
        spk.tone(880, 90)
        time.sleep_ms(60)
        spk.tone(1175, 110)
    except Exception as e:
        print("pidash: beep skipped:", e)


# ---- chrome ---------------------------------------------------------


def _card_x(index):
    """Left x-origin of card ``index`` (0 = left, 1 = right)."""
    return 0 if index == 0 else _W // 2 + 1


def _card_w():
    """Drawable width of a single card."""
    return _W // 2 - 1


# Content area sits between the header hairline and the hint strip.
_TOP = 24
_BOTTOM = _H - 18


def _draw_chrome(page):
    """Full repaint of header + divider + hint strip for ``page``.

    The card bodies are painted separately by ``_draw_card`` as data
    lands; this just lays down the fixed furniture, so we call it on
    startup and on every page change.
    """
    _LCD.fillScreen(_BLACK)

    # Header band with the page title on the right.
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Pi dashboard", 6, 5)
    title = "{} [{}/{}]".format(_PAGE_TITLES[page], page + 1, _N_PAGES)
    _LCD.setTextColor(_CREAM, _DARK)
    _LCD.drawString(title, _W - _LCD.textWidth(title) - 6, 5)

    # Vertical divider between the two cards.
    _LCD.fillRect(_W // 2, _TOP, 1, _BOTTOM - _TOP, _DARK)

    # Hint strip along the bottom.
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "<- -> page   Q/ESC menu"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _wrap(text, max_chars):
    """Greedy word-wrap into a list of lines of at most ``max_chars``.

    Used for the uptime string, which is the only field long enough to
    need more than one line in a half-screen card.
    """
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        cand = w if not cur else cur + " " + w
        if len(cand) <= max_chars:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_card(index, page, row):
    """Repaint one Pi's card for the current ``page``.

    ``row`` is the carried per-Pi state dict. On a failed poll its
    ``error`` is set and we show that in place of the data, keeping the
    name and timestamp so the user can see the reading is stale.
    """
    x0 = _card_x(index)
    w = _card_w()
    _LCD.fillRect(x0, _TOP, w, _BOTTOM - _TOP, _BLACK)
    cx = x0 + w // 2  # horizontal centre of this card

    # Card title — the configured friendly name (NC-Pi5 / TaSc-Pi5).
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _BLACK)
    name = _PIS[index]["name"]
    _LCD.drawString(name, cx - _LCD.textWidth(name) // 2, _TOP + 4)

    if row["error"]:
        _LCD.setTextColor(_RED, _BLACK)
        msg = row["error"]
        _LCD.drawString(msg, cx - _LCD.textWidth(msg) // 2, _TOP + 34)
    elif page == 0:
        _draw_temp(cx, row)
    elif page == 1:
        _draw_uptime(cx, w, row)
    else:
        _draw_updates_disk(cx, row)

    # Last-update timestamp, dim, at the foot of the card.
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    stamp = row["stamp"] or "--:--:--"
    _LCD.drawString(stamp, cx - _LCD.textWidth(stamp) // 2, _BOTTOM - 14)


def _draw_temp(cx, row):
    """Page 1: the temperature as the headline number (size 2)."""
    temp_c = row["temp_c"]
    _LCD.setTextSize(2)
    _LCD.setTextColor(_RED if temp_c >= _HOT_C else _GREEN, _BLACK)
    reading = "{:.1f}C".format(temp_c)
    _LCD.drawString(reading, cx - _LCD.textWidth(reading) // 2, _TOP + 30)


def _draw_uptime(cx, w, row):
    """Page 2: uptime, size 1, word-wrapped to fit the card width."""
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _BLACK)
    # ~6 px/char at size 1; leave a little inner padding.
    max_chars = max(1, (w - 8) // 6)
    lines = _wrap(row["uptime"] or "?", max_chars)
    y = _TOP + 28
    for line in lines[:3]:  # cap at 3 lines; uptime never needs more
        _LCD.drawString(line, cx - _LCD.textWidth(line) // 2, y)
        y += 14


def _draw_updates_disk(cx, row):
    """Page 3: pending update count and free disk space, two rows."""
    _LCD.setTextSize(1)

    updates = row["updates"]
    # Yellow when there's something to install, gray when clean.
    _LCD.setTextColor(_YELLOW if updates else _GRAY_MID, _BLACK)
    up_line = "{} update{}".format(updates, "" if updates == 1 else "s")
    _LCD.drawString(up_line, cx - _LCD.textWidth(up_line) // 2, _TOP + 30)

    _LCD.setTextColor(_CREAM, _BLACK)
    disk_line = "{:.1f} GB free".format(row["disk_free_gb"])
    _LCD.drawString(disk_line, cx - _LCD.textWidth(disk_line) // 2, _TOP + 48)


def _draw_page(page, state):
    """Repaint both cards for ``page`` (chrome already drawn)."""
    for index in range(len(_PIS)):
        _draw_card(index, page, state[index])


def _now_hms():
    """Local wall-clock time as HH:MM:SS for the update stamp.

    Matches pi_temp_monitor's convention of nudging UTC to Amsterdam
    (UTC+2) since the device clock is set from NTP in UTC.
    """
    t = time.localtime(time.time() + 7200)  # UTC+2 Amsterdam
    return "{:02d}:{:02d}:{:02d}".format(t[3], t[4], t[5])


# ---- network --------------------------------------------------------


def _ensure_wifi():
    """Bring the station interface up and connect. Returns True on success.

    The launcher normally connects WiFi at boot, so this is usually a
    no-op fast path (isconnected() is already True); the wifi_event
    fallback covers the case where we were launched without it.
    """
    try:
        sta = network.WLAN(network.STA_IF)
        if not sta.active():
            sta.active(True)
        if sta.isconnected():
            return True
        try:
            import wifi_event
            res = wifi_event.connect()
            return bool(res.get("ok"))
        except Exception as e:
            print("pidash: wifi_event err:", e)
            return sta.isconnected()
    except Exception as e:
        print("pidash: ensure_wifi err:", e)
        return False


def _poll(url):
    """GET ``url`` and parse the JSON. Returns ``(data, None)`` on
    success or ``(None, error_str)`` on any failure."""
    import requests
    r = None
    try:
        r = requests.get(url, timeout=4)
        if r.status_code != 200:
            return None, "HTTP {}".format(r.status_code)
        return r.json(), None
    except Exception as e:
        # Connection refused, timeout, bad JSON — all collapse to a
        # short label; the detail goes to the console for debugging.
        print("pidash: poll", url, "err:", e)
        return None, "no data"
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def _poll_all(state):
    """Poll both endpoints, updating carried state in place.

    Returns True if any Pi now reports ``updates > 0`` — the caller uses
    that to decide whether to chirp.
    """
    any_updates = False
    for index in range(len(_PIS)):
        data, error = _poll(_PIS[index]["url"])
        row = state[index]
        if error is None:
            try:
                row["hostname"] = data.get("hostname")
                row["temp_c"] = float(data.get("temp_c"))
                row["uptime"] = data.get("uptime") or "?"
                row["updates"] = int(data.get("updates") or 0)
                row["disk_free_gb"] = float(data.get("disk_free_gb") or 0.0)
                row["stamp"] = _now_hms()
                row["error"] = None
                if row["updates"] > 0:
                    any_updates = True
            except (TypeError, ValueError) as e:
                # Well-formed HTTP 200 but a field we can't parse.
                print("pidash: parse err:", e)
                row["error"] = "bad data"
        else:
            # Keep the stale values; just flag the error.
            row["error"] = error
    return any_updates


# ---- input ----------------------------------------------------------


def _intent(k):
    """Normalize a MatrixKeyboard return to 'left' / 'right' / 'exit' / None.

    The Cardputer-Adv arrow cluster reports unshifted ASCII: ``,`` is
    the left-arrow key and ``/`` the right-arrow key (same mapping the
    launcher documents). Q or ESC exits.
    """
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x1B:  # ESC
            return "exit"
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch == "q":
        return "exit"
    if ch == ",":
        return "left"
    if ch == "/":
        return "right"
    return None


# ---- main loop ------------------------------------------------------


def _new_state():
    return [
        {
            "hostname": None,
            "temp_c": 0.0,
            "uptime": None,
            "updates": 0,
            "disk_free_gb": 0.0,
            "stamp": None,
            "error": "...",
        }
        for _ in _PIS
    ]


def run():
    _set_font()

    page = 0
    state = _new_state()
    _draw_chrome(page)
    _draw_page(page, state)

    kb = MatrixKeyboard()
    # Debounce the launch keypress (Enter from the launcher) so it
    # doesn't immediately register as input — same 400 ms the other
    # apps use.
    time.sleep_ms(400)

    _ensure_wifi()
    # Immediate first poll so the user isn't staring at "..." for 5 s.
    had_updates = _poll_all(state)
    _draw_page(page, state)
    if had_updates:
        _beep()

    now = time.ticks_ms()
    next_poll = time.ticks_add(now, _POLL_MS)
    next_page = time.ticks_add(now, _PAGE_MS)

    while True:
        kb.tick()
        intent = _intent(kb.get_key())
        if intent == "exit":
            return
        elif intent == "left":
            page = (page - 1) % _N_PAGES
            _draw_chrome(page)
            _draw_page(page, state)
            next_page = time.ticks_add(time.ticks_ms(), _PAGE_MS)
        elif intent == "right":
            page = (page + 1) % _N_PAGES
            _draw_chrome(page)
            _draw_page(page, state)
            next_page = time.ticks_add(time.ticks_ms(), _PAGE_MS)

        now = time.ticks_ms()

        # Poll on the deadline rather than sleeping the whole interval,
        # so the arrows and Q/ESC stay responsive throughout.
        if time.ticks_diff(now, next_poll) >= 0:
            # Edge-triggered chirp: beep only when updates appear on a
            # poll where the previous state had none, so we don't nag
            # every 5 s while updates remain pending.
            prev = _any_updates(state)
            now_updates = _poll_all(state)
            _draw_page(page, state)
            if now_updates and not prev:
                _beep()
            next_poll = time.ticks_add(time.ticks_ms(), _POLL_MS)

        # Auto-rotate pages.
        if time.ticks_diff(time.ticks_ms(), next_page) >= 0:
            page = (page + 1) % _N_PAGES
            _draw_chrome(page)
            _draw_page(page, state)
            next_page = time.ticks_add(time.ticks_ms(), _PAGE_MS)

        time.sleep_ms(_TICK_MS)


def _any_updates(state):
    """True if any Pi's last good reading had updates pending."""
    for row in state:
        if not row["error"] and row["updates"] > 0:
            return True
    return False


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
