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
