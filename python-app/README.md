# NOC_Beam

A professional Python SIP softphone built on PJSIP (pjsua2) and PySide6.
Designed as a modern, polished replacement for CounterPath Eyebeam, targeted at
SIP/VoIP testing and internal company telephony use on Windows 10/11.

## Features

- **Multi-account SIP** — register simultaneously with multiple SIP servers / identities
- **Wide codec support** — G.711 (PCMU/PCMA), G.722, G.729 (via BCG729), Opus, GSM, iLBC, Speex; user-configurable priorities
- **TLS + SRTP** — encrypted signaling and media, per-account toggle
- **DTMF** — RFC 2833, SIP INFO, and in-band tones, switchable per account
- **SIP trace viewer** — live capture of SIP signaling with color coding and filtering
- **Audio device picker** — separate microphone, speaker, and ringer device selection
- **NAT traversal** — STUN/TURN/ICE built in via PJSIP
- **Modern Qt UI** — PySide6 with a custom dark theme
- **Portable .exe** — single-file Windows executable via PyInstaller

## Repository layout

```
python-app/
├── pyproject.toml
├── src/noc_beam/
│   ├── __main__.py            # Entry point
│   ├── app.py                 # QApplication bootstrap
│   ├── sip/                   # pjsua2 wrappers (endpoint, account, call, trace)
│   ├── audio/                 # WASAPI device enumeration
│   ├── codecs/                # Codec priority manager
│   ├── ui/                    # PySide6 windows, dialogs, widgets
│   └── config/                # Settings + DPAPI password store
├── build/
│   ├── build_pjsip_windows.md # Reproducible PJSIP build recipe
│   ├── build_windows.ps1      # One-shot Windows build script
│   └── noc_beam.spec          # PyInstaller spec
├── assets/                    # Icon, themes
└── tests/
```

## Building on Windows

NOC_Beam requires a custom build of PJSIP with BCG729, OpenSSL, and SRTP
support. See `build/build_pjsip_windows.md` for the full recipe, or run the
helper script:

```powershell
cd python-app
.\build\build_windows.ps1
```

This produces `dist\NOC_Beam.exe` — a portable, single-file Windows executable.

## Development on Linux/macOS

For UI development you can install the public `pjsua2` wheel (without G.729/SRTP)
and iterate without rebuilding:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m noc_beam
```

## License

Proprietary — internal company use only. Bundles PJSIP (GPL/commercial dual
license); commercial redistribution outside the company requires a PJSIP
commercial license from Teluu.
