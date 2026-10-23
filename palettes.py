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
    # scientific colormaps (perceptually even, matplotlib), sampled; darkest stop lifted so LEDs still show it
    "viridis":   ["46327E", "365C8D", "277F8E", "1FA187", "4AC16D", "A0DA39", "FDE725"],
    "magma":     ["3B0F70", "8C2981", "DE4968", "FE9F6D", "FCFDBF"],
    "inferno":   ["420A68", "932667", "DD513A", "FCA50A", "FCFFA4"],
    "plasma":    ["0D0887", "6A00A8", "B12A90", "E16462", "FCA636", "F0F921"],
    "turbo":     ["30123B", "4686FB", "1AE4B6", "A2FC3C", "FABA39", "E4460A", "7A0403"],
    # editor / terminal themes
    "nord":      ["5E81AC", "81A1C1", "88C0D0", "8FBCBB", "A3BE8C", "EBCB8B", "D08770", "BF616A", "B48EAD"],
    "dracula":   ["BD93F9", "FF79C6", "8BE9FD", "50FA7B", "F1FA8C", "FFB86C", "FF5555"],
    "catppuccin": ["F5E0DC", "F5C2E7", "CBA6F7", "F38BA8", "FAB387", "F9E2AF", "A6E3A1", "94E2D5", "89B4FA", "B4BEFE"],
    "tokyo_night": ["7AA2F7", "BB9AF7", "7DCFFF", "9ECE6A", "E0AF68", "F7768E"],
    "gruvbox":   ["FB4934", "FE8019", "FABD2F", "B8BB26", "8EC07C", "83A598", "D3869B"],
    "solarized": ["B58900", "CB4B16", "DC322F", "D33682", "6C71C4", "268BD2", "2AA198", "859900"],
    # nature
    "sakura":    ["FFB7C5", "FF8FAB", "FB6F92", "FFE5EC", "C9184A"],
    "autumn":    ["8B1E00", "D2481F", "F28C28", "F6C445", "9C6B30"],
    "tropical":  ["00F5D4", "00BBF9", "FEE440", "F15BB5", "9B5DE5"],
    "deep_sea":  ["03045E", "0077B6", "00B4D8", "90E0EF", "48CAE4"],
    "desert":    ["E9C46A", "F4A261", "E76F51", "BC6C25", "FFE8D6"],
    "galaxy":    ["2B0A57", "6A0DAD", "C71585", "FF6EC7", "4B6CFF", "00E5FF"],
    "jungle":    ["0B6E4F", "08A045", "6BBF59", "DDB771", "1C7C54"],
    "lagoon":    ["00A896", "02C39A", "F0F3BD", "05668D", "028090"],
    # moods
    "frost":     ["FFFFFF", "C9F1FF", "5CE1FF", "8B9DFF", "3A6BFF"],
    "miami":     ["FF2A6D", "FF6AD5", "05D9E8", "01FFC3", "FFD319"],
    "outrun":    ["FF0090", "FF6C11", "FD3777", "6A00F4", "00C2FF"],
    "lavender":  ["E0BBE4", "957DAD", "D291BC", "FEC8D8", "B39CD0"],
    "mint":      ["B8F2E6", "5EEAD4", "2DD4BF", "AEF6C7", "14B8A6"],
    "gold":      ["FFF1A8", "FFD700", "FFB300", "FF8F00", "FFE082"],
    "blood_moon": ["FF0000", "B3001B", "FF4D00", "7A0010", "FF2E2E"],
    "bubblegum": ["FF77E9", "FFC4F2", "8AF3FF", "B8B8FF", "FF9AA2"],
    "matrix":    ["003B00", "008F11", "00FF41", "A8FF60"],
    "hacker":    ["00FF41", "39FF14", "00E5FF", "00FFB3"],
    "sunrise":   ["FF5F6D", "FF8E53", "FFC371", "FFE29A", "FF9A8B"],
    "cotton_candy": ["FFB5E8", "B28DFF", "85E3FF", "BFFCC6", "FFF5BA"],
}
NAMES = {
    "rainbow": "Rainbow", "fire": "Fire", "lava": "Lava", "ocean": "Ocean", "ice": "Ice", "forest": "Forest",
    "aurora": "Aurora", "sunset": "Sunset", "neon": "Neon", "cyberpunk": "Cyberpunk", "vaporwave": "Vaporwave",
    "pastel": "Pastel", "candy": "Candy", "toxic": "Toxic", "royal": "Royal", "heat": "Heat", "vu": "VU",
    "police": "Police", "mono": "Mono",
    "viridis": "Viridis", "magma": "Magma", "inferno": "Inferno", "plasma": "Plasma", "turbo": "Turbo",
    "nord": "Nord", "dracula": "Dracula", "catppuccin": "Catppuccin", "tokyo_night": "Tokyo Night",
    "gruvbox": "Gruvbox", "solarized": "Solarized", "sakura": "Sakura", "autumn": "Autumn", "tropical": "Tropical",
    "deep_sea": "Deep Sea", "desert": "Desert", "galaxy": "Galaxy", "jungle": "Jungle", "lagoon": "Lagoon",
    "frost": "Frøst", "miami": "Miami", "outrun": "Outrun", "lavender": "Lavender", "mint": "Mint", "gold": "Gold",
    "blood_moon": "Blood Moon", "bubblegum": "Bubblegum", "matrix": "Matrix", "hacker": "Hacker",
    "sunrise": "Sunrise", "cotton_candy": "Cotton Candy",
}
assert NAMES.keys() == BUILTIN.keys(), "every built-in palette needs a display name"
MAX_CUSTOM = 100
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
    if not isinstance(p, dict) or len(p) > MAX_CUSTOM:
        raise ValueError(f"palettes must be an object (max {MAX_CUSTOM})")
    out = {}
    for k, v in p.items():
        if not (isinstance(k, str) and 0 < len(k) <= 40 and k not in BUILTIN):
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
