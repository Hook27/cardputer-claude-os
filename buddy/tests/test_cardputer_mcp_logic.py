"""Hardware-free logic tests for buddy/device/apps/cardputer_mcp.py.

The device app is MicroPython and normally only runs on the Cardputer. But
its *logic* — how `show` updates the ambient channel ring, how a `confirm`
action diff is wrapped, how the arrow cluster scrolls it, and the scroll
bounds — is pure Python and identical under CPython. This harness stubs the
MicroPython/M5 hardware modules (`bluetooth`, `machine`, `micropython`, `M5`,
`hardware`) and a MicroPython-flavoured `time`, then execs the app source
(minus its trailing top-level `run()` call) so we can drive `App` directly.

It deliberately does NOT assert pixel coordinates — rendering can only be
judged on real hardware. It catches the logic bugs that don't need a screen.

Run standalone:   python3 buddy/tests/test_cardputer_mcp_logic.py
Or via pytest:    pytest buddy/tests/
"""

import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(_HERE, "..", "device", "apps", "cardputer_mcp.py")


def _install_fakes():
    """Put minimal fakes for the MicroPython/M5 modules into sys.modules so
    the app source can import + construct App() under CPython."""

    class _FakeUUID:
        def __init__(self, s):
            self.s = s

        def __bytes__(self):
            return b"\x00" * 16

    class _FakeBLE:
        def __init__(self):
            self._active = False

        def active(self, *a):
            if a:
                self._active = bool(a[0])
                return None
            return self._active

        def config(self, *a, **k):
            if a and a[0] == "mac":
                return (0, b"\x00\x11\x22\x33\x44\x55")
            return None

        def gatts_register_services(self, _svcs):
            return ((1, 2),)

        def irq(self, _handler):
            self._handler = _handler

        def gatts_set_buffer(self, *a, **k):
            return None

        def gap_advertise(self, *a, **k):
            return None

        def gatts_notify(self, *a, **k):
            return None

        def gatts_read(self, _h):
            return b""

        def gap_disconnect(self, *a, **k):
            return None

    bluetooth = types.ModuleType("bluetooth")
    bluetooth.BLE = _FakeBLE
    bluetooth.UUID = _FakeUUID
    sys.modules["bluetooth"] = bluetooth

    machine = types.ModuleType("machine")
    machine.reset = lambda: None
    sys.modules["machine"] = machine

    micropython = types.ModuleType("micropython")
    micropython.const = lambda x: x
    micropython.schedule = lambda fn, arg: None  # never run async work in tests
    sys.modules["micropython"] = micropython

    class _FakeLCD:
        FONTS = types.SimpleNamespace(DejaVu9=0)

        def setFont(self, *a):
            pass

        def fillScreen(self, *a):
            pass

        def fillRect(self, *a):
            pass

        def drawRect(self, *a):
            pass

        def setTextSize(self, *a):
            pass

        def setTextColor(self, *a):
            pass

        def drawString(self, *a):
            pass

        def textWidth(self, s):
            return len(s) * 6

    M5 = types.ModuleType("M5")
    M5.Lcd = _FakeLCD()
    M5.Speaker = types.SimpleNamespace(tone=lambda *a, **k: None)
    sys.modules["M5"] = M5

    hardware = types.ModuleType("hardware")

    class _FakeKB:
        def tick(self):
            pass

        def get_key(self):
            return None

    hardware.MatrixKeyboard = _FakeKB
    sys.modules["hardware"] = hardware

    # MicroPython-flavoured time: mirror the real module, add the MP-only
    # tick/sleep API, and let tests advance a virtual clock.
    import time as _real_time

    faketime = types.ModuleType("time")
    for attr in dir(_real_time):
        if not attr.startswith("__"):
            setattr(faketime, attr, getattr(_real_time, attr))
    _now = {"t": 10_000}
    faketime.sleep_ms = lambda ms: None
    faketime.ticks_ms = lambda: _now["t"]
    faketime.ticks_add = lambda a, b: a + b
    faketime.ticks_diff = lambda a, b: a - b
    faketime._advance = lambda dt: _now.__setitem__("t", _now["t"] + dt)
    sys.modules["time"] = faketime
    return faketime


def _load_module():
    faketime = _install_fakes()
    with open(_APP) as f:
        src = f.read()
    # Strip the single trailing top-level `run()` call so importing doesn't
    # launch the device main loop.
    lines = [ln for ln in src.splitlines() if ln.strip() != "run()"]
    glb = {"__name__": "cardputer_mcp_under_test"}
    exec(compile("\n".join(lines), _APP, "exec"), glb)
    glb["_faketime"] = faketime
    return glb


_M = _load_module()


# ---- pure helpers --------------------------------------------------------


def test_scroll_intent_arrow_mapping():
    si = _M["_scroll_intent"]
    assert si(ord(";")) == "up"
    assert si(ord(",")) == "up"
    assert si(ord(".")) == "down"
    assert si(ord("/")) == "down"
    assert si(ord("y")) is None
    assert si(None) is None
    assert si(0x1B) is None  # ESC is not a scroll key


def test_wrap_detail_lines_splits_on_newlines():
    wrap = _M["_wrap_detail_lines"]
    out = wrap("line one\nline two")
    assert out == ["line one", "line two"]


def test_wrap_detail_lines_hard_wraps_long_line():
    wrap = _M["_wrap_detail_lines"]
    width = _M["_DETAIL_WRAP"]
    out = wrap("x" * (width * 2 + 5))
    assert out[0] == "x" * width
    assert out[1] == "x" * width
    assert out[2] == "x" * 5


def test_wrap_detail_lines_preserves_blank_lines():
    wrap = _M["_wrap_detail_lines"]
    out = wrap("a\n\nb")
    assert out == ["a", "", "b"]


def test_wrap_detail_lines_caps_max_lines():
    wrap = _M["_wrap_detail_lines"]
    out = wrap("\n".join(str(i) for i in range(200)), max_lines=10)
    assert len(out) == 10


# ---- App: ambient `show` ring -------------------------------------------


def _fresh_app():
    return _M["App"]()


def test_show_adds_ambient_entry_and_acks():
    app = _fresh_app()
    sent = []
    app.ble.send = lambda payload: sent.append(payload) or True
    app._cmd_show({"channel": "ci", "text": "running pytest"}, "m1")
    assert app.ambient == [{"channel": "ci", "text": "running pytest"}]
    assert sent == [{"ack": "show", "id": "m1", "ok": True}]


def test_show_newest_first_and_dedupes_channel():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._cmd_show({"channel": "a", "text": "1"}, "1")
    app._cmd_show({"channel": "b", "text": "2"}, "2")
    app._cmd_show({"channel": "a", "text": "3"}, "3")  # updates 'a', moves front
    assert app.ambient == [
        {"channel": "a", "text": "3"},
        {"channel": "b", "text": "2"},
    ]


def test_show_caps_channel_count():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    cap = _M["_AMBIENT_MAX"]
    for i in range(cap + 3):
        app._cmd_show({"channel": "c%d" % i, "text": "t"}, str(i))
    assert len(app.ambient) == cap
    # Newest channel is at the front; oldest were evicted.
    assert app.ambient[0]["channel"] == "c%d" % (cap + 2)


def test_show_does_not_chirp():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._pending_chirp = None
    app._cmd_show({"channel": "x", "text": "y"}, "1")
    assert app._pending_chirp is None  # ambient is silent


def test_show_whitespace_channel_normalized_to_agent():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._cmd_show({"channel": "   ", "text": "y"}, "1")
    assert app.ambient == [{"channel": "agent", "text": "y"}]


# ---- App: ambient `progress` bar ----------------------------------------


def test_progress_adds_pct_entry_and_acks():
    app = _fresh_app()
    sent = []
    app.ble.send = lambda payload: sent.append(payload) or True
    app._cmd_progress({"channel": "build", "label": "compiling", "percent": 42}, "p1")
    assert app.ambient == [{"channel": "build", "text": "compiling", "pct": 42}]
    assert sent == [{"ack": "progress", "id": "p1", "ok": True}]


def test_progress_clamps_out_of_range():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._cmd_progress({"channel": "a", "label": "x", "percent": 250}, "1")
    assert app.ambient[0]["pct"] == 100
    app._cmd_progress({"channel": "b", "label": "y", "percent": -7}, "2")
    assert app.ambient[0]["pct"] == 0


def test_progress_non_numeric_percent_treated_as_zero():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._cmd_progress({"channel": "a", "label": "x", "percent": "bogus"}, "1")
    assert app.ambient[0]["pct"] == 0


def test_progress_shares_channel_ring_with_show():
    """A progress write and a show write contend for the same channel slot;
    the latest wins, flipping the entry between a bar and a text line."""
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._cmd_show({"channel": "ci", "text": "running pytest"}, "1")
    app._cmd_progress({"channel": "ci", "label": "tests", "percent": 30}, "2")
    assert len(app.ambient) == 1
    assert app.ambient[0] == {"channel": "ci", "text": "tests", "pct": 30}
    # And back to a plain status line.
    app._cmd_show({"channel": "ci", "text": "done"}, "3")
    assert "pct" not in app.ambient[0]


def test_progress_caps_channel_count():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    cap = _M["_AMBIENT_MAX"]
    for i in range(cap + 3):
        app._cmd_progress({"channel": "c%d" % i, "label": "t", "percent": i}, str(i))
    assert len(app.ambient) == cap


def test_progress_does_not_chirp():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._pending_chirp = None
    app._cmd_progress({"channel": "x", "label": "y", "percent": 10}, "1")
    assert app._pending_chirp is None  # ambient is silent


def test_progress_whitespace_channel_normalized_to_agent():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._cmd_progress({"channel": "  ", "label": "y", "percent": 5}, "1")
    assert app.ambient[0]["channel"] == "agent"


# ---- App: confirm action diff + scrolling --------------------------------


def test_confirm_with_details_wraps_and_resets_scroll():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._confirm_scroll = 5
    long_details = "\n".join("cmd %d" % i for i in range(8))
    app._cmd_confirm(
        {"title": "DROP customers", "details": long_details, "timeout_s": 30},
        "c1",
    )
    assert app.state == "confirm"
    assert app.pending_confirm["detail_lines"] == ["cmd %d" % i for i in range(8)]
    assert app._confirm_scroll == 0


def test_confirm_without_details_has_none_detail_lines():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._cmd_confirm({"title": "deploy prod", "timeout_s": 30}, "c2")
    assert app.pending_confirm["detail_lines"] is None


def test_confirm_whitespace_only_details_treated_as_none():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._cmd_confirm(
        {"title": "deploy prod", "details": "   \n  ", "timeout_s": 30}, "c2b"
    )
    assert app.pending_confirm["detail_lines"] is None


def test_confirm_scroll_down_and_up_bounded():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    visible = _M["_CONFIRM_DETAIL_VISIBLE"]
    details = "\n".join("L%d" % i for i in range(visible + 3))
    app._cmd_confirm({"title": "t", "details": details, "timeout_s": 30}, "c3")

    down = ord(".")
    up = ord(";")
    # Can scroll down exactly len-visible times, then it clamps.
    for _ in range(10):
        app.handle_keypress(down)
    assert app._confirm_scroll == (visible + 3) - visible  # == 3
    # Scrolling back up clamps at 0.
    for _ in range(10):
        app.handle_keypress(up)
    assert app._confirm_scroll == 0


def test_confirm_scroll_noop_without_details():
    app = _fresh_app()
    app.ble.send = lambda payload: True
    app._cmd_confirm({"title": "t", "timeout_s": 30}, "c4")
    app.handle_keypress(ord("."))
    assert app._confirm_scroll == 0  # no detail lines -> arrows do nothing


def test_confirm_y_tap_does_not_advance_via_scroll_keys():
    # An arrow key must not register as a Y hold.
    app = _fresh_app()
    app.ble.send = lambda payload: True
    details = "\n".join("L%d" % i for i in range(10))
    app._cmd_confirm({"title": "t", "details": details, "timeout_s": 30}, "c5")
    app.handle_keypress(ord("."))
    assert app._y_held_since_ms is None  # scrolling never starts the hold


# ---- App: heartbeat sender -----------------------------------------------


def test_build_heartbeat_has_dnd_and_uptime():
    app = _fresh_app()
    app.dnd = True
    ft = _M["_faketime"]
    ft._advance(7000)
    hb = app._build_heartbeat(ft.ticks_ms())
    assert hb["event"] == "heartbeat"
    assert hb["dnd"] is True
    assert hb["uptime"] == 7  # 7000 ms // 1000
    # The stub M5 has no Power API, so battery is omitted and disabled.
    assert "bat" not in hb
    assert app._bat_ok is False


def test_heartbeat_not_sent_when_disconnected():
    app = _fresh_app()
    app.ble_connected = False
    sent = []
    app.ble.send = lambda p: sent.append(p) or True
    ft = _M["_faketime"]
    ft._advance(_M["_HEARTBEAT_INTERVAL_MS"] + 1000)
    app._maybe_send_heartbeat(ft.ticks_ms())
    assert sent == []


def test_heartbeat_cadence_when_connected():
    app = _fresh_app()
    app.ble_connected = True
    sent = []
    app.ble.send = lambda p: sent.append(p) or True
    ft = _M["_faketime"]
    interval = _M["_HEARTBEAT_INTERVAL_MS"]
    ft._advance(interval - 1000)
    app._maybe_send_heartbeat(ft.ticks_ms())
    assert sent == [], "not due yet"
    ft._advance(1000)
    app._maybe_send_heartbeat(ft.ticks_ms())
    assert len(sent) == 1 and sent[0]["event"] == "heartbeat"
    # Immediately after a send it's not due again.
    app._maybe_send_heartbeat(ft.ticks_ms())
    assert len(sent) == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print("ok   %s" % fn.__name__)
        except AssertionError as e:
            failures += 1
            print("FAIL %s: %s" % (fn.__name__, e))
        except Exception as e:  # noqa: BLE001
            failures += 1
            print("ERR  %s: %r" % (fn.__name__, e))
    print("\n%d/%d passed" % (len(fns) - failures, len(fns)))
    sys.exit(1 if failures else 0)
