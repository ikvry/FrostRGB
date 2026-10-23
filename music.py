"""Music-reactive modes for the GK104. Each reads an analyzer snapshot every frame.

Same interface and params as effects: frame(now) -> 132 (r, g, b), on_key(index, now).
"""
import math
import random

import effects
import gk104
from effects import BLACK, DN, POS, ROWN, XN, blank, boost, fade, scale

def lift(v):
    """Band levels sit around 0.3; lift them so a normal song lights the keys well."""
    return min(1.0, max(0.0, v)) ** 0.5

def band_of(x, n):
    return min(n - 1, int(x * n))

class MusicMode(effects.Effect):
    def __init__(self, analyzer, params=None, **kw):
        super().__init__(params, **kw)
        self.an = analyzer
        self.seen_beat = 0

    def new_beat(self, s):
        if s["beat"] != self.seen_beat:
            self.seen_beat = s["beat"]
            return True
        return False

class Equalizer(MusicMode):
    """Spectrum bars: one band per column, rising from the bottom row."""
    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        bands = s["bands"]
        for i in POS:
            level = 5 - ROWN[i]  # 0 bottom row .. 5 function row
            if bands[band_of(XN[i], len(bands))] * 6 > level + 0.15:  # each sixth lights one more row
                f[i] = self.pal.sample(level / 5, cyclic=False)
        return f

class BandsGlow(MusicMode):
    """Every column glows with its band; color by position."""
    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        bands = s["bands"]
        for i in POS:
            v = bands[band_of(XN[i], len(bands))]
            f[i] = scale(self.pal.sample(XN[i] * self.size + now * 0.03 * self.speed), 0.12 + 0.88 * lift(v))
        return f

class RadialEq(MusicMode):
    """Spectrum mapped from the center out: bass in the middle, treble at the edges."""
    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        bands = s["bands"]
        for i in POS:
            k = band_of(DN[i], len(bands))
            f[i] = scale(self.pal.sample(k / len(bands) + now * 0.03 * self.speed), 0.08 + 0.92 * lift(bands[k]))
        return f

class VUMeter(MusicMode):
    """Volume grows from the center out to both sides."""
    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        for i in POS:
            d = abs(XN[i] - 0.5) * 2  # 0 center .. 1 edges
            if d < s["level"]:
                f[i] = self.pal.sample(d, cyclic=False)
        return f

class Pulse(MusicMode):
    """Whole keyboard breathes with the volume; color jumps on every beat."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.hue = 0.0

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        if self.new_beat(s):
            self.hue = (self.hue + 0.13) % 1
        c = scale(self.pick(self.hue), 0.1 + 0.9 * s["level"])
        for i in POS:
            f[i] = c
        return f

class BeatFlash(MusicMode):
    """Every beat flashes the board in the next palette color, then it fades."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.t0, self.k, self.c = -10.0, 0, BLACK

    def frame(self, now):
        s = self.an.snapshot()
        if self.new_beat(s):
            self.t0, self.k = now, self.k + 1
            self.c = self.pick(self.k * 0.17)
        a = fade(1 - (now - self.t0) / (0.35 / self.speed))
        f = blank()
        for i in POS:
            f[i] = scale(self.c, a)
        return f

class BeatRipple(MusicMode):
    """Every beat fires a ring from a random key."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ripple = effects.make("ripple", {**self.p, "palette": self.p["palette"]})

    def frame(self, now):
        if self.new_beat(self.an.snapshot()):
            self.ripple.on_key(random.choice(list(POS)), now)
        return self.ripple.frame(now)

    def on_key(self, index, now):
        self.ripple.on_key(index, now)

class BassWave(MusicMode):
    """Wave that speeds up and brightens with the bass."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.phase = 0.0

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        self.phase += self.dt(now) * (0.1 + 1.5 * s["bass"]) * self.speed
        v = 0.15 + 0.85 * max(s["bass"], s["level"])
        for i in POS:
            f[i] = scale(self.pal.sample(effects.coord(i, self.direction) * self.size - self.phase), v)
        return f

class Sparkle(MusicMode):
    """Hi-hats and cymbals light random keys; beats add a burst."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.stars = {}

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        dt = self.dt(now)
        burst = 12 if self.new_beat(s) else 0
        rate = s["treble"] * 60 * self.speed * self.density * dt + burst
        for _ in range(int(rate) + (random.random() < rate % 1)):
            self.stars[random.choice(list(POS))] = (now, self.pick(random.random()))
        for i, (t0, c) in list(self.stars.items()):
            left = 1 - (now - t0) / 0.5
            if left <= 0:
                del self.stars[i]
            else:
                f[i] = scale(c, fade(left))
        return f

class ColorByFrequency(MusicMode):
    """One color for the whole board: bass = red, mids = green, treble = blue."""
    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        mix = (s["bass"], s["mid"], s["treble"])
        top = max(mix)
        # hue from the mix, brightness from the volume: quiet treble no longer means a dark board
        c = tuple(int(255 * v / top * (0.25 + 0.75 * lift(s["level"]))) for v in mix) if top > 0.01 else BLACK
        for i in POS:
            f[i] = c
        return f

def group(bands, k, n):
    """Mean of the k-th of n equal slices of the band list (k = 0 is the bass end)."""
    a, b = k * len(bands) // n, max(k * len(bands) // n + 1, (k + 1) * len(bands) // n)
    return sum(bands[a:b]) / (b - a)

ROWS = 6
COLS = 1 + max(effects.COLN.values())

class Waterfall(MusicMode):
    """Scrolling spectrogram: each row is a frequency range (bass at the bottom), time flows right to left."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.hist, self.acc = [[0.0] * ROWS for _ in range(COLS)], 0.0

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        self.acc += self.dt(now) * 12 * self.speed
        while self.acc >= 1:  # one column step every 1/12 s at speed 1
            self.acc -= 1
            self.hist = self.hist[1:] + [[group(s["bands"], ROWS - 1 - r, ROWS) for r in range(ROWS)]]
        for i in POS:
            v = lift(self.hist[effects.COLN[i]][ROWN[i]])
            if v > 0.12:
                f[i] = scale(self.pal.sample(v, cyclic=False), boost(v, 2.2))
        return f

class Shockwave(MusicMode):
    """Every bass hit sends a ring from the center; its color comes from the strongest frequency."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rings = []

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        if self.new_beat(s):
            b = s["bands"]
            self.rings.append((now, self.pick(b.index(max(b)) / len(b)), 0.6 + 0.4 * s["bass"]))
        self.rings = [r for r in self.rings if now - r[0] < 1.2 / self.speed]
        width = 0.16 * self.size
        for i in POS:
            best = 0.0
            for t0, c, power in self.rings:
                age = (now - t0) * self.speed
                d = abs(DN[i] - age * 1.1)
                if d < width:
                    best = max(best, (1 - d / width) * power * fade(1 - age / 1.2))
                    f[i] = scale(c, boost(best, 2.2))
        return f

class Lightning(MusicMode):
    """Strong beats strike jagged bolts down the keyboard; the board flickers after each strike."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.bolts, self.t_flash = [], -9.0

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        if self.new_beat(s) and s["level"] > 0.2:
            for _ in range(1 + int(self.density * s["bass"] * 2)):
                col, path = random.randrange(COLS), set()
                for r in range(ROWS):
                    path.add((r, col))
                    col = max(0, min(COLS - 1, col + random.choice((-1, 0, 1))))
                self.bolts.append((now, path, self.pick(random.random())))
            self.t_flash = now
        self.bolts = [b for b in self.bolts if now - b[0] < 0.7]
        flash = max(0.0, 1 - (now - self.t_flash) / 0.25) * 0.45 * (random.random() > 0.3)
        for i in POS:
            c = scale((180, 190, 255), flash)
            for t0, path, color in self.bolts:
                if (ROWN[i], effects.COLN[i]) in path:
                    c = scale(color, boost(fade(1 - (now - t0) / 0.7), 1.5))
            f[i] = c
        return f

class Kaleidoscope(MusicMode):
    """The spectrum mirrored from the center out in all four directions, slowly rotating hue."""
    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        bands, spin = s["bands"], now * 0.05 * self.speed + s["mid"] * 0.3
        for i in POS:
            dx, dy = abs(XN[i] - 0.5) * 2, abs(effects.YN[i] - 0.5) * 2
            d = min(1.0, (dx * dx + dy * dy) ** 0.5 / 1.2)
            v = lift(bands[band_of(d, len(bands))])
            f[i] = scale(self.pal.sample(d * self.size + spin + effects.ANG[i] * 0.5), 0.15 + 0.85 * boost(v, 1.8))
        return f

class Fountain(MusicMode):
    """Bass shoots sparks up from the bottom row; they arc and fall back with gravity."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sparks = []  # [x, y, vx, vy, color]

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        dt = self.dt(now) * self.speed
        burst = 10 if self.new_beat(s) else 0
        rate = s["bass"] * 40 * self.density * dt + burst * s["level"]
        for _ in range(int(rate) + (random.random() < rate % 1)):
            x = random.random()
            self.sparks.append([x, 1.0, (0.5 - x) * 0.4 + random.uniform(-0.2, 0.2),
                                -(1.2 + 1.3 * s["bass"]) * random.uniform(0.7, 1.1), self.pick(x)])
        light = {}
        for p in self.sparks:
            p[3] += 2.6 * dt  # gravity
            p[0] += p[2] * dt
            p[1] += p[3] * dt
        self.sparks = [p for p in self.sparks if p[1] <= 1.05 and 0 <= p[0] <= 1][-150:]
        for x, y, _, _, c in self.sparks:
            light[(min(COLS - 1, int(x * COLS)), min(ROWS - 1, max(0, int(y * ROWS))))] = c
        for i in POS:
            c = light.get((min(COLS - 1, int(XN[i] * COLS)), ROWN[i]))
            if c:
                f[i] = c
        return f

HALFW = {k["index"]: k["w"] / 2 / effects.W for k in gk104.KEYMAP}  # key half-width, 0..1 units

def wave_at(wave, x):
    """Linear interpolation of the trace at x (0..1)."""
    p = min(max(x, 0.0), 1.0) * (len(wave) - 1)
    k = min(int(p), len(wave) - 2)
    return wave[k] + (wave[k + 1] - wave[k]) * (p - k)

class Scope(MusicMode):
    """Real oscilloscope: the system audio waveform, triggered on a rising zero crossing so it stands
    still. The trace is drawn as a continuous line (the jump between columns is filled like a scope
    beam) with phosphor afterglow. Size = time window (zoom), speed = how fast the afterglow fades."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.glow = {}  # key -> brightness 0..1 (phosphor persistence)

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        decay = math.exp(-self.dt(now) * 9 * self.speed)
        full = s["wave"]
        wave = full[:max(8, min(len(full), int(48 * self.size)))]  # size 1 = ~8 ms on screen
        for i in POS:
            hw = HALFW[i]
            ys = [0.5 - 0.46 * wave_at(wave, XN[i] + dx) for dx in (-hw, 0.0, hw)]
            lo, hi, y = min(ys), max(ys), effects.YN[i]
            dist = 0.0 if lo <= y <= hi else min(abs(y - lo), abs(y - hi))
            beam = boost(1 - dist / 0.17, 1.6)
            g = self.glow[i] = max(beam, self.glow.get(i, 0.0) * decay)
            if g > 0.02:
                f[i] = scale(self.pal.sample(XN[i] * 0.8 + now * 0.03), g if beam >= g else g * 0.7)
        return f

class PeakEQ(MusicMode):
    """Classic spectrum bars with peak caps that hang and then fall slowly."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.peaks = [0.0] * COLS

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        dt = self.dt(now)
        bands = s["bands"]
        heights = [lift(bands[band_of(c / COLS, len(bands))]) * ROWS for c in range(COLS)]
        self.peaks = [max(h, p - dt * 2.5 * self.speed) for h, p in zip(heights, self.peaks)]
        for i in POS:
            c, level = effects.COLN[i], 5 - ROWN[i]
            if heights[c] > level + 0.2:
                f[i] = self.pal.sample(level / 5, cyclic=False)
            elif int(self.peaks[c]) == level and self.peaks[c] > 0.5:
                f[i] = (255, 255, 255)
        return f

class Mood(MusicMode):
    """The whole board drifts warm with bass-heavy music and cool with bright music; volume breathes it."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.hue, self.v = 0.6, 0.3

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        dt = self.dt(now)
        b = s["bands"]
        centroid = sum(k * v for k, v in enumerate(b)) / max(1e-6, sum(b)) / len(b)  # 0 bass .. 1 treble
        target = 0.02 + 0.6 * min(1.0, centroid * 1.8)  # red/orange .. cyan/blue
        self.hue += (target - self.hue) * min(1.0, dt * 1.5 * self.speed)
        self.v += (0.25 + 0.75 * lift(s["level"]) - self.v) * min(1.0, dt * 6)
        for i in POS:
            ripple = 0.85 + 0.15 * math.sin(now * 1.3 + XN[i] * 6 * self.size)
            f[i] = effects.hsv(self.hue + XN[i] * 0.06, 0.9, self.v * ripple)
        return f

class Tunnel(MusicMode):
    """Rings fly in from the edges toward the center; beats launch new ones and kick the speed."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.z, self.kick = 0.0, 0.0

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        dt = self.dt(now)
        if self.new_beat(s):
            self.kick = 1.0
        self.kick = max(0.0, self.kick - dt * 2.5)
        self.z += dt * (0.4 + 1.6 * self.kick + s["level"]) * self.speed
        for i in POS:
            depth = 1 / (0.15 + DN[i])  # center = far away
            ring = (depth * 0.35 * self.size - self.z) % 1
            v = boost(1 - abs(ring - 0.5) * 2, 1.3) * (0.3 + 0.7 * lift(s["level"])) * min(1.0, DN[i] * 2.5)
            f[i] = scale(self.pal.sample(depth * 0.1 + self.z * 0.05), v)
        return f

class Drop(MusicMode):
    """Calm, slow glow during the build-up; when the music suddenly gets loud (the drop), a white
    burst and fast rainbow strobing until it settles again."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.slow, self.t_drop = 0.0, -99.0

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        dt = self.dt(now)
        lvl = s["level"]
        if lvl > 0.75 and lvl > self.slow + 0.35 and now - self.t_drop > 4:
            self.t_drop = now  # loud right after a quieter stretch
        self.slow += (lvl - self.slow) * min(1.0, dt / 3)  # 3 s memory of how loud it was
        age = now - self.t_drop
        if age < 0.25:
            return [(255, 255, 255)] * len(f)
        if age < 6:
            on = self.new_beat(s) or (int(now * 12 * self.speed) % 2 == 0 and age < 2)
            for i in POS:
                f[i] = scale(self.pal.sample(XN[i] + now * 1.5), (1.0 if on else 0.35) * (1 - age / 12))
            return f
        for i in POS:
            f[i] = scale(self.pal.sample(XN[i] * 0.3 + now * 0.02 * self.speed), 0.1 + 0.4 * lift(lvl))
        return f

def envelope(old, new, dt, attack=0.03, release=0.35):
    """Smooth follower: rises within ~attack s, falls within ~release s. Removes flicker."""
    tau = attack if new > old else release
    return old + (new - old) * min(1.0, dt / tau)

def centroid(bands):
    """Spectral centroid 0 (all bass) .. 1 (all treble): the 'brightness' of the sound."""
    return sum(k * v for k, v in enumerate(bands)) / max(1e-6, sum(bands)) / (len(bands) - 1)

class LiquidEQ(MusicMode):
    """Smooth spectrum bars: bands interpolated across the columns, partial brightness on the top key
    (fractional height) and a soft glowing peak."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.h = [0.0] * COLS

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        dt, b = self.dt(now), s["bands"]
        for c in range(COLS):
            x = c / (COLS - 1) * (len(b) - 1)
            k = min(int(x), len(b) - 2)
            v = b[k] + (b[k + 1] - b[k]) * (x - k)
            self.h[c] = envelope(self.h[c], lift(v) * ROWS, dt, 0.02, 0.25 / self.speed)
        for i in POS:
            c, level = effects.COLN[i], 5 - ROWN[i]
            fill = min(1.0, max(0.0, self.h[c] - level))  # 1 = key fully under the bar, 0..1 = top edge
            if fill > 0.02:
                top = self.h[c] - level < 1.0
                col = self.pal.sample(level / 5, cyclic=False)
                f[i] = scale(effects.lerp(col, (255, 255, 255), 0.35 * fill) if top else col, 0.25 + 0.75 * fill)
        return f

class Stage(MusicMode):
    """Stage lighting: bass is a floor glow on the bottom rows, the vocals/mids a spotlight that moves
    with the dominant frequency, treble a shimmer along the top. Three palette colors, one per layer."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.bass = self.mid = self.treble = 0.0
        self.spot = 0.5
        self.shimmer = {}

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        dt, b = self.dt(now), s["bands"]
        self.bass = envelope(self.bass, s["bass"], dt, 0.02, 0.3)
        self.mid = envelope(self.mid, s["mid"], dt, 0.05, 0.4)
        self.treble = envelope(self.treble, s["treble"], dt, 0.01, 0.15)
        mids = b[len(b) // 4: 3 * len(b) // 4]
        target = (mids.index(max(mids)) + 0.5) / len(mids) if max(mids) > 0.05 else 0.5
        self.spot += (target - self.spot) * min(1.0, dt * 2.5 * self.speed)  # spotlight glides, never jumps
        c_floor, c_spot, c_top = self.pick(0.0), self.pick(0.34), self.pick(0.67)
        for i in POS:
            y = effects.YN[i]
            base = 0.15 if s["active"] else 0.0  # music playing: the stage never goes fully dark
            floor = boost((base + lift(self.bass)) * boost(y - 0.35, 1.6), 1.5)
            dx, dy = (XN[i] - self.spot) / (0.18 * self.size), (y - 0.45) / 0.55
            spot = boost((base + lift(self.mid)) * max(0.0, 1 - (dx * dx + dy * dy)), 1.8)
            if random.random() < self.treble * 0.25 * self.density and ROWN[i] <= 1:
                self.shimmer[i] = 1.0
            sh = self.shimmer.get(i, 0.0)
            if sh:
                self.shimmer[i] = sh * math.exp(-dt * 8)
                if self.shimmer[i] < 0.03:
                    del self.shimmer[i]
            out = [0.0, 0.0, 0.0]
            for col, a in ((c_floor, floor), (c_spot, spot), (c_top, sh)):
                for ch in range(3):
                    out[ch] = min(255.0, out[ch] + col[ch] * a)
            f[i] = tuple(int(v) for v in out)
        return f

class SpectralTrail(MusicMode):
    """A timeline flowing across the keyboard: each new column takes its color from the timbre
    (spectral centroid: warm = bass-heavy, cool = bright) and its height from the volume.
    Beats leave a white tick."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.cols = [(0.0, 0.0, False)] * COLS  # (centroid, level, beat)
        self.acc, self.c, self.v = 0.0, 0.5, 0.0

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        dt = self.dt(now)
        self.c = envelope(self.c, centroid(s["bands"]), dt, 0.15, 0.15)
        self.v = envelope(self.v, lift(s["level"]), dt, 0.02, 0.2)
        beat = self.new_beat(s)
        self.acc += dt * 10 * self.speed
        if self.acc >= 1 or beat:
            self.acc = 0.0
            self.cols = [(self.c, self.v, beat)] + self.cols[:-1]  # newest on the left, flows right
        for i in POS:
            c, v, bt = self.cols[effects.COLN[i]]
            height = v * ROWS
            dist = abs((ROWN[i] + 0.5) - ROWS / 2) * 2  # 0 = middle rows .. 6 = edges
            a = boost(height - dist + 0.5, 1.0)
            if a > 0.02:
                f[i] = effects.lerp(scale(self.pal.sample(c * self.size, cyclic=False), a), (255, 255, 255), 0.6 if bt else 0.0)
        return f

class Bloom(MusicMode):
    """Per-band onsets: when a frequency range suddenly jumps, a soft glow blooms in its part of the
    keyboard (bass left, treble right) and fades slowly. Busy music = a field of flowers."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.prev, self.avg, self.blooms = None, None, []  # bloom: (t0, x, y, color, power)

    def frame(self, now):
        s, f = self.an.snapshot(), blank()
        b = s["bands"]
        if self.prev is not None:
            for k, (v, p) in enumerate(zip(b, self.prev)):
                self.avg[k] = self.avg[k] * 0.97 + (v - p if v > p else 0) * 0.03
                flux = v - p
                if flux > 0.12 and flux > self.avg[k] * 3 and random.random() < 0.6 * self.density:
                    x = (k + random.random()) / len(b)
                    self.blooms.append((now, x, random.uniform(0.1, 0.9), self.pick(k / len(b)), min(1.0, flux * 3)))
        else:
            self.avg = [0.0] * len(b)
        self.prev = b
        life = 1.4 / self.speed
        self.blooms = [x for x in self.blooms if now - x[0] < life][-40:]
        acc = {}
        for t0, bx, by, col, power in self.blooms:
            age = (now - t0) / life
            radius = 0.06 + 0.16 * self.size * (1 - (1 - age) ** 3)  # opens fast, then settles
            a = power * fade(1 - age)
            for i in POS:
                d = math.hypot((XN[i] - bx) * 2.2, effects.YN[i] - by) / radius
                if d < 1:
                    o = acc.setdefault(i, [0.0, 0.0, 0.0])
                    w = a * (1 - d * d)
                    for ch in range(3):
                        o[ch] = min(255.0, o[ch] + col[ch] * w)
        for i, o in acc.items():
            f[i] = tuple(int(v) for v in o)
        return f

_m = effects._m
_REG = {
    "equalizer":   _m(Equalizer, "Spectrum bars, one per column", palette="vu"),
    "bands_glow":  _m(BandsGlow, "Columns glow with each band", opts=("size",)),
    "radial_eq":   _m(RadialEq, "Bass in the center, treble at the edges"),
    "vu":          _m(VUMeter, "Volume grows from the center out", palette="vu"),
    "pulse":       _m(Pulse, "Pulses with volume, new color on each beat", 1),
    "beat_flash":  _m(BeatFlash, "Flash on every beat", 1),
    "beat_ripple": _m(BeatRipple, "Every beat fires a ripple", 1, opts=("size",)),
    "bass_wave":   _m(BassWave, "Wave that speeds up with the bass", opts=("direction", "size")),
    "sparkle":     _m(Sparkle, "Treble lights random keys", 1, opts=("density",)),
    "freq_color":  _m(ColorByFrequency, "Color by frequency: bass, mid, treble", palette=None),
    "waterfall":   _m(Waterfall, "Scrolling spectrogram: rows are frequencies, time flows across", palette="heat"),
    "shockwave":   _m(Shockwave, "Bass hits send rings from the center", 1, opts=("size",)),
    "lightning":   _m(Lightning, "Strong beats strike jagged bolts down the keys", 1, palette="ice", opts=("density",)),
    "kaleido":     _m(Kaleidoscope, "Spectrum mirrored in four directions, rotating hue", opts=("size",)),
    "fountain":    _m(Fountain, "Bass shoots sparks up that fall back with gravity", 1, opts=("density",)),
    "scope":       _m(Scope, "Real oscilloscope of the audio waveform with phosphor glow", palette="neon", opts=("size",)),
    "peak_eq":     _m(PeakEQ, "Spectrum bars with falling peak caps", palette="vu"),
    "mood":        _m(Mood, "Warm for bass-heavy music, cool for bright music", palette=None),
    "tunnel":      _m(Tunnel, "Rings fly into the center, kicked by the beat", palette="cyberpunk", opts=("size",)),
    "drop":        _m(Drop, "Calm build-up, then a burst and strobe on the drop", palette="rainbow"),
    "liquid_eq":   _m(LiquidEQ, "Smooth spectrum bars with soft tops and glowing peaks", palette="vu"),
    "stage":       _m(Stage, "Stage lights: bass floor, vocal spotlight, treble shimmer", palette="cyberpunk", opts=("size", "density")),
    "trail":       _m(SpectralTrail, "Timeline of the sound's color and volume, beats as white ticks", palette="sunset", opts=("size",)),
    "bloom":       _m(Bloom, "Each frequency blooms soft glows where it spikes", opts=("size", "density")),
}
MODES = {k: v[0] for k, v in _REG.items()}
META = {k: v[1] for k, v in _REG.items()}
for _name, _cls in MODES.items():
    _cls.NAME = _name
effects.DEFAULTS.update({k: v["defaults"] for k, v in META.items()})  # clean_params defaults
DESCRIPTIONS = {k: v["desc"] for k, v in META.items()}

def make(name, analyzer, params=None, **kw):
    return MODES[name](analyzer, {**(params or {}), **kw})

def selftest():
    import audio
    import numpy as np
    an = audio.Analyzer(48000)
    t = np.arange(48000) / 48000
    an.push((0.5 * np.sin(2 * np.pi * 60 * t) + 0.2 * np.sin(2 * np.pi * 9000 * t)).astype(np.float32))
    an.update(1.0)
    for name in MODES:
        for pal in ("rainbow", ""):
            m = make(name, an, palette=pal, color="FF0000")
            for now in (1.0, 1.02, 1.3):
                fr = m.frame(now)
                assert len(fr) == gk104.KEY_SLOTS and all(0 <= v <= 255 for c in fr for v in c), name
    eq = make("equalizer", an).frame(1.0)
    left = [i for i in POS if XN[i] < 0.1 and ROWN[i] == 5]
    right_top = [i for i in POS if XN[i] > 0.6 and ROWN[i] == 0]
    assert any(eq[i] != BLACK for i in left), "bass column lit at the bottom"
    assert all(eq[i] == BLACK for i in right_top), "quiet high-mid columns stay dark at the top"
    silent = audio.Analyzer(48000)
    assert all(c == BLACK for c in make("vu", silent).frame(0.0)), "silence = dark VU"
    print("selftest ok:", len(MODES), "music modes")

if __name__ == "__main__":
    selftest()
