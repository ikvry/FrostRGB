"""Music-reactive modes for the GK104. Each reads an analyzer snapshot every frame.

Same interface and params as effects: frame(now) -> 132 (r, g, b), on_key(index, now).
"""
import random

import effects
import gk104
from effects import BLACK, DN, POS, ROWN, XN, blank, fade, scale

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
