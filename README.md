# FrøstRGB

Lighting control for the **SKYLOONG GK104 Pro** keyboard on Windows, built to replace GK6+.

- 29 effects and 24 music-sync modes (system audio via WASAPI), including a real triggered oscilloscope
- Screen Ambient: mirrors the main monitor (per key, dominant color, top colors or average)
- 50 built-in palettes plus a Discover browser that imports palettes from Color Hunt and Lospec
- Studio: per-key RGB editor with layers, blend modes, profiles and undo
- Razer Chroma: emulates the Chroma SDK REST API (port 54235) so Chroma apps light the keyboard,
  and plays Synapse `.ChromaEffects` and Chroma Editor `.chroma` files
- PC Sync: fans, RAM, motherboard and GPU through the [OpenRGB](https://openrgb.org) SDK server, either
  following the keyboard or running their own effect per device
- Tray icon, start with Windows, idle dimming, color calibration

## Download

Get `FrostRGB.exe` from [Releases](../../releases). Close GK6+ first: only one app can drive the keyboard.

## Run from source

Python 3.11+ on Windows, with `numpy`.

```
python app.py               # window + tray
python app.py --no-window   # server only, UI at http://localhost:54235/
python app.py --selftest
python build_exe.py         # builds dist/FrostRGB.exe (needs pyinstaller)
```

## Notes

- The keyboard protocol was reverse engineered from GK6+. FrøstRGB only uses its live-lighting mode and never
  sends firmware or data-erase commands.
- PC Sync needs OpenRGB running with its SDK server on (as administrator for RAM). Close other RGB software
  (MSI Center, iCUE, ...) so they don't fight over the colors.
