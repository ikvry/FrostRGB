"""System audio capture (WASAPI loopback via ctypes) + music analysis (numpy).

Captures what the PC is playing (the default output, or whichever output actually has sound);
no extra packages.

  python audio.py            # prints live level / bass / beat for 15 s
  python audio.py --selftest # analysis on synthetic signals, no audio device needed
"""
import ctypes
import threading
import time
import uuid
import winreg
from ctypes import wintypes

import numpy as np

# ------------------------------------------------------------------ WASAPI loopback (COM via ctypes)

def _guid(s):
    return ctypes.create_string_buffer(uuid.UUID(s).bytes_le, 16)

CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioClient = _guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = _guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
IID_IAudioMeterInformation = _guid("C02216F6-8C67-4B5B-9D00-D008E73E0064")
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

P = ctypes.c_void_p

def _dev_id(dev):
    s = ctypes.c_wchar_p()
    _vcall(dev, 5, [P], ctypes.byref(s))  # IMMDevice::GetId
    try:
        return s.value
    finally:
        _ole.CoTaskMemFree(ctypes.cast(s, P))

def device_name(dev_id):
    """'Speakers (Focusrite USB Audio)' from the endpoint's registry properties."""
    try:
        key = dev_id.split("}.")[-1]
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\{key}\Properties") as k:
            name = winreg.QueryValueEx(k, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")[0]
            try:
                return f"{name} ({winreg.QueryValueEx(k, '{b3f8fa53-0004-438e-9003-51a46e139bfc},6')[0]})"
            except OSError:
                return name
    except OSError:
        return "audio output"

def default_id(enum):
    dev = P()
    _vcall(enum, 4, [ctypes.c_int, ctypes.c_int, P], 0, 0, ctypes.byref(dev))  # eRender, eConsole
    try:
        return _dev_id(dev)
    finally:
        _release(dev)

def render_peaks(enum):
    """{device id: current peak 0..1} for every active output device (Windows' own volume meters)."""
    coll, n, out = P(), ctypes.c_uint(), {}
    _vcall(enum, 3, [ctypes.c_int, wintypes.DWORD, P], 0, 1, ctypes.byref(coll))  # eRender, ACTIVE
    try:
        _vcall(coll, 3, [P], ctypes.byref(n))
        for k in range(n.value):
            dev, meter, v = P(), P(), ctypes.c_float()
            try:
                _vcall(coll, 4, [ctypes.c_uint, P], k, ctypes.byref(dev))
                _vcall(dev, 3, [P, wintypes.DWORD, P, P], IID_IAudioMeterInformation, CLSCTX_ALL, None, ctypes.byref(meter))
                _vcall(meter, 3, [P], ctypes.byref(v))  # GetPeakValue
                out[_dev_id(dev)] = v.value
            except OSError:
                pass
            finally:
                _release(meter)
                _release(dev)
    finally:
        _release(coll)
    return out

class Loopback:
    """Reads float32 mono samples from one render device (default if device_id is None).
    Use from a single thread."""

    def __init__(self, device_id=None):
        _ole.CoInitializeEx(None, 0)  # multithreaded apartment for this thread
        self.enum, self.dev, self.client, self.capture = P(), P(), P(), P()
        _ole.CoCreateInstance(CLSID_MMDeviceEnumerator, None, CLSCTX_ALL, IID_IMMDeviceEnumerator, ctypes.byref(self.enum))
        if device_id:
            _vcall(self.enum, 5, [ctypes.c_wchar_p, P], device_id, ctypes.byref(self.dev))  # GetDevice
        else:
            _vcall(self.enum, 4, [ctypes.c_int, ctypes.c_int, P], 0, 0, ctypes.byref(self.dev))  # eRender, eConsole
        self.id = _dev_id(self.dev)
        self.name = device_name(self.id)
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
    WAVE_SPAN, WAVE_POINTS = 1024, 128  # oscilloscope: ~21 ms at 48 kHz, 8 samples averaged per point

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
        self.wave_peak = 1e-3       # auto gain reference (waveform)
        self.lock = threading.Lock()
        self.state = {"bands": [0.0] * self.BANDS, "level": 0.0, "bass": 0.0, "mid": 0.0, "treble": 0.0,
                      "beat": 0, "beat_time": 0.0, "active": False, "wave": [0.0] * self.WAVE_POINTS}

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
        wave = self._scope()
        s = self.state
        with self.lock:
            s["wave"] = wave
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

    def _scope(self):
        """Waveform like a triggered oscilloscope: starts at a rising zero crossing so the trace stands
        still instead of jittering, averaged down to WAVE_POINTS (-1..1, auto gain)."""
        buf, span = self.buf, self.WAVE_SPAN
        last = len(buf) - span  # latest start that still leaves a full window
        seg = buf[last - span:last + 1]
        rising = np.where((seg[:-1] < 0) & (seg[1:] >= 0))[0]
        start = last - span + 1 + int(rising[-1]) if len(rising) else last
        pts = buf[start:start + span].reshape(self.WAVE_POINTS, -1).mean(axis=1)  # box low-pass
        self.wave_peak = max(self.wave_peak * 0.995, float(np.abs(pts).max()), 1e-3)
        return np.clip(pts / self.wave_peak * self.sensitivity, -1, 1).round(3).tolist()

    def snapshot(self):
        with self.lock:
            return {k: (list(v) if isinstance(v, list) else v) for k, v in self.state.items()}

class AudioEngine:
    """Background thread: loopback capture -> Analyzer at ~100 Hz. Retries if the device goes away."""

    def __init__(self):
        self.analyzer = Analyzer()
        self.error = None
        self.device = None   # name of the output being captured
        self.want = None     # device id to capture; None = Windows default
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        while not self.stop.is_set():
            lb = None
            try:
                lb = Loopback(self.want)
                if lb.rate != self.analyzer.rate:
                    sens = self.analyzer.sensitivity
                    self.analyzer = Analyzer(lb.rate)
                    self.analyzer.sensitivity = sens
                self.error, self.device = None, lb.name
                checked, silent_since = time.monotonic(), None
                while not self.stop.is_set():
                    x = lb.read()
                    now = time.monotonic()
                    self.analyzer.push(x)
                    self.analyzer.update(now)
                    silent_since = None if len(x) and float(np.abs(x).max()) > 1e-4 else (silent_since or now)
                    if now - checked > 1:
                        checked = now
                        if self._should_switch(lb, silent_since, now):
                            break  # reopen on the new device right away
                    time.sleep(0.01)
            except OSError as e:  # e.g. output device changed or unplugged: reopen the new default
                self.error = str(e)
                time.sleep(1)
            finally:
                if lb:
                    lb.close()

    def _should_switch(self, lb, silent_since, now):
        """Follow the sound: the default output changed, or we hear nothing while another output
        plays (a player or game sent to the Focusrite/headset while Windows' default is elsewhere)."""
        default = default_id(lb.enum)
        if self.want is None and default != lb.id:
            return True
        if silent_since and now - silent_since > 2:
            peaks = render_peaks(lb.enum)
            if peaks.get(lb.id, 0) > 0.003:
                return True  # Windows' meter hears our device but the stream is dead (device reset): reopen
            best = max(peaks, key=peaks.get, default=None)
            if best and best != lb.id and peaks[best] > 0.003:
                self.want = None if best == default else best
                return True
        return False

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
    for phase in (0.3, 2.1):  # any phase: the trace starts at a rising zero crossing and is full scale
        w = Analyzer(48000)
        w.push((0.5 * np.sin(2 * np.pi * 440 * t[:4096] + phase)).astype(np.float32)); w.update(0.0)
        wave = w.snapshot()["wave"]
        assert len(wave) == Analyzer.WAVE_POINTS and abs(wave[0]) < 0.35 and wave[2] > wave[0], wave[:4]
        assert max(wave) > 0.9 and min(wave) < -0.9, "auto gain fills the screen"
    q = Analyzer(48000)
    for k in range(300):  # quiet song (1% amplitude) for 3 s: auto gain must bring the meter up
        q.push((0.01 * np.sin(2 * np.pi * 220 * t[:480] + k)).astype(np.float32)); q.update(k / 100)
    assert q.snapshot()["level"] > 0.4, "quiet music still reaches the meter"
    b = Analyzer(48000)
    b.push((0.5 * np.sin(2 * np.pi * 8000 * t)).astype(np.float32)); b.update(0.0)
    s = b.snapshot()
    assert s["bands"].index(max(s["bands"])) >= 17 and s["treble"] > s["bass"], "8 kHz is treble"
    global render_peaks, default_id  # device switching, with fake Windows meters
    real = render_peaks, default_id
    class Dead:  # capture stream that delivers nothing
        id, enum = "X", None
    try:
        default_id = lambda enum: "X"
        render_peaks = lambda enum: {"X": 0.2, "Y": 0.0}
        e = AudioEngine()
        assert e._should_switch(Dead(), 100.0, 103.0), "dead stream on a playing device is reopened"
        assert not e._should_switch(Dead(), 102.0, 103.0), "short silence is fine"
        render_peaks = lambda enum: {"X": 0.0, "Y": 0.3}
        assert e._should_switch(Dead(), 100.0, 103.0) and e.want == "Y", "follows the output that has sound"
    finally:
        render_peaks, default_id = real
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
