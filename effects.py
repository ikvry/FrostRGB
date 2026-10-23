"""Keyboard effects for the GK104. Each effect: frame(now) -> 132 (r, g, b), on_key(index, now).

Every effect takes one params dict (see PARAM_DEFAULTS); META says which params each effect uses.
Geometry comes from gk104_keymap.json (pixel centers from GK6+).

  python effects.py demo                       # cycles all effects (type during reactive ones)
  python effects.py ripple --seconds 20
  python effects.py --selftest                 # no keyboard needed
"""
import argparse
import math
import random
import re
import threading
import time

import numpy as np

import gk104
import palettes

N = gk104.KEY_SLOTS
BLACK = (0, 0, 0)
KEYS = gk104.KEYMAP
POS = {k["index"]: (k["x"], k["y"]) for k in KEYS}
ROWN = {k["index"]: k["row"] for k in KEYS}   # 0 = function row .. 5 = bottom row
COLN = {k["index"]: k["col"] for k in KEYS}
GRID = {(k["row"], k["col"]): k["index"] for k in KEYS}
_X0, _X1 = min(x for x, _ in POS.values()), max(x for x, _ in POS.values())
_Y0, _Y1 = min(y for _, y in POS.values()), max(y for _, y in POS.values())
W, H = _X1 - _X0, _Y1 - _Y0
CX, CY = (_X0 + _X1) / 2, (_Y0 + _Y1) / 2
XN = {i: (x - _X0) / W for i, (x, _) in POS.items()}   # 0 = left .. 1 = right
YN = {i: (y - _Y0) / H for i, (_, y) in POS.items()}   # 0 = top .. 1 = bottom
_D = {i: math.hypot(x - CX, y - CY) for i, (x, y) in POS.items()}
DN = {i: d / max(_D.values()) for i, d in _D.items()}  # 0 = center .. 1 = farthest key
ANG = {i: (math.atan2(y - CY, x - CX) / (2 * math.pi)) % 1 for i, (x, y) in POS.items()}

def hsv(h, s=1.0, v=1.0):
    import colorsys
    return tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h % 1, s, v))

def scale(rgb, a):
    return (int(rgb[0] * a), int(rgb[1] * a), int(rgb[2] * a))

def lerp(a, b, t):
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))

def blank():
    return [BLACK] * N

def fade(left):
    """Fade curve for 'left' = remaining life 1..0: stays bright most of the time, drops at the end.
    Linear fades spend half their life below 50% and read as dim on the keyboard."""
    left = min(1.0, max(0.0, left))
    return 1 - (1 - left) ** 2

def boost(a, k=1.7):
    """Flat-topped falloff: the core of a ring/splash is full brightness, only the edge is soft."""
    return min(1.0, max(0.0, a * k))

def tri(t):
    """0 -> 1 -> 0 triangle wave with period 1."""
    t %= 1
    return 1 - abs(2 * t - 1)

DIRECTIONS = {"right": "Left to right", "left": "Right to left", "down": "Top to bottom", "up": "Bottom to top",
              "out": "Center out", "in": "Edges in", "diag": "Diagonal"}

def coord(i, d):
    """Position of key i along direction d, 0..1."""
    if d == "left": return 1 - XN[i]
    if d == "down": return YN[i]
    if d == "up": return 1 - YN[i]
    if d == "out": return DN[i]
    if d == "in": return 1 - DN[i]
    if d == "diag": return XN[i] * 0.7 + YN[i] * 0.3
    return XN[i]

# ------------------------------------------------------------------ params

PARAM_DEFAULTS = {"color": "FF0000", "color2": "0000FF", "speed": 1.0, "palette": "",
                  "direction": "right", "size": 1.0, "density": 1.0, "glow": 0.0}
HEX = re.compile(r"^[0-9A-Fa-f]{6}$")

def param_keys(name):
    """All param names an effect stores: the shared ones plus its own extras (META "extra")."""
    return [*PARAM_DEFAULTS, *EXTRA.get(name, {})]

def _clean_extra(spec, k, v):
    if spec["type"] == "choice":
        if v not in [o for o, _ in spec["options"]]:
            raise ValueError(f"bad {k}")
    elif spec["type"] == "bool":
        if not isinstance(v, bool):
            raise ValueError(f"bad {k}")
    elif isinstance(v, bool) or not isinstance(v, (int, float)) or not spec["min"] <= v <= spec["max"]:
        raise ValueError(f"bad {k}")
    return v

def clean_params(p, name=None):
    """Validated params with defaults. Unknown keys are dropped; bad values raise ValueError."""
    if not isinstance(p, dict):
        raise ValueError("params must be an object")
    out = dict(PARAM_DEFAULTS)
    out.update(DEFAULTS.get(name, {}))
    extra = EXTRA.get(name, {})
    for k, v in p.items():
        if k in extra:
            out[k] = _clean_extra(extra[k], k, v)
            continue
        if k not in PARAM_DEFAULTS:
            continue
        if k in ("color", "color2"):
            if not (isinstance(v, str) and HEX.match(v)):
                raise ValueError(f"bad {k}")
            v = v.upper()
        elif k == "palette":
            if not (isinstance(v, str) and palettes.exists(v)):
                raise ValueError("unknown palette")
        elif k == "direction":
            if v not in DIRECTIONS:
                raise ValueError("bad direction")
        else:
            lo, hi = {"speed": (0.1, 5), "density": (0.1, 5), "glow": (0, 0.6)}.get(k, (0.2, 5))
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"bad {k}")
        out[k] = v
    return out

class Effect:
    def __init__(self, p=None, **kw):
        p = clean_params({**(p or {}), **kw}, type(self).NAME if hasattr(type(self), "NAME") else None)
        self.p = p
        self.color, self.color2 = palettes.rgb(p["color"]), palettes.rgb(p["color2"])
        self.speed, self.size, self.density, self.direction = p["speed"], p["size"], p["density"], p["direction"]
        self.solid = p["palette"] == ""  # "" = use the effect's own colors
        self.pal = palettes.get(p["palette"], p["color"], p["color2"])
        self.lock = threading.Lock()
        self.last = None

    def dt(self, now):
        d = 0.0 if self.last is None else max(0.0, min(0.2, now - self.last))
        self.last = now
        return d

    def pick(self, t):
        """Main color, or a palette color when a palette is selected."""
        return self.color if self.solid else self.pal.sample(t)

    def on_key(self, index, now):
        pass

class Glow:
    """Wraps a sparse effect: keys it leaves dark show a dim base of the effect's colors."""
    def __init__(self, inner, amount):
        self.inner, self.amount = inner, amount
        base = [inner.pick(XN[i]) if i in XN else BLACK for i in range(N)]
        self.base = [scale(c, amount) for c in base]

    def on_key(self, index, now):
        self.inner.on_key(index, now)

    def frame(self, now):
        return [c if sum(c) >= sum(b) else b for c, b in zip(self.inner.frame(now), self.base)]

    def __getattr__(self, name):  # keep params / state reachable (tests, music modes)
        return getattr(self.inner, name)

# ------------------------------------------------------------------ ambient effects

class Static(Effect):
    def frame(self, now):
        f = blank()
        for i in POS:
            f[i] = self.color
        return f

class Breathing(Effect):
    """Fades in and out; each breath takes the next color (both colors, or the palette stops)."""
    def frame(self, now):
        period = 4.0 / self.speed
        n, phase = divmod(now / period, 1)
        level = (1 - math.cos(2 * math.pi * phase)) / 2
        stops = self.pal.stops
        c = scale(stops[int(n) % len(stops)], level)
        f = blank()
        for i in POS:
            f[i] = c
        return f

class Spectrum(Effect):
    def frame(self, now):
        f, c = blank(), self.pal.sample(now * 0.1 * self.speed)
        for i in POS:
            f[i] = c
        return f

class Wave(Effect):
    def frame(self, now):
        f, t = blank(), now * 0.4 * self.speed
        for i in POS:
            f[i] = self.pal.sample(coord(i, self.direction) * self.size - t)
        return f

class Gradient(Effect):
    def frame(self, now):
        f, shift = blank(), now * 0.05 * self.speed
        for i in POS:
            f[i] = self.pal.sample(coord(i, self.direction) * self.size * 0.5 + shift)
        return f

class Plasma(Effect):
    def frame(self, now):
        f, t, s = blank(), now * 0.8 * self.speed, self.size
        for i in POS:
            x, y = XN[i], YN[i]
            v = math.sin(x * 9 * s + t) + math.sin(y * 7 * s - t * 1.3) + math.sin((x + y) * 6 * s + t * 0.7)
            f[i] = self.pal.sample(v / 6 + t * 0.05)
        return f

class Radial(Effect):
    """Rings of color flowing out of (or into) the center."""
    def frame(self, now):
        f, t = blank(), now * 0.5 * self.speed * (-1 if self.direction == "in" else 1)
        for i in POS:
            f[i] = self.pal.sample(DN[i] * self.size * 1.5 - t)
        return f

class Spiral(Effect):
    def frame(self, now):
        f, t = blank(), now * 0.3 * self.speed
        for i in POS:
            f[i] = self.pal.sample(ANG[i] + DN[i] * self.size - t)
        return f

class Aurora(Effect):
    """Slow vertical curtains of light, brighter at the top."""
    def frame(self, now):
        f, t = blank(), now * self.speed
        for i in POS:
            x, y = XN[i], YN[i]
            band = 0.5 + 0.5 * math.sin(x * 6 * self.size + t * 0.6 + math.sin(x * 3 + t * 0.2) * 2)
            c = self.pal.sample(x * 0.6 + t * 0.03 + 0.2 * math.sin(t * 0.4))
            f[i] = scale(c, 0.25 + 0.75 * band ** 0.6 * (1 - y * 0.3))
        return f

class Ocean(Effect):
    def frame(self, now):
        f, t = blank(), now * self.speed
        for i in POS:
            x, y = XN[i], YN[i]
            v = math.sin(x * 7 * self.size - t * 1.6 + y * 2.5)
            w = math.sin(x * 3 * self.size + t * 0.7)
            f[i] = scale(self.pal.sample(0.5 + 0.25 * v + 0.25 * w, cyclic=False), 0.6 + 0.2 * (v + 1))
        return f

class Lava(Effect):
    """Lava lamp: soft blobs drifting and merging."""
    BLOBS = [(0.23, 0.31, 0.0, 1.7), (0.17, 0.27, 2.1, 0.4), (0.29, 0.19, 4.2, 2.9), (0.13, 0.37, 1.3, 5.1), (0.21, 0.23, 3.3, 0.9)]

    def frame(self, now):
        t, r2 = now * self.speed, (55 * self.size) ** 2
        blobs = [(_X0 + W * (0.5 + 0.47 * math.sin(t * a + p)), _Y0 + H * (0.5 + 0.5 * math.sin(t * b + q)))
                 for a, b, p, q in self.BLOBS]
        f = blank()
        for i, (x, y) in POS.items():
            field = sum(r2 / ((x - bx) ** 2 + (y - by) ** 2 + 1) for bx, by in blobs)
            f[i] = self.pal.sample(min(1.0, field * 0.45), cyclic=False)
        return f

class Fire(Effect):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.heat = {i: 0.0 for i in POS}
        self.cols = {}

    def frame(self, now):
        f = blank()
        for i in POS:
            c = COLN[i]
            base = self.cols[c] = min(1.0, max(0.3, self.cols.get(c, 0.7) + random.uniform(-0.15, 0.15) * self.speed))
            target = max(0.0, base * 1.25 - (5 - ROWN[i]) * 0.2 / self.size + random.uniform(-0.1, 0.1))
            h = self.heat[i] = self.heat[i] * 0.75 + target * 0.25
            f[i] = self.pal.sample(min(1.0, h), cyclic=False)
        return f

class Candle(Effect):
    """Warm flicker on every key."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.b = {i: 0.8 for i in POS}

    def frame(self, now):
        f = blank()
        for i in POS:
            self.b[i] = min(1.0, max(0.5, self.b[i] + random.uniform(-0.12, 0.12) * self.speed))
            f[i] = scale(self.color, self.b[i])
        return f

class Police(Effect):
    """Left half / right half double-flash, like emergency lights."""
    def frame(self, now):
        ph = (now * self.speed) % 1
        on = lambda p: (p % 0.5) < 0.12 or 0.2 < (p % 0.5) < 0.32
        left, right = ph < 0.5 and on(ph), ph >= 0.5 and on(ph)
        f = blank()
        for i in POS:
            if XN[i] < 0.5 and left:
                f[i] = self.color
            elif XN[i] >= 0.5 and right:
                f[i] = self.color2
        return f

class Chase(Effect):
    """Theater-marquee lights running along the keyboard."""
    def frame(self, now):
        gap = max(2, int(round(3 * self.size)))
        step = int(now * 8 * self.speed)
        vertical = self.direction in ("up", "down")
        sign = -1 if self.direction in ("left", "up") else 1
        f = blank()
        for i in POS:
            c = ROWN[i] if vertical else COLN[i]
            if (c - sign * step) % gap == 0:
                f[i] = self.pal.sample(XN[i] + now * 0.05)
        return f

class Scanner(Effect):
    """A bar sweeping back and forth (KITT / Cylon)."""
    def frame(self, now):
        p = tri(now * 0.35 * self.speed)
        vertical = self.direction in ("up", "down")
        width = (0.2 if vertical else 0.07) * self.size
        c = self.pick(p)
        f = blank()
        for i in POS:
            a = boost(1 - abs((YN[i] if vertical else XN[i]) - p) / width)
            if a > 0:
                f[i] = scale(c, a)
        return f

# ------------------------------------------------------------------ particle effects

class Comet(Effect):
    """Comets with fading tails shooting along the rows."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.comets = []  # [row, x, dir, color]

    def frame(self, now):
        dt = self.dt(now)
        if random.random() < 1.6 * self.density * dt:
            d = {"right": 1, "left": -1}.get(self.direction, random.choice((1, -1)))
            self.comets.append([random.randrange(6), -0.1 if d > 0 else 1.1, d, self.pick(random.random())])
        for c in self.comets:
            c[1] += c[2] * dt * 0.7 * self.speed
        self.comets = [c for c in self.comets if -0.5 < c[1] < 1.5]
        tail = 0.25 * self.size
        f = blank()
        for row, x, d, col in self.comets:
            for i in POS:
                if ROWN[i] == row:
                    back = (x - XN[i]) * d
                    if 0 <= back <= tail:
                        f[i] = max(f[i], scale(col, fade(1 - back / tail)), key=sum)
        return f

class Matrix(Effect):
    """Drops falling down the columns with a fading trail."""
    NAME = "matrix"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.drops = []  # [col, row position, color]

    def frame(self, now):
        dt = self.dt(now)
        if random.random() < 8 * self.speed * self.density * dt:
            self.drops.append([random.randrange(22), -1.0, self.pick(random.random())])
        for d in self.drops:
            d[1] += dt * 7 * self.speed
        self.drops = [d for d in self.drops if d[1] < 9]
        f = blank()
        trail = 3 * self.size
        for i in POS:
            best = None
            for c, pos, col in self.drops:
                if c == COLN[i] and ROWN[i] <= pos:
                    a = fade(1 - (pos - ROWN[i]) / trail)
                    if a > 0 and (best is None or a > best[0]):
                        best = (a, col)
            if best:
                f[i] = scale(best[1], best[0])
        return f

class Starlight(Effect):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.stars = {}

    def frame(self, now):
        dt = self.dt(now)
        dur = 2.0 / self.speed
        for i in POS:
            if i not in self.stars and random.random() < 0.15 * self.density * dt / dur:
                self.stars[i] = (now, self.pick(random.random()))
        f = blank()
        for i, (t0, c) in list(self.stars.items()):
            p = (now - t0) / dur
            if p >= 1:
                del self.stars[i]
            else:
                f[i] = scale(c, math.sin(math.pi * p) ** 0.5)  # quick rise, long bright middle
        return f

class Confetti(Effect):
    """Fast random pops of palette colors."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.dots = {}

    def frame(self, now):
        dt = self.dt(now)
        n = 25 * self.density * self.speed * dt
        for _ in range(int(n) + (random.random() < n % 1)):
            self.dots[random.choice(list(POS))] = (now, self.pal.sample(random.random()))
        life = 0.7 / self.speed
        f = blank()
        for i, (t0, c) in list(self.dots.items()):
            left = 1 - (now - t0) / life
            if left <= 0:
                del self.dots[i]
            else:
                f[i] = scale(c, fade(left))
        return f

class Fireworks(Effect):
    """Rockets climb a column and burst into rings of color."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rockets, self.bursts = [], []  # [col, row pos, target, color], [x, y, t0, color]
        self.cols = sorted({c for (_, c) in GRID})

    def frame(self, now):
        dt = self.dt(now)
        if random.random() < 0.9 * self.density * self.speed * dt:
            self.rockets.append([random.choice(self.cols), 5.6, random.uniform(0, 2.2), self.pal.sample(random.random())])
        f = blank()
        alive = []
        for r in self.rockets:
            r[1] -= dt * 7 * self.speed
            key = GRID.get((max(0, round(r[1])), r[0]))
            if r[1] <= r[2]:
                x = _X0 + W * r[0] / 21
                self.bursts.append([x, _Y0 + H * max(0.0, r[1]) / 5, now, r[3]])
            else:
                alive.append(r)
                if key is not None:
                    f[key] = (255, 240, 200)
        self.rockets = alive
        life = 1.1 / self.speed
        self.bursts = [b for b in self.bursts if now - b[2] < life]
        for i, (x, y) in POS.items():
            best = None
            for bx, by, t0, c in self.bursts:
                age = (now - t0) / life
                radius = age * 160 * self.size
                dist = math.hypot(x - bx, y - by)
                # bright ring plus a softer glow inside it, so the burst reads as a filled flower
                ring = max(boost(1 - abs(dist - radius) / 45), 0.45 * boost(1 - dist / max(radius, 1)))
                a = ring * fade(1 - age)
                if a > 0 and (best is None or a > best[0]):
                    best = (a, c)
            if best:
                f[i] = max(f[i], scale(best[1], best[0]), key=sum)
        return f

class Snake(Effect):
    """A snake roaming the keyboard chasing food; grows when it eats."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.cells = set(GRID)
        self.reset()
        self.acc = 0.0

    def reset(self):
        start = random.choice(list(self.cells))
        self.body, self.dir = [start], (0, 1)
        self.grow = int(4 * self.size)
        self.food = random.choice(list(self.cells))

    def step(self):
        r, c = self.body[0]
        fr, fc = self.food
        dirs = [(0, 1), (0, -1), (1, 0), (-1, 0)]
        random.shuffle(dirs)
        dirs.sort(key=lambda d: abs(r + d[0] - fr) + abs(c + d[1] - fc) + (0 if d == self.dir else 0.5) + random.random())
        for d in dirs:
            nxt = (r + d[0], c + d[1])
            if nxt in self.cells and nxt not in self.body:
                self.dir = d
                self.body.insert(0, nxt)
                if nxt == self.food:
                    self.grow += 2
                    self.food = random.choice(list(self.cells - set(self.body)) or [nxt])
                if self.grow > 0:
                    self.grow -= 1
                else:
                    self.body.pop()
                if len(self.body) > 30:
                    self.reset()
                return
        self.reset()  # trapped

    def frame(self, now):
        self.acc += self.dt(now) * 12 * self.speed
        while self.acc >= 1:
            self.acc -= 1
            self.step()
        f = blank()
        n = len(self.body)
        for k, cell in enumerate(self.body):
            f[GRID[cell]] = scale(self.pal.sample(k / max(n, 1) * 0.6 + now * 0.05), 1 - k / max(n, 1) * 0.4)
        if int(now * 4) % 2:
            f[GRID[self.food]] = (255, 255, 255)
        return f

class Life(Effect):
    """Conway's Game of Life on the key grid; reseeds when it dies out or loops."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.cells = set(GRID)
        self.alive, self.age, self.history = {}, {}, []
        self.acc, self.glow = 0.0, {i: 0.0 for i in POS}
        self.seed()

    def seed(self):
        self.alive = {c for c in self.cells if random.random() < 0.35}
        self.age = {c: 0 for c in self.alive}
        self.history = []

    def step(self):
        nb = {}
        for r, c in self.alive:
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr or dc:
                        nb[(r + dr, c + dc)] = nb.get((r + dr, c + dc), 0) + 1
        new = {cell for cell, n in nb.items() if cell in self.cells and (n == 3 or (n == 2 and cell in self.alive))}
        self.age = {c: self.age.get(c, -1) + 1 for c in new}
        key = frozenset(new)
        if len(new) < 3 or key in self.history:
            self.seed()
            return
        self.history = (self.history + [key])[-6:]
        self.alive = new

    def frame(self, now):
        self.acc += self.dt(now) * 4 * self.speed
        while self.acc >= 1:
            self.acc -= 1
            self.step()
        f = blank()
        for cell, i in GRID.items():
            target = 1.0 if cell in self.alive else 0.0
            g = self.glow[i] = self.glow[i] + (target - self.glow[i]) * 0.25
            if g > 0.02:
                f[i] = scale(self.pal.sample(self.age.get(cell, 0) * 0.07 + XN[i] * 0.3), g)
        return f

# ------------------------------------------------------------------ typing-reactive effects

class Reactive(Effect):
    """Pressed key lights up and fades out."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pressed = {}  # index -> (press time, color)

    def on_key(self, index, now):
        with self.lock:
            self.pressed[index] = (now, self.pick(random.random()))

    def frame(self, now):
        dur = 1.0 / self.speed
        f = blank()
        with self.lock:
            for i, (t0, c) in list(self.pressed.items()):
                left = 1 - (now - t0) / dur
                if left <= 0:
                    del self.pressed[i]
                elif i < N:
                    f[i] = scale(c, fade(left))
        return f

class Ripple(Effect):
    """Ring expanding from the pressed key."""
    WIDTH = 45  # ring thickness in GK6+ layout pixels (a key is ~42 px)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ripples = []  # (x, y, t0, color)

    def on_key(self, index, now):
        if index in POS:
            with self.lock:
                self.ripples.append((*POS[index], now, self.pick(random.random())))

    def frame(self, now):
        life = 1.5 / self.speed
        f = blank()
        with self.lock:
            self.ripples = [r for r in self.ripples if now - r[2] < life]
            ripples = list(self.ripples)
        for i, (x, y) in POS.items():
            best = None
            for rx, ry, t0, c in ripples:
                age = now - t0
                radius = age * 600 * self.speed
                ring = boost(1 - abs(math.hypot(x - rx, y - ry) - radius) / (self.WIDTH * self.size))
                a = ring * fade(1 - age / life)
                if a > 0 and (best is None or a > best[0]):
                    best = (a, c)
            if best:
                f[i] = scale(best[1], best[0])
        return f

class Splash(Effect):
    """Pressed key splashes palette colors onto its neighbors."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.hits = []  # (x, y, t0, palette offset)

    def on_key(self, index, now):
        if index in POS:
            with self.lock:
                self.hits.append((*POS[index], now, random.random()))

    def frame(self, now):
        dur, radius = 0.9 / self.speed, 110 * self.size
        with self.lock:
            self.hits = [h for h in self.hits if now - h[2] < dur]
            hits = list(self.hits)
        f = blank()
        for i, (x, y) in POS.items():
            best = None
            for hx, hy, t0, off in hits:
                d = math.hypot(x - hx, y - hy) / radius
                a = boost(1 - d) * fade(1 - (now - t0) / dur)
                if a > 0 and (best is None or a > best[0]):
                    best = (a, self.pal.sample(off + d * 0.5))
            if best:
                f[i] = scale(best[1], best[0])
        return f

class Cross(Effect):
    """Pressed key shoots light along its row and column."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.hits = []  # (row, col, t0, color)

    def on_key(self, index, now):
        if index in ROWN:
            with self.lock:
                self.hits.append((ROWN[index], COLN[index], now, self.pick(random.random())))

    def frame(self, now):
        dur = 0.8 / self.speed
        with self.lock:
            self.hits = [h for h in self.hits if now - h[2] < dur]
            hits = list(self.hits)
        f = blank()
        for i in POS:
            r, c = ROWN[i], COLN[i]
            best = None
            for hr, hc, t0, col in hits:
                age = now - t0
                reach = age * 30 * self.speed * self.size
                dist = abs(c - hc) if r == hr else abs(r - hr) * 2 if c == hc else None
                if dist is not None and dist <= reach:
                    a = fade(1 - age / dur) * (1 - dist / max(reach, 1) * 0.3)
                    if a > 0 and (best is None or a > best[0]):
                        best = (a, col)
            if best:
                f[i] = scale(best[1], best[0])
        return f

class Heatmap(Effect):
    """Keys you press most glow hotter; cools down over time."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.heat = {}

    def on_key(self, index, now):
        with self.lock:
            self.heat[index] = self.heat.get(index, 0.0) + 1

    def frame(self, now):
        decay = math.exp(-self.dt(now) * 0.15 * self.speed)
        f = blank()
        with self.lock:
            for i in POS:
                h = self.heat[i] = self.heat.get(i, 0.0) * decay
                v = min(1.0, h / (3 * self.size))
                f[i] = scale(self.pal.sample(v, cyclic=False), 0.45 + 0.55 * v)  # cold keys stay visible
        return f

_KEYMAP = {k["index"]: k for k in KEYS}

class Screen(Effect):
    """Ambient light from the main monitor. Modes: mirror (each key shows the screen above it),
    dominant (the most prominent color), top colors (the 3 most prominent, laid out where they are
    on screen), average. Extras: area, black-bar crop, saturation, min brightness. speed = smoothing."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        import screen  # Windows GDI; imported here so effects.py stays importable anywhere
        self.cap = screen.CAPTURE
        # each key's rectangle as fractions of the (possibly cropped) image: y0, y1, x0, x1
        self.rects = {i: (max(0.0, YN[i] - k["h"] / 2 / H), min(1.0, YN[i] + k["h"] / 2 / H),
                          max(0.0, XN[i] - k["w"] / 2 / W), min(1.0, XN[i] + k["w"] / 2 / W))
                      for i, k in _KEYMAP.items()}
        self.cur = {}

    def _area(self, img):
        if self.p["crop"]:  # letterbox / pillarbox: drop edge rows/cols that are (almost) black
            lum = img.mean(axis=2)
            rows, cols = np.where(lum.max(axis=1) > 16)[0], np.where(lum.max(axis=0) > 16)[0]
            if len(rows) >= 3 and len(cols) >= 3:
                img = img[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
        if self.p["area"] == "center":  # middle 60%: skips HUDs, taskbar, subtitles
            h, w = img.shape[:2]
            img = img[int(h * 0.2):max(int(h * 0.2) + 1, int(h * 0.8)), int(w * 0.2):max(int(w * 0.2) + 1, int(w * 0.8))]
        return img

    @staticmethod
    def peaks(img, n):
        """Up to n prominent colors: hue histogram weighted by saturation^2 * value, so vivid areas win
        over gray/dark ones. Returns [(rgb, x_center 0..1)], strongest first."""
        h, w = img.shape[:2]
        px = img.reshape(-1, 3).astype(np.float32)
        mx, mn = px.max(axis=1), px.min(axis=1)
        weight = ((mx - mn) / np.maximum(mx, 1)) ** 2 * (mx / 255)
        r, g, b = px[:, 0], px[:, 1], px[:, 2]
        hue = (np.degrees(np.arctan2(np.sqrt(3) * (g - b), 2 * r - g - b)) % 360) / 360
        bins = (hue * 24).astype(int) % 24
        hist = np.bincount(bins, weights=weight, minlength=24)
        xs = np.tile(np.arange(w) / max(1, w - 1), h)
        out, used = [], set()
        for k in np.argsort(hist)[::-1]:
            if len(out) == n or hist[k] <= 1e-6 or hist[k] < hist.max() * 0.15:
                break
            fam = {(k - 1) % 24, int(k), (k + 1) % 24}
            if fam & used:
                continue  # next to a hue already picked: same color family
            sel = np.isin(bins, list(fam))
            wk = weight[sel]
            out.append((tuple((px[sel] * wk[:, None]).sum(0) / wk.sum()), float((xs[sel] * wk).sum() / wk.sum())))
            used |= fam
        return out

    def frame(self, now):
        img, f = self.cap.latest(), blank()
        if img is None:
            return f
        img = self._area(img)
        h, w = img.shape[:2]
        mode = self.p["smode"]
        avg = tuple(img.reshape(-1, 3).mean(0))
        if mode == "mirror":
            colors = {}
            for i, (y0, y1, x0, x1) in self.rects.items():
                ys = slice(int(y0 * h), max(int(y0 * h) + 1, int(y1 * h)))
                xs = slice(int(x0 * w), max(int(x0 * w) + 1, int(x1 * w)))
                colors[i] = tuple(img[ys, xs].reshape(-1, 3).mean(0))
        elif mode == "average":
            colors = dict.fromkeys(_KEYMAP, avg)
        else:
            pk = self.peaks(img, 1 if mode == "dominant" else 3)
            if not pk:  # gray / dark scene: nothing stands out
                colors = dict.fromkeys(_KEYMAP, avg)
            elif mode == "dominant" or len(pk) == 1:
                colors = dict.fromkeys(_KEYMAP, pk[0][0])
            else:  # top colors, placed left to right where they sit on screen
                pts = sorted((x, c) for c, x in pk)
                colors = {}
                for i in _KEYMAP:
                    x = min(max(XN[i], pts[0][0]), pts[-1][0])
                    k = next(j for j in range(len(pts) - 1) if x <= pts[j + 1][0])
                    t = (x - pts[k][0]) / max(1e-6, pts[k + 1][0] - pts[k][0])
                    colors[i] = tuple(p0 + (p1 - p0) * t for p0, p1 in zip(pts[k][1], pts[k + 1][1]))
        a = 1 - math.exp(-self.dt(now) * 10 * self.speed)
        sat, floor = self.p["sat"], self.p["floor"] * 255
        for i, (r, g, b) in colors.items():
            lum = (r + g + b) / 3
            r, g, b = (min(255.0, max(0.0, lum + (c - lum) * sat)) for c in (r, g, b))
            m = max(r, g, b)
            if 0 < m < floor:  # dark scenes keep their hue instead of going off
                r, g, b = (c * floor / m for c in (r, g, b))
            o = self.cur.get(i, (r, g, b))
            o = self.cur[i] = tuple(p0 + (n - p0) * a for p0, n in zip(o, (r, g, b)))
            f[i] = (int(o[0]), int(o[1]), int(o[2]))
        return f

# ------------------------------------------------------------------ registry

# colors: how many own colors the effect uses; palette: default palette ("" = own colors, None = no palette);
# opts: extra controls shown in the UI; reactive: reacts to typing.
def _m(cls, desc, colors=0, palette="rainbow", opts=(), reactive=False, extra=None, **defaults):
    """extra: effect-only params {key: {label, type: choice|bool|num, default, options | min/max/step}}."""
    extra = extra or {}
    return cls, {"desc": desc, "colors": colors, "palette": palette, "opts": list(opts), "reactive": reactive,
                 "extra": extra, "defaults": {**({"palette": palette} if palette is not None else {}), **defaults,
                                              **{k: e["default"] for k, e in extra.items()}}}

_REG = {
    "static":     _m(Static, "One solid color", 1, None),
    "breathing":  _m(Breathing, "Slowly fades in and out, changing color", 2, ""),
    "spectrum":   _m(Spectrum, "Whole keyboard cycles through colors"),
    "wave":       _m(Wave, "Color wave in the chosen direction", opts=("direction", "size")),
    "gradient":   _m(Gradient, "Smooth sliding gradient", 2, "", opts=("direction", "size")),
    "plasma":     _m(Plasma, "Flowing plasma colors", opts=("size",)),
    "radial":     _m(Radial, "Rings expanding from the center", opts=("size",), direction="out"),
    "spiral":     _m(Spiral, "Rotating spiral", opts=("size",)),
    "aurora":     _m(Aurora, "Northern lights curtains", palette="aurora", opts=("size",)),
    "ocean":      _m(Ocean, "Ocean waves", palette="ocean", opts=("size",)),
    "lava":       _m(Lava, "Lava lamp blobs", palette="lava", opts=("size",)),
    "fire":       _m(Fire, "Flames rising from the bottom", palette="fire", opts=("size",)),
    "candle":     _m(Candle, "Flickering candle light", 1, None, color="FF8C1A"),
    "police":     _m(Police, "Red and blue siren", 2, None),
    "chase":      _m(Chase, "Running marquee lights", opts=("direction", "size")),
    "scanner":    _m(Scanner, "Bar sweeping back and forth", 1, "", opts=("direction", "size", "glow")),
    "comet":      _m(Comet, "Comets crossing the rows", opts=("direction", "size", "density", "glow")),
    "matrix":     _m(Matrix, "Digital rain down the columns", 1, "", opts=("size", "density", "glow"), color="00FF41"),
    "starlight":  _m(Starlight, "Keys twinkling like stars", 1, "", opts=("density", "glow"), color="FFFFFF"),
    "confetti":   _m(Confetti, "Colorful confetti bursts", opts=("density", "glow")),
    "fireworks":  _m(Fireworks, "Fireworks", opts=("size", "density", "glow")),
    "snake":      _m(Snake, "Snake hunting for food", opts=("size", "glow")),
    "life":       _m(Life, "Conway's Game of Life", palette="toxic", opts=("glow",)),
    "reactive":   _m(Reactive, "Pressed keys light up and fade", 1, "", opts=("glow",), reactive=True),
    "ripple":     _m(Ripple, "Ripple from the pressed key", 1, "", opts=("size", "glow"), reactive=True),
    "splash":     _m(Splash, "Color splash while typing", opts=("size", "glow"), reactive=True),
    "cross":      _m(Cross, "Row and column of the key light up", 1, "", opts=("size", "glow"), reactive=True),
    "heatmap":    _m(Heatmap, "Most used keys heat up", palette="heat", opts=("size",), reactive=True),
    "screen":     _m(Screen, "Mirrors the colors of your main monitor (ambient light)", palette=None, extra={
        "smode": {"label": "Mode", "type": "choice", "default": "mirror", "options": [
            ["mirror", "Mirror"], ["dominant", "Dominant"], ["palette", "Top colors"], ["average", "Average"]]},
        "area": {"label": "Area", "type": "choice", "default": "full", "options": [["full", "Whole screen"], ["center", "Center"]]},
        "crop": {"label": "Ignore black bars", "type": "bool", "default": True},
        "sat": {"label": "Saturation", "type": "num", "min": 0.0, "max": 2.5, "step": 0.05, "default": 1.35, "fmt": "pct"},
        "floor": {"label": "Min brightness", "type": "num", "min": 0.0, "max": 0.5, "step": 0.01, "default": 0.0, "fmt": "pct"}}),
}
EFFECTS = {k: v[0] for k, v in _REG.items()}
META = {k: v[1] for k, v in _REG.items()}
DEFAULTS = {k: v["defaults"] for k, v in META.items()}
EXTRA = {k: v["extra"] for k, v in META.items() if v["extra"]}  # per-name param defaults (music.py adds its own)
for _name, _cls in EFFECTS.items():
    _cls.NAME = _name

def make(name, params=None, **kw):
    e = EFFECTS[name]({**(params or {}), **kw})
    return Glow(e, e.p["glow"]) if e.p["glow"] > 0 and "glow" in META[name]["opts"] else e

def play(kb, effect, seconds):
    """Run one effect on an already-online keyboard; key presses go to effect.on_key."""
    names = {k["index"]: k["name"] for k in KEYS}
    kb.on_key = lambda i: (effect.on_key(i, time.monotonic()), print("key", names[i], flush=True))
    end = time.monotonic() + seconds
    last_ping = last = 0
    while (now := time.monotonic()) < end:
        if now - last_ping > 1.5:
            kb.ping()
            last_ping = now
        f = effect.frame(now)
        if f != last:
            kb.frame(f)
            last = f
        else:
            time.sleep(0.005)
    kb.on_key = None

def selftest():
    import numpy as np
    class FakeCap:  # left half red, right half blue, top rows green
        img = np.zeros((24, 88, 3), np.uint8)
        img[:, :44] = (255, 0, 0)
        img[:, 44:] = (0, 0, 255)
        img[:3] = (0, 255, 0)
        def latest(self): return self.img
    sc = make("screen")
    sc.cap = FakeCap()
    f = sc.frame(1.0)
    k = gk104.KEY_INDEX
    assert f[k["a"]][0] > 200 and f[k["a"]][2] < 50, ("left of screen -> left keys", f[k["a"]])
    assert f[k["keypad 5"]][2] > 200, "right of screen -> numpad"
    assert f[k["escape"]][1] > 150, "top of screen -> F row"
    sc = make("screen", {"smode": "dominant", "crop": False})
    sc.cap = FakeCap()
    d = sc.frame(1.0)[k["a"]]
    assert d[0] > 200 or d[2] > 200, ("dominant picks a vivid color, not the mix", d)
    sc = make("screen", {"smode": "palette", "crop": False})
    sc.cap = FakeCap()
    f = sc.frame(1.0)
    assert f[k["escape"]] != f[k["keypad 5"]], "top colors spread across the board"
    letterbox = FakeCap()
    letterbox.img = np.zeros((24, 88, 3), np.uint8)
    letterbox.img[6:18] = (0, 0, 255)  # movie with black bars
    sc = make("screen", {"crop": True})
    sc.cap = letterbox
    assert sc.frame(1.0)[k["escape"]][2] > 200, "black bars cropped: top row shows the movie"
    for bad in ({"smode": "x"}, {"sat": 9}, {"crop": 1}):
        try:
            clean_params(bad, "screen")
            raise AssertionError(bad)
        except ValueError:
            pass
    w = gk104.KEY_INDEX["w"]
    for name in EFFECTS:
        for pal in ("rainbow", "", "fire"):
            e = make(name, palette=pal, color="FF0000", density=3, speed=2)
            e.on_key(w, 10.0)
            for t in (10.0, 10.05, 10.4, 11.0, 13.0):
                f = e.frame(t)
                assert len(f) == N and all(len(c) == 3 and all(0 <= v <= 255 for v in c) for c in f), name
    r = make("reactive", color="FF0000")
    r.on_key(w, 0.0)
    assert r.frame(0.0)[w] == (255, 0, 0) and r.frame(0.5)[w][0] == 191 and r.frame(1.1)[w] == BLACK, "75% at half life"
    p = make("ripple", color="FFFFFF")
    p.on_key(w, 0.0)
    assert p.frame(0.0)[w] != BLACK, "ripple starts at the key"
    assert p.frame(0.3)[w] == BLACK and any(p.frame(0.3)), "ring moved away"
    gl = make("reactive", color="FF0000", glow=0.3)
    assert gl.frame(0)[w] == (76, 0, 0), "glow fills unlit keys"
    gl.on_key(w, 0.0)
    assert gl.frame(0.0)[w] == (255, 0, 0), "effect wins over glow"
    s = make("static", color="00FF00")
    assert s.frame(0)[w] == (0, 255, 0)
    g = gk104.KEY_INDEX["f"]  # a key off-center: left and right waves must differ there
    assert make("wave", direction="left").frame(0)[g] != make("wave").frame(0)[g]
    for bad in ({"color": "red"}, {"speed": 0}, {"palette": "nope"}, {"direction": "sideways"}, {"size": True}):
        try:
            clean_params(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert clean_params({"junk": 1}) == PARAM_DEFAULTS, "unknown keys dropped"
    print("selftest ok:", len(EFFECTS), "effects")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("effect", nargs="?", choices=[*EFFECTS, "demo"], default="demo")
    ap.add_argument("--palette", default=None)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--seconds", type=float, default=15)
    ap.add_argument("--restore", type=int, default=1, choices=[0, 1, 2, 3, 4])
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    names = list(EFFECTS) if a.effect == "demo" else [a.effect]
    secs = 5 if a.effect == "demo" else a.seconds
    kb = gk104.Keyboard()
    try:
        kb.enter_online()
        for name in names:
            print(f"== {name}", flush=True)
            kw = {"speed": a.speed, **({"palette": a.palette} if a.palette is not None else {})}
            play(kb, make(name, **kw), secs)
    finally:
        kb.leave_online(a.restore)
        kb.close()

if __name__ == "__main__":
    main()
