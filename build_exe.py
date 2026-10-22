"""Builds dist/GK104 Studio.exe (needs: pip install pyinstaller).

  python build_exe.py
"""
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent

def _png(img):
    h, w = img.shape[:2]
    raw = b"".join(b"\0" + row.tobytes() for row in img)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

def _seg(px, py, ax, ay, bx, by):
    """Distance from every pixel to the segment a-b."""
    dx, dy = bx - ax, by - ay
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy), 0, 1)
    return np.hypot(px - ax - t * dx, py - ay - t * dy)

def render_icon(size):
    """FrøstRGB logo: glowing ice snowflake with an RGB sheen on a dark rounded tile."""
    ss = 4  # supersampling
    n = size * ss
    y, x = (np.mgrid[0:n, 0:n] + 0.5) / n * 2 - 1
    r = 0.36
    d = np.maximum(np.abs(x) - (1 - r), 0) ** 2 + np.maximum(np.abs(y) - (1 - r), 0) ** 2
    tile = np.clip((r - np.sqrt(d)) * n / 2, 0, 1)
    bg = np.stack([0.04 + 0.05 * (1 - y), 0.07 + 0.08 * (1 - y), 0.13 + 0.12 * (1 - y)], -1)  # navy, lighter at top
    dist = np.full(x.shape, 9.0)
    for k in range(6):
        a = np.pi / 2 + k * np.pi / 3
        c, s_ = np.cos(a), np.sin(a)
        dist = np.minimum(dist, _seg(x, y, 0, 0, 0.72 * c, 0.72 * s_))
        for t, L in ((0.36, 0.22), (0.58, 0.14)):  # two branches per arm
            bx, by = t * c, t * s_
            for side in (-1, 1):
                b = a + side * np.pi / 4
                dist = np.minimum(dist, _seg(x, y, bx, by, bx + L * np.cos(b), by + L * np.sin(b)))
    w = 0.075 if size <= 24 else 0.055  # thicker strokes stay readable at 16 px
    flake = np.clip((w - dist) * n / 2, 0, 1)
    glow = np.exp(-np.maximum(dist - w, 0) / 0.09) * 0.55
    hue = (np.arctan2(y, x) / (2 * np.pi) + 0.5) % 1
    sheen = np.stack([np.clip(np.abs((hue * 6 + o) % 6 - 3) - 1, 0, 1) for o in (0, 4, 2)], -1)
    ice = np.array([0.55, 0.92, 1.0])
    color = ice * 0.7 + sheen * 0.3  # icy cyan with a hint of RGB
    rgb = bg * (1 - glow[..., None]) + color * glow[..., None]
    rgb = rgb * (1 - flake[..., None]) + (0.85 + 0.15 * color) * flake[..., None]
    img = np.dstack([np.clip(rgb, 0, 1), tile])
    img = img.reshape(size, ss, size, ss, 4).mean((1, 3))
    return (img * 255 + 0.5).astype(np.uint8)

def make_icon(path, sizes=(16, 20, 24, 32, 40, 48, 64, 128, 256)):
    """Multi-size ICO, one PNG per size."""
    pngs = [_png(render_icon(s)) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    for s, png in zip(sizes, pngs):
        head += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(png), off)
        off += len(png)
    path.write_bytes(head + b"".join(pngs))

def main():
    icon = HERE / "icon.ico"
    make_icon(icon)
    sep = ";"  # PyInstaller --add-data separator on Windows
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noconsole",
                    "--name", "FrostRGB", "--icon", str(icon),
                    "--add-data", f"{HERE / 'ui.html'}{sep}.", "--add-data", f"{HERE / 'gk104_keymap.json'}{sep}.",
                    "--add-data", f"{icon}{sep}.",  # tray icon
                    "--exclude-module", "scipy", "--exclude-module", "tkinter",
                    "--distpath", str(HERE / "dist"), "--workpath", str(HERE / "build"), "--specpath", str(HERE / "build"),
                    str(HERE / "app.py")], check=True)
    exe = HERE / "dist" / "FrostRGB.exe"
    print(f"{exe} ({exe.stat().st_size / 1e6:.1f} MB)")

if __name__ == "__main__":
    main()
