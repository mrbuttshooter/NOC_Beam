# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for NOC_Beam.
#
# Builds in --onedir mode: produces dist/NOC_Beam/ containing NOC_Beam.exe
# plus an _internal/ folder. Distribution is the zipped folder, not a single
# file. With the FAS detection bundle (~500 MB) --onefile cold-start would
# be 5-15 seconds per launch because PyInstaller would extract the entire
# bundle to %TEMP% every time; --onedir launches instantly.
#
# Run from the repo root via build/build_windows.ps1, or directly:
#   pyinstaller --clean --noconfirm build/noc_beam.spec

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs, collect_data_files

REPO_ROOT = Path(SPECPATH).parent.resolve()
SRC = REPO_ROOT / "src"
RESOURCES = SRC / "noc_beam" / "ui" / "resources"
VERSION_INFO = REPO_ROOT / "build" / "version_info.txt"
# Icon now lives next to the other UI resources (Phase B). Fall back
# to the legacy assets/ path for older trees.
ICON = RESOURCES / "icon.ico"
if not ICON.exists():
    ICON = REPO_ROOT / "assets" / "icon.ico"

# Bundle the custom pjsua2 native extension if present
native_pkg = SRC / "noc_beam" / "_native" / "pjsua2"
binaries = []
datas = []

if native_pkg.exists():
    for f in native_pkg.glob("*.pyd"):
        binaries.append((str(f), "noc_beam/_native/pjsua2"))
    for f in native_pkg.glob("*.dll"):
        binaries.append((str(f), "noc_beam/_native/pjsua2"))
    for f in native_pkg.glob("*.py"):
        datas.append((str(f), "noc_beam/_native/pjsua2"))

# FAS detection: bundle ONNX models + Chromaprint fpcalc binary.
# Files are fetched at build time by build/fetch_fas_models.py from the
# URLs pinned in build/MODELS.lock. If the fetch script hasn't been run
# the spec still builds -- the bundle just won't include FAS assets, and
# --fas-smoke at runtime will report missing files.
fas_models_dir = SRC / "noc_beam" / "audio" / "models"
if fas_models_dir.exists():
    # Collect all model-adjacent files. ONNX models stored in the
    # external-data format (e.g. Cnn14_16k.onnx + Cnn14_16k.onnx.data)
    # fail to load at runtime with "External data path does not exist"
    # unless the sidecar is bundled next to the .onnx. Also collect
    # *.bin and *.weights defensively in case other models use those
    # sidecar naming conventions.
    for pattern in ("*.onnx", "*.onnx.data", "*.bin", "*.weights"):
        for f in fas_models_dir.glob(pattern):
            datas.append((str(f), "noc_beam/audio/models"))
    # Loud build-time check: a FAS build missing the anti-spoof model is
    # degraded (verdicts collapse to INCONCLUSIVE). Announce it on every
    # build so shipping a degraded bundle is a deliberate choice, never a
    # silent accident. See build/MODELS.lock for the model contract.
    _required_models = ("silero_vad.onnx", "aasist.onnx", "Cnn14_16k.onnx")
    _missing_models = [m for m in _required_models
                       if not (fas_models_dir / m).exists()]
    if _missing_models:
        print("=" * 70)
        print("  *** WARNING: FAS DEGRADED BUILD ***")
        print("  Missing model(s):", ", ".join(_missing_models))
        if "aasist.onnx" in _missing_models:
            print("  aasist.onnx is the PRIMARY anti-spoof signal -- without")
            print("  it FAS verdicts are unreliable. See build/MODELS.lock.")
        print("=" * 70)
    else:
        print("FAS models OK: all of", ", ".join(_required_models), "bundled.")

chromaprint_dir = SRC / "noc_beam" / "_native" / "chromaprint"
if chromaprint_dir.exists():
    for f in chromaprint_dir.glob("*.exe"):
        binaries.append((str(f), "noc_beam/_native/chromaprint"))
    for f in chromaprint_dir.glob("*.dll"):
        binaries.append((str(f), "noc_beam/_native/chromaprint"))

# Bundled default supplier list (seed data copied to %APPDATA% on
# first run so the Settings -> Suppliers editor has somewhere to
# write to).
data_dir_src = SRC / "noc_beam" / "data"
if data_dir_src.exists():
    for f in data_dir_src.glob("*.json"):
        datas.append((str(f), "noc_beam/data"))

# ONNX Runtime DLLs (onnxruntime + onnxruntime_providers_shared).
# collect_dynamic_libs returns [] gracefully if the package isn't installed.
binaries += collect_dynamic_libs("onnxruntime")

# Bundle every UI resource alongside the package -- both stylesheets
# (so the high-contrast toggle works), tokens, the wordmark + mark
# SVGs, and the .ico itself for QIcon lookup. Anything new dropped
# into ui/resources/ is automatically included.
for f in RESOURCES.iterdir():
    if f.is_file():
        datas.append((str(f), "noc_beam/ui/resources"))

# Web softphone assets (index.html, app.js, qwebchannel.js). The WebShell
# loads these from noc_beam/webui/assets at runtime via QUrl.fromLocalFile,
# so they must ship as data files next to the package. QtWebEngine itself is
# pulled in automatically by PyInstaller's PySide6 hook (adds ~150MB to dist).
WEBUI_ASSETS = SRC / "noc_beam" / "webui" / "assets"
if WEBUI_ASSETS.is_dir():
    for f in WEBUI_ASSETS.iterdir():
        if f.is_file():
            datas.append((str(f), "noc_beam/webui/assets"))

if ICON.exists() and not any(d[0] == str(ICON) for d in datas):
    datas.append((str(ICON), "assets"))

# PySide6 plugins are picked up automatically via PyInstaller's PySide6 hook.
hiddenimports = collect_submodules("noc_beam") + [
    "win32crypt",
    "win32api",
    "onnxruntime",
    "onnxruntime.capi",
    "onnxruntime.capi.onnxruntime_pybind11_state",
]

block_cipher = None

a = Analysis(
    [str(SRC / "noc_beam" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PyQt5", "PyQt6",   # avoid mixing Qt bindings
        "PIL", "numpy.testing",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# Dist diet (webui phase 2):
#   1. QtWebEngine ships 53 locale .paks (~37 MB); the app UI is English-only,
#      so keep en-US.pak and drop the other 52.
#   2. The web softphone embeds QtWebEngine via WIDGETS (QWebEngineView) only;
#      the QtWebEngineQuick QML layer is never imported. Drop its DLLs, the
#      PySide6 QtWebEngineQuick pyd, and the qml/QtWebEngine* plugin tree.
#      NOTE: Qt6Quick / Qt6QuickWidgets / Qt6Qml must STAY -- Qt6's
#      QWebEngineView composites through Quick internally.
def _keep_dist_entry(entry):
    dest = entry[0].replace("\\", "/").lower()
    if "qtwebengine_locales" in dest and not dest.endswith("en-us.pak"):
        return False
    name = dest.rsplit("/", 1)[-1]
    if name.startswith((
        "qt6webenginequick",      # Qt6WebEngineQuick.dll / ...DelegatesQml.dll
        "qtwebenginequick",       # PySide6 QtWebEngineQuick.pyd/.pyi
        "qt6webchannelquick",     # QML-side webchannel (widgets path unused)
    )):
        return False
    if "/qml/qtwebengine" in dest:
        return False
    return True


a.datas = [e for e in a.datas if _keep_dist_entry(e)]
a.binaries = [e for e in a.binaries if _keep_dist_entry(e)]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --onedir build: EXE bundles only the launcher; binaries/datas go in
# COLLECT so they land in dist/NOC_Beam/_internal/ next to the exe.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NOC_Beam",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX can break antivirus / Defender
    console=False,                   # GUI app, no console window
    disable_windowed_traceback=False,
    icon=str(ICON) if ICON.exists() else None,
    version=str(VERSION_INFO) if VERSION_INFO.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NOC_Beam",
)
