# CLAUDE.md

Working notes for Claude Code in this repo. The owner (Jörg) is moving his
Cardputer work out of browser-Claude chats and into Claude Code (Desktop
app), and is rebuilding project context here. This file travels with the
repo; there is also per-project auto-memory (`MEMORY.md`) that may be more
current — check both.

## What this repo is

A DIY MicroPython "OS" + cloud bundle for the **M5Stack Cardputer-Adv**.
The full tour is in [`README.md`](README.md); device-side layout and
iteration tooling in [`buddy/README.md`](buddy/README.md). Read those for
detail rather than duplicating them here.

Big pieces:
- `buddy/device/` — MicroPython launcher (`main.py`) + the apps that run on
  the Cardputer. Shared peer modules (`buddy_ble`, `buddy_protocol`,
  `buddy_state`, `buddy_chars`, `buddy_ui_cp`) live alongside.
- `mcp/` — host-side `bleak` BLE bridge exposing `notify`/`ask`/`confirm`
  to any MCP client.
- `worker/` — Cloudflare Worker (voice STT + chat memory; Pager backend).
- `tunnel/`, `mac/` — the cloud-agent path (MCP tunnel + launchd jobs).
- `.claude/skills/m5-onboard/` — flash + push tooling ("m5-onboard go").
- `.claude/skills/cardputer-companion/` — runtime etiquette for the MCP tools.

## The owner's hardware (not derivable from the repo)

- **Device:** M5Stack Cardputer-Adv with the **STAMP S3A** core module
  (ESP32-S3FN8, 8MB flash, no PSRAM, 512KB SRAM). The STAMP is the brain
  (CPU + RAM + flash + WiFi + BLE + PCB antenna, all RF certification on the
  module); the Cardputer body is just keyboard / LCD (240×135) / speaker /
  mic / USB-C wired to STAMP GPIO. The STAMP is swappable.
- **On order, not yet arrived:** M5Stack **Cap LoRa-1262** — SX1262 868MHz
  LoRa + ATGM336H GNSS (two independent chips). The UIFlow firmware already
  ships `cap/lora1262` and `driver/atgm336h`. **Roadmap:** extend
  `radar_love` with LoRa/GNSS once the cap arrives; a mobile Meshtastic node
  is the longer-term idea. Grove Port.A (I²C) stays free for a sensor.
- Pis the device knows about: **NC-Pi5**, **TaSc-Pi5** (pi_dashboard targets).

## Apps in `buddy/device/apps/`

- **claude_buddy** — BLE permission prompts from Claude Desktop's Hardware
  Buddy; chirps the speaker on a new prompt (`take_alert` / `_beep_confirm`).
- **radar_love** — WiFi/BLE scanner with detail views + a channel-map page.
- **webkey** — a *parked* WiFi-keyboard experiment. See the memory note
  `webkey-experiment-status` before trusting any external summary of it
  (an inaccurate browser-Claude summary circulates).
- **pi_dashboard**, **snake**, **particle_life** — launcher extras.

`buddy/device/apps - backup/` is an old snapshot; the live apps are in
`apps/`. Some apps from the README (push_to_claude, pager, cardputer_mcp)
live in the backup dir / are pushed separately — confirm what's actually in
`apps/` before assuming an app is installed.

## Dev workflow on this machine (Windows)

- Push changed device files **without re-flashing**:
  ```
  python .claude/skills/m5-onboard/scripts/install_apps.py --port COM5 --src buddy --files <name.py> ...
  ```
  Omit `--files` to push the whole bundle. The Cardputer enumerates as
  **COM5** (VID_303A native USB-CDC); after a reset it can land on another
  COMx — re-check with `Get-PnpDevice -Class Ports | ? InstanceId -match VID_303A`.
- **Port-busy gotcha:** the push fails with `Toegang geweigerd` / "could not
  open port" if anything holds the serial port. Before pushing: disconnect
  the Hardware Buddy in Claude Desktop and leave the Cardputer on the
  **launcher menu** (not inside a running app), so the USB-CDC REPL is free.
- A file in `/flash/apps/` (e.g. `claude_buddy.py`) imports peer modules
  from `/flash/` (e.g. `buddy_protocol.py`) — if you change an app *and* a
  peer module, push **both**, or the app crashes on a missing symbol.
- Device app conventions: `import M5` (never `from M5 import *`), DejaVu9
  font, 24-bit RGB palette, direct LCD drawing (no canvas/blit), and the
  `try: run() finally: sys.modules.pop(__name__)` clean return-to-launcher.
  Crib `hello_cardputer.py` (in the backup dir) for the smallest example.

## Working with this owner

- Commit and push only when asked; he reviews and approves outward actions
  explicitly. He works in Dutch.
