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

Physics engine — fixed-point + @micropython.viper
──────────────────────────────────────────────────
The hot step (_step_v) is a viper-compiled integer kernel: particle state
lives in Q8 fixed-point (1 unit = 1/256 px) inside ``array('i')`` buffers,
accessed as ptr32, and distance uses an integer ``isqrt``.  This sidesteps
MicroPython's boxed-float arithmetic — which @micropython.native could NOT
accelerate — and is the reason the step is ~an order of magnitude cheaper
than the float version.  A faithful float fallback (_step_float, validated
to ~0.01 px/step against viper) stays behind ``_USE_VIPER`` so we can A/B or
fall back if a future firmware breaks viper.  Roughly ~30 fps at N=100 and
~50 fps at N=60 (the float build managed ~15 at N=60); drawing is now the
frame-time floor, not the physics, so fps scales mostly with _N.

Tuning knobs (top of this file)
────────────────────────────────
  _N        particle count (default 100)
  _K        species count  (2–6; must be ≤ len(palette))
  _RMAX     interaction radius in pixels (default 20)
  _USE_VIPER  True = viper fixed-point kernel; False = float fallback
"""

import random
import math
import sys
import time
import array
import micropython
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
_N    = 100     # particle count
_K    = 3       # species count  (2–6; must be ≤ len(palette))
_RMAX = 20.0    # interaction radius (pixels)
_FORCE = 10.0   # force multiplier
_FHL  = 0.04    # friction half-life (higher = more glide)
_BETA = 0.3     # hard-core repulsion zone (fraction of _RMAX)
_DT   = 0.04    # time step

_USE_VIPER = True   # False → float fallback (_step_float); see module docstring

# Precomputed float constants (used by the float fallback)
_FF     = 0.5 ** (_DT / _FHL)  # friction factor per frame
_FPF_DT = _RMAX * _FORCE * _DT  # combined force scale
_R2     = _RMAX * _RMAX
_IB     = 1.0 / (1.0 - _BETA)

# ── fixed-point engine constants (Q8: 1 unit = 1/256 px) ──────────────────────
# Positions/velocities live in Q8 ints. The squared distance dx*dx+dy*dy is
# Q16; with |dx| capped at W/2 after the periodic wrap it stays < 2^31, so Q8
# is the largest scale that never overflows int32 on the squaring.
_S    = 256
_Wq   = _W * _S
_Hq   = _H * _S
_Wh   = _Wq // 2
_Hh   = _Hq // 2
_RMAXq = int(_RMAX * _S)
_R2q  = _RMAXq * _RMAXq                 # Q16
_BETAq = int(_BETA * _S + 0.5)
_IBq  = int(_IB * _S + 0.5)
_FFq  = int(_FF * _S + 0.5)             # = 128 for FF=0.5 → exact halving
_FPFq = int(_FPF_DT * _S + 0.5)         # = 2048 for FPF=8.0 → exact ×8
_dtq  = int(_DT * 65536 + 0.5)          # dt in Q16 for the integration
_gc   = max(1, int(_W / _RMAX))         # grid columns
_gr   = max(1, int(_H / _RMAX))         # grid rows
_nc   = _gc * _gr                       # grid cells

# Scalar params handed to the viper kernel (index order is load-bearing —
# keep in sync with _step_v's sp[...] reads).
_params = array.array('i', [
    _N, _K, _Wq, _Hq, _Wh, _Hh, _R2q, _RMAXq,
    _BETAq, _IBq, _FFq, _FPFq, _dtq, _gc, _gr, _nc,
])
# Spatial-hash grid, preallocated once: head[0.._nc) then nxt[_nc.._nc+_N).
_grid = array.array('i', bytearray(4 * (_nc + _N)))

# ── runtime state ─────────────────────────────────────────────────────────────
_x = _y = _vx = _vy = None   # array('i') Q8 positions/velocities (set by _spawn)
_typ = None                  # bytearray of species ids
_mat  = None                 # flat array('i') K*K interaction matrix, Q8
_cols = []                   # RGB888 colour per species
_pal  = 0                    # current palette index

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

# ── matrix helpers (flat K*K, Q8 ints) ──────────────────────────────────────────

def _rand_mat():
    global _mat
    _mat = array.array('i', [int(random.uniform(-1.0, 1.0) * _S)
                             for _ in range(_K * _K)])


def _mutate(amt=0.25):
    """Nudge every matrix cell by ±amt (clamped to [-1, 1])."""
    for idx in range(_K * _K):
        v = _mat[idx] / _S + (random.random() * 2.0 - 1.0) * amt
        v = -1.0 if v < -1.0 else (1.0 if v > 1.0 else v)
        _mat[idx] = int(v * _S)


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
            _mat[i * k + j] = int(v * _S)

# ── particle init ─────────────────────────────────────────────────────────────

def _spawn():
    """Scatter particles uniformly across the screen with zero velocity."""
    global _x, _y, _vx, _vy, _typ
    n = _N
    _x   = array.array('i', [int(random.uniform(0.0, float(_W)) * _S) for _ in range(n)])
    _y   = array.array('i', [int(random.uniform(0.0, float(_H)) * _S) for _ in range(n)])
    _vx  = array.array('i', bytearray(4 * n))
    _vy  = array.array('i', bytearray(4 * n))
    _typ = bytearray([random.randint(0, _K - 1) for _ in range(n)])

# ── physics step ──────────────────────────────────────────────────────────────

@micropython.viper
def _isqrt(n: int) -> int:
    """Integer sqrt (bit-by-bit). sqrt of a Q16 value gives Q8 directly."""
    n = int(n)
    if n <= 0:
        return 0
    x = int(0)
    b = int(1 << 30)
    while b > n:
        b >>= 2
    while b != 0:
        if n >= x + b:
            n -= x + b
            x = (x >> 1) + b
        else:
            x >>= 1
        b >>= 2
    return x


@micropython.viper
def _step_v(xp: ptr32, yp: ptr32, vxp: ptr32, vyp: ptr32,
            tp: ptr8, mp: ptr32, gp: ptr32, sp: ptr32):
    """Viper fixed-point step: build spatial grid, accumulate forces, integrate.

    All integer / Q8. Forces accumulate as (dx*f)//d (never f/d separately —
    that overflows when d is tiny). Signed scaling uses // (not >>) so
    negatives stay correct; >> is reserved for provably-nonnegative values.
    """
    n = int(sp[0]);   k = int(sp[1])
    Wq = int(sp[2]);  Hq = int(sp[3]);  Wh = int(sp[4]);  Hh = int(sp[5])
    R2q = int(sp[6]); RMAXq = int(sp[7])
    BETAq = int(sp[8]); IBq = int(sp[9]); FFq = int(sp[10]); FPFq = int(sp[11])
    dtq = int(sp[12]); gcn = int(sp[13]); grn = int(sp[14]); nc = int(sp[15])

    # reset grid heads
    c = int(0)
    while c < nc:
        gp[c] = -1
        c += 1
    # build grid (nxt of particle i stored at gp[nc + i])
    i = int(0)
    while i < n:
        cx = int(xp[i]) // RMAXq
        if cx >= gcn: cx = gcn - 1
        cy = int(yp[i]) // RMAXq
        if cy >= grn: cy = grn - 1
        cc = cy * gcn + cx
        gp[nc + i] = gp[cc]
        gp[cc] = i
        i += 1

    # forces
    i = 0
    while i < n:
        fx = int(0); fy = int(0)
        xi = int(xp[i]); yi = int(yp[i])
        rb = int(tp[i]) * k
        cx = xi // RMAXq
        if cx >= gcn: cx = gcn - 1
        cy = yi // RMAXq
        if cy >= grn: cy = grn - 1
        oy = -1
        while oy < 2:
            ncy = (cy + oy + grn) % grn
            ox = -1
            while ox < 2:
                ncx = (cx + ox + gcn) % gcn
                j = int(gp[ncy * gcn + ncx])
                while j != -1:
                    if j != i:
                        dx = int(xp[j]) - xi
                        dy = int(yp[j]) - yi
                        if dx > Wh: dx -= Wq
                        elif dx < -Wh: dx += Wq
                        if dy > Hh: dy -= Hq
                        elif dy < -Hh: dy += Hq
                        d2 = dx * dx + dy * dy
                        if d2 > 0 and d2 < R2q:
                            d = int(_isqrt(d2))
                            rr = (d << 8) // RMAXq
                            if rr < BETAq:
                                f = ((rr << 8) // BETAq) - 256
                            else:
                                t = (rr << 1) - 256 - BETAq
                                if t < 0: t = -t
                                term = 256 - ((t * IBq) >> 8)
                                f = (int(mp[rb + int(tp[j])]) * term) // 256
                            fx += (dx * f) // d
                            fy += (dy * f) // d
                    j = int(gp[nc + j])
                ox += 1
            oy += 1
        vxp[i] = (int(vxp[i]) * FFq) // 256 + (fx * FPFq) // 256
        vyp[i] = (int(vyp[i]) * FFq) // 256 + (fy * FPFq) // 256
        i += 1

    # integrate (single-wrap conditional; velocities never exceed a screen/step)
    i = 0
    while i < n:
        nx = int(xp[i]) + (int(vxp[i]) * dtq) // 65536
        if nx >= Wq: nx -= Wq
        elif nx < 0: nx += Wq
        xp[i] = nx
        ny = int(yp[i]) + (int(vyp[i]) * dtq) // 65536
        if ny >= Hq: ny -= Hq
        elif ny < 0: ny += Hq
        yp[i] = ny
        i += 1


def _step_float(x, y, vx, vy):
    """Float fallback: the original algorithm, on float-list positions, reading
    the flat Q8 matrix. Kept as a validated reference / firmware safety net."""
    n   = _N
    W   = float(_W);  H  = float(_H)
    rM  = _RMAX;      r2 = _R2
    ff  = _FF;        ib = _IB;  beta = _BETA
    fpf = _FPF_DT;    dt = _DT
    mat = _mat;       typ = _typ;  K = _K
    sqrt = math.sqrt
    cw = rM;  ch = rM
    gc = max(1, int(W / cw));  gr = max(1, int(H / ch));  nc = gc * gr
    head = [-1] * nc;  nxt = [-1] * n
    for i in range(n):
        cx = int(x[i] / cw);  cx = min(cx, gc - 1)
        cy = int(y[i] / ch);  cy = min(cy, gr - 1)
        c = cy * gc + cx
        nxt[i] = head[c];  head[c] = i
    for i in range(n):
        fx = 0.0;  fy = 0.0
        xi = x[i];  yi = y[i];  rb = typ[i] * K
        cx = int(xi / cw);  cx = min(cx, gc - 1)
        cy = int(yi / ch);  cy = min(cy, gr - 1)
        for oy in range(-1, 2):
            ncy = (cy + oy + gr) % gr
            for ox in range(-1, 2):
                j = head[ncy * gc + (cx + ox + gc) % gc]
                while j != -1:
                    if j != i:
                        dx = x[j] - xi;  dy = y[j] - yi
                        if   dx >  W * 0.5: dx -= W
                        elif dx < -W * 0.5: dx += W
                        if   dy >  H * 0.5: dy -= H
                        elif dy < -H * 0.5: dy += H
                        d2 = dx * dx + dy * dy
                        if 0.0 < d2 < r2:
                            d = sqrt(d2);  rr = d / rM
                            if rr < beta:
                                f = rr / beta - 1.0
                            else:
                                f = (mat[rb + typ[j]] / _S) * (
                                    1.0 - abs(2.0 * rr - 1.0 - beta) * ib)
                            idd = f / d
                            fx += dx * idd;  fy += dy * idd
                    j = nxt[j]
        vx[i] = vx[i] * ff + fx * fpf
        vy[i] = vy[i] * ff + fy * fpf
    for i in range(n):
        x[i] = (x[i] + vx[i] * dt) % W
        y[i] = (y[i] + vy[i] * dt) % H


_ftmp = None   # lazily-allocated float scratch buffers for the fallback path


def _step_ref():
    """Run the float fallback over the Q8 int arrays (convert in/out)."""
    global _ftmp
    if _ftmp is None:
        _ftmp = ([0.0] * _N, [0.0] * _N, [0.0] * _N, [0.0] * _N)
    fx, fy, fvx, fvy = _ftmp
    inv = 1.0 / _S
    for i in range(_N):
        fx[i] = _x[i] * inv;  fy[i] = _y[i] * inv
        fvx[i] = _vx[i] * inv;  fvy[i] = _vy[i] * inv
    _step_float(fx, fy, fvx, fvy)
    for i in range(_N):
        _x[i] = int(fx[i] * _S);  _y[i] = int(fy[i] * _S)
        _vx[i] = int(fvx[i] * _S);  _vy[i] = int(fvy[i] * _S)


def _step():
    """Advance the simulation one frame (viper kernel, or float fallback)."""
    if _USE_VIPER:
        _step_v(_x, _y, _vx, _vy, _typ, _mat, _grid, _params)
    else:
        _step_ref()

# ── trail buffer ──────────────────────────────────────────────────────────────
#
# Keep the last _TRAIL frames of particle positions.  Older frames are drawn
# dimmer, creating a fading-comet trail without needing a full framebuffer.

_TRAIL = 2        # trail depth (frames)
_tbuf  = []       # list of [(x, y, species), …]  (newest last)


def _push_trail():
    """Snapshot current positions (Q8 → px); evict oldest frame when full."""
    _tbuf.append([(_x[i] >> 8, _y[i] >> 8, _typ[i]) for i in range(_N)])
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

    # Current particles at full brightness (2×2 px squares), Q8 → px
    for i in range(_N):
        px = _x[i] >> 8;  py = _y[i] >> 8
        draws.append((px, py, _cols[_typ[i]]))
        new_lit.add((px, py))

    # Erase only cells that were lit last frame but aren't now → no flicker
    # on pixels that stay lit, no stale trails left behind.
    _LCD.startWrite()                 # hold the SPI bus open across the
    for px, py in _lit:               # whole frame's fillRects/drawString
        if (px, py) not in new_lit:   # instead of re-acquiring per call
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
    _LCD.endWrite()

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
