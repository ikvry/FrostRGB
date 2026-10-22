"""System audio capture (WASAPI loopback via ctypes) + music analysis (numpy).

Captures whatever the default playback device is playing; no extra packages.

  python audio.py            # prints live level / bass / beat for 15 s
  python audio.py --selftest # analysis on synthetic signals, no audio device needed
"""
import ctypes
import threading
import time
import uuid
from ctypes import wintypes

import numpy as np

# ------------------------------------------------------------------ WASAPI loopback (COM via ctypes)

def _guid(s):
    return ctypes.create_string_buffer(uuid.UUID(s).bytes_le, 16)

CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioClient = _guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = _guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
KSDATAFORMAT_FLOAT = uuid.UUID("00000003-0000-0010-8000-00AA00389B71").bytes_le
LOOPBACK, CLSCTX_ALL, BUFFER_SILENT = 0x00020000, 0x17, 0x2

_ole = ctypes.WinDLL("ole32")
_ole.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
_ole.CoCreateInstance.restype = ctypes.HRESULT

def _vcall(obj, idx, argtypes, *args):
    """Call COM method #idx of obj's vtable; ctypes.HRESULT raises OSError on failure."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[idx])
    return fn(obj, *args)

def _release(obj):
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)

class Loopback:
    """Reads float32 mono samples from the default render device. Use from a single thread."""

    def __init__(self):
        _ole.CoInitializeEx(None, 0)  # multithreaded apartment for this thread
        P = ctypes.c_void_p
        self.enum, self.dev, self.client, self.capture = P(), P(), P(), P()
        _ole.CoCreateInstance(CLSID_MMDeviceEnumerator, None, CLSCTX_ALL, IID_IMMDeviceEnumerator, ctypes.byref(self.enum))
        _vcall(self.enum, 4, [ctypes.c_int, ctypes.c_int, P], 0, 0, ctypes.byref(self.dev))  # eRender, eConsole
        _vcall(self.dev, 3, [P, wintypes.DWORD, P, P], IID_IAudioClient, CLSCTX_ALL, None, ctypes.byref(self.client))
        fmt = P()
        _vcall(self.client, 8, [P], ctypes.byref(fmt))  # GetMixFormat
        raw = ctypes.string_at(fmt, 40)
        tag, self.channels, self.rate = int.from_bytes(raw[0:2], "little"), raw[2] | raw[3] << 8, int.from_bytes(raw[4:8], "little")
        self.bits = int.from_bytes(raw[14:16], "little")
        self.is_float = tag == 3 or (tag == 0xFFFE and raw[24:40] == KSDATAFORMAT_FLOAT)
        if not (self.is_float and self.bits == 32) and self.bits != 16:
            raise OSError(f"unsupported mix format tag={tag} bits={self.bits}")
        # 100 ms shared-mode buffer, loopback of the render stream
        _vcall(self.client, 3, [ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong, P, P],
               0, LOOPBACK, 1_000_000, 0, fmt, None)
        _ole.CoTaskMemFree(fmt)
        _vcall(self.client, 14, [P, P], IID_IAudioCaptureClient, ctypes.byref(self.capture))
        _vcall(self.client, 10, [])  # Start

    def read(self):
        """All samples available now, mixed to mono float32 (may be empty)."""
        out = []
        size = ctypes.c_uint32()
        while True:
            _vcall(self.capture, 5, [ctypes.c_void_p], ctypes.byref(size))  # GetNextPacketSize
            if not size.value:
                break
            data, frames, flags = ctypes.c_void_p(), ctypes.c_uint32(), wintypes.DWORD()
            _vcall(self.capture, 3, [ctypes.c_void_p] * 5, ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None)
            n = frames.value * self.channels
            if flags.value & BUFFER_SILENT or not data:
                block = np.zeros(frames.value, np.float32)
            elif self.is_float:
                block = np.ctypeslib.as_array(ctypes.cast(data, ctypes.POINTER(ctypes.c_float)), (n,)).reshape(-1, self.channels).mean(1)
            else:
                block = np.ctypeslib.as_array(ctypes.cast(data, ctypes.POINTER(ctypes.c_int16)), (n,)).reshape(-1, self.channels).mean(1) / 32768
            out.append(block.astype(np.float32))  # copy before releasing the buffer
            _vcall(self.capture, 4, [ctypes.c_uint32], frames.value)  # ReleaseBuffer
        return np.concatenate(out) if out else np.zeros(0, np.float32)

    def close(self):
        try:
            _vcall(self.client, 11, [])  # Stop
        except OSError:
            pass
        for o in (self.capture, self.client, self.dev, self.enum):
            _release(o)

# ------------------------------------------------------------------ analysis

class Analyzer:
    """Turns raw samples into bands / level / bass / mid / treble / beat, all 0..1."""
    BANDS = 22  # one per keyboard grid column
    FFT = 2048

    def __init__(self, rate=48000):
        self.rate = rate
        self.buf = np.zeros(self.FFT, np.float32)
        self.window = np.hanning(self.FFT).astype(np.float32)
        freqs = np.fft.rfftfreq(self.FFT, 1 / rate)
        edges = np.geomspace(40, 16000, self.BANDS + 1)
        self.band_bins = [np.where((freqs >= lo) & (freqs < hi))[0] for lo, hi in zip(edges[:-1], edges[1:])]
        self.band_bins = [b if len(b) else np.array([np.searchsorted(freqs, lo)]) for b, lo in zip(self.band_bins, edges)]
        self.bass_bins = np.where((freqs >= 40) & (freqs < 160))[0]
        self.mid_bins = np.where((freqs >= 400) & (freqs < 2500))[0]
        self.treble_bins = np.where((freqs >= 4000) & (freqs < 14000))[0]
        self.sensitivity = 1.0
        self.peak = 1e-3            # auto gain reference (bands)
        self.rms_peak = 0.01        # auto gain reference (volume)
        self.bass_avg = 0.0
        self.last_beat = 0.0
        self.lock = threading.Lock()
        self.state = {"bands": [0.0] * self.BANDS, "level": 0.0, "bass": 0.0, "mid": 0.0, "treble": 0.0,
                      "beat": 0, "beat_time": 0.0, "active": False}

    def push(self, samples):
        if len(samples) >= self.FFT:
            self.buf[:] = samples[-self.FFT:]
        elif len(samples):
            self.buf = np.roll(self.buf, -len(samples))
            self.buf[-len(samples):] = samples

    def update(self, now):
        mag = np.abs(np.fft.rfft(self.buf * self.window))
        rms = float(np.sqrt(np.mean(self.buf ** 2)))
        bands = np.array([mag[b].mean() for b in self.band_bins])
        bands *= np.linspace(1, 6, self.BANDS)  # tilt: music has far less energy up high
        # auto gain: follow the loudest recent band, decay slowly so quiet songs still fill the keyboard
        self.peak = max(self.peak * 0.995, float(bands.max()), 1e-3)
        norm = np.clip(bands / self.peak * self.sensitivity, 0, 1) ** 0.8
        bass = float(mag[self.bass_bins].mean())
        mid = float(mag[self.mid_bins].mean())
        treble = float(mag[self.treble_bins].mean()) * 4
        ref = self.peak
        s = self.state
        with self.lock:
            # fast attack, slow release
            s["bands"] = [max(n, o * 0.85) for n, o in zip(norm.tolist(), s["bands"])]
            for k, v in (("bass", bass), ("mid", mid), ("treble", treble)):
                v = min(1.0, v / ref * self.sensitivity)
                s[k] = max(v, s[k] * 0.85)
            # auto gain for volume too, so quiet songs still swing the meter; silence stays dark
            s["active"] = rms > 1e-4
            self.rms_peak = max(self.rms_peak * 0.998, rms, 0.01)
            lvl = min(1.0, rms / self.rms_peak * 0.8 * self.sensitivity) if s["active"] else 0.0
            s["level"] = max(lvl, s["level"] * 0.9)
            # beat: bass well above its running average, at most ~6 per second
            if bass > self.bass_avg * 1.5 and bass > ref * 0.15 and now - self.last_beat > 0.16:
                self.last_beat = now
                s["beat"] += 1
                s["beat_time"] = now
            self.bass_avg = self.bass_avg * 0.95 + bass * 0.05

    def snapshot(self):
        with self.lock:
            return {k: (list(v) if isinstance(v, list) else v) for k, v in self.state.items()}

class AudioEngine:
    """Background thread: loopback capture -> Analyzer at ~100 Hz. Retries if the device goes away."""

    def __init__(self):
        self.analyzer = Analyzer()
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        while not self.stop.is_set():
            lb = None
            try:
                lb = Loopback()
                if lb.rate != self.analyzer.rate:
                    sens = self.analyzer.sensitivity
                    self.analyzer = Analyzer(lb.rate)
                    self.analyzer.sensitivity = sens
                self.error = None
                while not self.stop.is_set():
                    self.analyzer.push(lb.read())
                    self.analyzer.update(time.monotonic())
                    time.sleep(0.01)
            except OSError as e:  # e.g. output device changed or unplugged: reopen the new default
                self.error = str(e)
                time.sleep(1)
            finally:
                if lb:
                    lb.close()

    def snapshot(self):
        return self.analyzer.snapshot()  # analyzer may be replaced when the device rate changes

    def set_sensitivity(self, v):
        self.analyzer.sensitivity = v

    def close(self):
        self.stop.set()

def selftest():
    a = Analyzer(48000)
    t = np.arange(48000) / 48000
    a.push(np.zeros(48000, np.float32)); a.update(0.0)
    assert a.snapshot()["level"] == 0 and not a.snapshot()["active"]
    a.push((0.5 * np.sin(2 * np.pi * 60 * t)).astype(np.float32)); a.update(1.0)
    s = a.snapshot()
    assert s["bands"].index(max(s["bands"])) <= 2 and s["bass"] > s["treble"], "60 Hz is bass"
    assert s["beat"] == 1, "first loud bass hit is a beat"
    q = Analyzer(48000)
    for k in range(300):  # quiet song (1% amplitude) for 3 s: auto gain must bring the meter up
        q.push((0.01 * np.sin(2 * np.pi * 220 * t[:480] + k)).astype(np.float32)); q.update(k / 100)
    assert q.snapshot()["level"] > 0.4, "quiet music still reaches the meter"
    b = Analyzer(48000)
    b.push((0.5 * np.sin(2 * np.pi * 8000 * t)).astype(np.float32)); b.update(0.0)
    s = b.snapshot()
    assert s["bands"].index(max(s["bands"])) >= 17 and s["treble"] > s["bass"], "8 kHz is treble"
    print("selftest ok")

if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
    else:
        eng = AudioEngine().start()
        for _ in range(30):
            time.sleep(0.5)
            s = eng.analyzer.snapshot()
            bars = "".join(" .:-=+*#%@"[min(9, int(b * 10))] for b in s["bands"])
            print(f"[{bars}] lvl={s['level']:.2f} bass={s['bass']:.2f} beat={s['beat']} err={eng.error}", flush=True)
        eng.close()
