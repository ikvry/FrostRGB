"""FrøstRGB: keyboard + Chroma SDK emulation + effects + music sync + RGB editor + UI.

  python app.py                 # opens the UI in its own window (Edge app mode) + tray icon
  python app.py --background    # tray only (used when starting with Windows)
  python app.py --no-window     # server only, UI at http://localhost:54235/
  python app.py --selftest      # no keyboard or audio device needed

Close GK6+ first: both cannot drive the keyboard at the same time.
"""
import argparse
import ctypes
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import winreg
from pathlib import Path

import animations
import audio
import chroma_server as cs
import editor
import effects
import gk104
import music
import openrgb
import palette_sources
import palettes
import tray

HERE = Path(__file__).parent  # inside the .exe this is the unpacked bundle
FROZEN = getattr(sys, "frozen", False)
APPDATA = Path(os.environ.get("APPDATA", HERE))
DATA = APPDATA / "FrostRGB" if FROZEN else HERE
OLD_DATA = APPDATA / "GK104 Studio"  # name before the rename
SETTINGS_FILE = DATA / "settings.json"
UI_FILE = HERE / "ui.html"
ICON = HERE / "icon.ico"
URL = f"http://localhost:{cs.PORT}/"
ORIGINS = {f"http://localhost:{cs.PORT}", f"http://127.0.0.1:{cs.PORT}"}
FADE = 0.35  # seconds of crossfade when switching mode
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "FrøstRGB"
OLD_RUN_NAME = "GK104 Studio"
LIB = animations.Library(DATA / "animations")

DEFAULTS = {"source": "effect", "effect": "wave", "music": "equalizer", "fx": {}, "mfx": {},
            "brightness": 100, "sensitivity": 1.0, "chroma_override": True,
            "editor": editor.default_editor(), "profiles": {}, "palettes": {},
            "gain_r": 100, "gain_g": 100, "gain_b": 100, "saturation": 100,
            "idle_minutes": 0, "idle_action": "off", "close_to_tray": True,
            "animation": "", "anim_speed": 1.0,
            "rgb_sync": False, "rgb_mode": "spread", "rgb_off": [], "rgb_port": openrgb.PORT, "rgb_zones": {}, "rgb_gamma": 2.2,
            "rgb_dev": {}, "kb_leds": True}

def _check(ok):
    def run(v):
        if not ok(v):
            raise ValueError
        return v
    return run

def _num(lo, hi):
    return _check(lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi)

def _params_map(registry):
    """{name: params} for effects or music modes; each params dict is fully validated."""
    def run(v):
        if not isinstance(v, dict) or not all(k in registry for k in v):
            raise ValueError
        return {k: effects.clean_params(p, k) for k, p in v.items()}
    return run

# Each validator returns the cleaned value or raises ValueError.
VALID = {
    "source": _check(lambda v: v in ("effect", "music", "editor", "animation", "off")),
    "animation": _check(lambda v: v == "" or (isinstance(v, str) and animations.NAME.match(v))),
    "anim_speed": _num(0.1, 5),
    "rgb_sync": _check(lambda v: isinstance(v, bool)),
    "rgb_mode": _check(lambda v: v in openrgb.MODES),
    "rgb_dev": lambda v: clean_rgb_dev(v),
    "kb_leds": _check(lambda v: isinstance(v, bool)),
    "rgb_off": _check(lambda v: isinstance(v, list) and len(v) <= 64 and all(isinstance(x, str) and len(x) <= 200 for x in v)),
    "rgb_gamma": _num(1.0, 3.0),
    "rgb_zones": _check(lambda v: isinstance(v, dict) and len(v) <= 64 and all(
        isinstance(k, str) and len(k) <= 300 and isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 1000 for k, n in v.items())),
    "rgb_port": _check(lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535),
    "effect": _check(lambda v: v in effects.EFFECTS),
    "music": _check(lambda v: v in music.MODES),
    "fx": _params_map(effects.EFFECTS),
    "mfx": _params_map(music.MODES),
    "chroma_override": _check(lambda v: isinstance(v, bool)),
    "close_to_tray": _check(lambda v: isinstance(v, bool)),
    "brightness": _num(0, 100),
    "sensitivity": _num(0.2, 8),
    "gain_r": _num(0, 100), "gain_g": _num(0, 100), "gain_b": _num(0, 100),
    "saturation": _num(0, 200),
    "idle_minutes": _num(0, 240),
    "idle_action": _check(lambda v: v in ("off", "dim")),
    "editor": editor.validate_editor,
    "profiles": editor.validate_profiles,
    "palettes": palettes.validate_custom,
}
MERGE = {"fx", "mfx"}  # updates merge per effect instead of replacing the whole map
REBUILD = {"source", "effect", "music", "fx", "mfx", "palettes", "animation", "anim_speed"}  # need a new effect object

def clean_rgb_dev(v):
    """Per PC device lighting: {device name: {mode: keyboard|effect, effect, params}}."""
    if not isinstance(v, dict) or len(v) > 64:
        raise ValueError("bad rgb_dev")
    out = {}
    for name, c in v.items():
        if not (isinstance(name, str) and 0 < len(name) <= 200 and isinstance(c, dict)):
            raise ValueError("bad rgb_dev entry")
        mode, fx = c.get("mode"), c.get("effect", "wave")
        if mode not in ("keyboard", "effect") or (fx not in effects.EFFECTS and fx not in music.MODES):
            raise ValueError("bad rgb_dev mode or effect")
        out[name] = {"mode": mode, "effect": fx, "params": effects.clean_params(c.get("params", {}), fx)}
    return out

def clean(changes):
    out, bad = {}, []
    if "palettes" in changes:  # params below may reference a palette created in this same update
        try:
            palettes.CUSTOM = VALID["palettes"](changes["palettes"])
        except (ValueError, TypeError):
            bad.append("palettes")
    for k, v in changes.items():
        try:
            out[k] = VALID[k](v)
        except (KeyError, ValueError, TypeError, AttributeError):
            if k not in bad:
                bad.append(k)
    if bad:
        raise ValueError(f"invalid settings: {bad}")
    return out

def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}  # first run or corrupt file: defaults
    for k in sorted(saved, key=lambda k: k != "palettes"):  # palettes first: fx may use them
        try:
            if k in MERGE:  # keep the valid effects, drop only broken ones
                for name, p in saved[k].items():
                    try:
                        s[k].update(clean({k: {name: p}})[k])
                    except ValueError:
                        pass
            else:
                s.update(clean({k: saved[k]}))
        except (ValueError, AttributeError):
            pass  # drop one bad entry, keep the rest
    palettes.CUSTOM = s["palettes"]
    return s

# ------------------------------------------------------------------ Windows integration

def startup_command():
    if FROZEN:
        return f'"{sys.executable}" --background'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" "{Path(__file__).resolve()}" --background'

def migrate_old_install():
    """Settings and autostart from the GK104 Studio days move over to FrøstRGB once."""
    if not FROZEN:
        return  # dev runs must not point Windows startup at python
    if OLD_DATA.exists() and not SETTINGS_FILE.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(OLD_DATA / "settings.json", SETTINGS_FILE)
        except OSError:
            pass
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        for name in (OLD_RUN_NAME, APP_NAME):
            try:
                if name == APP_NAME and winreg.QueryValueEx(k, name)[0] == startup_command():
                    continue
                winreg.QueryValueEx(k, name)
                winreg.DeleteValue(k, name)
            except OSError:
                continue  # no entry under this name
            # autostart was on: point it at this exe (also heals a moved or renamed exe)
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command())

def get_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return bool(winreg.QueryValueEx(k, APP_NAME)[0])
    except OSError:
        return False

def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

def idle_seconds():
    """Seconds since the last mouse or keyboard input anywhere in Windows."""
    li = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO))
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000

# ------------------------------------------------------------------ controller

class Controller:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.settings = load_settings()
        self.engine.set_sensitivity(self.settings["sensitivity"])
        self.status = {"connected": False, "fps": 0.0, "error": None, "idle": False}
        self.last_frame = effects.blank()
        self.compositor = editor.Compositor(engine)
        self.compositor.set(self.settings["editor"])
        self.prev, self.switch_t = None, 0.0
        self.idle_level = 1.0
        self.last_on = self.settings["source"] if self.settings["source"] != "off" else "effect"  # for "turn on"
        self.source = self._make()

    def params(self, kind, name):
        saved = self.settings["fx" if kind == "effect" else "mfx"].get(name, {})
        return effects.clean_params(saved, name)

    def _make(self):
        s = self.settings
        if s["source"] == "effect":
            return effects.make(s["effect"], self.params("effect", s["effect"]))
        if s["source"] == "music":
            return music.make(s["music"], self.engine, self.params("music", s["music"]))
        if s["source"] == "editor":
            return self.compositor
        if s["source"] == "animation" and s["animation"]:
            try:
                return LIB.make(s["animation"], {"speed": s["anim_speed"]})
            except (OSError, ValueError):
                return None  # file deleted or broken: dark until another one is picked
        return None

    def update(self, changes):
        changes = dict(changes)
        autostart = changes.pop("autostart", None)
        if autostart is not None:
            if not isinstance(autostart, bool):
                raise ValueError("invalid settings: ['autostart']")
            set_autostart(autostart)
        changes = clean(changes)
        with self.lock:
            if changes.get("source", "off") != "off":
                self.last_on = changes["source"]
            for k, v in changes.items():
                if k in MERGE:
                    self.settings[k].update(v)
                else:
                    self.settings[k] = v
            self.engine.set_sensitivity(self.settings["sensitivity"])
            if "editor" in changes or "palettes" in changes:
                if "palettes" in changes:
                    self.compositor = editor.Compositor(self.engine)  # palette colors are baked into effects
                self.compositor.set(self.settings["editor"])
            if REBUILD & changes.keys():
                self.prev, self.switch_t = self.source, time.monotonic()
                self.source = self._make()
            DATA.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(self.settings), encoding="utf-8")
        return dict(self.settings)

    def on_key(self, index):
        now = time.monotonic()
        for src in {id(x): x for x in (self.source, self.compositor) if x}.values():
            src.on_key(index, now)

    def chroma_title(self):
        with cs.STATE.lock:
            sess = cs.STATE.sessions.get(cs.STATE.owner)
            return sess["title"] if sess and sess["frame"] else None

    @staticmethod
    def _frame_of(src, now):
        return src.frame(now) if src else effects.blank()

    def _post(self, frame, now, dt):
        """Brightness, idle dimming and color calibration on the final frame."""
        s = self.settings
        mins = s["idle_minutes"]
        idle = bool(mins) and idle_seconds() > mins * 60
        self.status["idle"] = idle
        target = (0.0 if s["idle_action"] == "off" else 0.2) if idle else 1.0
        step = dt / 1.5 if idle else dt / 0.25  # slow fade out, quick wake up
        self.idle_level += max(-step, min(step, target - self.idle_level))
        k = s["brightness"] / 100 * self.idle_level
        gains = (s["gain_r"] / 100 * k, s["gain_g"] / 100 * k, s["gain_b"] / 100 * k)
        sat = s["saturation"] / 100
        if gains == (1, 1, 1) and sat == 1:
            return frame
        out = []
        for r, g, b in frame:
            if sat != 1:
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                r, g, b = (lum + (c - lum) * sat for c in (r, g, b))
            out.append(tuple(int(min(255, max(0, c * m))) for c, m in zip((r, g, b), gains)))
        return out

    def render(self, now, dt=1 / 60):
        cs.STATE.expire()
        frame = cs.STATE.current() if self.settings["chroma_override"] else None
        if frame is None:
            frame = self._frame_of(self.source, now)
            t = (now - self.switch_t) / FADE
            if self.prev is not None and t < 1:  # crossfade from the previous mode
                old = self._frame_of(self.prev, now)
                frame = [effects.lerp(o, n, t) for o, n in zip(old, frame)]
            else:
                self.prev = None
        frame = self._post(frame, now, dt)
        self.last_frame = frame
        return frame

def device_loop(ctl, stop):
    """Keeps the keyboard in online mode and streams frames; reconnects after unplug or errors."""
    while not stop.is_set():
        kb = None
        try:
            kb = gk104.Keyboard()
            kb.enter_online()
            kb.on_key = ctl.on_key
            ctl.status.update(connected=True, error=None)
            last_ping, last, frames, t_fps, t_prev = time.monotonic(), None, 0, time.monotonic(), time.monotonic()
            while not stop.is_set():
                now = time.monotonic()
                if now - last_ping > 1.5:  # keyboard leaves online mode without pings
                    kb.ping()
                    last_ping = now
                f = ctl.render(now, now - t_prev)
                t_prev = now
                if not ctl.settings["kb_leds"]:
                    f = effects.blank()  # keyboard dark, PC devices keep the effect
                if f != last:
                    kb.frame(f)
                    last = f
                    frames += 1
                else:
                    time.sleep(0.005)
                if now - t_fps >= 1:
                    ctl.status["fps"] = frames / (now - t_fps)
                    frames, t_fps = 0, now
        except (OSError, TimeoutError, RuntimeError, SystemExit) as e:  # SystemExit: keyboard not found
            ctl.status.update(connected=False, fps=0.0, error=str(e))
            t_prev = end = time.monotonic()
            end += 2
            while not stop.is_set() and time.monotonic() < end:  # no keyboard: PC lights and the UI keep going
                now = time.monotonic()
                ctl.render(now, now - t_prev)
                t_prev = now
                time.sleep(1 / 30)
        finally:
            if kb:
                try:
                    kb.on_key = None
                    if stop.is_set():
                        kb.leave_online(1)
                except (OSError, TimeoutError):
                    pass
                kb.close()

# ------------------------------------------------------------------ HTTP: Chroma REST + UI API

CTL = None

def screen_status():
    """Capture warning for the Screen effect; screen.py is only imported once the effect is used."""
    mod = sys.modules.get("screen")
    return mod.CAPTURE.status() if mod else None
SYNC = None  # OpenRGB mirror, started in main()

class Server(cs.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):  # a closed window/tab is normal
            super().handle_error(request, client_address)

class Handler(cs.Handler):
    def _same_origin(self):
        origin = self.headers.get("Origin")
        return origin is None or origin in ORIGINS  # blocks other web pages from driving the UI API

    def do_GET(self):
        if self.path in ("/", "/index.html", "/favicon.ico"):
            ico = self.path == "/favicon.ico"
            data = (ICON if ico else UI_FILE).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/x-icon" if ico else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if self.path == "/api/state":
            return self._send({"settings": CTL.settings, "effects": effects.META, "music": music.META,
                               "palettes": palettes.BUILTIN, "palette_names": palettes.NAMES,
                               "directions": effects.DIRECTIONS, "param_defaults": effects.PARAM_DEFAULTS,
                               "keymap": gk104.KEYMAP, "autostart": get_autostart(), "exe": FROZEN,
                               "animations": LIB.list(), "app": APP_NAME})
        if self.path == "/api/stream":
            return self._stream()
        if self.path.startswith("/api/palettes/discover?"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                res = palette_sources.discover(q.get("source", [""])[0], q.get("q", [""])[0], q.get("page", ["0"])[0])
                return self._send({"results": res})
            except ValueError as e:
                return self._send({"error": str(e)}, 400)
            except OSError as e:  # offline or the site changed
                return self._send({"error": f"could not reach the palette site ({e})"}, 502)
        super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/") and not self._same_origin():
            return self._send({"error": "forbidden origin"}, 403)
        if self.path.startswith("/api/animations/import?name="):
            try:
                n = int(self.headers.get("Content-Length", 0))
                if not 0 < n <= animations.MAX_BYTES:
                    raise ValueError("file empty or too large")
                name = urllib.parse.unquote(self.path.split("=", 1)[1])
                return self._send({"name": LIB.add(name, self.rfile.read(n)), "animations": LIB.list()})
            except (ValueError, OSError, struct.error) as e:
                return self._send({"error": str(e)}, 400)
        if self.path == "/api/animations/delete":
            try:
                LIB.remove(self._body().get("name"))
                return self._send({"animations": LIB.list()})
            except (ValueError, OSError, AttributeError) as e:
                return self._send({"error": str(e)}, 400)
        if self.path == "/api/rgb/rescan":
            if SYNC:
                SYNC.rescan()
            return self._send({"ok": True})
        if self.path == "/api/settings":
            try:
                return self._send(CTL.update(self._body()))
            except (ValueError, OSError) as e:
                return self._send({"error": str(e)}, 400)
        super().do_POST()

    def _stream(self):
        """Server-sent events at ~30 Hz: current keyboard frame + audio analysis + status."""
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                a = CTL.engine.snapshot()
                msg = {"frame": "".join(f"{r:02x}{g:02x}{b:02x}" for r, g, b in CTL.last_frame),
                       "bands": [round(v, 3) for v in a["bands"]], "level": round(a["level"], 3),
                       "beat": a["beat"], "audio_error": CTL.engine.error, "chroma": CTL.chroma_title(),
                       "audio_device": getattr(CTL.engine, "device", None),
                       "rgb": SYNC.status if SYNC else None,
                       "screen": screen_status(),
                       **CTL.status}
                self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                self.wfile.flush()
                time.sleep(1 / 30)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

# ------------------------------------------------------------------ window

def open_window():
    """Edge in app mode = a borderless app window. Returns the process to wait on, or None."""
    edge = shutil.which("msedge") or next((p for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if Path(p).exists()), None)
    if not edge:
        webbrowser.open(URL)
        return None
    profile = DATA / "window"  # own profile: the process lives exactly as long as the window
    return subprocess.Popen([edge, f"--app={URL}", f"--user-data-dir={profile}", "--window-size=1400,900",
                             "--no-first-run", "--no-default-browser-check", "--lang=en-US",
                             "--disable-features=Translate,TranslateUI", "--disable-translate"])

def already_running():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", cs.PORT)) == 0

def selftest():
    global CTL, SETTINGS_FILE
    class FakeEngine:
        error = None
        def snapshot(self): return audio.Analyzer().snapshot()
        def set_sensitivity(self, v): pass
    real = SETTINGS_FILE
    SETTINGS_FILE = HERE / "settings.selftest.json"
    w = gk104.KEY_INDEX["w"]
    try:
        CTL = Controller(FakeEngine())
        for src in ("effect", "music", "editor", "off"):
            CTL.update({"source": src})
            assert len(CTL.render(time.monotonic())) == gk104.KEY_SLOTS
        for name in effects.EFFECTS:
            CTL.update({"source": "effect", "effect": name})
            CTL.render(time.monotonic() + 1)
        for name in music.MODES:
            CTL.update({"source": "music", "music": name})
            CTL.render(time.monotonic() + 1)
        CTL.update({"source": "effect", "effect": "static", "fx": {"static": {"color": "FF0000"}}})
        assert CTL.render(time.monotonic() + 1)[w] == (255, 0, 0)
        CTL.update({"fx": {"static": {"color": "0000FF"}}})
        mid = CTL.render(CTL.switch_t + FADE / 2)[w]
        assert 0 < mid[0] < 255 and 0 < mid[2] < 255, f"crossfade midway {mid}"
        assert CTL.settings["fx"]["static"]["color"] == "0000FF", "per-effect params saved"
        CTL.update({"palettes": {"Minha": ["123456", "654321"]}, "fx": {"wave": {"palette": "Minha"}}})
        assert CTL.settings["fx"]["wave"]["palette"] == "Minha", "custom palette usable in the same update"
        CTL.update({"brightness": 50})
        assert max(CTL.render(time.monotonic() + 5)[w]) <= 128, "brightness scales output"
        CTL.update({"brightness": 100, "gain_r": 50, "fx": {"static": {"color": "FFFFFF"}}})
        assert CTL.render(time.monotonic() + 5)[w] == (127, 255, 255), "red gain calibration"
        CTL.update({"gain_r": 100, "saturation": 0, "fx": {"static": {"color": "FF0000"}}})
        r, g, b = CTL.render(time.monotonic() + 5)[w]
        assert r == g == b, "saturation 0 = gray"
        CTL.update({"saturation": 100})
        global LIB
        real_lib, LIB = LIB, animations.Library(HERE / "animations.selftest")
        try:
            red = effects.blank(); red[w] = (255, 0, 0)
            name = LIB.add("Test", animations.build([red]))
            CTL.update({"source": "animation", "animation": name, "anim_speed": 2})
            assert CTL.render(time.monotonic() + 5)[w] == (255, 0, 0), "animation source"
            LIB.remove(name)
            CTL.update({"anim_speed": 1})  # missing file: dark, no crash
            CTL.render(time.monotonic() + 6)
        finally:
            shutil.rmtree(LIB.folder, ignore_errors=True)
            LIB = real_lib
        ed = editor.default_editor()
        ed["layers"][0]["paint"] = {str(w): "00FF00"}
        CTL.update({"source": "editor", "editor": ed, "profiles": {"Jogo": ed}})
        assert CTL.render(time.monotonic() + 5)[w] == (0, 255, 0), "editor paint"
        for bad in ({"effect": "nope"}, {"fx": {"wave": {"speed": 99}}}, {"fx": {"nope": {}}}, {"evil": 1},
                    {"editor": {"layers": [{"type": "x"}]}}, {"profiles": {"": ed}}, {"palettes": {"fire": ["000000", "FFFFFF"]}},
                    {"idle_action": "explode"}, {"autostart": "yes"},
                    {"rgb_mode": "x"}, {"rgb_off": "GPU"}, {"rgb_port": 0},
                    {"rgb_dev": {"RAM": {"mode": "effect", "effect": "nope"}}}, {"rgb_dev": {"RAM": {"mode": "x"}}}):
            try:
                CTL.update(bad)
                raise AssertionError(f"accepted {bad}")
            except ValueError:
                pass
        sid = cs.STATE.create_session({"title": "T"})
        cs.STATE.show(sid, [(1, 2, 3)] * gk104.KEY_SLOTS)
        assert CTL.render(time.monotonic() + 5)[0] == (1, 2, 3) and CTL.chroma_title() == "T", "Chroma overrides"
        CTL.update({"chroma_override": False})
        assert CTL.render(time.monotonic() + 5)[0] != (1, 2, 3), "override off ignores Chroma apps"
        cs.STATE.close_session(sid)
        loaded = load_settings()
        assert loaded["profiles"]["Jogo"]["layers"][0]["paint"] and loaded["fx"]["wave"]["palette"] == "Minha", "persisted"
        assert startup_command().endswith("--background")
        print("selftest ok")
    finally:
        SETTINGS_FILE.unlink(missing_ok=True)
        SETTINGS_FILE = real

def main():
    global CTL, SYNC
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-window", action="store_true", help="server only, no window and no tray")
    ap.add_argument("--background", action="store_true", help="start in the tray (used at Windows startup)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if sys.stdout is None:  # windowed .exe has no console: keep messages and errors in a log file
        DATA.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(DATA / "log.txt", "w", encoding="utf-8", buffering=1)
    if already_running():  # second launch: just show the window of the running instance
        if not a.no_window and not a.background:
            open_window()
        return
    migrate_old_install()
    engine = audio.AudioEngine().start()
    CTL = Controller(engine)
    server = Server(("127.0.0.1", cs.PORT), Handler)  # localhost only
    stop, quit_app = threading.Event(), threading.Event()
    SYNC = openrgb.Sync(lambda: CTL.last_frame, lambda: CTL.settings,
                        lambda n, p: effects.make(n, p) if n in effects.EFFECTS else music.make(n, engine, p),
                        idle_seconds).start()
    dev = threading.Thread(target=device_loop, args=(CTL, stop), daemon=True)
    dev.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"{APP_NAME} at {URL}", flush=True)

    win = {"proc": None}
    def show():
        if win["proc"] is None or win["proc"].poll() is not None:
            win["proc"] = open_window()
    def watch_window():  # closing the window quits, unless the app lives in the tray
        while not quit_app.is_set():
            p = win["proc"]
            if p is not None and p.poll() is not None:
                win["proc"] = None
                if not CTL.settings["close_to_tray"] or tray_icon is None:
                    quit_app.set()
            time.sleep(0.5)
    def toggle_light():
        CTL.update({"source": "off" if CTL.settings["source"] != "off" else CTL.last_on})
    tray_icon = None
    if not a.no_window:
        try:
            tray_icon = tray.Tray(APP_NAME, ICON, show, lambda: [
                (f"Open {APP_NAME}", show),
                ("Turn lighting off" if CTL.settings["source"] != "off" else "Turn lighting on", toggle_light),
                None,
                ("Quit", quit_app.set)]).start()
        except OSError as e:
            print("tray unavailable:", e, flush=True)
        if not a.background:
            show()
        threading.Thread(target=watch_window, daemon=True).start()
    try:
        while not quit_app.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        if tray_icon:
            tray_icon.stop()
        stop.set()
        dev.join(3)
        server.shutdown()
        SYNC.close()
        engine.close()

if __name__ == "__main__":
    main()
