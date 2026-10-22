"""Advanced RGB editor: a stack of layers composited per key.

Layer types:
  paint            per-key colors painted in the UI (unpainted keys are transparent)
  <effect name>    any effects.EFFECTS entry, with its own params (palette, speed, ...)
  <music mode>     any music.MODES entry
Each layer can be limited to a set of keys, has opacity 0-100 and blend "normal" or "add".
"""
import re

import effects
import gk104
import music

N = gk104.KEY_SLOTS
LAYER_TYPES = {"paint", *effects.EFFECTS, *music.MODES}
KEY_IDS = {k["index"] for k in gk104.KEYMAP}
HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
MAX_LAYERS, MAX_PROFILES = 16, 40

def default_editor():
    return {"layers": [new_layer(1, "Base", "paint")]}

def new_layer(lid, name, kind, **params):
    return {"id": lid, "name": name, "type": kind, **effects.clean_params(params, kind),
            "opacity": 100, "blend": "normal", "enabled": True, "keys": None, "paint": {}}

def _num(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValueError(f"bad number {v!r}")
    return v

def validate_layer(l):
    if not isinstance(l, dict):
        raise ValueError("layer must be an object")
    if l.get("type") not in LAYER_TYPES:
        raise ValueError(f"bad layer type {l.get('type')!r}")
    name = l.get("name", "")
    if not isinstance(name, str) or len(name) > 40:
        raise ValueError("bad layer name")
    keys = l.get("keys")
    if keys is not None and not (isinstance(keys, list) and all(isinstance(k, int) and k in KEY_IDS for k in keys)):
        raise ValueError("bad key list")
    paint = l.get("paint", {})
    if not isinstance(paint, dict) or not all(isinstance(k, str) and k.isdigit() and int(k) in KEY_IDS for k in paint):
        raise ValueError("bad paint map")
    if not all(isinstance(v, str) and HEX.match(v) for v in paint.values()):
        raise ValueError("bad paint color")
    if l.get("blend", "normal") not in ("normal", "add"):
        raise ValueError("bad blend")
    if not isinstance(l.get("enabled", True), bool):
        raise ValueError("bad enabled")
    params = effects.clean_params({k: l[k] for k in effects.PARAM_DEFAULTS if k in l}, l["type"])
    return {"id": int(_num(l.get("id", 0), 0, 1e9)), "name": name, "type": l["type"], **params,
            "opacity": _num(l.get("opacity", 100), 0, 100), "blend": l.get("blend", "normal"),
            "enabled": l.get("enabled", True), "keys": sorted(set(keys)) if keys is not None else None,
            "paint": {k: v.upper() for k, v in paint.items()}}

def validate_editor(e):
    if not isinstance(e, dict) or not isinstance(e.get("layers"), list) or len(e["layers"]) > MAX_LAYERS:
        raise ValueError("editor needs a layers list (max 16)")
    return {"layers": [validate_layer(l) for l in e["layers"]]}

def validate_profiles(p):
    if not isinstance(p, dict) or len(p) > MAX_PROFILES:
        raise ValueError("profiles must be an object (max 40)")
    if not all(isinstance(k, str) and 0 < len(k) <= 40 for k in p):
        raise ValueError("bad profile name")
    return {k: validate_editor(v) for k, v in p.items()}

class Compositor:
    """Builds effect objects for the layers and blends their frames. Keeps an effect's state
    (e.g. reactive presses) when only unrelated settings change."""

    def __init__(self, engine):
        self.engine = engine
        self.cache = {}   # layer id -> (signature, effect object)
        self.layers = []  # (layer dict, effect object or None, key set or None, paint colors)

    def set(self, editor):
        built, cache = [], {}
        for l in editor["layers"]:
            obj = None
            if l["type"] != "paint":
                params = {k: l[k] for k in effects.PARAM_DEFAULTS}
                sig = (l["type"], tuple(sorted(params.items())))
                old = self.cache.get(l["id"])
                if old and old[0] == sig:
                    obj = old[1]
                else:
                    obj = (effects.make(l["type"], params) if l["type"] in effects.EFFECTS
                           else music.make(l["type"], self.engine, params))
                cache[l["id"]] = (sig, obj)
            paint = {int(k): tuple(bytes.fromhex(v)) for k, v in l["paint"].items()}
            built.append((l, obj, set(l["keys"]) if l["keys"] is not None else None, paint))
        self.cache, self.layers = cache, built

    def on_key(self, index, now):
        for _, obj, _, _ in self.layers:
            if obj:
                obj.on_key(index, now)

    def frame(self, now):
        out = [[0.0, 0.0, 0.0] for _ in range(N)]
        for l, obj, mask, paint in self.layers:
            if not l["enabled"] or l["opacity"] <= 0:
                continue
            a = l["opacity"] / 100
            if obj is None:  # paint: only painted keys are drawn
                src, keys = paint, paint.keys()
            else:
                f = obj.frame(now)
                src, keys = None, KEY_IDS
            add = l["blend"] == "add"
            for i in keys:
                if mask is not None and i not in mask:
                    continue
                c = src[i] if src is not None else f[i]
                o = out[i]
                for ch in range(3):
                    o[ch] = min(255.0, o[ch] + c[ch] * a) if add else o[ch] + (c[ch] - o[ch]) * a
        return [(int(r), int(g), int(b)) for r, g, b in out]

def selftest():
    class E:
        def snapshot(self):
            import audio
            return audio.Analyzer().snapshot()
    w, a = gk104.KEY_INDEX["w"], gk104.KEY_INDEX["a"]
    ed = default_editor()
    ed["layers"][0]["paint"] = {str(w): "FF0000"}
    top = new_layer(2, "Azul em A", "static", color="0000FF")
    top.update(keys=[a], opacity=50)
    ed["layers"].append(top)
    ed = validate_editor(ed)
    c = Compositor(E())
    c.set(ed)
    f = c.frame(1.0)
    assert f[w] == (255, 0, 0), "painted key"
    assert f[a] == (0, 0, 127) and f[gk104.KEY_INDEX["s"]] == (0, 0, 0), "masked 50% static layer"
    obj = c.layers[1][1]
    ed["layers"][0]["paint"][str(a)] = "00FF00"
    c.set(validate_editor(ed))
    assert c.layers[1][1] is obj, "effect object kept when only paint changed"
    assert c.frame(1.0)[a] == (0, 127, 127), "50% blue over green"
    react = new_layer(3, "R", "reactive", color="FFFFFF")
    react.update(blend="add")
    ed["layers"].append(react)
    c.set(validate_editor(ed))
    c.on_key(w, 1.0)
    assert c.frame(1.0)[w] == (255, 255, 255), "add blend saturates"
    ed["layers"].append(new_layer(4, "M", "equalizer"))
    c.set(validate_editor(ed))
    c.frame(1.0)
    for bad in ({"layers": [{"type": "nope"}]}, {"layers": [dict(new_layer(1, "x", "paint"), keys=[999])]},
                {"layers": [dict(new_layer(1, "x", "paint"), paint={"46": "red"})]}, {"layers": "x"},
                {"layers": [dict(new_layer(1, "x", "wave"), palette="nope")]}):
        try:
            validate_editor(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    print("selftest ok")

if __name__ == "__main__":
    selftest()
