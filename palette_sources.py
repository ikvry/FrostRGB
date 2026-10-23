"""Online palette discovery: Lospec (public JSON) and Color Hunt (the feed its own site uses).

COLOURlovers closed its API in 2026, so it is not offered. Results are cached for 10 minutes.
Every color goes through `for_leds`: near-black stops are dropped (an off LED looks like a gap,
not a color) and long palettes are thinned to 10 evenly spaced stops.
"""
import json
import re
import time
import urllib.parse
import urllib.request

UA = "FrostRGB/1.0 (+https://github.com/ikvry/FrostRGB)"
HEX = re.compile(r"^[0-9a-fA-F]{6}$")
_cache = {}

def for_leds(colors, max_stops=10):
    """Clean hex colors for keyboard LEDs; returns [] if fewer than 2 usable colors remain."""
    out = [c.upper() for c in colors if isinstance(c, str) and HEX.match(c) and max(bytes.fromhex(c)) >= 40]
    if len(out) > max_stops:
        out = [out[round(k * (len(out) - 1) / (max_stops - 1))] for k in range(max_stops)]
    return out if len(out) >= 2 else []

def _get(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=8) as r:
        if r.length and r.length > 4_000_000:
            raise ValueError("response too large")
        return json.loads(r.read(4_000_000))

def lospec(query, page):
    # max 8 colors: bigger Lospec sets are pixel-art color banks, not gradients
    q = urllib.parse.urlencode({"colorNumberFilterType": "max", "colorNumber": 8, "page": page, "tag": query,
                                "sortingType": "downloads"})
    out = []
    for p in _get(f"https://lospec.com/palette-list/load?{q}").get("palettes", []):
        colors = for_leds(p.get("colors", []))
        user = p.get("user")
        author = user.get("name") if isinstance(user, dict) else (user if isinstance(user, str) else "")
        if colors:
            out.append({"name": str(p.get("title") or p.get("slug") or "Lospec")[:40], "author": str(author or "")[:60],
                        "colors": colors, "url": f"https://lospec.com/palette-list/{p.get('slug', '')}"})
    return out

def colorhunt(query, page):
    body = urllib.parse.urlencode({"step": page, "sort": "popular", "tags": query, "timeframe": 4000}).encode()
    rows = _get("https://colorhunt.co/php/feed.php", body, {"X-Requested-With": "XMLHttpRequest",
                "Referer": "https://colorhunt.co/", "Content-Type": "application/x-www-form-urlencoded"})
    out = []
    for p in rows if isinstance(rows, list) else []:
        code = str(p.get("code", ""))
        colors = for_leds([code[k:k + 6] for k in range(0, len(code), 6)])
        if colors and len(code) == 24:
            out.append({"name": f"Color Hunt {code[:6].upper()}", "author": f"{p.get('likes', '?')} likes",
                        "colors": colors, "url": f"https://colorhunt.co/palette/{code}"})
    return out

SOURCES = {"lospec": lospec, "colorhunt": colorhunt}

def discover(source, query="", page=0):
    """[{name, author, colors, url}]. Raises ValueError for bad input, OSError when the site fails."""
    if source not in SOURCES:
        raise ValueError("unknown source")
    query = re.sub(r"[^\w -]", "", str(query))[:40].strip().lower()
    page = int(page)
    if not 0 <= page <= 50:
        raise ValueError("bad page")
    key = (source, query, page)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    try:
        res = SOURCES[source](query, page)
    except (ValueError, KeyError, AttributeError, TypeError) as e:  # site changed its format
        raise OSError(f"{source} returned something unexpected ({e})")
    _cache[key] = (time.monotonic(), res)
    return res

def selftest():
    assert for_leds(["000000", "ff0000", "00ff00"]) == ["FF0000", "00FF00"], "black dropped"
    assert for_leds(["101010", "ff0000"]) == [], "one usable color is not a palette"
    many = for_leds([f"{k:02X}FF00" for k in range(40, 240, 5)])
    assert len(many) == 10 and many[0] == "28FF00" and many[-1] == "EBFF00", many
    for bad in (("nope", "", 0), ("lospec", "", 99)):
        try:
            discover(*bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    print("selftest ok")

if __name__ == "__main__":
    import sys
    selftest()
    if "--live" in sys.argv:
        for src in SOURCES:
            res = discover(src, "sunset")
            print(src, len(res), res[:2])
