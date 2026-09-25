"""cardputer-mcp — pocket pager for AI agents, exposed over MCP.

Iteration 2: real BLE transport.

This process speaks stdio MCP to its client (Claude Code, Cursor, etc.)
and bridges tool calls to a Cardputer running the `cardputer_mcp.py`
device app over Bluetooth Low Energy.

Architecture:

  MCP client  ──stdio──▶  this process  ──BLE/bleak──▶  Cardputer
                                          (a5cd0001-…)

Tool calls become BLE writes; the device's acknowledgments resolve
in-flight asyncio Futures keyed by a per-call `id`. Disconnect events
fail all in-flight RPCs cleanly so the client gets a real error
rather than a hung tool.

Why FastMCP rather than the low-level Server API: this server's tool
surface is small (six tools at full build-out), each with a clean
typed signature. FastMCP's decorator style keeps the call-site code
close to the description text, which is what we'll iterate on most
often as we tune Claude's tool-selection behavior.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Literal, Optional

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from mcp.server.fastmcp import Context, FastMCP

from audit import ConsentAuditLog
from auth import label_for_authorization
from ratelimit import MinIntervalLimiter


# ---- protocol constants --------------------------------------------
#
# Keep these in sync with buddy/references/mcp_protocol.md and the
# device-side cardputer_mcp.py. If you change a UUID here, change it
# in all three places — there's no central manifest because grepping
# `a5cd` is faster than maintaining a config file.

SERVICE_UUID = "a5cd0001-c0de-4abe-9c1a-4d5e6f7a8b90"
RX_UUID = "a5cd0002-c0de-4abe-9c1a-4d5e6f7a8b90"  # host → device
TX_UUID = "a5cd0003-c0de-4abe-9c1a-4d5e6f7a8b90"  # device → host

# Device advertises as CardputerMCP_<6 hex>; we filter on the prefix.
NAME_PREFIX = "CardputerMCP_"

SCAN_TIMEOUT_S = 5.0
HELLO_TIMEOUT_S = 5.0
DEFAULT_RPC_TIMEOUT_S = 30.0

# Max length of the optional `confirm` action-diff `details` payload. Kept
# deliberately small for TWO device limits: (1) the device parses each inbound
# JSON line in BLE IRQ context (see cardputer_mcp.py), so the line must stay
# well inside the ~5 ms IRQ budget; (2) the device's RX reassembly buffer is
# 512 bytes — the full confirm line (cmd+id+agent+title≤64+flags+details) must
# fit under it. Worst case here ≈ 195 B of envelope + 256 B details ≈ 451 B,
# leaving comfortable margin. ~256 chars still scrolls to ~7 lines of diff.
_CONFIRM_DETAILS_MAX = 256

# When connection fails, suppress retries for this long so we don't
# stall every tool call with a fresh 5-second scan when the device
# is simply not in range. The MCP client will see a fast "unavailable"
# result instead.
FAIL_BACKOFF_S = 30.0

# Where we remember the device's BLE address after first successful
# connect. macOS hands out a per-host UUID rather than the real MAC —
# fine, it's stable across reboots of the laptop.
PAIR_CACHE_DIR = Path.home() / ".cardputer-mcp"
PAIR_CACHE_FILE = PAIR_CACHE_DIR / "paired.json"


def _log(line: str) -> None:
    """Write to stderr, which is what Claude Code surfaces in its MCP
    log pane. Never write to stdout — that's the MCP protocol stream
    and any non-protocol bytes there corrupt the transport."""
    print(f"[cardputer-mcp] {line}", file=sys.stderr, flush=True)


def _load_cached_address() -> Optional[str]:
    try:
        with open(PAIR_CACHE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    addr = data.get("address")
    return addr if isinstance(addr, str) else None


def _save_cached_address(addr: str, name: str) -> None:
    try:
        PAIR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        PAIR_CACHE_FILE.write_text(
            json.dumps(
                {"address": addr, "name": name, "paired_at": int(time.time())}
            )
        )
    except OSError as e:
        _log(f"cache save failed: {e}")


# ---- bridge --------------------------------------------------------


class Bridge:
    """Manages one BLE connection to a Cardputer and correlates RPCs.

    The lifecycle is lazy: `ensure_connected()` is a no-op when the
    link is already up, otherwise it scans (or uses the cached
    address) and waits for the device's `hello` event before
    returning. Every tool call is one or more 20-byte RX writes
    matched to a TX ack by a generated `id` string.

    State machine, simplified:

        disconnected  ──ensure_connected──▶  scanning ──▶ connecting
                                                                │
                                                                ▼
        ◀──disconnected_callback──  connected  ◀── hello-received
    """

    def __init__(self) -> None:
        self.client: Optional[BleakClient] = None
        self.hello: Optional[dict] = None

        # Latest device heartbeat (dnd / uptime / battery) and when we saw it,
        # cached so the read-only `device_status` tool can report device state
        # without a round-trip. Cleared on disconnect so stale state can't
        # outlive the link.
        self.last_heartbeat: Optional[dict] = None
        self.last_heartbeat_at: Optional[float] = None

        self._rx_buf = bytearray()
        self._pending: dict[str, asyncio.Future] = {}
        self._connect_lock = asyncio.Lock()
        self._hello_event = asyncio.Event()

        # Serializes the chunked write of ONE message so two concurrent
        # tool calls (now possible — many agents share one daemon) can't
        # interleave their 20-byte fragments on the RX characteristic and
        # corrupt the device's line reassembly. Scoped to the write loop
        # only — NOT the blocking wait — so the device's own pre-emption
        # (a confirm can pre-empt a pending ask) still works.
        self._write_lock = asyncio.Lock()

        # Suppress reconnect storms when the device is plainly absent —
        # without this, every tool call eats 5 s of scan time before
        # returning "unavailable", which makes Claude wait forever
        # when the user just hasn't powered the device on.
        self._last_fail_at: Optional[float] = None

    # --- connection lifecycle ---------------------------------------

    async def ensure_connected(self) -> None:
        if self.client and self.client.is_connected and self.hello is not None:
            return

        if (
            self._last_fail_at is not None
            and (time.monotonic() - self._last_fail_at) < FAIL_BACKOFF_S
        ):
            raise ConnectionError(
                f"device not found in last {int(FAIL_BACKOFF_S)} s "
                "(power-on Cardputer, launch the MCP app, then retry)"
            )

        async with self._connect_lock:
            # Re-check under the lock — another caller may have raced
            # us through scan/connect while we were waiting.
            if self.client and self.client.is_connected and self.hello is not None:
                return
            try:
                await self._connect()
                self._last_fail_at = None
            except Exception:
                self._last_fail_at = time.monotonic()
                raise

    async def _connect(self) -> None:
        addr = _load_cached_address()
        if addr:
            try:
                _log(f"connecting to cached address {addr}")
                await self._open_client(addr)
                return
            except (BleakError, asyncio.TimeoutError, ConnectionError) as e:
                _log(f"cached address failed ({e}); falling back to scan")

        addr, name = await self._scan()
        if addr is None:
            raise ConnectionError("no Cardputer-MCP device found in BLE scan")

        _log(f"connecting to discovered {name} ({addr})")
        await self._open_client(addr)
        _save_cached_address(addr, name or "")

    async def _scan(self) -> tuple[Optional[str], Optional[str]]:
        _log(f"scanning for {NAME_PREFIX}* ({SCAN_TIMEOUT_S} s)")
        try:
            # `return_adv=True` makes discover() return a dict
            # {addr: (device, AdvertisementData)} so we can read RSSI
            # and pick the strongest signal when multiple devices are
            # in range.
            discovered = await BleakScanner.discover(
                timeout=SCAN_TIMEOUT_S,
                return_adv=True,
            )
        except BleakError as e:
            _log(f"scan failed: {e}")
            return None, None

        candidates: list[tuple[int, str, str]] = []
        for addr, (device, adv) in discovered.items():
            name = device.name or (adv.local_name if adv else "") or ""
            # Two routes to discovery: name prefix (active scan) or
            # service UUID (passive scan). The device tries to put both
            # in its advertising payload but the radio sometimes
            # rejects rich payloads — see the cascade fallback in
            # `_advertise` on the device side.
            adv_uuids = [str(u).lower() for u in (adv.service_uuids or [])] if adv else []
            if name.startswith(NAME_PREFIX) or SERVICE_UUID in adv_uuids:
                rssi = adv.rssi if (adv and adv.rssi is not None) else -127
                candidates.append((rssi, addr, name or "Cardputer"))

        if not candidates:
            return None, None

        # Strongest RSSI wins. If two devices have the same RSSI, the
        # tuple comparison falls through to address, which is arbitrary
        # but stable — fine for a tiebreaker we don't expect to hit.
        candidates.sort(reverse=True)
        _, addr, name = candidates[0]
        return addr, name

    async def _open_client(self, addr: str) -> None:
        # Tear down any prior client. We've seen bleak hold a "live"
        # client reference that returns is_connected=False but still
        # refuses a new connect() until explicitly disconnected.
        if self.client is not None:
            with suppress(Exception):
                await self.client.disconnect()

        self._rx_buf = bytearray()
        # Fail any RPCs that were somehow still pending from a prior
        # connection — they'd never resolve against a fresh peer.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("reconnecting"))
        self._pending.clear()
        self.hello = None
        self._hello_event.clear()

        self.client = BleakClient(
            addr, disconnected_callback=self._on_disconnect_sync
        )
        await self.client.connect()
        await self.client.start_notify(TX_UUID, self._on_tx)

        # The device sends a `hello` event a moment after the central
        # subscribes to TX. If it doesn't arrive within HELLO_TIMEOUT_S
        # we're probably talking to something that looks like our
        # service but isn't (or an old firmware that doesn't speak the
        # current protocol). Abort the connection so the next call
        # retries cleanly.
        try:
            await asyncio.wait_for(
                self._hello_event.wait(), timeout=HELLO_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            with suppress(Exception):
                await self.client.disconnect()
            raise ConnectionError(
                "device connected but didn't send hello within "
                f"{HELLO_TIMEOUT_S} s (wrong firmware?)"
            )

        caps = (self.hello or {}).get("caps") or []
        _log(f"connected; caps={caps}; mtu={(self.hello or {}).get('mtu')}")

    def _on_disconnect_sync(self, _client: BleakClient) -> None:
        # bleak calls this synchronously from its own thread/loop.
        # Resolve in-flight futures with an error so the tools return
        # promptly rather than hanging on their wait_for(). Future
        # callbacks scheduled via set_exception run on the loop that
        # owns the future, so this is safe across threads.
        _log("BLE disconnected")
        self.hello = None
        self._hello_event.clear()
        # Drop cached telemetry so device_status can't report a stale dnd /
        # battery for a device that's no longer on the other end of the link.
        self.last_heartbeat = None
        self.last_heartbeat_at = None
        for mid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(
                    ConnectionError("device disconnected mid-call")
                )
        self._pending.clear()

    # --- inbound stream ---------------------------------------------

    def _on_tx(self, _char, data: bytearray) -> None:
        """Called by bleak whenever the device pushes bytes on TX.

        TX is chunked at 20 bytes by the device to stay under the
        default ATT MTU; we accumulate until we see a `\\n` and then
        parse one JSON object per line.
        """
        self._rx_buf.extend(data)
        while b"\n" in self._rx_buf:
            line, _, rest = self._rx_buf.partition(b"\n")
            self._rx_buf = bytearray(rest)
            try:
                msg = json.loads(line.decode())
            except (ValueError, UnicodeError) as e:
                _log(f"bad TX line: {e!r} raw={bytes(line)!r}")
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        if "event" in msg:
            ev = msg["event"]
            if ev == "hello":
                self.hello = msg
                self._hello_event.set()
            elif ev == "heartbeat":
                # Device liveness + DND/battery telemetry (fw 0.4.1+). Cache it
                # so `device_status` can report without waking the radio. A
                # pre-0.4.1 device simply never sends these, and the tool falls
                # back to connection + hello info.
                self.last_heartbeat = msg
                self.last_heartbeat_at = time.monotonic()
            else:
                _log(f"unknown event: {ev}")
            return

        if "ack" in msg:
            mid = msg.get("id")
            if not isinstance(mid, str):
                # Hello/heartbeat won't have ids, but a malformed ack
                # without one is a protocol error from the device.
                _log(f"ack without id: {msg!r}")
                return
            if msg.get("pending"):
                # Delivery confirmation — the device has received the
                # request but the resolution will arrive later (after
                # user input or timeout). Don't resolve the future yet.
                return
            fut = self._pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return

        _log(f"unknown TX shape: {msg!r}")

    # --- outbound RPC ------------------------------------------------

    async def send(
        self,
        cmd: str,
        payload: dict,
        rpc_timeout_s: float = DEFAULT_RPC_TIMEOUT_S,
        agent: str = "mcp-client",
    ) -> dict:
        """Send one command, await its ack. Returns the ack dict.

        On no-connection / write-fail / timeout, returns a synthetic
        ack with `ok: false` and an `err` so the tool layer can map
        cleanly to a user-visible string without bubbling exceptions.
        """
        try:
            await self.ensure_connected()
        except ConnectionError as e:
            return {"ack": cmd, "ok": False, "err": f"unavailable: {e}"}
        assert self.client is not None

        # Capability gate: tools the device doesn't advertise in
        # `hello.caps` short-circuit here without ever hitting the
        # radio. Older firmware staying compatible with newer tools
        # is the whole reason we negotiate caps.
        caps = (self.hello or {}).get("caps") or []
        if caps and cmd not in caps and cmd not in ("ping", "cancel"):
            return {
                "ack": cmd,
                "ok": False,
                "err": f"device firmware does not advertise '{cmd}'",
            }

        mid = uuid.uuid4().hex[:8]
        msg = {"cmd": cmd, "id": mid, "agent": agent, **payload}
        data = (json.dumps(msg) + "\n").encode()

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut

        try:
            mtu = (self.hello or {}).get("mtu") or 20
            async with self._write_lock:
                for i in range(0, len(data), mtu):
                    await self.client.write_gatt_char(
                        RX_UUID, data[i : i + mtu], response=False
                    )
        except (BleakError, OSError) as e:
            self._pending.pop(mid, None)
            return {"ack": cmd, "ok": False, "err": f"ble write failed: {e}"}

        try:
            return await asyncio.wait_for(fut, timeout=rpc_timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            # Best-effort: tell the device to abandon the request so
            # the LCD doesn't sit on a stale question.
            with suppress(Exception):
                cancel = (
                    json.dumps({"cmd": "cancel", "id": uuid.uuid4().hex[:8], "target_id": mid})
                    + "\n"
                ).encode()
                async with self._write_lock:
                    for i in range(0, len(cancel), mtu):
                        await self.client.write_gatt_char(
                            RX_UUID, cancel[i : i + mtu], response=False
                        )
            return {"ack": cmd, "ok": False, "err": "rpc timeout"}
        except ConnectionError as e:
            return {"ack": cmd, "ok": False, "err": str(e)}


# ---- MCP surface ---------------------------------------------------

bridge = Bridge()
mcp = FastMCP("cardputer")


def _notify_min_interval_s() -> float:
    """Read the per-agent notify floor from the environment (seconds).

    Default 60 s; set CARDPUTER_NOTIFY_MIN_INTERVAL_S=0 to disable. A
    malformed value falls back to the default rather than crashing the daemon.
    """
    try:
        return float(os.environ.get("CARDPUTER_NOTIFY_MIN_INTERVAL_S", "60"))
    except ValueError:
        return 60.0


# Backstop for the "default to silence" etiquette the companion skill asks of
# agents (see ratelimit.py). Keyed by the token-derived agent label so one
# chatty agent can't bury the device or starve another agent's alerts. `crit`
# notifies and the blocking tools (ask/confirm) bypass it at the call site.
_notify_limiter = MinIntervalLimiter(_notify_min_interval_s())


def _audit_log_path() -> Optional[Path]:
    """Resolve where `confirm` decisions are logged (see audit.py).

    Defaults to ~/.cardputer-mcp/audit.log — a sibling of the pairing cache, so
    the daemon's private state stays in one place. Set CARDPUTER_AUDIT_LOG to
    an explicit path to relocate it, or to an empty string to disable the
    consent trail entirely (some operators won't want any on-disk record).
    """
    raw = os.environ.get("CARDPUTER_AUDIT_LOG")
    if raw is None:
        return PAIR_CACHE_DIR / "audit.log"
    raw = raw.strip()
    return Path(raw).expanduser() if raw else None


# Append-only JSONL trail of every `confirm` decision (agent, title, the
# action-diff the user approved, outcome, hold duration). The honest,
# non-cryptographic first rung of the signed-consent-receipts roadmap item:
# it answers "what did an agent get me to approve, and when?" long after the
# physical gesture is over. Best-effort — a log failure never breaks a confirm.
_audit = ConsentAuditLog(_audit_log_path(), warn=_log)

# Populated by build_http_app() in HTTP mode: maps bearer token -> agent
# label. Read by _agent_label() so the device banner can show *which*
# agent is asking. Empty in stdio mode (there's no HTTP request to read a
# token from), where the label falls back to "local".
_TOKEN_MAP: dict[str, str] = {}


def _agent_label(ctx) -> str:
    """Resolve the requesting agent's banner label from its bearer token.

    The label is derived from WHICH token authenticated (mapped in
    `_TOKEN_MAP`), not from anything the caller can put in the tool
    arguments — so a misled or injected agent can't forge its own
    identity on the device's `ask`/`confirm` screen. stdio mode (no HTTP
    request) resolves to "local".
    """
    if ctx is None:
        return "local"
    req = getattr(getattr(ctx, "request_context", None), "request", None)
    if req is None:
        return "local"
    label = label_for_authorization(req.headers.get("authorization"), _TOKEN_MAP)
    return label or "agent"


@mcp.tool()
async def notify(
    ctx: Context,
    title: str,
    body: str = "",
    urgency: Literal["info", "warn", "crit"] = "info",
) -> str:
    """Display a non-blocking notification on the user's Cardputer.

    The Cardputer is a credit-card-sized handheld device the user
    carries with them. Use this tool when you want the user to glance
    at something — a status update, a result, a heads-up — without
    interrupting their main screen.

    Returns once the notification is shown on the device. The
    Cardputer LCD is 240×135 pixels, so keep `title` to ~20 characters
    and `body` to ~3 short lines. `urgency` controls the alert sound:
    'info' is a soft chirp, 'warn' is a louder double-beep, 'crit'
    is an urgent triple-beep. Prefer 'info' for most uses; reserve
    'crit' for things the user needs to react to within seconds.

    Do not call this in rapid succession. The daemon enforces a per-agent
    floor (default ~1 non-critical notify per 60 s, set by
    CARDPUTER_NOTIFY_MIN_INTERVAL_S): a non-critical notify sent inside that
    window returns 'rate-limited' without reaching the device. 'crit'
    notifications always bypass the floor, so reserve 'crit' for things the
    user must react to within seconds. Returns 'shown', 'rate-limited',
    'dnd', 'unavailable: <reason>', or 'failed: <reason>'.
    """
    title = title[:64]
    body = body[:240]
    agent = _agent_label(ctx)
    # Per-agent backstop for the "default to silence" etiquette: a
    # non-critical notify from an agent that just buzzed is dropped before it
    # reaches the radio, so one chatty agent can't bury the device. `crit`
    # always rings — a real emergency is exactly what the floor must not eat.
    if urgency != "crit" and not _notify_limiter.allow(agent):
        return "rate-limited"
    # Notify is non-blocking on the device, so the RPC should resolve
    # within milliseconds. 10 s is generous slack for radio + device
    # render — if it exceeds that something is wrong.
    result = await bridge.send(
        "notify",
        {"title": title, "body": body, "urgency": urgency},
        rpc_timeout_s=10,
        agent=agent,
    )
    if result.get("ok"):
        return "shown"
    if result.get("dnd"):
        # Device is in Do Not Disturb; a non-critical notify was
        # suppressed. Surface it so the agent knows it wasn't seen.
        return "dnd"
    err = result.get("err", "unknown")
    if err.startswith("unavailable"):
        return err
    return f"failed: {err}"


@mcp.tool()
async def ask(
    ctx: Context,
    question: str,
    choices: list[str],
    timeout_s: int = 60,
) -> str:
    """Ask the user a multiple-choice question on their Cardputer.

    BLOCKING — returns once the user presses a number key (1–4) on
    the device's QWERTY keyboard, or after `timeout_s` seconds have
    elapsed. Use when you need a quick decision from the user and
    don't want to interrupt their main screen, especially if they
    might be away from their laptop.

    The Cardputer LCD is 240×135 pixels, so keep `question` short
    (~60 chars wraps to 2 lines) and provide 2–4 short choices (each
    ≤ ~32 chars). The user picks by pressing the digit that matches
    their choice; ESC on the device cancels. Returns one of:

      - the exact choice string the user selected
      - 'timeout' if the user didn't respond in `timeout_s` seconds
      - 'cancelled' if the user pressed ESC on the device, or if
         a follow-up cancel was requested
      - 'unavailable: <reason>' if the device is not connected

    Prefer this over blocking your assistant message with a question
    when the user might not be at their laptop. Do NOT use this for
    destructive operations — call the `confirm` tool (iter 3+)
    instead, which requires a physical hold-to-confirm gesture.
    """
    if len(choices) < 2:
        return "error: need at least 2 choices"
    if len(choices) > 4:
        return "error: at most 4 choices (LCD is small)"
    if timeout_s < 1 or timeout_s > 600:
        return "error: timeout_s must be between 1 and 600"

    # RPC timeout is the device's own input timeout + 10 s slack for
    # radio jitter and the device's grace window. Without slack the
    # host can race the device and return "rpc timeout" when the user
    # genuinely was about to answer.
    rpc_timeout = timeout_s + 10
    result = await bridge.send(
        "ask",
        {
            "question": question[:120],
            "choices": [str(c)[:32] for c in choices],
            "timeout_s": timeout_s,
        },
        rpc_timeout_s=rpc_timeout,
        agent=_agent_label(ctx),
    )

    if result.get("ok") and "choice" in result:
        return str(result["choice"])
    if result.get("timed_out"):
        return "timeout"
    if result.get("cancelled"):
        return "cancelled"
    if result.get("dnd"):
        return "dnd"
    err = result.get("err", "unknown")
    if err.startswith("unavailable"):
        return err
    return f"failed: {err}"


@mcp.tool()
async def confirm(
    ctx: Context,
    title: str,
    details: str = "",
    timeout_s: int = 30,
) -> str:
    """Demand physical confirmation from the user before executing a
    destructive operation.

    This tool is for IRREVERSIBLE actions only — production deploys,
    force pushes, DROP TABLE / DELETE without WHERE, unstaged-file
    deletions, financial transactions, paid API calls with large
    side effects, etc. The user must complete a sustained ~3-second
    physical gesture on the Cardputer's Y key: on the current firmware
    the hardware keyboard emits no auto-repeat, so the gesture is
    rapid Y tapping (the on-screen prompt says "TAP Y fast for 3s") —
    a progress bar fills as they tap and resets if they stop for more
    than ~300 ms. A single tap is not enough.

    The point is that no amount of tool-output content or prompt
    injection can synthesize a sustained burst of physical key events.
    If you're about to do something the user couldn't un-do in a
    minute, use this instead of trusting an `ask` or your own
    assistant-message confirmation.

    PASS `details` whenever you can. It is the *actual content* being
    approved — the real shell command, the SQL statement, a short diff
    hunk, the payee + amount, or the list of files. On firmware that
    supports it (fw >= 0.4.0) `details` is rendered in a scrollable box
    ABOVE the gesture, so the user approves *what they read*, not just an
    18-character title — the hardware-wallet model. Keep it to the
    essential ~256 characters (it's truncated past that); strip noise so
    the operation is legible on a 240×135 screen. NOTE: `details` is
    text you supply, so it adds *legibility of intent* — it is not a
    cryptographic proof, and the un-forgeable consent remains the
    physical hold. Older firmware ignores `details` and shows the title
    only, so the `title` must still stand on its own.

    Returns one of:
      - 'confirmed (held <N> ms)' — user completed the ~3 s physical Y gesture
      - 'cancelled' — user pressed N or ESC on the device
      - 'timeout' — user did not respond within `timeout_s` seconds
      - 'unavailable: <reason>' — device not connected

    `title` should fit roughly 18 characters on the device's 240×135
    LCD ("FORCE PUSH origin/main" or "DROP customers"). Keep it
    declarative. The user reading this on a tiny screen must
    instantly recognize the operation.

    Do NOT use this for routine yes/no decisions — that's what
    `ask` is for. Do NOT call this rapidly; every invocation demands
    a deliberate 3-second physical gesture, which is exhausting if
    abused. Reserve this for the handful of actions per session
    where wrong = bad.
    """
    title = title[:64]
    # Preserve internal whitespace (diffs/commands are indentation-sensitive),
    # but cap the length. The strip()-check below decides whether to send it.
    details = str(details)[:_CONFIRM_DETAILS_MAX]
    if timeout_s < 5 or timeout_s > 120:
        return "error: timeout_s must be between 5 and 120"

    # RPC timeout is the device's own deadline + slack for radio
    # jitter and the device's hold-detection grace window. Without
    # slack the host can race the device and report rpc-timeout
    # while the user is mid-hold.
    rpc_timeout = timeout_s + 10
    payload = {"title": title, "danger": True, "timeout_s": timeout_s}
    # Send `details` only when it carries real content (strip()-check) so an
    # empty/whitespace string never bloats the BLE line or renders as blank
    # rows. Old firmware ignores the field; new firmware renders it as a
    # scrollable action diff (capability `confirm_details`).
    if details.strip():
        payload["details"] = details
    agent = _agent_label(ctx)
    result = await bridge.send(
        "confirm",
        payload,
        rpc_timeout_s=rpc_timeout,
        agent=agent,
    )

    # Classify the outcome once, then both return it to the caller and append
    # it to the consent audit trail. Every branch — approved, denied, timed
    # out, unreachable — is a decision worth recording; only the pre-send
    # validation errors above (which never reached the device) are not.
    if result.get("ok") and result.get("confirmed"):
        # We surface the recorded hold duration to encourage tools
        # that want to log it — most callers will just check the
        # 'confirmed' prefix and move on.
        hold_ms = result.get("hold_ms", 0)
        outcome, ret = "confirmed", f"confirmed (held {hold_ms} ms)"
    elif result.get("cancelled"):
        hold_ms, outcome, ret = None, "cancelled", "cancelled"
    elif result.get("timed_out"):
        hold_ms, outcome, ret = None, "timeout", "timeout"
    else:
        hold_ms = None
        err = result.get("err", "unknown")
        if err.startswith("unavailable"):
            outcome, ret = "unavailable", err
        else:
            outcome, ret = "failed", f"failed: {err}"

    _audit.record(
        tool="confirm",
        agent=agent,
        title=title,
        details=details if details.strip() else None,
        outcome=outcome,
        hold_ms=hold_ms,
    )
    return ret


@mcp.tool()
async def show(
    ctx: Context,
    text: str,
    channel: str = "",
) -> str:
    """Update a single ambient status line on the user's Cardputer.

    Non-blocking, silent, and unobtrusive — unlike `notify`, this makes no
    sound and never takes over the screen. It writes one line to a small
    status area on the device's idle screen so the user can *glance* at their
    pocket and see what you're doing right now ("running pytest", "wrote
    auth.py", "idle ok"). Think of it as a status bar, not an alert.

    Use it to keep a live heartbeat of a long task visible without buzzing the
    user. Call it as your work progresses; each call replaces the previous
    line for the same `channel`. `channel` is a short tag (defaults to your
    agent label) so several agents can each own a line — the device keeps the
    most recent few. Keep `text` to ~40 characters (the LCD is 240×135).

    Because it's ambient it does NOT honor Do Not Disturb (there's nothing to
    disturb: no sound, no takeover) and it is NOT rate-limited — but update at
    a human-readable cadence, not on every token.

    Returns 'shown', 'unavailable: <reason>' if the device isn't connected, or
    'failed: <reason>' (e.g. older firmware that doesn't support `show`).
    """
    text = str(text)[:48]
    agent = _agent_label(ctx)
    # Channel defaults to the (unforgeable, token-derived) agent label so each
    # agent owns its own status line without having to invent a tag.
    channel = (str(channel).strip() or agent)[:16]
    result = await bridge.send(
        "show",
        {"text": text, "channel": channel},
        rpc_timeout_s=5,
        agent=agent,
    )
    if result.get("ok"):
        return "shown"
    err = result.get("err", "unknown")
    if err.startswith("unavailable"):
        return err
    return f"failed: {err}"


@mcp.tool()
async def progress(
    ctx: Context,
    label: str,
    percent: int,
    channel: str = "",
) -> str:
    """Show a live progress bar (0–100%) on the user's Cardputer.

    The visual sibling of `show`: non-blocking, silent, never takes over the
    screen. Instead of a text line it draws a labeled bar that fills as the
    `percent` you pass climbs, so the user can glance at their pocket and see
    *how far along* a long task is — building, downloading, a test sweep, a
    multi-step migration. Think progress indicator, not alert.

    Call it repeatedly as the work advances (e.g. 0, 25, 60, 100). Each call
    replaces the bar for the same `channel`, a short tag that defaults to your
    agent label so several agents can each own a bar — the device keeps the
    most recent few. `percent` is clamped to 0..100. Keep `label` short (~12
    chars share the row with the bar; the LCD is 240×135). A `progress` and a
    `show` on the same channel share one slot, latest wins — so you can flip a
    channel from a status line to a bar and, at 100%, back to "done".

    Because it's ambient it does NOT honor Do Not Disturb (nothing to disturb:
    no sound, no takeover) and is NOT rate-limited — but update at a
    human-readable cadence, not on every token.

    Returns 'shown', 'unavailable: <reason>' if the device isn't connected, or
    'failed: <reason>' (e.g. firmware older than 0.4.2 that lacks `progress`).
    """
    try:
        pct = int(percent)
    except (TypeError, ValueError):
        return "error: percent must be an integer 0-100"
    pct = 0 if pct < 0 else 100 if pct > 100 else pct
    label = str(label)[:48]
    agent = _agent_label(ctx)
    # Channel defaults to the (unforgeable, token-derived) agent label so each
    # agent owns its own bar without inventing a tag — mirrors `show`.
    channel = (str(channel).strip() or agent)[:16]
    result = await bridge.send(
        "progress",
        {"label": label, "percent": pct, "channel": channel},
        rpc_timeout_s=5,
        agent=agent,
    )
    if result.get("ok"):
        return "shown"
    err = result.get("err", "unknown")
    if err.startswith("unavailable"):
        return err
    return f"failed: {err}"


@mcp.tool()
async def device_status() -> str:
    """Report whether the user's Cardputer is reachable and its current state.

    READ-ONLY and passive: returns the bridge's *cached* view — it does NOT
    wake the radio, scan, or block. Call it to decide whether to interrupt the
    user: if the device is offline or in Do Not Disturb, prefer staying quiet
    (or fall back to chat) rather than firing a `notify`/`ask` that will bounce.
    Also handy to learn which tools the connected firmware supports (`caps`).

    Returns a one-line summary, e.g.
      'online; dnd=off; fw=0.4.2;
       caps=notify,ask,confirm,confirm_details,show,progress;
       uptime=142s; battery=87%; heartbeat 3s ago'
    or 'offline (device off, out of BLE range, or not yet connected this
    session)'.

    The view is only as fresh as the last heartbeat (~10 s) or the last tool
    call — to *actively* reach the device, just call notify/ask/confirm, which
    connect on demand and fail closed if it's unreachable.
    """
    connected = bool(
        bridge.client and bridge.client.is_connected and bridge.hello
    )
    if not connected:
        return (
            "offline (device off, out of BLE range, or not yet connected "
            "this session)"
        )

    hello = bridge.hello or {}
    hb = bridge.last_heartbeat or {}
    parts = ["online"]

    dnd = hb.get("dnd")
    parts.append("dnd=" + ("on" if dnd else "off") if dnd is not None else "dnd=unknown")

    ver = hello.get("version")
    if ver:
        parts.append("fw=" + str(ver))
    caps = hello.get("caps") or []
    if caps:
        parts.append("caps=" + ",".join(str(c) for c in caps))

    up = hb.get("uptime")
    if isinstance(up, (int, float)):
        parts.append("uptime={}s".format(int(up)))

    bat = hb.get("bat")
    if isinstance(bat, dict) and isinstance(bat.get("pct"), (int, float)):
        charging = " (charging)" if bat.get("usb") else ""
        parts.append("battery={}%{}".format(int(bat["pct"]), charging))

    if bridge.last_heartbeat_at is not None:
        age = max(0, int(time.monotonic() - bridge.last_heartbeat_at))
        parts.append("heartbeat {}s ago".format(age))

    return "; ".join(parts)


# ---- HTTP transport (the cloud-bridge path, via an MCP tunnel) ------


def _allowed_hosts(
    host: str,
    port: int,
    tunnel_domain: Optional[str] = None,
    extra: Optional[list] = None,
) -> list:
    """Build the Host allow-list for the streamable-HTTP transport.

    The MCP transport does DNS-rebinding protection: it 421s any `Host`
    header not in this list. Its matcher only does EXACT matches and
    `host:*` port wildcards — it does NOT understand `*.domain` prefix
    wildcards, so we must enumerate the concrete hosts the daemon can
    actually receive:

    - loopback `127.0.0.1:port` / `localhost:port` — local Claude Code.
    - `cardputer.<tunnel_domain>` — what the tunnel's mcp-proxy forwards
      when it routes the `cardputer` subdomain (the bare domain too, in
      case a proxy preserves it differently).
    - `host.docker.internal[:port]` — in case the proxy rewrites Host to
      the upstream target instead of preserving the original.
    - `CARDPUTER_ALLOWED_HOSTS` (comma-separated) — escape hatch: if you
      ever see "Invalid Host header: X" in the proxy/daemon logs, add X
      here without a code change.

    This is defense-in-depth; the bearer token (checked first, in
    BearerAuthMiddleware) is the real gate.
    """
    allowed = [
        f"{host}:{port}",
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"host.docker.internal:{port}",
        "host.docker.internal",
    ]
    if tunnel_domain:
        allowed += [tunnel_domain, f"cardputer.{tunnel_domain}"]
    env_extra = os.environ.get("CARDPUTER_ALLOWED_HOSTS")
    if env_extra:
        allowed += [h.strip() for h in env_extra.split(",") if h.strip()]
    if extra:
        allowed += list(extra)
    return allowed


def build_http_app(
    token_map: dict,
    host: str = "127.0.0.1",
    port: int = 9000,
    tunnel_domain: Optional[str] = None,
    extra_allowed_hosts: Optional[list] = None,
):
    """Build the streamable-HTTP ASGI app for the same three tools.

    Reuses the module-level `mcp`/`bridge` verbatim — only the transport
    changes. Two things are load-bearing and easy to get wrong:

    1. **Host allow-list** (see `_allowed_hosts`) — without the right
       entries the transport 421s tunneled requests via DNS-rebinding
       protection.
    2. **Bearer auth.** The tunnel does not authenticate to us, so we wrap
       the app in BearerAuthMiddleware (see auth.py). It's added last, so
       it runs FIRST (outermost) — auth before transport-security.

    `token_map` is stashed in the module global so the tools can resolve a
    request's token to an agent label for the device banner.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    from auth import BearerAuthMiddleware

    global _TOKEN_MAP
    _TOKEN_MAP = token_map

    mcp.settings.host = host
    mcp.settings.port = port
    # Keep DNS-rebinding host protection on (defense-in-depth). We leave
    # allowed_origins at its default ([]): server-side callers (Managed
    # Agents, the Messages API) send no Origin header, which always passes;
    # the matcher has no real wildcard for origins anyway, and bearer auth
    # is the actual gate.
    mcp.settings.transport_security = TransportSecuritySettings(
        allowed_hosts=_allowed_hosts(host, port, tunnel_domain, extra_allowed_hosts)
    )

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, token_map=token_map)
    return app


# ---- entrypoint -----------------------------------------------------


def main() -> None:
    _log(f"starting (pid={os.getpid()})")
    # Default transport is stdio, which is what `claude mcp add` (no
    # --transport) expects — the original local-only path. Setting
    # CARDPUTER_HTTP=1 switches to the streamable-HTTP daemon that an MCP
    # tunnel exposes to cloud agents AND that local Claude Code can reach
    # over loopback (`claude mcp add --transport http`). One BLE owner,
    # one gate, both transports.
    if os.environ.get("CARDPUTER_HTTP"):
        import uvicorn

        from auth import parse_token_map

        host = os.environ.get("CARDPUTER_HTTP_HOST", "127.0.0.1")
        port = int(os.environ.get("CARDPUTER_HTTP_PORT", "9000"))
        token_map = parse_token_map(os.environ.get("CARDPUTER_TOKENS"))
        tunnel_domain = os.environ.get("CARDPUTER_TUNNEL_DOMAIN")
        if not token_map:
            _log(
                "WARNING: CARDPUTER_TOKENS is empty — every HTTP request "
                "will be rejected 401 (fail-closed). Set token=label pairs."
            )
        app = build_http_app(
            token_map, host=host, port=port, tunnel_domain=tunnel_domain
        )
        _log(f"http transport on {host}:{port} (tunnel_domain={tunnel_domain})")
        uvicorn.run(app, host=host, port=port, log_config=None)
        return

    # stdio (legacy / local fallback)
    mcp.run()


if __name__ == "__main__":
    main()
