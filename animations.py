"""Razer Chroma animation files (.chroma, from the Chroma Editor / ChromaSDK samples).

File layout (CChromaEditor Animation2D::Save, little endian):
  int32 version (=1), uint8 deviceType (0 = 1D, 1 = 2D), uint8 device, uint32 frameCount
  per frame: float32 duration, then rows*cols int32 COLORREF (0x00BBGGRR), row-major
2D devices: 0 Keyboard 6x22, 1 Keypad, 2 Mouse, 3 KeyboardExtended 8x24.
Only keyboard animations are useful here; they are mapped through the Razer key grid.
"""
import re
import struct
from pathlib import Path

import chroma_server as cs
import chroma_studio
import effects
import gk104

GRIDS = {0: (6, 22), 3: (8, 24)}
# shortcut: assumes the 6x22 keyboard sits at row 1 / col 1 of the 8x24 extended grid;
# check against a real KeyboardExtended file if one ever looks shifted.
OFFSET = {0: (0, 0), 3: (1, 1)}
NAME = re.compile(r"^[\w .()\-]{1,60}$")
MAX_BYTES = 20_000_000
MAX_LIST = 200

def parse(data):
    """Returns (frames, info): frames = [(duration_s, 132 RGB tuples)]. Raises ValueError."""
    if len(data) < 10:
        raise ValueError("file too small")
    version, kind, device, count = struct.unpack_from("<iBBI", data, 0)
    if version != 1:
        raise ValueError(f"unsupported .chroma version {version}")
    if kind != 1 or device not in GRIDS:
        raise ValueError("not a keyboard animation (only Keyboard / KeyboardExtended are supported)")
    rows, cols = GRIDS[device]
    size = 4 + rows * cols * 4
    if count == 0 or 10 + count * size > len(data):
        raise ValueError("truncated or empty animation")
    dr, dc = OFFSET[device]
    frames, pos = [], 10
    for _ in range(count):
        dur = struct.unpack_from("<f", data, pos)[0]
        grid = struct.unpack_from(f"<{rows * cols}I", data, pos + 4)
        pos += size
        f = effects.blank()
        for idx, r, c in cs.MAPPING:
            v = grid[(r + dr) * cols + c + dc]
            f[idx] = (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)
        frames.append((max(0.033, dur) if dur == dur else 0.033, f))  # same floor as the Chroma player; NaN -> floor
    dur = round(sum(d for d, _ in frames), 2)
    info = {"kind": "frames", "frames": count, "duration": dur,
            "summary": f"{'Keyboard' if device == 0 else 'KeyboardExtended'} · {count} frames · {dur}s"}
    return frames, info

class Library:
    """Imported effects live as files in one folder: .chroma (frame animations) and
    .chromaeffects (Synapse Chroma Studio layers). Parsed results are cached by mtime."""
    EXTS = (".chroma", ".chromaeffects")

    def __init__(self, folder):
        self.folder = Path(folder)
        self.cache = {}

    def path(self, name, ext=None):
        if not isinstance(name, str) or not NAME.match(name) or name.strip(". ") != name.strip():
            raise ValueError("bad animation name")
        if ext:
            return self.folder / f"{name}{ext}"
        for e in self.EXTS:
            if (self.folder / f"{name}{e}").exists():
                return self.folder / f"{name}{e}"
        raise ValueError(f"no animation named {name!r}")

    @staticmethod
    def _parse(ext, data):
        if ext == ".chromaeffects":
            return ("studio", *chroma_studio.parse(data))
        return ("frames", *parse(data))

    def add(self, name, data):
        name = re.sub(r"[^\w .()\-]", "_", Path(name).stem)[:60].strip(" .") or "animation"
        if len(data) > MAX_BYTES:
            raise ValueError("file too large")
        ext = ".chromaeffects" if data[:2] == b"PK" else ".chroma"  # Studio files are ZIPs
        self._parse(ext, data)  # reject before saving
        self.folder.mkdir(parents=True, exist_ok=True)
        if len(self.list()) >= MAX_LIST:
            raise ValueError("too many animations")
        for e in self.EXTS:
            self.path(name, e).unlink(missing_ok=True)  # one file per name
        self.path(name, ext).write_bytes(data)
        return name

    def remove(self, name):
        for e in self.EXTS:
            self.path(name, e).unlink(missing_ok=True)
        self.cache.pop(name, None)

    def load(self, name):
        """(kind, data, info) with kind "frames" or "studio"."""
        p = self.path(name)
        key = (p.suffix, p.stat().st_mtime)
        hit = self.cache.get(name)
        if not hit or hit[0] != key:
            hit = self.cache[name] = (key, self._parse(p.suffix.lower(), p.read_bytes()))
        return hit[1]

    def make(self, name, params):
        kind, data, _ = self.load(name)
        return chroma_studio.StudioPlayer(data, params) if kind == "studio" else Player(data, params)

    def list(self):
        out = []
        files = sorted(p for p in self.folder.iterdir() if p.suffix.lower() in self.EXTS) if self.folder.exists() else []
        for p in files:
            try:
                out.append({"name": p.stem, **self.load(p.stem)[2]})
            except (ValueError, OSError, struct.error):
                pass  # broken file: hidden, not fatal
        return out

class Player(effects.Effect):
    """Plays an imported animation in a loop. speed scales the frame durations."""

    def __init__(self, frames, params=None, **kw):
        super().__init__(params, **kw)
        self.frames = frames
        self.total = sum(d for d, _ in frames)
        self.t0 = None

    def frame(self, now):
        if self.t0 is None:
            self.t0 = now
        t = ((now - self.t0) * self.speed) % self.total
        for dur, f in self.frames:
            if t < dur:
                return f
            t -= dur
        return self.frames[-1][1]

def build(frames_rgb, durations=None, device=0):
    """Encodes a .chroma file; used by the self-test and handy for exporting."""
    rows, cols = GRIDS[device]
    dr, dc = OFFSET[device]
    out = [struct.pack("<iBBI", 1, 1, device, len(frames_rgb))]
    for n, f in enumerate(frames_rgb):
        grid = [0] * (rows * cols)
        for idx, r, c in cs.MAPPING:
            red, g, b = f[idx]
            grid[(r + dr) * cols + c + dc] = red | g << 8 | b << 16
        out.append(struct.pack("<f", (durations or [0.1] * len(frames_rgb))[n]))
        out.append(struct.pack(f"<{rows * cols}I", *grid))
    return b"".join(out)

def selftest():
    import tempfile
    w = gk104.KEY_INDEX["w"]
    a, b = effects.blank(), effects.blank()
    a[w], b[w] = (255, 0, 0), (0, 0, 255)
    for device in GRIDS:
        frames, info = parse(build([a, b], [0.5, 0.25], device))
        assert info["frames"] == 2 and info["duration"] == 0.75 and frames[0][1][w] == (255, 0, 0), device
    p = Player(frames, {"speed": 1.0})
    assert p.frame(10.0)[w] == (255, 0, 0) and p.frame(10.6)[w] == (0, 0, 255) and p.frame(10.8)[w] == (255, 0, 0)
    for bad in (b"", build([a])[:-4], struct.pack("<iBBI", 2, 1, 0, 1), struct.pack("<iBBI", 1, 0, 0, 1) + b"\0" * 400):
        try:
            parse(bad)
            raise AssertionError("accepted bad file")
        except ValueError:
            pass
    with tempfile.TemporaryDirectory() as d:
        lib = Library(d)
        name = lib.add("C:/x/My Fx!.chroma", build([a, b]))
        assert name == "My Fx_" and [x["name"] for x in lib.list()] == [name]
        assert lib.load(name)[1][1][1][w] == (0, 0, 255)
        assert lib.make(name, {}).frame(0.0)[w] == (255, 0, 0)
        assert lib.add("Studio", chroma_studio.build([("static", [(2, 3)], ["00FF00"], 0)])) == "Studio"
        assert lib.make("Studio", {}).frame(0.0)[w] == (0, 255, 0) and lib.list()[1]["kind"] == "studio"
        lib.remove("Studio")
        for bad in ("../evil", "", "a/b"):
            try:
                lib.path(bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        lib.remove(name)
        assert lib.list() == []
    print("selftest ok")

if __name__ == "__main__":
    selftest()
