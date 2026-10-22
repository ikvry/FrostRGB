"""Razer Synapse 3 Chroma Studio effects (.ChromaEffects files shared online).

The file is a ZIP holding one XML <LightingEffects> document:
  EffectLayers/EffectLayer: Effect (wave, spectrum, static, breathing, reactive, ripple, starlight, fire, ...),
  Visible, Alpha (0-100), Regions/EffectRegion: Cells (per device Row/Col), Colors/RzColor (Red/Green/Blue,
  missing = 0, Alpha only written when 0), Stops (gradient positions 0-100), Angle, Speed, Width, Split.
The first layer in the file is the top of the stack. Only the first keyboard (Device Type 2) is used.
"""
import io
import math
import re
import xml.etree.ElementTree as ET
import zipfile

import chroma_server as cs
import effects
import gk104
from effects import XN, YN

MAX_XML = 8_000_000
KEYS = {i for i, _, _ in cs.MAPPING}

def _elite(r, c):
    """Razer v1 6x22 cell -> Synapse's 11x24 underglow keyboard grid (Huntsman Elite style):
    one row of underglow on top, and the blocks right of the main keys sit one column further."""
    return r + 1, c + (c >= 15 or (r == 0 and c >= 3) or (r == 5 and c >= 11))

def _num(e, tag, default=0.0):
    try:
        return float(e.findtext(tag))
    except (TypeError, ValueError):
        return default

def _color(c):
    ch = [int(_num(c, t)) & 255 for t in ("Red", "Green", "Blue")]
    return (*ch, 0 if c.find("Alpha") is not None and _num(c, "Alpha") == 0 else 255)

def parse(data):
    """Returns (spec, info). Raises ValueError on anything that is not a usable keyboard effect."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        xmls = [i for i in z.infolist() if i.filename.lower().endswith(".xml")]
        if len(xmls) != 1 or xmls[0].file_size > MAX_XML:
            raise ValueError("expected one XML file inside the .ChromaEffects archive")
        text = z.read(xmls[0])
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise ValueError(f"not a .ChromaEffects file ({e})")
    if b"<!DOCTYPE" in text or b"<!ENTITY" in text:
        raise ValueError("unsupported XML")  # no entity tricks in untrusted files
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"broken XML ({e})")
    if root.tag != "LightingEffects":
        raise ValueError("not a Chroma Studio effect")

    def keyboard_cells(region):
        for it in region.iter("Item"):
            dev = it.find("Key/Device")
            if dev is not None and dev.findtext("Type") == "2":
                return dev.findtext("Name") or "", {(int(_num(c, "Row")), int(_num(c, "Col"))) for c in it.iter("DeviceCell")}
        return None, set()

    regions_xml = [(l, r) for l in root.iter("EffectLayer") for r in l.iter("EffectRegion")]
    used = [keyboard_cells(r) for _, r in regions_xml]
    names = {n for n, cells in used if cells}
    if not names:
        raise ValueError("this effect has no keyboard lighting")
    allcells = set().union(*(cells for _, cells in used))
    # shortcut: two known grids (plain 6x22 and the Huntsman Elite style 11x24); other Razer
    # keyboards with their own grid may come out shifted. Add their layout here if one does.
    elite = max(r for r, _ in allcells) > 5 or max(c for _, c in allcells) > 21 or any("ELITE" in n.upper() for n in names)
    cell_of = {i: (_elite(r, c) if elite else (r, c)) for i, r, c in cs.MAPPING}

    layers, unsupported = [], set()
    for layer in root.iter("EffectLayer"):
        kind = (layer.findtext("Effect") or "").strip().lower()
        if (layer.findtext("Visible") or "true").strip().lower() == "false":
            continue
        if kind not in RENDER:
            unsupported.add(kind or "?")
            continue
        regs = []
        for region in layer.iter("EffectRegion"):
            _, cells = keyboard_cells(region)
            keys = sorted(i for i, cell in cell_of.items() if cell in cells)
            colors = [_color(c) for c in region.iter("RzColor")] or [(255, 255, 255, 255)]
            stops = [_num(s, ".") / 100 for s in region.iter("unsignedInt")]
            if len(stops) != len(colors):
                stops = [k / max(1, len(colors) - 1) for k in range(len(colors))]
            if keys:
                regs.append({"keys": keys, "grad": sorted(zip(stops, colors)), "angle": _num(region, "Angle", 90),
                             "speed": max(0.1, _num(region, "Speed", 5)), "width": max(1.0, _num(region, "Width", 100)),
                             "split": (region.findtext("Split") or "").lower() == "true"})
        if regs:
            layers.append({"effect": kind, "alpha": max(0.0, min(1.0, _num(layer, "Alpha", 100) / 100)), "regions": regs})
    if not layers:
        raise ValueError("no supported keyboard layers" + (f" (unsupported: {', '.join(sorted(unsupported))})" if unsupported else ""))
    layers.reverse()  # draw bottom first
    info = {"kind": "studio", "layers": len(layers), "unsupported": sorted(unsupported),
            "summary": "Chroma Studio · " + (f"{len(layers)} layers" if len(layers) != 1 else "1 layer")
                       + (f" · skipped: {', '.join(sorted(unsupported))}" if unsupported else "")}
    return {"name": root.findtext("Name") or "", "layers": layers}, info

def sample(grad, u):
    """Gradient color at u (0..1) from [(stop, rgba)], clamped at the ends."""
    if u <= grad[0][0]:
        return grad[0][1]
    for (s0, c0), (s1, c1) in zip(grad, grad[1:]):
        if u <= s1:
            f = (u - s0) / (s1 - s0) if s1 > s0 else 1.0
            return tuple(a + (b - a) * f for a, b in zip(c0, c1))
    return grad[-1][1]

def _wave(reg, t):
    # Angle like CSS gradients: 0 = towards the top, 90 = to the right, 180 = down
    a = math.radians(reg["angle"])
    dx, dy = math.sin(a), -math.cos(a)
    proj = {i: XN[i] * dx + YN[i] * dy for i in reg["keys"]}
    lo = min(XN[i] * dx + YN[i] * dy for i in KEYS)
    hi = max(XN[i] * dx + YN[i] * dy for i in KEYS)
    span = (hi - lo) or 1.0
    shift = t * reg["speed"] / 30  # speed 15 = one gradient length every 2 s
    out = {}
    for i, p in proj.items():
        p = (p - lo) / span
        if reg["split"]:
            p = abs(p - 0.5) * 2
        out[i] = sample(reg["grad"], (p * 100 / reg["width"] - shift) % 1)
    return out

def _spectrum(reg, t):
    c = sample(reg["grad"], (t * reg["speed"] / 60) % 1)  # speed 5 = one full cycle every 12 s
    return dict.fromkeys(reg["keys"], c)

def _static(reg, t):
    return dict.fromkeys(reg["keys"], reg["grad"][0][1])

def _breathing(reg, t):
    per = 20 / reg["speed"]  # speed 5 = one breath every 4 s
    cols = [c for _, c in reg["grad"]]
    r, g, b, a = cols[int(t / per) % len(cols)]
    v = math.sin(math.pi * ((t / per) % 1)) ** 2
    return dict.fromkeys(reg["keys"], (r * v, g * v, b * v, a))

# typing / particle effects reuse the app's own effects, masked to the region and drawn additively
OBJ = {"reactive": "reactive", "ripple": "ripple", "starlight": "starlight", "fire": "fire", "wheel": "spiral"}
RENDER = {"wave": _wave, "spectrum": _spectrum, "static": _static, "breathing": _breathing, **dict.fromkeys(OBJ)}

class StudioPlayer(effects.Effect):
    def __init__(self, spec, params=None, **kw):
        super().__init__(params, **kw)
        self.layers = spec["layers"]
        self.t0 = None
        self.objs = {}
        for li, layer in enumerate(self.layers):
            if layer["effect"] in OBJ:
                for ri, reg in enumerate(layer["regions"]):
                    c = reg["grad"][0][1]
                    self.objs[li, ri] = effects.make(OBJ[layer["effect"]], {
                        "color": "%02X%02X%02X" % tuple(int(v) for v in c[:3]), "palette": "",
                        "speed": max(0.1, min(5.0, reg["speed"] / 5 * self.speed))})

    def on_key(self, index, now):
        for o in self.objs.values():
            o.on_key(index, now)

    def frame(self, now):
        if self.t0 is None:
            self.t0 = now
        t = (now - self.t0) * self.speed
        out = [[0.0, 0.0, 0.0] for _ in range(gk104.KEY_SLOTS)]
        for li, layer in enumerate(self.layers):
            for ri, reg in enumerate(layer["regions"]):
                obj = self.objs.get((li, ri))
                if obj:
                    f = obj.frame(now)
                    for i in reg["keys"]:
                        o = out[i]
                        for ch in range(3):
                            o[ch] = min(255.0, o[ch] + f[i][ch] * layer["alpha"])
                    continue
                for i, (r, g, b, a) in RENDER[layer["effect"]](reg, t).items():
                    k = layer["alpha"] * a / 255
                    o = out[i]
                    o[0] += (r - o[0]) * k
                    o[1] += (g - o[1]) * k
                    o[2] += (b - o[2]) * k
        return [(int(r), int(g), int(b)) for r, g, b in out]

def build(layers_xml):
    """Tiny .ChromaEffects writer for the self-test: layers_xml = [(effect, [(row, col)], [rgb hex], angle)]."""
    def color(h):
        return "<RzColor>" + "".join(f"<{n}>{int(h[k:k + 2], 16)}</{n}>" for n, k in (("Red", 0), ("Green", 2), ("Blue", 4))) + "</RzColor>"
    layers = "".join(
        f"<EffectLayer><Effect>{eff}</Effect><Regions><EffectRegion><Cells><Item><Key><Device><Name>RAZER TEST</Name><Type>2</Type></Device></Key>"
        f"<Value><ArrayOfDeviceCell>{''.join(f'<DeviceCell><Row>{r}</Row><Col>{c}</Col></DeviceCell>' for r, c in cells)}</ArrayOfDeviceCell></Value></Item></Cells>"
        f"<Angle>{angle}</Angle><Colors>{''.join(color(h) for h in cols)}</Colors><Speed>15</Speed><Width>100</Width><Split>false</Split>"
        f"</EffectRegion></Regions><Visible>true</Visible><Alpha>100</Alpha></EffectLayer>"
        for eff, cells, cols, angle in layers_xml)
    xml = f'<?xml version="1.0"?><LightingEffects><EffectLayers>{layers}</EffectLayers><Name>Test</Name></LightingEffects>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.xml", xml)
    return buf.getvalue()

def selftest():
    w, a = gk104.KEY_INDEX["w"], gk104.KEY_INDEX["a"]
    all_cells = [(r, c) for _, r, c in cs.MAPPING]
    wr, wc = next((r, c) for i, r, c in cs.MAPPING if i == w)
    spec, info = parse(build([("static", [(wr, wc)], ["FF0000"], 90),              # W key on top
                              ("wave", all_cells, ["0000FF", "00FF00"], 90)]))     # under it everything
    assert info["layers"] == 2 and spec["layers"][0]["effect"] == "wave", "first layer in file = top"
    p = StudioPlayer(spec, {"speed": 1.0})
    f = p.frame(10.0)
    assert f[w] == (255, 0, 0), f[w]
    assert f[a] != (0, 0, 0) and f[a] != f[w], "wave under the static key"
    left, right = (min(KEYS, key=lambda i: abs(XN[i] - x)) for x in (0.25, 0.75))
    assert f[left][2] > f[left][1] and f[right][1] > f[right][2], "angle 90 = blue left, green right"
    # Huntsman Elite grid: same W key one row and column right
    spec, _ = parse(build([("static", [_elite(wr, wc), (10, 23)], ["00FF00"], 0)]))
    assert StudioPlayer(spec).frame(1.0)[w] == (0, 255, 0), "elite grid mapping"
    spec, info = parse(build([("reactive", all_cells, ["FFFFFF"], 0), ("audiometer", all_cells, ["FFFFFF"], 0)]))
    assert info["unsupported"] == ["audiometer"]
    p = StudioPlayer(spec)
    p.on_key(w, 5.0)
    assert max(p.frame(5.0)[w]) > 200 and p.frame(5.0)[a] == (0, 0, 0), "reactive"
    for bad in (b"", b"PK\x03\x04junk", build([("audiometer", all_cells, ["FFFFFF"], 0)])):
        try:
            parse(bad)
            raise AssertionError("accepted bad file")
        except ValueError:
            pass
    print("selftest ok")

if __name__ == "__main__":
    selftest()
