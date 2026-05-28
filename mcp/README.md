# cardputer-mcp

A Model Context Protocol server that gives any Claude (Code, Desktop,
Managed Agents — anything that speaks MCP) a physical channel to the
user via their Cardputer.

The Cardputer becomes the agent's pocket pager: it can buzz the user,
ask a multiple-choice question, demand physical confirmation for
destructive operations, dictate via the mic, and display ambient
status — all without the user needing to refocus to their laptop.

## Status — iteration 3 (confirm gesture; the 2FA-for-AI moment)

The host-side bridge speaks Bluetooth Low Energy via `bleak` to the
`cardputer_mcp.py` device app. Three tools work end-to-end:

| Tool                                | iter 2                           | iter 3                             | iter 4                      |
| ----------------------------------- | -------------------------------- | ---------------------------------- | --------------------------- |
| `notify(title, body, urgency)`      | ✅ visual banner + speaker chirp | rate-limit, per-agent tags         | —                           |
| `ask(question, choices, timeout_s)` | ✅ blocks on QWERTY input        | DND awareness                      | —                           |
| `confirm(title, timeout_s)`         | —                                | ✅ TAP-Y-fast 3 s physical gesture | —                           |
| `show(text, channel)`               | —                                | —                                  | ambient line on LCD         |
| `dictate(prompt, max_seconds)`      | —                                | —                                  | mic → Worker/Whisper → text |

`confirm` is the differentiator. It demands a physical, sustained
gesture from the user before returning success — a prompt injection
or runaway agent loop cannot synthesize "hold a key for 3 seconds"
through any tool-output content. Use it for destructive operations
(deploys, force pushes, DROP TABLE, etc.). The hold timer resets the
instant the user releases Y; the gesture cannot be interrupted and
resumed by accident, and the device pre-empts any pending `ask` when
confirm arrives so a malicious tool result can't swap the screen
out from under the user.

## Architecture

```
┌─────────────────────────┐  stdio  ┌─────────────────────┐
│ Claude Code / Desktop / │◄───────►│  cardputer-mcp      │
│ Cursor / any MCP client │         │  (this directory,   │
│                         │         │   bleak transport)  │
└─────────────────────────┘         └──────────┬──────────┘
                                               │ BLE GATT
                                               │ Service: a5cd0001-…
                                               │ RX: a5cd0002-… (host → device)
                                               │ TX: a5cd0003-… (device → host)
                                               ▼
                                    ┌─────────────────────┐
                                    │   Cardputer         │
                                    │   buddy/device/apps │
                                    │   /cardputer_mcp.py │
                                    └─────────────────────┘
```

The BLE service uses a UUID block (`a5cd0001-…`) distinct from the
Nordic UART Service that Buddy uses (`6e400001-…`). The two apps
can't run on the device at the same time (you pick one from the
launcher, and exiting an app `machine.reset()`s the whole stack
anyway), but the separation lets the wire formats evolve
independently and means a future build could host both peripherals
side-by-side without contention.

Wire format is documented in
[`buddy/references/mcp_protocol.md`](../buddy/references/mcp_protocol.md).

## Why local stdio first (not the Cloudflare Worker)

Two reasons:

1. **Latency.** BLE is ~50–100 ms round-trip; a Worker hop adds
   internet RTT for no benefit when the user's laptop and pocket
   are in the same room. The whole point of the device is that
   the interaction feels instant.
2. **No new secrets.** Stdio runs as the user. The MCP client (Claude
   Code) talks to the device via BLE locally; nothing transits the
   public internet.

**Update (iter 5):** cloud agents that can't reach BLE are now served by
a second transport — a streamable-http daemon exposed through an
Anthropic **MCP tunnel** (outbound-only; no inbound ports). Local Claude
Code can use it too (`claude mcp add --transport http`), unifying both
onto one BLE owner and one physical `confirm` gate. The bespoke
Worker-bridge plan was dropped in favor of the tunnel. Setup lives in
[`/tunnel/`](../tunnel/); design in [`/docs/superpowers/`](../docs/superpowers/).

## Install + first run

```bash
cd mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Point Claude Code at the server. The full python path is required
# because Claude Code's MCP runner doesn't know about your venv.
claude mcp add cardputer "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

Push the device app, boot it, then in a fresh Claude Code session:

```bash
# Push apps to the device (no firmware re-flash needed).
python3 .claude/skills/m5-onboard/scripts/install_apps.py --port <PORT> --src buddy
```

Reboot the device, pick **cardputer_mcp** from the launcher menu.
The screen will read `waiting for bridge` until the first tool call
triggers the host to scan and connect. Once connected the screen
flips to a green `READY`.

Then in Claude Code, try:

> Use the cardputer notify tool to tell me 'tests passing'.

The Cardputer's screen flips to a notification banner, plays a soft
chirp, and auto-clears after 5 s. The tool returns `"shown"` to
Claude.

For `ask`:

> Use cardputer.ask to ask "deploy now?" with choices ["yes", "no"]
> and a 30 second timeout.

The device shows the question with numbered choices. Press `1` or
`2` on the Cardputer's QWERTY; Claude gets back the choice string.
Press `ESC` on the device to cancel — Claude gets `"cancelled"`.

### macOS Bluetooth permission

On macOS the first scan triggers a permission prompt. Approve it
once; bleak caches the grant for future sessions. If Claude Code
runs in a sandboxed context (some terminal multiplexers) you may
need to explicitly grant Bluetooth to the terminal app itself
under System Settings → Privacy & Security → Bluetooth.

### Connection caching

After the first successful connect, the device's BLE address is
saved to `~/.cardputer-mcp/paired.json`. Subsequent calls skip the
scan (faster startup). If the cached address fails (different
device, address changed), we fall back to a fresh scan and update
the cache.

### Backoff when device is off

If a scan finds no Cardputer, the bridge stops trying for 30 s. This
prevents every tool call from eating a 5 s scan timeout while the
device is simply not powered on — Claude gets a fast
`"unavailable: …"` instead.

## Tool descriptions matter

The single biggest lever for "does Claude actually call this tool
when it should?" is the tool description. The descriptions in
`server.py` are tuned for Claude — they include:

- a one-sentence summary of what the tool does
- a "use when…" line that names a specific scenario
- size constraints (240×135 is tiny — agents need to be told)
- the exact return-value contract

When iterating on these, validate by giving Claude a prompt like
"the user is away from their laptop, ask if they want to deploy"
and seeing whether it reaches for `cardputer.ask` unprompted. If
not, the description is too generic — make it more specific to the
scenario.

## Wire-protocol notes carried over from Buddy

The device-side code in `cardputer_mcp.py` mirrors `buddy_ble.py`'s
patterns in detail because the same hard-won lessons apply:

- **Init order is load-bearing.** `BLE()` → sleep → `active(True)` →
  settle → `config(gap_name)` → `gatts_register_services` → first
  `gap_advertise` → THEN `gatts_set_buffer`. Reordering produces
  silent failures (controller wedges, dropped bytes, payloads that
  refuse to advertise).
- **IRQ handlers buffer-and-dispatch.** RX bytes accumulate in a
  bytearray; complete lines are split on `\n` and handed up.
- **Re-advertise is scheduler-deferred.** Inline `gap_advertise`
  the instant a disconnect IRQ fires returns OSError(-30) on this
  build; we use `micropython.schedule` and a 150/300/450/600/750 ms
  staircase of retries.
- **Cascade advertising fallback.** Five payload shapes, from rich
  (UUID + name) to empty, so the device shows up SOMETHING even
  when NimBLE is wedged.
- **20-byte MTU chunking.** Default ATT MTU on ESP32 is 23 → 20
  bytes of payload. Both sides chunk every write; the receiver
  reassembles on `\n`.

See `buddy/references/ble_on_micropython.md` for the full list of
MicroPython BLE gotchas these patterns paper over.

## Known limitations

A short list of rough edges we're aware of in the iter-3 release.
None blocks normal use of `notify` or `ask`; `confirm` is usable
but its gesture is less polished than the brand promised.

- **`confirm` gesture is rapid Y taps, not a continuous hold.** On
  UIFlow 2.0 the MatrixKeyboard driver does not generate auto-repeat
  events while a key is held — `get_key()` returns a single event per
  physical press. So the device screen, the `confirm` tool docstring,
  and these docs all say **"TAP Y fast for 3s"**: the user taps Y
  repeatedly (inter-tap gap under ~300 ms) to fill the progress bar,
  which resets if they stop. The security property is unchanged — a
  sustained burst of physical key events still can't be synthesized by
  tool output or prompt injection. A future iter may probe for a
  held-key/pressed-state API and switch to a true continuous hold.
- **Device-side resolution acks for blocking tools sometimes don't
  reach the host.** When `ask` or `confirm` hits its own
  device-supplied `timeout_s`, the device's tick-fired `timed_out`
  ack is occasionally dropped before the host sees it; the host's
  RPC then fails with `rpc timeout` (timeout*s + 10s) instead of
  the cleaner device-driven `timeout`. Tool result for the caller
  is the same shape ("unavailable" / error), just with a less
  specific error message. Suspect: race between IRQ-context
  `_on_state(disconnected)` and main-loop `tick()` mutating
  `pending*\*` state. Tracking.
- **macOS Bluetooth permission prompt on first scan.** Approve it
  once per laptop; if Claude Code runs in a sandboxed terminal
  multiplexer you may need to grant Bluetooth to the terminal app
  itself under System Settings → Privacy & Security → Bluetooth.
- **Buddy and cardputer_mcp can't run simultaneously on the device.**
  They register different BLE services, but UIFlow's launcher
  hands the BLE controller to whichever app is active — and
  `machine.reset()` on app exit clears the stack anyway. Pick one
  from the launcher menu.

## Roadmap

- [x] iter 1: scaffold with stubbed transport
- [x] iter 2: real BLE transport; `notify` and `ask` end-to-end
- [x] iter 3: `confirm` with hold-Y-3s physical gesture
- [~] iter 4: DND switch ✅ + per-agent identity on the banner ✅;
  `show` (ambient line) / `dictate` (mic → Whisper) still pending
- [x] iter 5: **cloud agents via MCP tunnel** — a streamable-http daemon
      (`CARDPUTER_HTTP=1`) behind `cloudflared` + `mcp-proxy`, reached by
      Managed Agents / the Messages API. Replaces the originally-planned
      bespoke Worker bridge with Anthropic's productized MCP tunnel. See
      `/tunnel/` and `/docs/superpowers/`.
- [ ] iter 6: inverse direction — programmable launcher buttons
      that fire Managed Agents tasks
- [ ] later: signed-consent receipts, on-device action diff, multi-party
      quorum (documented future ladder in the design spec)

## License

Same as the parent project (Apache 2.0).
