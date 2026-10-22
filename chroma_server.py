"""Razer Chroma SDK REST emulator that drives the SKYLOONG GK104 Pro (via gk104.py).

Listens on http://localhost:54235 like Razer's Chroma SDK service, so Chroma REST apps
(browser/HTML5 samples, tools using the REST API) light the GK104 instead of a Razer keyboard.
Only works while no Razer Synapse/Chroma service owns port 54235.

  python chroma_server.py [--idle wave|spectrum|breathing|reactive|ripple|starlight|off]
                         [--color RRGGBB] [--color2 RRGGBB|random] [--speed 1.0] [--restore 1]
  python chroma_server.py --selftest     # no keyboard needed

Keyboard effects: CHROMA_NONE, CHROMA_STATIC, CHROMA_CUSTOM (6x22), CHROMA_CUSTOM_KEY.
Other devices (mouse, headset, ...) are accepted and ignored.
shortcut: CHROMA_CUSTOM2 (8x24 grid) and deprecated effects return an error; add when an app needs them.
"""
import argparse
import itertools
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import effects
import gk104

PORT = 54235
HEARTBEAT_TIMEOUT = 15  # seconds; Razer drops sessions without heartbeat
RESULT_OK, RESULT_NOT_SUPPORTED, RESULT_NOT_FOUND = 0, 50, 1168  # Win32 codes Razer uses

# ------------------------------------------------------------------ Razer grid -> GK104 index

def _rz(row, col):
    return row << 8 | col

# GK6+ key name -> Razer RZKEY (row << 8 | col), from RzChromaSDKTypes.h.
RZKEY = {"Escape": 0x0001, "Print Screen": 0x000F, "Scroll Lock": 0x0010, "Pause": 0x0011,
         "`": 0x0101, "-": 0x010C, "=": 0x010D, "Backspace": 0x010E, "Insert": 0x010F, "Home": 0x0110,
         "Page Up": 0x0111, "Num Lock": 0x0112, "Keypad /": 0x0113, "Keypad *": 0x0114, "Keypad -": 0x0115,
         "Tab": 0x0201, "[": 0x020C, "]": 0x020D, "|": 0x020E, "Delete": 0x020F, "End": 0x0210,
         "Page Down": 0x0211, "Keypad +": 0x0215,
         "Caps Lock": 0x0301, ";": 0x030B, "'": 0x030C, "Return": 0x030E,
         "Left Shift": 0x0401, ",": 0x040A, ".": 0x040B, "/": 0x040C, "Right Shift": 0x040E,
         "Up Arrow": 0x0410, "Keypad Enter": 0x0415,
         "Left Control": 0x0501, "Left GUI": 0x0502, "Left Alt": 0x0503, "Space": 0x0507,
         "Left Space": 0x0507, "Right Space": 0x0507, "Right Alt": 0x050B, "Fn": 0x050C, "App": 0x050D,
         "Right Control": 0x050E, "Left Arrow": 0x050F, "Down Arrow": 0x0510, "Right Arrow": 0x0511,
         "Keypad 0": 0x0513, "Keypad .": 0x0514}
RZKEY.update({f"F{i}": 0x0002 + i for i in range(1, 13)})
RZKEY.update({str(d): 0x0102 + i for i, d in enumerate("1234567890")})
for row, letters in ((2, "QWERTYUIOP"), (3, "ASDFGHJKL"), (4, "ZXCVBNM")):
    start = 3 if row == 4 else 2
    RZKEY.update({ch: _rz(row, start + i) for i, ch in enumerate(letters)})
RZKEY.update({"Keypad 7": 0x0212, "Keypad 8": 0x0213, "Keypad 9": 0x0214, "Keypad 4": 0x0312,
              "Keypad 5": 0x0313, "Keypad 6": 0x0314, "Keypad 1": 0x0412, "Keypad 2": 0x0413,
              "Keypad 3": 0x0414})

# (gk_index, razer_row, razer_col) for every mapped GK key
MAPPING = [(k["index"], RZKEY[k["name"]] >> 8, RZKEY[k["name"]] & 0xFF) for k in gk104.KEYMAP]

def bgr(c):
    """Razer COLORREF 0x00BBGGRR -> (r, g, b)."""
    c = int(c)
    return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)

def keyboard_frame(body):
    """Chroma keyboard effect JSON -> 132-slot GK104 frame, or None if unsupported."""
    effect, param = body.get("effect"), body.get("param")
    if effect == "CHROMA_NONE":
        return [(0, 0, 0)] * gk104.KEY_SLOTS
    if effect == "CHROMA_STATIC":
        return [bgr(param["color"])] * gk104.KEY_SLOTS
    if effect == "CHROMA_CUSTOM":
        grid, keys = param, None
    elif effect == "CHROMA_CUSTOM_KEY":
        grid, keys = param["color"], param["key"]
    else:
        return None
    frame = [(0, 0, 0)] * gk104.KEY_SLOTS
    for idx, r, c in MAPPING:
        if keys and int(keys[r][c]) & 0x01000000:  # key layer overrides grid when its flag bit is set
            frame[idx] = bgr(int(keys[r][c]) & 0xFFFFFF)
        else:
            frame[idx] = bgr(grid[r][c])
    return frame

# ------------------------------------------------------------------ sessions and effects

class Chroma:
    def __init__(self):
        self.lock = threading.Lock()
        self.ids = itertools.count(1000)
        self.sessions = {}   # sid -> {"title", "beat", "frame"}
        self.effects = {}    # effect id -> (sid, device, frame)
        self.owner = None    # sid whose frame is shown (last to set an effect)

    def create_session(self, info):
        with self.lock:
            sid = next(self.ids)
            self.sessions[sid] = {"title": info.get("title", "?"), "beat": time.monotonic(), "frame": None}
            print(f"session {sid} opened: {self.sessions[sid]['title']}")
            return sid

    def close_session(self, sid):
        with self.lock:
            if self.sessions.pop(sid, None):
                print(f"session {sid} closed")
            self.effects = {k: v for k, v in self.effects.items() if v[0] != sid}
            if self.owner == sid:
                self.owner = next((s for s in reversed(self.sessions) if self.sessions[s]["frame"]), None)

    def beat(self, sid):
        with self.lock:
            self.sessions[sid]["beat"] = time.monotonic()

    def show(self, sid, frame):
        with self.lock:
            self.sessions[sid]["frame"] = frame
            self.owner = sid

    def expire(self):
        now = time.monotonic()
        for sid in [s for s, v in self.sessions.items() if now - v["beat"] > HEARTBEAT_TIMEOUT]:
            print(f"session {sid} heartbeat timeout")
            self.close_session(sid)

    def current(self):
        with self.lock:
            return self.sessions[self.owner]["frame"] if self.owner in self.sessions else None

STATE = Chroma()

# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _route(self):
        """'/razer/chromasdk' -> (None, None); '/s/<sid>/chromasdk/<dev>' -> (sid, dev)."""
        parts = self.path.strip("/").split("/")
        if parts[:2] == ["razer", "chromasdk"]:
            return None, None
        if len(parts) >= 3 and parts[0] == "s" and parts[2] == "chromasdk" and parts[1].isdigit():
            sid = int(parts[1])
            if sid in STATE.sessions:
                return sid, (parts[3] if len(parts) > 3 else "")
        return -1, None

    def do_OPTIONS(self):
        self._send({})

    def do_GET(self):
        sid, _ = self._route()
        if sid is None:
            return self._send({"core": "3.36.1", "device": "3.36.1", "version": "3.36.1"})
        self._send({"result": RESULT_NOT_FOUND}, 404)

    def do_POST(self):
        sid, dev = self._route()
        body = self._body()
        if sid is None:  # init
            sid = STATE.create_session(body)
            return self._send({"sessionid": sid, "uri": f"http://localhost:{PORT}/s/{sid}/chromasdk"})
        if sid == -1:
            return self._send({"result": RESULT_NOT_FOUND}, 404)
        frame = keyboard_frame(body) if dev == "keyboard" else None
        if dev == "keyboard" and frame is None:
            return self._send({"result": RESULT_NOT_SUPPORTED})
        eid = str(uuid.uuid4())
        with STATE.lock:
            STATE.effects[eid] = (sid, dev, frame)
        self._send({"result": RESULT_OK, "id": eid})

    def do_PUT(self):
        sid, dev = self._route()
        if sid in (None, -1):
            return self._send({"result": RESULT_NOT_FOUND}, 404)
        body = self._body()
        STATE.beat(sid)  # any call keeps the session alive
        if dev == "heartbeat":
            return self._send({"tick": int(time.monotonic() * 1000) & 0x7FFFFFFF})
        if dev == "effect":
            ids = body.get("ids") or [body.get("id")]
            results = []
            for eid in ids:
                e = STATE.effects.get(eid)
                if e and e[2] is not None:
                    STATE.show(sid, e[2])
                results.append({"id": eid, "result": RESULT_OK if e else RESULT_NOT_FOUND})
            return self._send({"results": results} if "ids" in body else results[0])
        if dev == "keyboard":
            frame = keyboard_frame(body)
            if frame is None:
                print("unsupported keyboard effect:", body.get("effect"))
                return self._send({"result": RESULT_NOT_SUPPORTED})
            STATE.show(sid, frame)
        self._send({"result": RESULT_OK})

    def do_DELETE(self):
        sid, dev = self._route()
        if sid in (None, -1):
            return self._send({"result": RESULT_NOT_FOUND}, 404)
        if dev == "effect":
            body = self._body()
            ids = body.get("ids") or [body.get("id")]
            with STATE.lock:
                for eid in ids:
                    STATE.effects.pop(eid, None)
            return self._send({"result": RESULT_OK})
        STATE.close_session(sid)  # DELETE {uri} = uninit
        self._send({"result": RESULT_OK})

# ------------------------------------------------------------------ device loop

def device_loop(kb, idle, stop):
    """idle: an effects.Effect shown when no Chroma app is active, or None for all off."""
    kb.enter_online()
    if idle:
        kb.on_key = lambda i: idle.on_key(i, time.monotonic())
    last_ping, last_frame = time.monotonic(), None
    while not stop.is_set():
        now = time.monotonic()
        STATE.expire()
        frame = STATE.current() or (idle.frame(now) if idle else effects.blank())
        if now - last_ping > 1.5:  # keyboard leaves online mode without pings
            kb.ping()
            last_ping = now
        if frame != last_frame:
            kb.frame(frame)
            last_frame = frame
        else:
            time.sleep(0.005)

def selftest():
    w = gk104.KEY_INDEX["w"]
    grid = [[0] * 22 for _ in range(6)]
    grid[2][3] = 0x0000FF  # Razer W, red in BGR
    f = keyboard_frame({"effect": "CHROMA_CUSTOM", "param": grid})
    assert f[w] == (255, 0, 0) and sum(map(sum, f)) == 255, "CUSTOM mapping"
    keys = [[0] * 22 for _ in range(6)]
    keys[0][1] = 0x01FF0000  # Esc, blue, flag set
    f = keyboard_frame({"effect": "CHROMA_CUSTOM_KEY", "param": {"color": grid, "key": keys}})
    assert f[gk104.KEY_INDEX["escape"]] == (0, 0, 255) and f[w] == (255, 0, 0), "CUSTOM_KEY"
    assert keyboard_frame({"effect": "CHROMA_STATIC", "param": {"color": 0x00FF00}})[0] == (0, 255, 0)
    assert len({(r, c) for _, r, c in MAPPING}) == len(MAPPING) - 2, "only split space shares a cell"
    print("selftest ok:", len(MAPPING), "keys mapped")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle", choices=[*effects.EFFECTS, "off"], default="wave", help="shown when no app is active")
    ap.add_argument("--color", default="00FF00", help="idle effect color")
    ap.add_argument("--color2", default=None, help="RRGGBB or 'random' (breathing, starlight)")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--restore", type=int, default=1, choices=[0, 1, 2, 3, 4])
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # localhost only, like Razer's service
    kb = gk104.Keyboard()
    print(kb.version())
    stop = threading.Event()
    idle = None if a.idle == "off" else effects.make(a.idle, a.color, a.color2, a.speed)
    dev = threading.Thread(target=device_loop, args=(kb, idle, stop), daemon=True)
    dev.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Chroma SDK REST emulator on http://localhost:{PORT} (Ctrl+C to stop)")
    try:
        while dev.is_alive():
            dev.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        dev.join(2)
        server.shutdown()
        kb.leave_online(a.restore)
        kb.close()

if __name__ == "__main__":
    main()
