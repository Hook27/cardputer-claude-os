"""Concurrency test for Bridge.send's BLE chunk-writing.

Multiple agents now share one daemon and one BLE link. Each tool call is
a JSON line chunked into <=20-byte BLE writes; if two calls' chunks
interleave on the RX characteristic, the device reassembles garbage. This
proves a write-lock keeps each message's chunks contiguous.
"""

import asyncio
import json

import server


class _FakeClient:
    """Records every chunk written, yielding control between chunks so a
    second coroutine gets the chance to interleave if nothing serializes
    the writes."""

    def __init__(self, sink):
        self.sink = sink

    async def write_gatt_char(self, uuid, data, response=False):
        self.sink.append(bytes(data))
        await asyncio.sleep(0)  # yield: lets a racing send() jump in


async def _drive_two_concurrent_sends():
    bridge = server.Bridge()
    bridge.hello = {"caps": ["notify"], "mtu": 20}

    async def _no_connect():
        return None

    bridge.ensure_connected = _no_connect

    sink: list[bytes] = []
    bridge.client = _FakeClient(sink)

    async def _resolver():
        # Resolve each in-flight RPC future shortly after it registers so
        # send() returns instead of hanging on its wait_for.
        while True:
            await asyncio.sleep(0.001)
            for mid, fut in list(bridge._pending.items()):
                if not fut.done():
                    fut.set_result({"ack": "notify", "id": mid, "ok": True})

    resolver = asyncio.create_task(_resolver())
    try:
        await asyncio.gather(
            bridge.send("notify", {"k": "A" * 40}, agent="a"),
            bridge.send("notify", {"k": "B" * 40}, agent="b"),
        )
    finally:
        resolver.cancel()
    return sink


def test_concurrent_sends_do_not_interleave_chunks():
    sink = asyncio.run(_drive_two_concurrent_sends())
    stream = b"".join(sink)
    # If writes are serialized, the concatenated stream is exactly two
    # clean newline-terminated JSON lines. Interleaving corrupts framing
    # so json.loads fails or the count is wrong.
    segments = [s for s in stream.split(b"\n") if s.strip()]
    objs = [json.loads(s) for s in segments]  # raises if a line is corrupt
    assert len(objs) == 2, f"expected 2 clean messages, got {len(objs)}"
    payload_values = sorted(o["k"] for o in objs)
    assert payload_values == ["A" * 40, "B" * 40]


def test_uncapped_command_short_circuits_without_writing():
    """A command the device doesn't advertise (e.g. `show` on pre-0.4.0
    firmware) is rejected by the capability gate before any BLE write, so
    older firmware degrades gracefully instead of hanging."""

    async def _drive():
        bridge = server.Bridge()
        bridge.hello = {"caps": ["notify", "ask", "confirm"], "mtu": 20}

        async def _no_connect():
            return None

        bridge.ensure_connected = _no_connect

        wrote = []

        class _Client:
            async def write_gatt_char(self, uuid, data, response=False):
                wrote.append(bytes(data))

        bridge.client = _Client()
        result = await bridge.send("show", {"text": "hi", "channel": "ci"})
        return result, wrote

    result, wrote = asyncio.run(_drive())
    assert result["ok"] is False
    assert "does not advertise 'show'" in result["err"]
    assert wrote == [], "uncapped command must not reach the radio"


def test_dispatch_caches_and_clears_heartbeat():
    """A device heartbeat is cached for device_status; a disconnect clears it
    so stale dnd/battery can't outlive the link."""
    bridge = server.Bridge()
    assert bridge.last_heartbeat is None

    bridge._dispatch({"event": "heartbeat", "dnd": True, "uptime": 42})
    assert bridge.last_heartbeat == {"event": "heartbeat", "dnd": True, "uptime": 42}
    assert bridge.last_heartbeat_at is not None

    # Simulate the BLE disconnect callback.
    bridge._on_disconnect_sync(None)
    assert bridge.last_heartbeat is None
    assert bridge.last_heartbeat_at is None
