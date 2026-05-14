# Building PJSIP for NOC_Beam on Windows

This recipe produces a custom `pjsua2` Python extension for Windows x64 with:

- **BCG729** (G.729 codec, royalty-free)
- **OpenSSL** (for SIP/TLS transport)
- **libSRTP** (for media encryption — bundled in PJSIP)
- **Opus** (wideband audio codec — bundled in PJSIP)
- **WASAPI** audio backend (low-latency Windows audio)

It must be run on a Windows 10/11 x64 host. Estimated time: 30–60 min on the
first run, 5 min on rebuilds.

## Prerequisites

Install the following (admin PowerShell):

```powershell
winget install --id Microsoft.VisualStudio.2022.BuildTools `
    --override "--quiet --add Microsoft.VisualStudio.Workload.VCTools `
                --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
                --add Microsoft.VisualStudio.Component.Windows11SDK.22621"
winget install --id Git.Git
winget install --id Python.Python.3.11
winget install --id Kitware.CMake
winget install --id NASM.NASM
```

You also need **SWIG 4.x** for the Python bindings:

```powershell
choco install swig
# or download from https://www.swig.org/download.html and add to PATH
```

Verify:

```powershell
cl       # MSVC compiler
python --version    # 3.11.x
swig -version       # 4.x
nasm -v             # for OpenSSL asm
cmake --version
```

## 1. Prepare the workspace

From the repo root:

```powershell
mkdir python-app\third_party
cd python-app\third_party
```

## 2. Build OpenSSL (static, x64)

```powershell
git clone --depth 1 --branch openssl-3.0.13 https://github.com/openssl/openssl.git
cd openssl

# Open "x64 Native Tools Command Prompt for VS 2022" — or in PowerShell:
& "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"

perl Configure VC-WIN64A no-shared no-tests --prefix=$PWD\..\openssl-install
nmake
nmake install_sw
cd ..
```

## 3. Build BCG729 (G.729 codec)

```powershell
git clone --depth 1 https://github.com/BelledonneCommunications/bcg729.git
cd bcg729
mkdir build-win
cd build-win
cmake -G "Visual Studio 17 2022" -A x64 `
      -DCMAKE_INSTALL_PREFIX=..\..\bcg729-install `
      -DENABLE_SHARED=OFF -DENABLE_STATIC=ON ..
cmake --build . --config Release --target install
cd ..\..
```

## 4. Clone PJSIP

```powershell
git clone --depth 1 --branch 2.14.1 https://github.com/pjsip/pjproject.git
cd pjproject
```

### 4a. Create config_site.h

Create `pjlib\include\pj\config_site.h`:

```c
#define PJ_HAS_IPV6                 1
#define PJ_HAS_SSL_SOCK             1
#define PJMEDIA_HAS_SRTP            1
#define PJMEDIA_HAS_BCG729          1
#define PJMEDIA_HAS_OPUS_CODEC      1
#define PJMEDIA_AUDIO_DEV_HAS_WASAPI 1
#define PJMEDIA_AUDIO_DEV_HAS_WMME  1
#define PJ_ENABLE_EXTRA_CHECK       1
#define PJSUA_MAX_ACC               32
#define PJSUA_MAX_CALLS             16
#include <pj/config_site_sample.h>
```

### 4b. Configure include/lib paths

PJSIP looks up OpenSSL and BCG729 via environment variables. From the
`pjproject\` directory:

```powershell
$env:OPENSSL_DIR    = (Resolve-Path ..\openssl-install).Path
$env:BCG729_DIR     = (Resolve-Path ..\bcg729-install).Path

# Tell MSVC where to find them
$env:INCLUDE = "$env:OPENSSL_DIR\include;$env:BCG729_DIR\include;$env:INCLUDE"
$env:LIB     = "$env:OPENSSL_DIR\lib;$env:BCG729_DIR\lib;$env:LIB"
```

### 4c. Build PJSIP

Open `pjproject-vs14.sln` in Visual Studio (or use msbuild from the command
line). Set configuration to **Release**, platform **x64**.

```powershell
msbuild pjproject-vs14.sln /p:Configuration=Release /p:Platform=x64 /m
```

This produces `lib\*.lib` for all PJSIP components.

## 5. Build the pjsua2 Python extension

```powershell
cd pjsip-apps\src\swig
nmake python
```

The result is a `_pjsua2.pyd` and `pjsua2.py` under
`pjsip-apps\src\swig\python\`. Copy them next to NOC_Beam:

```powershell
$dest = "..\..\..\..\..\src\noc_beam\_native\pjsua2"
mkdir $dest -Force
copy python\_pjsua2.pyd $dest\
copy python\pjsua2.py    $dest\
```

## 6. Sanity check

```powershell
cd ..\..\..\..\..\
python -c "from noc_beam._native.pjsua2 import pjsua2 as pj; ep = pj.Endpoint(); ep.libCreate(); print('PJSIP', ep.libVersion().full); ep.libDestroy()"
```

You should see something like `PJSIP 2.14.1`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `LNK2019` unresolved external on `BCG729_*` | `INCLUDE`/`LIB` env vars not picked up — re-export and rebuild. |
| `error C1083: cannot open openssl/ssl.h` | Same — verify `OPENSSL_DIR` and run `vcvars64.bat`. |
| `swig: command not found` | Add SWIG install dir to `PATH`. |
| PJSIP build picks wrong compiler | Always open the *x64 Native Tools* prompt, not plain `cmd`. |
| `_pjsua2.pyd` fails to import | Run `python -c "import _pjsua2"` directly; if it crashes, OpenSSL DLLs missing — we built static so this should not happen. Run `dumpbin /dependents _pjsua2.pyd`. |

## Why this isn't automated yet

The above is documented step-by-step so you can run it once and confirm each
piece works. `build_windows.ps1` automates steps 1–6 end-to-end; this Markdown
remains the canonical reference if something goes wrong.
