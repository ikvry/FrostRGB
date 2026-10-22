"""Acts like a game: loads RzChromaSDK64.dll and checks that GK104 Studio (app.py) receives the effects.

Run with app.py already running:  python games/test_dll.py
Also checks the 32-bit DLL's export table (it cannot be loaded into 64-bit Python).
"""
import ctypes
import json
import struct
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import gk104

EXPORTS = {"Init", "InitSDK", "UnInit", "CreateEffect", "CreateKeyboardEffect", "CreateMouseEffect",
           "CreateHeadsetEffect", "CreateMousepadEffect", "CreateKeypadEffect", "CreateChromaLinkEffect",
           "SetEffect", "DeleteEffect", "RegisterEventNotification", "UnregisterEventNotification", "QueryDevice"}

def pe_exports(path):
    d = Path(path).read_bytes()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    opt = pe + 24
    dd = opt + (96 if struct.unpack_from("<H", d, opt)[0] == 0x10B else 112)
    exp_rva = struct.unpack_from("<I", d, dd)[0]
    secs = [struct.unpack_from("<IIII", d, pe + 24 + struct.unpack_from("<H", d, pe + 20)[0] + 40 * i + 8) for i in range(nsec)]
    off = lambda rva: next(rva - va + raw for vs, va, rs, raw in secs if va <= rva < va + max(vs, rs))
    n, names = struct.unpack_from("<I", d, off(exp_rva) + 24)[0], struct.unpack_from("<I", d, off(exp_rva) + 32)[0]
    out = set()
    for i in range(n):
        p = off(struct.unpack_from("<I", d, off(names) + 4 * i)[0])
        out.add(d[p:d.index(b"\0", p)].decode())
    return out

def stream():
    line = urllib.request.urlopen("http://localhost:54235/api/stream", timeout=3).readline()
    return json.loads(line.decode()[6:])

def color_at(msg, name):
    i = gk104.KEY_INDEX[name] * 6
    return tuple(int(msg["frame"][i + k:i + k + 2], 16) for k in (0, 2, 4))

class GUID(ctypes.Structure):
    _fields_ = [("b", ctypes.c_ubyte * 16)]

for dll in ("RzChromaSDK64.dll", "RzChromaSDK.dll"):
    missing = EXPORTS - pe_exports(HERE / dll)
    assert not missing, f"{dll} missing exports {missing}"
print("exports ok (64 and 32 bit)")

sdk = ctypes.CDLL(str(HERE / "RzChromaSDK64.dll"))
assert sdk.Init() == 0
grid = ((ctypes.c_uint32 * 22) * 6)()
for r, c in ((2, 3), (3, 2), (3, 3), (3, 4)):  # W A S D in the Razer grid
    grid[r][c] = 0x0000FF                      # red (COLORREF is 0x00BBGGRR)
eid = GUID()
assert sdk.CreateKeyboardEffect(2, ctypes.byref(grid), ctypes.byref(eid)) == 0  # CHROMA_CUSTOM
assert sdk.SetEffect(eid) == 0
time.sleep(1.5)
m = stream()
print("chroma app:", m["chroma"], "| W =", color_at(m, "w"), "| Q =", color_at(m, "q"))
assert m["chroma"] == "python.exe" and color_at(m, "w")[0] > 200 and color_at(m, "q") == (0, 0, 0)

static = ctypes.c_uint32(0x00FF00)  # green, applied immediately (no id)
assert sdk.CreateKeyboardEffect(4, ctypes.byref(static), None) == 0
time.sleep(1.0)
assert color_at(stream(), "q")[1] > 200, "static green reached the keyboard"
print("static green ok")

info = (ctypes.c_int * 2)()
assert sdk.QueryDevice(GUID(), ctypes.byref(info)) == 0 and list(info) == [1, 1]
assert sdk.CreateMouseEffect(4, ctypes.byref(static), None) == 0
assert sdk.CreateKeyboardEffect(6, None, None) == 50, "deprecated wave -> not supported"
assert sdk.DeleteEffect(eid) == 0 and sdk.SetEffect(eid) == 1168
for _ in range(3):  # session must survive on heartbeats alone
    time.sleep(1)
assert stream()["chroma"] == "python.exe", "heartbeat keeps the session"
assert sdk.UnInit() == 0
time.sleep(0.5)
assert stream()["chroma"] is None, "UnInit closes the session"
print("DLL TEST OK")
