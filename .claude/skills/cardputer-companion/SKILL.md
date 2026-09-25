---
name: cardputer-companion
description: Use when the cardputer MCP tools (notify, ask, confirm, show, progress) are available in the session, or the user mentions their Cardputer / handheld / "buzz me" / "page me". Governs runtime etiquette for the pocket device — mandates a physical confirm gesture before irreversible operations (passing the real action diff as details), buzzes the device on completion of long tasks, asks quick questions when blocked and the user is away from the keyboard, shows ambient status for long work, and formats all device output for the 240×135 LCD. This is the behavioral counterpart to the cardputer MCP server: the server is the hands, this skill is the manners. Trigger even when the user doesn't name the skill — if the `cardputer` MCP tools are registered, these rules are in force.
---

# Cardputer Companion

The Cardputer is a credit-card-sized handheld the user carries in their pocket.
It exposes MCP tools over a BLE bridge — `notify`, `ask`, `confirm`, `show`, and
`progress` — provided by the `cardputer` MCP server in this repo (`mcp/server.py`).
Those tools are the _hands_. This skill is the _manners_: it tells you **when**
to reach for them and **how** to shape what you send, so the device stays
useful instead of annoying.

**These rules apply no matter where you run.** The same tools reach the same
device whether you're local Claude Code over loopback or a cloud Managed Agent /
Messages-API agent over an MCP tunnel (`tunnel/`). If anything, restraint and the
fail-closed `confirm` discipline matter **more** when you're an unattended cloud
agent — the user isn't watching a terminal, and the physical gesture is the only
thing standing between you and an irreversible mistake. The device shows **which
agent is asking** on every `ask`/`confirm` banner (derived from your bearer
token, so you can't misrepresent it) — keep your requests honest and legible.

## Core ethos: default to silence

A buzz should mean something. The user is wearing this thing; every alert costs
their attention. Earn each one. When in doubt, stay quiet and keep working — do
not narrate progress to the device. The daemon now enforces a per-agent floor
on non-critical `notify`s (a second non-`crit` buzz within ~60 s comes back
`rate-limited` and never reaches the device), but treat that as a safety net,
not a budget — keep self-imposed restraint as the real throttle. If you want a
long task to stay visible without buzzing, use `show` (see below), not repeated
`notify`s.

## 1. Confirm policy — mandatory, non-negotiable

Before any **irreversible** operation, call `cardputer.confirm` and wait for the
physical hold gesture. Do **not** substitute a chat-message "are you sure?" —
the whole point is that no tool output or prompt injection can synthesize a
sustained physical keypress, but it _can_ forge a convincing chat confirmation.

Operations that require `confirm`:

- Production deploys / releases
- `git push --force` (especially to `main`/`master`)
- `DROP TABLE`, `TRUNCATE`, or `DELETE` / `UPDATE` without a `WHERE`
- `rm -rf`, mass file deletion, or destroying uncommitted work
- Paid API calls or financial transactions with large side effects
- Anything the user could not undo in about a minute

Rules:

- If `confirm` returns `cancelled` or `timeout`, **abort the operation** and
  tell the user — never proceed on a non-confirmation.
- If `confirm` returns `unavailable: …` (device off or out of range), do **not**
  silently fall through to running the destructive command. Stop and tell the
  user the device isn't reachable; let them decide how to proceed.
- Keep the `title` to ~18 characters and declarative — the user must recognize
  the operation at a glance: `FORCE PUSH main`, `DROP customers`, `deploy prod`.
- **Pass `details`** whenever you can: the _actual_ content being approved —
  the real shell command, the SQL, a short diff hunk, or payee + amount. The
  device renders it in a scrollable box above the gesture, so the user approves
  _what they read_, not just the title (the hardware-wallet model). Keep it to
  the essential ~256 chars and strip noise. It's text you supply, so it's
  legibility of intent, not cryptographic proof — and the `title` must still
  stand on its own (older firmware shows the title only).
- Do not call `confirm` for routine yes/no decisions — that's what `ask` is for.
- Every `confirm` decision (confirmed / cancelled / timeout / unavailable) is
  written to a local consent audit log (`~/.cardputer-mcp/audit.log`) with the
  agent label, `title`, and `details`. Another reason to pass a real, legible
  `details`: it's the durable record of _what_ the user approved.

## 2. Proactive notify — quiet

Buzz the device on the **final completion of a genuinely long-running task** —
multi-step or multi-minute work the user is plausibly waiting on while away from
the keyboard (a full test suite, a migration, a build, a long agent run).

- **One** notify at the end, not one per step.
- Lead with the verdict: `tests green`, `build failed`, `deploy ready`.
- Do **not** notify for quick interactive turns where the user is clearly
  watching the screen — that's just noise.
- Use `urgency` honestly: `info` for "done, all good", `warn` for "done but
  needs your eyes", `crit` only for something that needs a reaction within
  seconds.

## 3. Ask when blocked — quiet

Use `cardputer.ask` only when you are **genuinely blocked** on a decision **and**
the user may be away from their laptop. If they are plainly at the keyboard,
ask in the chat instead — the device round-trip is slower and more intrusive.

- Provide 2–4 short choices (each ≤ ~32 chars).
- Keep the question to ~60 chars (wraps to two lines on the LCD).
- On `timeout` or `cancelled`, fall back to chat — do not loop re-asking.

## 3b. Ambient status — `show`, glanceable not noisy

`cardputer.show(text, channel)` writes one **silent** line to the device's idle
screen — no chirp, no screen takeover, not gated by DND. Use it to leave a live
heartbeat of a long task the user can glance at, _instead of_ buzzing them:
`building…`, `pytest 142/300`, `deploy ok`.

- It is NOT a `notify`. Never use it for something the user must react to —
  that's `notify` (or `confirm`). `show` is the status bar, not an alert.
- Update at a human cadence — a few times across a task, not every step/token.
- One line per `channel` (defaults to your agent label); keep `text` ≤ ~40
  chars. The device keeps only the most recent few channels.
- The pattern: a `show` heartbeat _while_ working, then one `notify` at the
  very end (`tests green`). Don't replace the end-of-task `notify` with `show`.

## 3c. Ambient progress — `progress`, when there's a percentage

`cardputer.progress(label, percent, channel)` is the visual sibling of `show`:
same silence, same channel ring, same etiquette — but it renders a **filling
0–100% bar** instead of a text line. Reach for it _instead of_ `show` whenever
the work has a real denominator: a build, a download, a test sweep, an N-of-M
migration.

- Call it as the work advances at a human cadence (`0 → 25 → 60 → 100`), not on
  every increment. `percent` is clamped to 0–100; `label` shares the row with
  the bar, so keep it ≤ ~12 chars (`build`, `tests`, `deploy`).
- It shares the `show` ring, so a `progress` and a `show` on the same `channel`
  contend for one slot — latest wins. A natural arc: `progress` to 100, then a
  `show("done")` (or the end-of-task `notify`) to close it out.
- Still ambient: silent, no takeover, ignores DND. Same rule as `show` — it is
  NOT an alert. Pair the bar _while_ working with one `notify` at the very end.
- `unavailable` / older firmware → just skip it; it's a nicety, never required.

## 4. Tiny-screen formatting

The LCD is **240×135 pixels**. Whatever you send must read in a glance:

- Titles ≤ ~20 characters; bodies ≤ 3 short lines (~30 chars each).
- No markdown tables, no code fences, no long file paths — strip to the
  essential token (`auth_test.py`, not the full path).
- Spell out the outcome in plain words; the user can't scroll a banner.
- Lead with the result, details second.

## 5. Do Not Disturb — respect it

The user can put the device in Do Not Disturb (a `DND` chip shows on its idle
screen). When they have:

- `notify` (non-critical) and `ask` return `"dnd"`. Treat it as "the user is
  heads-down": do **not** retry or escalate; degrade to normal chat / logging
  and carry on. For `ask`, pick the safe default or proceed without the device
  rather than looping.
- `crit` notifications and **`confirm` still ring** — DND never weakens the
  destructive-op gate. If you genuinely need a `confirm` and it `timeout`s
  because the user is asleep, that's the system working: **abort**, don't
  proceed.

## 6. Check before you interrupt — `device_status`

`device_status()` is a **read-only, passive** probe (no buzz, no radio wake):
it reports `online`/`offline`, `dnd`, firmware `caps`, uptime, and battery.
Use it when it would change your behavior — e.g. before starting a long
unattended job ("will I even be able to page them when it's done?"), or before
a non-urgent `notify` when you're unsure. If it says `offline` or `dnd=on`,
prefer staying quiet and falling back to chat over firing a call that bounces.
Don't poll it in a loop; it's a check, not a heartbeat for you to watch. To
_actively_ reach the device, just call `notify`/`ask`/`confirm` — they connect
on demand and fail closed.

## When the device is unavailable

All three tools return `"unavailable: <reason>"` when the Cardputer is off or out
of BLE range (and a cloud call also fails if the bridge daemon or tunnel is
down). For `notify`/`ask`, degrade silently to normal chat behavior. For
`confirm`, the unavailable case is a **hard stop** (see §1) — never treat an
unreachable safety device, a sleeping laptop, or a dead tunnel as implicit
approval. Fail closed, every time.

## What this skill is not

This is runtime etiquette only. Flashing firmware, pushing apps, and provisioning
the device are handled by the separate `m5-onboard` skill — don't duplicate that
here.
