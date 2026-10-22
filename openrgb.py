"""Sync other PC RGB (fans, RAM, motherboard, GPU, ...) with the keyboard through OpenRGB.

OpenRGB does the hardware work (SMBus RAM, Mystic Light, GPU, ARGB hubs); we talk to its
SDK server (Settings > SDK Server > Start, default 127.0.0.1:6742) with its binary protocol:
  header: b"ORGB", u32 device index, u32 packet id, u32 payload size (little endian)
Stdlib only. Protocol version 3 is requested so the controller layout stays fixed.
"""
import socket
import struct
import threading
import time

import effects

PORT = 6742
PROTOCOL = 3
COUNT, DATA, PROTO_VER, CLIENT_NAME, LIST_UPDATED = 0, 1, 40, 50, 100
RESIZE_ZONE, UPDATE_LEDS, SET_CUSTOM_MODE = 1000, 1050, 1100
OWN = "GK104"  # our own keyboard shows up in OpenRGB too: never drive it from there
TYPES = ["Motherboard", "RAM", "GPU", "Cooler", "LED strip", "Keyboard", "Mouse", "Mousemat", "Headset",
         "Headset stand", "Gamepad", "Light", "Speaker", "Virtual", "Storage", "Case", "Microphone",
         "Accessory", "Keypad"]

class Reader:
    def __init__(self, data):
        self.d, self.o = data, 0

    def take(self, fmt):
        v = struct.unpack_from("<" + fmt, self.d, self.o)
        self.o += struct.calcsize("<" + fmt)
        return v if len(v) > 1 else v[0]

    def text(self):
        n = self.take("H")
        s = self.d[self.o:self.o + n].rstrip(b"\0").decode("utf-8", "replace")
        self.o += n
        return s

def _parse(data):
    """Controller blob (protocol 3) -> {name, vendor, type, zones: [(name, leds)], leds}."""
    r = Reader(data)
    r.take("I")
    kind = r.take("i")
    name, vendor = r.text(), r.text()
    for _ in range(4):
        r.text()
    n_modes = r.take("H")
    r.take("i")  # active mode
    for _ in range(n_modes):
        r.text()
        r.take("iIIIIIIIIIII")
        n_colors = r.take("H")
        r.o += 4 * n_colors
    zones = []
    for _ in range(r.take("H")):
        zname = r.text()
        _, lo, hi, count = r.take("iIII")
        mlen = r.take("H")
        r.o += mlen  # matrix map (height, width, cells) as one blob
        zones.append((zname, count, lo, hi))
    leds = r.take("H")
    return {"name": name, "vendor": vendor, "type": TYPES[kind] if 0 <= kind < len(TYPES) else "Device",
            "zones": zones, "leds": leds}

class Client:
    def __init__(self, host="127.0.0.1", port=PORT, timeout=2.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.lock = threading.Lock()
        self.dirty = False  # server announced a device list change
        self.proto = self.request(PROTO_VER, struct.pack("<I", PROTOCOL), reply="I")
        self.proto = min(self.proto, PROTOCOL)
        self.send(CLIENT_NAME, b"FrostRGB\0")

    def send(self, pid, payload=b"", dev=0):
        with self.lock:
            self.sock.sendall(b"ORGB" + struct.pack("<III", dev, pid, len(payload)) + payload)

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("OpenRGB closed the connection")
            buf += chunk
        return buf

    def request(self, pid, payload=b"", dev=0, reply=None):
        self.send(pid, payload, dev)
        while True:
            magic, rdev, rpid, size = struct.unpack("<4sIII", self._recv(16))
            if magic != b"ORGB":
                raise ConnectionError("not an OpenRGB server")
            body = self._recv(size)
            if rpid == LIST_UPDATED:
                self.dirty = True
                continue
            if rpid == pid:
                return struct.unpack("<" + reply, body)[0] if reply else body

    def devices(self):
        self.dirty = False
        n = self.request(COUNT, reply="I")
        return [_parse(self.request(DATA, struct.pack("<I", self.proto), dev=i)) for i in range(n)]

    def resize(self, dev, zone, size):
        self.send(RESIZE_ZONE, struct.pack("<ii", zone, size), dev=dev)

    def set_custom(self, dev):
        self.send(SET_CUSTOM_MODE, dev=dev)

    def set_leds(self, dev, colors):
        body = struct.pack("<H", len(colors)) + b"".join(struct.pack("<BBBx", *c) for c in colors)
        self.send(UPDATE_LEDS, struct.pack("<I", len(body) + 4) + body, dev=dev)

    def poll(self):
        """Drain notifications without blocking (device list changes)."""
        self.sock.setblocking(False)
        try:
            while True:
                head = self.sock.recv(16, socket.MSG_PEEK)
                if len(head) < 16:
                    break
                self.sock.setblocking(True)
                _, _, pid, size = struct.unpack("<4sIII", self._recv(16))
                self._recv(size)
                self.dirty |= pid == LIST_UPDATED
                self.sock.setblocking(False)
        except (BlockingIOError, InterruptedError):
            pass
        finally:
            self.sock.setblocking(True)

    def close(self):
        self.sock.close()

def spread(frame, n):
    """n colors sampled left to right across the keyboard (column averages), so waves travel."""
    if n <= 0:
        return []
    cols = [[0, 0, 0, 0] for _ in range(n)]
    for i, x in effects.XN.items():
        c = cols[min(n - 1, int(x * n))]
        r, g, b = frame[i]
        c[0] += r; c[1] += g; c[2] += b; c[3] += 1
    out, last = [], (0, 0, 0)
    for r, g, b, k in cols:
        last = (r // k, g // k, b // k) if k else last  # more LEDs than columns: repeat neighbor
        out.append(last)
    return out

def average(frame, n):
    keys = list(effects.XN)
    c = tuple(sum(frame[i][ch] for i in keys) // len(keys) for ch in range(3))
    return [c] * n

def brightest(frame, n):
    """Average of the lit keys only: a few lit keys still show their color on the fans."""
    lit = [frame[i] for i in effects.XN if max(frame[i]) > 20]
    if not lit:
        return [(0, 0, 0)] * n
    return [tuple(sum(c[ch] for c in lit) // len(lit) for ch in range(3))] * n

MODES = {"spread": spread, "average": average, "lit": brightest}

class Sync:
    """Background thread: keeps a connection to OpenRGB and mirrors frames to every enabled device."""

    def __init__(self, get_frame, get_settings):
        self.get_frame, self.get_settings = get_frame, get_settings
        self.status = {"connected": False, "devices": [], "error": None}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop.set()

    def _run(self):
        while not self.stop.is_set():
            s = self.get_settings()
            if not s["rgb_sync"]:
                self.status.update(connected=False, devices=[], error=None)
                self.stop.wait(0.5)
                continue
            cli = None
            try:
                cli = Client(port=s["rgb_port"])
                devs, custom, last, sized = cli.devices(), set(), {}, None
                self.status.update(connected=True, devices=devs, error=None)
                while not self.stop.is_set():
                    s = self.get_settings()
                    if not s["rgb_sync"] or s["rgb_port"] != cli.sock.getpeername()[1]:
                        break
                    cli.poll()
                    if sized != s["rgb_zones"]:  # LED counts for ARGB headers (they can't detect them)
                        for i, d in enumerate(devs):
                            for z, (zname, count, lo, hi) in enumerate(d["zones"]):
                                want = s["rgb_zones"].get(f"{d['name']}|{zname}")
                                if want is not None and lo <= want <= hi and want != count:
                                    cli.resize(i, z, want)
                                    cli.dirty = True
                        sized = dict(s["rgb_zones"])
                    if cli.dirty:
                        devs, custom, last = cli.devices(), set(), {}
                        self.status["devices"] = devs
                    frame, fn = self.get_frame(), MODES[s["rgb_mode"]]
                    for i, d in enumerate(devs):
                        if d["name"] in s["rgb_off"] or not d["leds"] or OWN in d["name"].upper():
                            continue
                        if i not in custom:
                            cli.set_custom(i)  # direct mode, otherwise the device ignores our colors
                            custom.add(i)
                        # per zone: every fan / strip / RAM stick shows the whole keyboard, not a slice of the board
                        colors = [c for z in d["zones"] for c in fn(frame, z[1])]
                        if last.get(i) != colors:
                            cli.set_leds(i, colors)
                            last[i] = colors
                    self.stop.wait(1 / 30)
            except (OSError, ConnectionError, struct.error, ValueError) as e:
                self.status.update(connected=False, devices=[], error=str(e) or type(e).__name__)
                self.stop.wait(3)  # OpenRGB not running yet: retry quietly
            finally:
                if cli:
                    cli.close()

# ------------------------------------------------------------------ self-test with a fake server

def _blob(name, kind, zones):
    def text(s):
        b = s.encode() + b"\0"
        return struct.pack("<H", len(b)) + b
    mode = text("Direct") + struct.pack("<iIIIIIIIIIII", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0) + struct.pack("<H", 1) + b"\1\2\3\0"
    body = struct.pack("<i", kind) + text(name) + text("Vendor") + text("d") + text("v") + text("s") + text("l")
    body += struct.pack("<Hi", 1, 0) + mode
    body += struct.pack("<H", len(zones))
    for zname, n in zones:
        body += text(zname) + struct.pack("<iIII", 0, n, n, n) + struct.pack("<H", 12) + struct.pack("<III", 1, 1, 0)
    total = sum(n for _, n in zones)
    body += struct.pack("<H", total) + b"".join(text(f"LED {k}") + struct.pack("<I", k) for k in range(total))
    body += struct.pack("<H", total) + b"\0" * 4 * total
    return struct.pack("<I", len(body) + 4) + body

def selftest():
    import gk104
    devs = [_blob("Corsair Vengeance RGB DDR5", 1, [("DRAM", 10)]), _blob("MSI X870", 0, [("JRAINBOW1", 8), ("Onboard", 2)]),
            _blob("Skyloong GK104 Pro", 5, [("Keyboard", 106)])]
    got = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        def recv(n):
            b = b""
            while len(b) < n:
                b += c.recv(n - len(b))
            return b
        try:
            while True:
                _, dev, pid, size = struct.unpack("<4sIII", recv(16))
                body = recv(size)
                reply = {PROTO_VER: struct.pack("<I", 4), COUNT: struct.pack("<I", len(devs))}.get(pid)
                if pid == DATA:
                    assert struct.unpack("<I", body)[0] == 3, "asks for protocol 3"
                    reply = devs[dev]
                if reply is not None:
                    c.sendall(b"ORGB" + struct.pack("<III", dev, pid, len(reply)) + reply)
                if pid in (UPDATE_LEDS, SET_CUSTOM_MODE):
                    got.append((pid, dev, body))
        except (OSError, struct.error):
            pass
    threading.Thread(target=serve, daemon=True).start()

    frame = effects.blank()
    for k in gk104.KEYMAP:
        frame[k["index"]] = (255, 0, 0) if effects.XN[k["index"]] < 0.5 else (0, 0, 255)
    settings = {"rgb_sync": True, "rgb_port": port, "rgb_mode": "spread", "rgb_off": [], "rgb_zones": {"MSI X870|Onboard": 2}}
    sync = Sync(lambda: frame, lambda: settings).start()
    for _ in range(100):
        if sum(1 for p, _, _ in got if p == UPDATE_LEDS) >= 2:
            break
        time.sleep(0.05)
    sync.close()
    assert [d["leds"] for d in sync.status["devices"]] == [10, 10, 106] and sync.status["devices"][0]["type"] == "RAM", sync.status
    assert [z[:2] for z in sync.status["devices"][1]["zones"]] == [("JRAINBOW1", 8), ("Onboard", 2)]
    assert not any(d == 2 for p, d, _ in got), "own keyboard is never driven through OpenRGB"
    assert any(p == RESIZE_ZONE and d == 1 and struct.unpack("<ii", b) == (0, 8) for p, d, b in got) is False  # same size: no resize
    assert [(p, d) for p, d, _ in got[:2]] == [(SET_CUSTOM_MODE, 0), (UPDATE_LEDS, 0)], got[:2]
    body = next(b for p, d, b in got if p == UPDATE_LEDS and d == 0)
    size, n = struct.unpack_from("<IH", body)
    leds = [tuple(body[6 + 4 * k:9 + 4 * k]) for k in range(n)]
    assert size == len(body) and n == 10 and leds[0] == (255, 0, 0) and leds[-1] == (0, 0, 255), leds
    body = next(b for p, d, b in got if p == UPDATE_LEDS and d == 1)  # board: JRAINBOW1 (8) + Onboard (2)
    leds = [tuple(body[6 + 4 * k:9 + 4 * k]) for k in range(10)]
    assert leds[0] == (255, 0, 0) and leds[7] == (0, 0, 255) and leds[8] == (255, 0, 0) and leds[9] == (0, 0, 255), "each zone gets the full gradient"
    assert average(frame, 2)[0][0] > 0 and brightest(effects.blank(), 3) == [(0, 0, 0)] * 3
    assert len(spread(frame, 300)) == 300 and spread(frame, 0) == []
    print("selftest ok")

if __name__ == "__main__":
    selftest()
