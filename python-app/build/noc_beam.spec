# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for NOC_Beam.
#
# Run from the repo root via build/build_windows.ps1, or directly:
#   pyinstaller --clean --noconfirm build/noc_beam.spec

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs, collect_data_files

REPO_ROOT = Path(SPECPATH).parent.resolve()
SRC = REPO_ROOT / "src"
RESOURCES = SRC / "noc_beam" / "ui" / "resources"
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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="NOC_Beam",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX can break antivirus / Defender
    runtime_tmpdir=None,
    console=False,                   # GUI app, no console window
    disable_windowed_traceback=False,
    icon=str(ICON) if ICON.exists() else None,
    version=None,
)
