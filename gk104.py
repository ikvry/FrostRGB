"""Minimal SKYLOONG GK104 Pro RGB driver (GK6+ protocol), Windows only, stdlib only.

Usage:
  python gk104.py version
  python gk104.py rainbow [--seconds 10] [--restore 1]
  python gk104.py solid FF0000 [--seconds 5] [--restore 1]
  python gk104.py keys W A S D --color 00FF00 [--seconds 10]

Protocol taken from GK6+ 8.0.7 (src/libs/cmd.js, src/libs/driver.js).
Only safe commands are used: version/id queries, ping, mode change, live LED frames.
Never add 0x41 (WRITE_MAC_VID) or 0x21 (CleanData): they rewrite device data.
"""
import argparse
import colorsys
import ctypes
import json
import queue
import struct
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

VID_PID_IFACE = "vid_1ea7&pid_0907&mi_01"  # vendor-defined HID interface (usage page 0xFF00)
KEY_SLOTS = 132  # DefaultMatrix in driver.js
CHUNK = 56

# ---------------------------------------------------------------- protocol

def _crc_table():
    t = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else (c << 1)
        t.append(c & 0xFFFF)
    return t

_CRC = _crc_table()

def crc16_ccitt(data, crc=0xFFFF):
    for b in data:
        crc = ((crc << 8) ^ _CRC[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc

def packet(cmd, sub=0, header=0, body=b""):
    """64-byte request: [cmd, sub, u32 header, u16 crc, body...], crc over the whole buffer."""
    buf = bytearray(64)
    buf[0], buf[1] = cmd, sub
    struct.pack_into("<I", buf, 2, header)
    buf[8:8 + len(body)] = body
    struct.pack_into("<H", buf, 6, crc16_ccitt(buf))
    return bytes(buf)

# ---------------------------------------------------------------- Windows HID I/O

_cfg = ctypes.WinDLL("cfgmgr32")
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

class GUID(ctypes.Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

HID_GUID = GUID(0x4D1E55B2, 0xF16F, 0x11CF, (ctypes.c_ubyte * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))

_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_k32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
INVALID_HANDLE = wintypes.HANDLE(-1).value

def find_path():
    size = wintypes.ULONG()
    if _cfg.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(HID_GUID), None, 0):
        raise OSError("CM_Get_Device_Interface_List_SizeW failed")
    buf = ctypes.create_unicode_buffer(size.value)
    if _cfg.CM_Get_Device_Interface_ListW(ctypes.byref(HID_GUID), None, buf, size.value, 0):
        raise OSError("CM_Get_Device_Interface_ListW failed")
    paths = [p for p in buf[:size.value].split("\0") if p]
    hits = [p for p in paths if VID_PID_IFACE in p.lower()]
    if not hits:
        raise SystemExit("GK104 (1EA7:0907 MI_01) not found. Is the keyboard plugged in?")
    return hits[0]

class Keyboard:
    def __init__(self):
        path = find_path()
        # Two handles: sync I/O on one handle is serialized, so a pending ReadFile would block WriteFile.
        self.h, self.hw = self._open(path), self._open(path)
        self.inbox = queue.Queue()
        self.on_key = None  # callback(index) for each new key press while in online mode
        self._down = set()
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    @staticmethod
    def _open(path):
        h = _k32.CreateFileW(path, 0xC0000000, 0x3, None, 3, 0, None)  # RW, share RW, OPEN_EXISTING
        if h in (None, INVALID_HANDLE):
            raise OSError(ctypes.get_last_error(), "CreateFile failed on " + path)
        return h

    def _reader(self):
        buf = ctypes.create_string_buffer(65)
        n = wintypes.DWORD()
        while self.alive:
            if not _k32.ReadFile(self.h, buf, 65, ctypes.byref(n), None):
                return
            r = buf.raw[1:n.value]  # drop report id 0
            if r[:2] == b"\x15\x04":  # key report: bitmap of pressed keys in bytes 8..23 (driver.js _onData)
                down = {(i - 8) * 8 + j for i in range(8, 24) for j in range(8) if r[i] >> j & 1}
                if self.on_key:
                    for k in down - self._down:  # report bits are GK6+ LogicCode, not LED index
                        for idx in LOGIC_TO_INDICES.get(k, ()):
                            self.on_key(idx)
                self._down = down
            else:
                self.inbox.put(r)

    def request(self, pkt, timeout=1.0):
        """Send one packet, return the first reply whose byte 0 echoes the command."""
        while not self.inbox.empty():
            self.inbox.get_nowait()
        n = wintypes.DWORD()
        data = b"\x00" + pkt  # Windows needs report id 0 in front (driver.js does the same)
        if not _k32.WriteFile(self.hw, data, len(data), ctypes.byref(n), None):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            try:
                r = self.inbox.get(timeout=left)
            except queue.Empty:
                break
            if r and r[0] == pkt[0]:
                return r
        raise TimeoutError(f"no reply to cmd 0x{pkt[0]:02X}")

    def close(self):
        self.alive = False
        _k32.CancelIoEx(self.h, None)
        _k32.CloseHandle(self.h)
        _k32.CloseHandle(self.hw)

    # --- commands (names match cmd.js)
    def version(self):
        r = self.request(packet(0x01, 0x01))
        fwid, fwver = struct.unpack_from("<IH", r, 8)
        mid = struct.unpack_from("<I", self.request(packet(0x01, 0x08)), 8)[0]
        r = self.request(packet(0x01, 0x09))
        return {"FWID": f"0x{fwid:08X}", "FW": f"v{fwver >> 8}.{fwver & 0xFF}", "ModelID": mid,
                "matrix_cols": r[8], "matrix_rows": r[9]}

    def ping(self):
        self.request(packet(0x0C))

    def toggle_key_report(self, on):
        self.request(packet(0x15, 0x03, 0, b"\x01" if on else b"\x00"))

    def change_mode(self, mode):
        return self.request(packet(0x0B, mode), timeout=3.0)[1]

    def enter_online(self):
        # Same handshake as driver.js setOnlineMode(); LED frames get no reply without the key-report toggle.
        self.ping()
        print("online mode:", self.change_mode(5))
        self.ping()
        self.toggle_key_report(True)

    def leave_online(self, restore):
        self.toggle_key_report(False)  # driver.js ChangeMode() does this before leaving mode 5
        print("restore mode", restore, "->", self.change_mode(restore))

    def frame(self, colors):
        """colors: list of (r, g, b) per key slot, up to 132. Same layout as driver.js Display()."""
        data = bytearray(KEY_SLOTS * 4)
        for i, (r, g, b) in enumerate(colors[:KEY_SLOTS]):
            data[i * 4:i * 4 + 4] = bytes((r, g, b, 100))
        for addr in range(0, len(data), CHUNK):
            chunk = bytes(data[addr:addr + CHUNK])
            # Reply is [cmd, sub, ack]; GK6+ reads byte 1 (sub), which only works by luck for sub=1.
            if self.request(packet(0x1A, 0x01, addr | (len(chunk) << 24), chunk))[2] != 1:
                raise RuntimeError(f"LED chunk at {addr} rejected")
        if self.request(packet(0x1A, 0x02))[2] != 1:
            raise RuntimeError("LED frame commit rejected")

# ---------------------------------------------------------------- effects

# LED index = row * 22 + col (6x22 grid, same shape as Razer Chroma's keyboard grid).
# gk104_keymap.json is extracted from GK6+ resources/device/656802056/data/keymap.js (LocationCode).
KEYMAP = json.loads((Path(__file__).with_name("gk104_keymap.json")).read_text(encoding="utf-8"))
KEY_INDEX = {k["name"].lower(): k["index"] for k in KEYMAP}
# Key-report bit (GK6+ LogicCode) -> LED indices; the space bar has 3 LEDs but reports as "Left Space".
_SPACE = [k["index"] for k in KEYMAP if k["name"] in ("Left Space", "Space", "Right Space")]
LOGIC_TO_INDICES = {k["logic"]: (_SPACE if k["index"] in _SPACE else [k["index"]]) for k in KEYMAP}

def rainbow(t):
    """Horizontal wave across the physical layout (pixel x from GK6+)."""
    colors = [(0, 0, 0)] * KEY_SLOTS
    for k in KEYMAP:
        colors[k["index"]] = tuple(int(c * 255) for c in colorsys.hsv_to_rgb((k["x"] / 900 - t / 3) % 1, 1, 1))
    return colors

def keys_frame(names, rgb, base=(0, 0, 0)):
    colors = [base] * KEY_SLOTS
    for n in names:
        if n.lower() not in KEY_INDEX:
            raise SystemExit(f"unknown key {n!r}; names are in gk104_keymap.json")
        colors[KEY_INDEX[n.lower()]] = rgb
    return colors

def run_effect(kb, make_frame, seconds, restore):
    kb.enter_online()
    start = last_ping = time.monotonic()
    frames = 0
    try:
        while (now := time.monotonic()) - start < seconds:
            if now - last_ping > 1.5:  # keyboard expects a ping every 2 s in online mode
                kb.ping()
                last_ping = now
            kb.frame(make_frame(now - start))
            frames += 1
    finally:
        print(f"{frames} frames, {frames / max(time.monotonic() - start, 1e-6):.1f} fps")
        kb.leave_online(restore)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["version", "rainbow", "solid", "keys"])
    ap.add_argument("args", nargs="*", help="solid: RRGGBB; keys: key names (e.g. W A S D)")
    ap.add_argument("--color", default="FF0000", help="color for 'keys'")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--restore", type=int, default=1, choices=[0, 1, 2, 3, 4],
                    help="mode to return to (1 = standard, 2-4 = onboard profiles)")
    a = ap.parse_args()
    kb = Keyboard()
    try:
        print(kb.version())
        if a.action == "rainbow":
            run_effect(kb, rainbow, a.seconds, a.restore)
        elif a.action == "solid":
            rgb = tuple(bytes.fromhex(a.args[0] if a.args else "FFFFFF"))
            run_effect(kb, lambda t: [rgb] * KEY_SLOTS, a.seconds, a.restore)
        elif a.action == "keys":
            frame = keys_frame(a.args, tuple(bytes.fromhex(a.color)))
            run_effect(kb, lambda t: frame, a.seconds, a.restore)
    finally:
        kb.close()

if __name__ == "__main__":
    main()
