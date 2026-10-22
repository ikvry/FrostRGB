"""Simulates every effect / music mode for a few seconds and reports how bright it looks.

  python tools/brightness_report.py

mean = average brightness of all keys (0-100%), lit = share of keys above 10%,
peak = average brightness of the lit keys. Typing effects get simulated key presses,
music modes get a synthetic beat track.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np

import audio
import effects
import gk104
import music

def score(frames):
    keys = list(effects.POS)
    v = np.array([[max(f[i]) / 255 for i in keys] for f in frames])
    lit = v > 0.1
    peak = v[lit].mean() if lit.any() else 0.0
    return v.mean() * 100, lit.mean() * 100, peak * 100

def run(obj, secs=6, fps=30, typing=False, an=None, track=None):
    frames, rnd = [], random.Random(1)
    keys = list(effects.POS)
    for n in range(int(secs * fps)):
        t = 100 + n / fps
        if an is not None:
            chunk = track[(n * 1600) % (len(track) - 1600):][:1600]
            an.push(chunk)
            an.update(t)
        if typing and n % 4 == 0:
            obj.on_key(rnd.choice(keys), t)
        frames.append(obj.frame(t))
    return score(frames[fps:])  # skip warm-up

def main():
    random.seed(0)
    rows = []
    for name, m in effects.META.items():
        rows.append(("effect", name, *run(effects.make(name), typing=m["reactive"])))
    sr = 48000
    t = np.arange(sr * 4) / sr
    track = (0.6 * np.sin(2 * np.pi * 60 * t) * (np.sin(2 * np.pi * 2 * t) > 0.6)
             + 0.15 * np.sin(2 * np.pi * 440 * t) + 0.05 * np.random.default_rng(0).standard_normal(len(t))).astype(np.float32)
    for name in music.MODES:
        an = audio.Analyzer(sr)
        rows.append(("music", name, *run(music.make(name, an), an=an, track=track)))
    print(f"{'kind':7} {'name':12} {'mean':>6} {'lit':>7} {'peak':>6}")
    for kind, name, mean, lit, peak in sorted(rows, key=lambda r: r[2]):
        print(f"{kind:7} {name:12} {mean:6.1f} {lit:6.1f}% {peak:6.1f}")

if __name__ == "__main__":
    main()
