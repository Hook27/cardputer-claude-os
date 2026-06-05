"""particle_life.py — Primordium-style Particle Life for M5Stack Cardputer ADV
==========================================================================
Port of the Particle Life / Primordium simulation (https://primordia.io)
to MicroPython + UIFlow2 firmware.

Simple rules → emergent behaviour: coloured particles attract or repel each
other according to a species interaction matrix.  Tiny rule differences make
cells, chasers, snakes or gas.

Controls (QWERTY keyboard)
──────────────────────────
  ESC / Q   Quit → back to launcher
  SPACE     Pause / resume
  R         Randomise interaction matrix (new organism)
  N         Respawn particles (fresh positions, same rules)
  M         Mutate (nudge matrix — keeps the creature's character)
  P         Cycle colour palette (4 presets)
  1         Preset: Cells     (blobs repelling each other)
  2         Preset: Chase     (colour trains hunting each other)
  3         Preset: Snakes    (wriggling chains)
  4         Preset: Gas       (repulsive diffuse cloud)

Tuning knobs (top of this file)
────────────────────────────────
  _N        particle count (default 90; reduce to 60 if framerate is low)
  _K        species count  (default 4; max 6 to stay within palette)
  _RMAX     interaction radius in pixels (default 26)

Performance notes
─────────────────
Simulation is O(N) thanks to a spatial hash grid (same as the original).
At N=90 on ESP32-S3 (240 MHz) expect ~6–12 fps in pure MicroPython.
Reduce _N to 60 for ~15 fps.  The visual effect is already mesmerising at 6 fps.
"""

import random
import math
import sys
import time
import M5

# ── display ───────────────────────────────────────────────────────────────────
_LCD = M5.Lcd          # matches pi_dashboard pattern
_W, _H = 240, 135

# ── keyboard ─────────────────────────────────────────────────────────────────
try:
    from hardware import MatrixKeyboard
    _KB = MatrixKeyboard()
except Exception:
    _KB = None

def _get_key():
    """Return the currently pressed key as a single-char string, or None.

    Matches the working apps (claude_buddy / snake / pi_dashboard): the
    MatrixKeyboard driver must be ticked each poll, and get_key() hands
    back the raw ASCII byte as an **int** on this UIFlow 2.0 build, so we
    normalise it to a char (0x1B → ESC) for the matcher in run().
    Silently ignores errors."""
    if _KB is None:
        return None
    try:
        _KB.tick()
        k = _KB.get_key()
        if not k:
            return None
        if isinstance(k, int):
            if k == 0x1B:
                return '\x1b'
            if 0x20 <= k <= 0x7E:
                return chr(k)
            return None
        return k
    except Exception:
        return None

# ── palettes (4 × 6 species, RGB888) ─────────────────────────────────────────
# M5.Lcd takes 24-bit RGB888 colours (same as snake / pi_dashboard); we draw
# straight to the panel, so these must be 888 — not the 565 a sprite would use.
_PALETTES = [
    # Spectrum: red / green / cyan / magenta / yellow / blue
    [0xFF0000, 0x00FF00, 0x00FFFF, 0xFF00FF, 0xFFFF00, 0x0000FF],
    # Fire:    white-yellow / orange / red / dark-red / ember / ash
    [0xFFFFC0, 0xFF8000, 0xFF0000, 0xC00000, 0x802000, 0x404040],
    # Ice:     white-cyan / bright-cyan / teal / royal-blue / navy / deep
    [0xC0FFFF, 0x00FFFF, 0x008080, 0x2040FF, 0x000080, 0x000040],
    # Candy:   hot-pink / purple / lime / yellow / coral / sky
    [0xFF4080, 0x8000FF, 0x80FF00, 0xFFFF00, 0xFF6040, 0x40C0FF],
]

# ── simulation parameters (tune here) ────────────────────────────────────────
_N    = 60      # particle count (try 60 if slow, 120 if fast)
_K    = 3       # species count  (2–6; must be ≤ len(palette))
_RMAX = 20.0    # interaction radius (pixels)
_FORCE = 10.0   # force multiplier
_FHL  = 0.04    # friction half-life (higher = more glide)
_BETA = 0.3     # hard-core repulsion zone (fraction of _RMAX)
_DT   = 0.04    # time step

# Precomputed constants (derived from above)
_FF     = 0.5 ** (_DT / _FHL)  # friction factor per frame  (≈ 0.648)
_FPF_DT = _RMAX * _FORCE * _DT  # combined force scale       (≈ 6.5)
_R2     = _RMAX * _RMAX
_IB     = 1.0 / (1.0 - _BETA)

# ── runtime state ─────────────────────────────────────────────────────────────
_x = _y = _vx = _vy = _typ = None   # particle arrays (set by _spawn)
_mat  = None                          # K×K interaction matrix
_cols = []                            # RGB565 colour per species
_pal  = 0                             # current palette index

# ── colour helpers ────────────────────────────────────────────────────────────

def _dim(c, num, den):
    """Scale an RGB888 colour by the integer fraction num/den.
    Used to render the particle trail (older = dimmer)."""
    r = ((c >> 16) & 0xFF) * num // den
    g = ((c >>  8) & 0xFF) * num // den
    b = ( c        & 0xFF) * num // den
    return (r << 16) | (g << 8) | b


def _build_cols():
    global _cols
    p = _PALETTES[_pal]
    _cols = [p[i % len(p)] for i in range(_K)]

# ── matrix helpers ─────────────────────────────────────────────────────────────

def _rand_mat():
    global _mat
    _mat = [[random.uniform(-1.0, 1.0) for _ in range(_K)]
            for _ in range(_K)]


def _mutate(amt=0.25):
    """Nudge every matrix cell by ±amt (clamped to [-1, 1])."""
    for i in range(_K):
        for j in range(_K):
            v = _mat[i][j] + (random.random() * 2.0 - 1.0) * amt
            _mat[i][j] = -1.0 if v < -1.0 else (1.0 if v > 1.0 else v)


def _preset(name):
    """Apply one of the four hand-crafted presets."""
    k = _K
    for i in range(k):
        for j in range(k):
            if name == "cells":
                v = 1.0 if i == j else -0.35
            elif name == "chase":
                v = (1.0  if j == (i + 1) % k else
                     -0.15 if i == j           else -0.25)
            elif name == "snakes":
                if   i == j:            v =  0.55
                elif j == (i+1) % k:   v =  0.45
                elif i == (j+1) % k:   v = -0.45
                else:                   v = -0.10
            else:  # gas
                v = -0.10 if i == j else -(0.30 + random.random() * 0.50)
            _mat[i][j] = v

# ── particle init ─────────────────────────────────────────────────────────────

def _spawn():
    """Scatter particles uniformly across the screen with zero velocity."""
    global _x, _y, _vx, _vy, _typ
    n = _N
    _x   = [random.uniform(0.0, float(_W)) for _ in range(n)]
    _y   = [random.uniform(0.0, float(_H)) for _ in range(n)]
    _vx  = [0.0] * n
    _vy  = [0.0] * n
    _typ = [random.randint(0, _K - 1) for _ in range(n)]

# ── physics step (spatial-hash O(N)) ─────────────────────────────────────────

def _step():
    """Advance the simulation by one time step.

    Uses a spatial hash grid (cell size = _RMAX) so each particle only
    visits its 3×3 neighbourhood — O(N) instead of O(N²).
    """
    n   = _N
    W   = float(_W);  H  = float(_H)
    rM  = _RMAX;      r2 = _R2
    ff  = _FF;        ib = _IB;  beta = _BETA
    fpf = _FPF_DT     # velocity increment per raw force unit

    # ── build spatial grid ─────────────────────────────────────────────
    cw = rM                                       # cell width  = rMax
    ch = rM                                       # cell height = rMax
    gc = max(1, int(W / cw))                      # grid columns
    gr = max(1, int(H / ch))                      # grid rows
    nc = gc * gr

    head = [-1] * nc   # head[cell] = first particle (linked list)
    nxt  = [-1] * n    # nxt[i]     = next particle in same cell

    for i in range(n):
        cx = int(_x[i] / cw);  cx = min(cx, gc - 1)
        cy = int(_y[i] / ch);  cy = min(cy, gr - 1)
        c  = cy * gc + cx
        nxt[i]  = head[c]
        head[c] = i

    # ── compute forces ─────────────────────────────────────────────────
    for i in range(n):
        fx = 0.0;  fy = 0.0
        xi = _x[i];  yi = _y[i]
        row = _mat[_typ[i]]
        cx = int(xi / cw);  cx = min(cx, gc - 1)
        cy = int(yi / ch);  cy = min(cy, gr - 1)

        for oy in range(-1, 2):
            ncy = (cy + oy + gr) % gr
            for ox in range(-1, 2):
                j = head[ncy * gc + (cx + ox + gc) % gc]
                while j != -1:
                    if j != i:
                        dx = _x[j] - xi;  dy = _y[j] - yi
                        # periodic (wrapping) boundary
                        if   dx >  W * 0.5: dx -= W
                        elif dx < -W * 0.5: dx += W
                        if   dy >  H * 0.5: dy -= H
                        elif dy < -H * 0.5: dy += H
                        d2 = dx * dx + dy * dy
                        if 0.0 < d2 < r2:
                            d  = math.sqrt(d2)
                            rr = d / rM
                            if rr < beta:
                                # hard-core repulsion: always pushes outward
                                f = rr / beta - 1.0
                            else:
                                # species-dependent attraction / repulsion
                                f = row[_typ[j]] * (
                                    1.0 - abs(2.0 * rr - 1.0 - beta) * ib)
                            id_ = f / d
                            fx += dx * id_
                            fy += dy * id_
                    j = nxt[j]

        _vx[i] = _vx[i] * ff + fx * fpf
        _vy[i] = _vy[i] * ff + fy * fpf

    # ── integrate positions ────────────────────────────────────────────
    dt = _DT
    for i in range(n):
        _x[i] = (_x[i] + _vx[i] * dt) % W
        _y[i] = (_y[i] + _vy[i] * dt) % H

# ── trail buffer ──────────────────────────────────────────────────────────────
#
# Keep the last _TRAIL frames of particle positions.  Older frames are drawn
# dimmer, creating a fading-comet trail without needing a full framebuffer.

_TRAIL = 2        # trail depth (frames)
_tbuf  = []       # list of [(x, y, species), …]  (newest last)


def _push_trail():
    """Snapshot current positions; evict oldest frame when full."""
    _tbuf.append([(int(_x[i]), int(_y[i]), _typ[i]) for i in range(_N)])
    if len(_tbuf) > _TRAIL:
        _tbuf.pop(0)

# ── render ────────────────────────────────────────────────────────────────────

# Pixels lit on the previous frame, as a set of (x, y) top-left corners, so we
# can erase only the cells that go dark — no full-screen clear, hence no
# flicker, while drawing straight to the panel (no off-screen canvas, which
# this firmware's newCanvas/push doesn't blit reliably).
_lit = set()


def _draw(fps, paused):
    """Paint one frame straight onto _LCD using a differential update."""
    global _lit

    # Build this frame's draw list (oldest/dimmest trail first, current last
    # so it overwrites) and the set of cells that will be lit.
    draws = []          # (x, y, colour)  — applied in order, current on top
    new_lit = set()

    nt = len(_tbuf)
    if nt > 1:
        # Precompute dimmed colours per age level — only K×(nt-1) calls.
        for age in range(nt - 1):       # skip last slot = current frame
            num = age + 1
            den = nt + 1
            dc = [_dim(_cols[sp], num, den) for sp in range(_K)]
            for px, py, sp in _tbuf[age]:
                draws.append((px, py, dc[sp]))
                new_lit.add((px, py))

    # Current particles at full brightness (2×2 px squares)
    for i in range(_N):
        px = int(_x[i]);  py = int(_y[i])
        draws.append((px, py, _cols[_typ[i]]))
        new_lit.add((px, py))

    # Erase only cells that were lit last frame but aren't now → no flicker
    # on pixels that stay lit, no stale trails left behind.
    for px, py in _lit:
        if (px, py) not in new_lit:
            _LCD.fillRect(px, py, 2, 2, 0x000000)

    for px, py, col in draws:
        _LCD.fillRect(px, py, 2, 2, col)

    _lit = new_lit

    # ── HUD ───────────────────────────────────────────────────────────
    # Repaint over a cleared strip so a shrinking string leaves no residue.
    _LCD.fillRect(0, _H - 12, 70, 12, 0x000000)
    _LCD.setTextSize(1)
    _LCD.setTextColor(0x7B7B7B, 0x000000)   # 50% gray on black
    hud = ("|| " if paused else "") + str(fps) + "fps"
    _LCD.drawString(hud, 2, _H - 11)

    # Palette colour swatches — bottom-right corner (fixed position; opaque)
    for i in range(_K):
        _LCD.fillRect(_W - (_K - i) * 6, _H - 7, 5, 5, _cols[i])

# ── entry point ───────────────────────────────────────────────────────────────

def run():
    global _pal

    # Initialise
    _build_cols()
    _rand_mat()
    _spawn()

    # Draw straight to the panel (no off-screen canvas). Clear once; from then
    # on _draw only touches changed pixels.
    _LCD.fillScreen(0x000000)

    paused = False
    fps    = 0
    fc     = 0
    t_fps  = time.ticks_ms()

    while True:
        # ── keyboard ──────────────────────────────────────────────────
        k = _get_key()
        if k:
            c = k.lower() if (isinstance(k, str) and len(k) == 1) else k
            if   c in ('\x1b', 'q'):   break          # ESC / Q → quit
            elif c == ' ':             paused = not paused
            elif c == 'r':             _rand_mat()
            elif c == 'n':             _spawn()
            elif c == 'm':             _mutate()
            elif c == 'p':
                _pal = (_pal + 1) % len(_PALETTES)
                _build_cols()
            elif c == '1':             _preset("cells")
            elif c == '2':             _preset("chase")
            elif c == '3':             _preset("snakes")
            elif c == '4':             _preset("gas")

        # ── simulate ──────────────────────────────────────────────────
        if not paused:
            _step()
            _push_trail()

        # ── render ────────────────────────────────────────────────────
        _draw(fps, paused)

        # ── fps counter (update every 2 s) ────────────────────────────
        fc += 1
        now = time.ticks_ms()
        if time.ticks_diff(now, t_fps) >= 2000:
            fps   = fc * 1000 // time.ticks_diff(now, t_fps)
            fc    = 0
            t_fps = now

# Run, then drop ourselves from sys.modules so the launcher can re-import
# (and thus re-run) us next time it's selected — same clean return-to-menu
# pattern as pi_dashboard, no machine.reset(). The launcher's _launch holds
# its own reference to the module object, so popping here is safe; when run()
# returns (ESC / Q), _launch falls through and repaints the menu.
try:
    run()
finally:
    try:
        _LCD.fillScreen(0x000000)
    except Exception:
        pass
    sys.modules.pop(__name__, None)
