# Verified Approval, Ambient Status & Notify Rate-Limit

**Date:** 2026-06-17
**Status:** Design — implementation in progress
**Topic:** Three additive, capability-gated upgrades to the Cardputer MCP
surface that move the device from _"approve **that** something happens"_ to
_"approve **what** happens, glance at what your agent is doing, and don't get
spammed."_ Builds directly on the `notify`/`ask`/`confirm` stack
(`mcp/server.py` ↔ `buddy/device/apps/cardputer_mcp.py`) and closes three
gaps the repo itself already documents.

---

## 0. Why these three

Each item closes a gap the codebase explicitly flags — this is finishing
documented roadmap, not bolting on novelty:

| Feature                                                                                                             | Gap it closes                                                          | Source                                                                                                               |
| ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| **Verified Approval** — `confirm(details=…)` renders the real command/diff/SQL/payee, scrollable, before the hold-Y | "agent lied in the title" / on-device action diff                      | design spec `2026-05-28…-design.md` §9 "Future ladder"; `mcp/README.md` roadmap "later"                              |
| **Ambient status** — implement the spec'd-but-unbuilt `show(text, channel)`                                         | iter-4 `show` line                                                     | `buddy/references/mcp_protocol.md` (`show` row, "_iter 4_"); `mcp/README.md` roadmap iter 4 "`show` … still pending" |
| **Per-agent notify rate-limit**                                                                                     | "do not assume the device will rate-limit you, because today it won't" | `.claude/skills/cardputer-companion/SKILL.md` §Core ethos; `mcp_protocol.md` "per-agent rate limit … _future_"       |

## 1. Non-goals (explicitly deferred)

- **Trusted-daemon-computed diffs.** v1 `details` is **agent-supplied** text.
  The daemon does not yet compute the real `git diff`/SQL itself. This still
  forces the actual content in front of a human (you approve what you _read_,
  not an 18-char title the agent wrote) but a malicious agent could craft
  misleading `details`. The trusted-source variant remains the next rung and
  is documented honestly — we do not oversell v1 as closing the whole gap.
- **Signed-consent receipts** (Ed25519/HMAC) — separate, larger effort.
- **Device-side rate-limit enforcement.** v1 rate-limits **host-side** (the
  daemon is the shared chokepoint and has the real clock + agent identity).
  Device-side capping is documented as defense-in-depth future.
- **Wall-clock quiet-hours for `show`** — no RTC on the device.

## 2. Compatibility contract (load-bearing)

Every wire change is **additive** and **capability-gated**, so old firmware
and old hosts keep working:

- Device `hello.caps` grows from `["notify","ask","confirm"]` to
  `["notify","ask","confirm","confirm_details","show"]`; `_FW_VERSION`
  `0.3.0 → 0.4.0`.
- `show` is a **new command** — the host's existing capability gate in
  `Bridge.send` (`if caps and cmd not in caps …`) already returns a clean
  `"device firmware does not advertise 'show'"` against old firmware. No new
  gate code needed.
- `details` is a **new optional field** on the existing `confirm` command.
  Old firmware ignores unknown JSON fields and renders the title-only confirm
  exactly as today (graceful degradation). The host detects render support
  via the new `confirm_details` cap and the companion skill instructs Claude
  to keep the `title` self-sufficient when details may not render.
- Rate-limiting is **host-only** and changes no wire bytes; it adds one new
  `notify` return string (`"rate-limited"`).

## 3. Feature designs

### 3.1 Per-agent notify rate-limit (host) — smallest, fully testable

- **New unit:** `mcp/ratelimit.py` — `MinIntervalLimiter(min_interval_s,
clock=time.monotonic)` with `allow(key) -> bool`. Sibling to `auth.py`;
  pure, independently testable. Injectable clock for deterministic tests.
- **Wiring:** in `notify()`, before touching BLE: if `urgency != "crit"` and
  `not limiter.allow(agent_label)` → return `"rate-limited"` (no radio hit).
  `crit` always bypasses; `ask`/`confirm` are unaffected (not notifies).
- **Config:** `CARDPUTER_NOTIFY_MIN_INTERVAL_S` env, default `60`, `0`
  disables. Bucket keyed by the **token-derived agent label** (so one noisy
  agent can't starve another; `local`/stdio shares one bucket).
- **Return contract:** `notify` adds `"rate-limited"` alongside
  `shown`/`dnd`/`unavailable`/`failed`.
- **Tests** (`tests/test_ratelimit.py` + extend notify tests): 2nd rapid
  non-crit → `rate-limited`; crit always passes; independent buckets per
  agent; allowed again after interval (fake clock); `0` disables.

### 3.2 `show(text, channel)` ambient status (host + device)

- **Host tool** `show(ctx, text, channel="")`: non-blocking, short RPC
  timeout (~5 s). `channel` defaults to the agent label. Truncates `text`
  (host: ~48 chars). Sends `{"cmd":"show","text":…,"channel":…,"agent":…}`.
  **Ignores DND** and never chirps — it's passive ambient state on the idle
  screen, not an interruption. Returns `"shown"` / `"unavailable: …"` /
  firmware-gated error.
- **Device** `_cmd_show`: store `channel → (text, ticks)` in a small ordered
  ring (cap **3** channels, evict oldest) to bound RAM. Ack `ok` immediately.
  If `state == "idle"`, mark dirty.
- **Idle render:** when ≥1 ambient entry exists, `_draw_idle` shows a compact
  status block (most-recent 3, newest first, `chan· text` truncated) below the
  device identity. With **zero** entries the idle screen is byte-for-byte
  today's clean layout (no regression).
- **Tests:** payload shape (cmd/text/channel/agent), channel default = agent
  label, truncation, `shown` on ok, `unavailable` when off; `tools/list`
  includes `show`.

### 3.3 Verified Approval — `confirm(details=…)` (host + device)

- **Host tool** `confirm(ctx, title, details="", timeout_s=30)`: when
  `details` carries content (strip-check), include it (truncated to **256
  chars** — shipped value, hardened down from the initial 480 so the whole
  confirm line — envelope + ≤64-char title + details — fits the device's
  512-byte RX reassembly buffer with margin) in the BLE payload. Title
  semantics unchanged. _(The `_device_caps()` helper sketched here was dropped:
  old firmware ignores the additive field and the companion skill keeps the
  title self-sufficient, so an explicit host-side cap check added no value.)_
- **Device** `_cmd_confirm`: store `details`; pre-wrap into `detail_lines`
  once on receipt (avoid re-wrapping each redraw — mirrors
  `push_to_claude._result_layout`). Reset `_confirm_scroll = 0`.
- **Device render** `_draw_confirm`: branch on `detail_lines`:
  - **No details** → exactly today's layout (title size-2 centered, instr,
    bar, status, hint). No regression.
  - **With details** → header (DANGER CONFIRM · `from:agent`) · compact
    size-1 title · **scrollable details window** (5 lines @12px, `▲`/`▼`
    indicators when more exists) · progress bar · combined status line · hint
    strip `TAP Y · ;/. scroll · N cancel`.
- **Device keys** (`handle_keypress`, confirm state): arrow cluster
  (`;`/`,`=up, `.`/`/`=down — same mapping as the rest of the bundle) scrolls
  details, bounded `[0, len-visible]`; `Y/y` taps still drive the hold;
  `N/n/ESC` cancel; `Q` exits. Scrolling does not advance the hold (you read,
  then tap).
- **Security honesty:** the hold is still the un-forgeable consent; `details`
  add _legibility_ of intent, not cryptographic trust. The agent supplies the
  text in v1 (see §1). The hold-gesture security property is unchanged.
- **Tests:** payload includes `details` when provided + truncation; omitted
  when empty (back-compat); `confirm` still gated by the `confirm` cap.

## 4. Files touched

| File                                          | Change                                                                                                   |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `mcp/ratelimit.py`                            | **NEW** — `MinIntervalLimiter`                                                                           |
| `mcp/server.py`                               | `notify` rate-limit wiring; **new** `show` tool; `confirm` `details` param + caps helper                 |
| `mcp/tests/test_ratelimit.py`                 | **NEW**                                                                                                  |
| `mcp/tests/test_show_tool.py`                 | **NEW**                                                                                                  |
| `mcp/tests/test_confirm_details.py`           | **NEW**                                                                                                  |
| `mcp/tests/test_http_server.py`               | add `show` to `tools/list` assertion; notify rate-limit + show via TestClient                            |
| `buddy/device/apps/cardputer_mcp.py`          | `_FW_VERSION`→0.4.0, caps; `_cmd_show` + idle ambient render; `confirm` details scroll view + arrow keys |
| `buddy/references/mcp_protocol.md`            | `details` field, `show` implemented, caps/version, host-side rate-limit note                             |
| `mcp/README.md`                               | tool table, roadmap (iter-4 `show` done; "verified approval" landed), known-limitations note             |
| `README.md`                                   | top-level tool descriptions for `show` + `confirm` details                                               |
| `.claude/skills/cardputer-companion/SKILL.md` | `show` etiquette; `details` guidance; rate-limit now a backstop (restraint still expected)               |

## 5. Test & verification posture (honest)

- **Host side is fully unit-tested here** (mocked BLE bridge, `TestClient`),
  matching the existing `mcp/tests/` patterns. `python -m pytest tests/ -q`
  must stay green.
- **Device-side MicroPython is NOT runnable in this environment** (no
  Cardputer, no BLE). Device changes are written against the existing,
  battle-tested patterns in the same file and clearly commented as
  hardware-untested; the wire protocol is additive + cap-gated so a mismatch
  degrades gracefully rather than breaking `notify`/`ask`/`confirm`.
- Adversarial review (codex challenge + security-auditor) on the diff before
  finalizing.

## 6. Commit plan (atomic)

1. design spec (this file)
2. `feat(mcp): per-agent notify rate-limit` (ratelimit.py + wiring + tests)
3. `feat(mcp): show() ambient status tool` (host + tests)
4. `feat(mcp): confirm(details) for verified approval` (host + tests)
5. `feat(device): ambient show + scrollable action-diff confirm + caps 0.4.0`
6. `docs: protocol/README/companion updates for show + details + rate-limit`
