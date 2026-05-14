# NOC_Beam end-to-end Windows build script.
#
# Runs PJSIP + dependencies build (if missing), installs Python deps,
# then invokes PyInstaller to produce dist\NOC_Beam.exe.
#
# Requires: VS 2022 Build Tools, Python 3.11, Git, CMake, NASM, SWIG, Perl.
# See build\build_pjsip_windows.md for full prerequisites.

#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$SkipNativeBuild,
    [switch]$Clean,
    [string]$PythonExe = "python",
    [string]$OpenSslTag = "openssl-3.0.13",
    [string]$PjsipTag = "2.14.1",
    [string]$OpusTag = "v1.5.2"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ThirdParty = Join-Path $RepoRoot "third_party"
$VenvDir = Join-Path $RepoRoot ".venv"
$VcVars = "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"

function Write-Header($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Invoke-VcCmd([string]$cmd) {
    # Runs a command inside the MSVC x64 environment.
    cmd /c "`"$VcVars`" && $cmd"
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $cmd" }
}

if ($Clean) {
    Write-Header "Cleaning build artifacts"
    Remove-Item -Recurse -Force "$RepoRoot\dist", "$RepoRoot\build\work", "$ThirdParty" -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------------
# 1. Native build (PJSIP + OpenSSL + BCG729)
# ------------------------------------------------------------------
$NativeOut = Join-Path $RepoRoot "src\noc_beam\_native\pjsua2"
if (-not $SkipNativeBuild -and -not (Test-Path "$NativeOut\_pjsua2.pyd")) {

    if (-not (Test-Path $VcVars)) {
        throw "VS 2022 Build Tools not found at $VcVars. Install with: winget install Microsoft.VisualStudio.2022.BuildTools"
    }

    New-Item -ItemType Directory -Force -Path $ThirdParty | Out-Null
    Push-Location $ThirdParty

    # --- OpenSSL ---
    if (-not (Test-Path "openssl-install\lib\libssl.lib")) {
        Write-Header "Building OpenSSL ($OpenSslTag)"
        if (-not (Test-Path "openssl")) {
            git clone --depth 1 --branch $OpenSslTag https://github.com/openssl/openssl.git
        }
        Push-Location openssl
        Invoke-VcCmd "perl Configure VC-WIN64A no-shared no-tests --prefix=$ThirdParty\openssl-install && nmake && nmake install_sw"
        Pop-Location
    }

    # --- BCG729 ---
    if (-not (Test-Path "bcg729-install\lib\bcg729.lib")) {
        Write-Header "Building BCG729"
        if (-not (Test-Path "bcg729")) {
            git clone --depth 1 https://github.com/BelledonneCommunications/bcg729.git
        }
        Push-Location bcg729
        New-Item -ItemType Directory -Force -Path build-win | Out-Null
        Push-Location build-win
        cmake -G "Visual Studio 17 2022" -A x64 `
              -DCMAKE_INSTALL_PREFIX="$ThirdParty\bcg729-install" `
              -DENABLE_SHARED=OFF -DENABLE_STATIC=ON ..
        cmake --build . --config Release --target install
        Pop-Location
        Pop-Location
    }

    # --- Opus ---
    # PJSIP enables PJMEDIA_HAS_OPUS_CODEC in config_site.h below, so we
    # have to ship Opus headers + static lib too. Without this the PJSIP
    # build fails with `Cannot open include file: 'opus/opus.h'`.
    if (-not (Test-Path "opus-install\lib\opus.lib")) {
        Write-Header "Building Opus $OpusTag"
        if (-not (Test-Path "opus")) {
            git clone --depth 1 --branch $OpusTag https://github.com/xiph/opus.git
        }
        Push-Location opus
        New-Item -ItemType Directory -Force -Path build-win | Out-Null
        Push-Location build-win
        cmake -G "Visual Studio 17 2022" -A x64 `
              -DCMAKE_INSTALL_PREFIX="$ThirdParty\opus-install" `
              -DBUILD_SHARED_LIBS=OFF `
              -DOPUS_BUILD_TESTING=OFF `
              -DOPUS_BUILD_PROGRAMS=OFF ..
        cmake --build . --config Release --target install
        Pop-Location
        Pop-Location
    }

    # --- PJSIP ---
    Write-Header "Building PJSIP $PjsipTag"
    if (-not (Test-Path "pjproject")) {
        git clone --depth 1 --branch $PjsipTag https://github.com/pjsip/pjproject.git
    }
    Push-Location pjproject

    $ConfigSite = "pjlib\include\pj\config_site.h"
    if (-not (Test-Path $ConfigSite)) {
        @"
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
"@ | Set-Content -Encoding ASCII $ConfigSite
    }

    $env:OPENSSL_DIR = "$ThirdParty\openssl-install"
    $env:BCG729_DIR  = "$ThirdParty\bcg729-install"
    $env:OPUS_DIR    = "$ThirdParty\opus-install"

    # MSBuild does not reliably propagate %INCLUDE%/%LIB% to the cl.exe
    # processes it spawns for each .vcxproj. Inject the external paths via
    # a Directory.Build.props at the pjproject root — MSBuild auto-imports
    # it into every project under the tree.
    $externIncludes = "$env:OPENSSL_DIR\include;$env:BCG729_DIR\include;$env:OPUS_DIR\include"
    $externLibs     = "$env:OPENSSL_DIR\lib;$env:BCG729_DIR\lib;$env:OPUS_DIR\lib"
    @"
<Project>
  <ItemDefinitionGroup>
    <ClCompile>
      <AdditionalIncludeDirectories>$externIncludes;%(AdditionalIncludeDirectories)</AdditionalIncludeDirectories>
    </ClCompile>
    <Link>
      <AdditionalLibraryDirectories>$externLibs;%(AdditionalLibraryDirectories)</AdditionalLibraryDirectories>
    </Link>
  </ItemDefinitionGroup>
</Project>
"@ | Set-Content -Encoding UTF8 -Path "Directory.Build.props"

    # pjproject 2.14.1 vcxproj files target PlatformToolset v141 (VS2017) which
    # isn't installed on most modern Windows hosts — override to v143 (VS2022).
    Invoke-VcCmd "msbuild pjproject-vs14.sln /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v143 /p:WindowsTargetPlatformVersion=10.0 /m"

    Write-Header "Building pjsua2 Python extension"
    Push-Location pjsip-apps\src\swig
    Invoke-VcCmd "nmake python"
    Pop-Location

    New-Item -ItemType Directory -Force -Path $NativeOut | Out-Null
    Copy-Item -Force pjsip-apps\src\swig\python\_pjsua2.pyd $NativeOut\
    Copy-Item -Force pjsip-apps\src\swig\python\pjsua2.py    $NativeOut\
    # Empty __init__.py so this becomes a package
    "" | Set-Content "$NativeOut\__init__.py"

    Pop-Location  # pjproject
    Pop-Location  # third_party
}

# ------------------------------------------------------------------
# 2. Python virtualenv + dependencies
# ------------------------------------------------------------------
if (-not (Test-Path $VenvDir)) {
    Write-Header "Creating virtualenv at $VenvDir"
    & $PythonExe -m venv $VenvDir
}

$Py = Join-Path $VenvDir "Scripts\python.exe"
Write-Header "Installing Python dependencies"
& $Py -m pip install --upgrade pip
& $Py -m pip install -e "$RepoRoot[dev]"

# ------------------------------------------------------------------
# 3. PyInstaller
# ------------------------------------------------------------------
Write-Header "Running PyInstaller"
Push-Location $RepoRoot
& $Py -m PyInstaller --clean --noconfirm build\noc_beam.spec
Pop-Location

if (Test-Path "$RepoRoot\dist\NOC_Beam.exe") {
    Write-Header "SUCCESS"
    Write-Host "Built: $RepoRoot\dist\NOC_Beam.exe" -ForegroundColor Green
} else {
    throw "PyInstaller did not produce dist\NOC_Beam.exe"
}
