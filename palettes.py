"""Color palettes: built-in gradients plus user palettes saved in settings.

A palette is a list of color stops. sample(t) interpolates smoothly; cyclic palettes wrap
from the last stop back to the first so moving effects never show a seam.
"""
import re

BUILTIN = {
    "rainbow":   ["FF0000", "FFFF00", "00FF00", "00FFFF", "0000FF", "FF00FF"],
    "fire":      ["000000", "FF0000", "FF6A00", "FFD000", "FFFFCC"],
    "lava":      ["2A0000", "FF2200", "FF7700", "FFCC00"],
    "ocean":     ["001A33", "0047AB", "00A6FF", "00FFE5", "0066CC"],
    "ice":       ["FFFFFF", "A0E9FF", "00B4FF", "0050FF"],
    "forest":    ["003300", "1E7B1E", "7CFC00", "ADFF2F", "006400"],
    "aurora":    ["00FF88", "00CCFF", "7700FF", "00FF44"],
    "sunset":    ["FF4E00", "FF006E", "8338EC", "3A86FF", "FFBE0B"],
    "neon":      ["FF00FF", "00FFFF", "FFFF00", "FF0080"],
    "cyberpunk": ["FF0080", "7B00FF", "00F0FF", "FFE600"],
    "vaporwave": ["FF71CE", "01CDFE", "05FFA1", "B967FF", "FFFB96"],
    "pastel":    ["FFB3BA", "FFDFBA", "FFFFBA", "BAFFC9", "BAE1FF"],
    "candy":     ["FF5FA2", "FFD93D", "6BCB77", "4D96FF"],
    "toxic":     ["39FF14", "CCFF00", "00FF99"],
    "royal":     ["4B0082", "8A2BE2", "FFD700"],
    "heat":      ["0000FF", "00FFFF", "00FF00", "FFFF00", "FF0000"],
    "vu":        ["00FF00", "FFFF00", "FF0000"],
    "police":    ["FF0000", "0000FF"],
    "mono":      ["FFFFFF", "000000"],
}
NAMES = {
    "rainbow": "Rainbow", "fire": "Fire", "lava": "Lava", "ocean": "Ocean", "ice": "Ice", "forest": "Forest",
    "aurora": "Aurora", "sunset": "Sunset", "neon": "Neon", "cyberpunk": "Cyberpunk", "vaporwave": "Vaporwave",
    "pastel": "Pastel", "candy": "Candy", "toxic": "Toxic", "royal": "Royal", "heat": "Heat", "vu": "VU",
    "police": "Police", "mono": "Mono",
}
HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
CUSTOM = {}  # name -> list of hex stops, kept in sync with settings by the app

def rgb(h):
    return tuple(bytes.fromhex(h))

class Palette:
    def __init__(self, stops):
        self.stops = [rgb(s) if isinstance(s, str) else tuple(s) for s in stops] or [(255, 255, 255)]

    def sample(self, t, cyclic=True):
        s, n = self.stops, len(self.stops)
        if n == 1:
            return s[0]
        if cyclic:
            t = (t % 1) * n
            i = int(t) % n
            a, b, f = s[i], s[(i + 1) % n], t - int(t)
        else:
            t = min(1.0, max(0.0, t)) * (n - 1)
            i = min(int(t), n - 2)
            a, b, f = s[i], s[i + 1], t - i
        return (int(a[0] + (b[0] - a[0]) * f), int(a[1] + (b[1] - a[1]) * f), int(a[2] + (b[2] - a[2]) * f))

def get(name, color="FF0000", color2="0000FF"):
    """Palette by name. '' or 'colors' = the effect's own two colors."""
    if name in BUILTIN:
        return Palette(BUILTIN[name])
    if name in CUSTOM:
        return Palette(CUSTOM[name])
    return Palette([color, color2])

def exists(name):
    return name in ("", "colors") or name in BUILTIN or name in CUSTOM

def validate_custom(p):
    if not isinstance(p, dict) or len(p) > 40:
        raise ValueError("palettes must be an object (max 40)")
    out = {}
    for k, v in p.items():
        if not (isinstance(k, str) and 0 < len(k) <= 30 and k not in BUILTIN):
            raise ValueError(f"bad palette name {k!r}")
        if not (isinstance(v, list) and 2 <= len(v) <= 10 and all(isinstance(c, str) and HEX.match(c) for c in v)):
            raise ValueError(f"palette {k!r} needs 2-10 hex colors")
        out[k] = [c.upper() for c in v]
    return out

if __name__ == "__main__":
    p = Palette(["000000", "FF0000"])
    assert p.sample(0, False) == (0, 0, 0) and p.sample(1, False) == (255, 0, 0) and p.sample(0.5, False) == (127, 0, 0)
    assert get("rainbow").sample(0) == (255, 0, 0) and get("rainbow").sample(1) == (255, 0, 0), "cyclic wraps"
    assert get("", "00FF00", "0000FF").sample(0) == (0, 255, 0)
    for bad in ({"fire": ["000000", "FFFFFF"]}, {"x": ["000000"]}, {"x": ["zz0000", "000000"]}):
        try:
            validate_custom(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    print("selftest ok:", len(BUILTIN), "palettes")
