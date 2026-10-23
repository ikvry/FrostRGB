"""Primary-monitor capture shrunk to a tiny image, for the ambient "Screen" effect.

GDI samples the desktop at 4x the target size and numpy averages it down (e.g. 88x24). A capture
costs ~25 ms, so it runs in its own thread (Capture). Stdlib ctypes + numpy only.
shortcut: GDI cannot see exclusive-fullscreen DirectX games (they come out black); switch to DXGI
Desktop Duplication if that matters (borderless/windowed games are captured fine).
"""
import ctypes
import threading
import time
from ctypes import wintypes

import numpy as np

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels, not the DPI-scaled virtual size
except (AttributeError, OSError):
    user32.SetProcessDPIAware()

COLORONCOLOR, SRCCOPY, DIB_RGB_COLORS = 3, 0x00CC0020, 0
DESKTOPVERTRES, DESKTOPHORZRES = 117, 118
for f, args, res in (
        (user32.GetDC, [wintypes.HWND], wintypes.HDC),
        (user32.ReleaseDC, [wintypes.HWND, wintypes.HDC], ctypes.c_int),
        (gdi32.CreateCompatibleDC, [wintypes.HDC], wintypes.HDC),
        (gdi32.CreateCompatibleBitmap, [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
        (gdi32.SelectObject, [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
        (gdi32.DeleteObject, [wintypes.HGDIOBJ], wintypes.BOOL),
        (gdi32.DeleteDC, [wintypes.HDC], wintypes.BOOL),
        (gdi32.SetStretchBltMode, [wintypes.HDC, ctypes.c_int], ctypes.c_int),
        (gdi32.SetBrushOrgEx, [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p], wintypes.BOOL),
        (gdi32.StretchBlt, [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.DWORD], wintypes.BOOL),
        (gdi32.GetDIBits, [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT], ctypes.c_int),
        (gdi32.GetDeviceCaps, [wintypes.HDC, ctypes.c_int], ctypes.c_int)):
    f.argtypes, f.restype = args, res

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

def grab(w=88, h=24, oversample=4):
    """Primary screen averaged down to w x h. Returns an (h, w, 3) uint8 RGB array.
    Reading the screen costs ~25 ms whatever the size (compositor readback), so GDI only samples
    pixels (fast COLORONCOLOR) at 4x the size and numpy does the averaging."""
    W, H = w, h
    w, h = w * oversample, h * oversample
    sdc = user32.GetDC(None)
    if not sdc:
        raise OSError("no screen device context")
    mdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        sw, sh = gdi32.GetDeviceCaps(sdc, DESKTOPHORZRES), gdi32.GetDeviceCaps(sdc, DESKTOPVERTRES)
        gdi32.SetStretchBltMode(mdc, COLORONCOLOR)
        gdi32.SetBrushOrgEx(mdc, 0, 0, None)
        if not gdi32.StretchBlt(mdc, 0, 0, w, h, sdc, 0, 0, sw, sh, SRCCOPY):
            raise OSError("screen capture failed (locked screen or secure desktop?)")
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down BGRA
        buf = (ctypes.c_ubyte * (w * h * 4))()
        if gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS) != h:
            raise OSError("could not read the captured image")
        img = np.frombuffer(buf, np.uint8).reshape(H, oversample, W, oversample, 4)[..., 2::-1]
        return img.mean(axis=(1, 3)).astype(np.uint8)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, sdc)

class Capture:
    """Background grabber at ~30 fps. Runs only while someone reads it (stops 2 s after the last read),
    so the effect never blocks the keyboard loop and costs nothing when it is not used."""

    def __init__(self, w=88, h=24):
        self.w, self.h = w, h
        self.img, self.error, self.asked = None, None, 0.0
        self.black_since = None  # capture fully black: DRM video or exclusive fullscreen (Windows hides both)
        self.lock = threading.Lock()
        self.thread = None

    def latest(self):
        self.asked = time.monotonic()
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
        return self.img

    def _run(self):
        while time.monotonic() - self.asked < 2:
            t0 = time.monotonic()
            try:
                self.img, self.error = grab(self.w, self.h), None
                if self.img.max() <= 3:
                    self.black_since = self.black_since or t0
                else:
                    self.black_since = None
            except OSError as e:  # locked screen, UAC prompt: keep the last image
                self.error = str(e)
            time.sleep(max(0.0, 1 / 30 - (time.monotonic() - t0)))

    def status(self):
        """None when fine, else a short message for the UI. Only while the effect is in use."""
        if time.monotonic() - self.asked > 2:
            return None
        if self.error:
            return self.error
        if self.black_since and time.monotonic() - self.black_since > 1.5:
            return ("Windows is returning a black screen: protected video (Netflix, Prime, Disney+...) "
                    "or a game in exclusive fullscreen blocks capture")
        return None

CAPTURE = Capture()

if __name__ == "__main__":
    import time
    t0 = time.perf_counter()
    for _ in range(20):
        img = grab()
    print("grab ok:", img.shape, f"{(time.perf_counter() - t0) / 20 * 1000:.1f} ms/frame",
          "mean color", img.reshape(-1, 3).mean(0).round().astype(int).tolist())
