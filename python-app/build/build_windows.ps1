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
    [switch]$SkipPackagedSmoke,
    [switch]$SkipFasFetch,
    [switch]$SkipDistZip,
    [switch]$PreflightOnly,
    [switch]$Clean,
    [string]$PythonExe = "python",
    [string]$OpenSslTag = "openssl-3.0.13",
    [string]$PjsipTag = "2.14.1",
    [string]$OpusTag = "v1.5.2",
    [int]$PackagedSmokeTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ThirdParty = Join-Path $RepoRoot "third_party"
$VenvDir = Join-Path $RepoRoot ".venv"
$VcVars = $null
if (Test-Path -LiteralPath $PythonExe) {
    $PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
}

function Write-Header($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Test-Command($Name) {
    Get-Command $Name -ErrorAction SilentlyContinue
}

function Add-ToolPathIfExists([string]$PathPattern) {
    $matches = Resolve-Path $PathPattern -ErrorAction SilentlyContinue
    foreach ($match in $matches) {
        $toolPath = $match.Path
        if ((Test-Path $toolPath) -and ($env:PATH -notlike "*$toolPath*")) {
            $env:PATH = "$toolPath;$env:PATH"
        }
    }
}

function Add-KnownNativeToolPaths {
    Add-ToolPathIfExists "C:\Program Files\CMake\bin"
    Add-ToolPathIfExists "$env:LOCALAPPDATA\bin\NASM"
    Add-ToolPathIfExists "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\SWIG.SWIG_*\swigwin-*"
    Add-ToolPathIfExists "C:\Strawberry\perl\bin"
}

function Find-VcVars {
    $vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $installPath = & $vswhere -latest -products Microsoft.VisualStudio.Product.BuildTools -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
        if ($LASTEXITCODE -eq 0 -and $installPath) {
            $candidate = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
            if (Test-Path $candidate) {
                return $candidate
            }
        }
    }

    $fallbacks = @(
        "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
        "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
    )
    foreach ($candidate in $fallbacks) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return $null
}

function Assert-NativePrerequisites {
    $missing = @()

    if (-not $script:VcVars) {
        $missing += "VS 2022 Build Tools vcvars64.bat not found. Install hint: use Visual Studio Installer and select 'Desktop development with C++', or run winget install --id Microsoft.VisualStudio.2022.BuildTools -e."
    }

    $requiredCommands = @(
        @{ Name = "git";   Hint = "Install hint: winget install --id Git.Git -e, choco install git, or install from https://git-scm.com/download/win." },
        @{ Name = "cmake"; Hint = "Install hint: winget install --id Kitware.CMake -e, choco install cmake, or install from https://cmake.org/download/." },
        @{ Name = "nasm";  Hint = "Install hint: winget install --id NASM.NASM -e, choco install nasm, or install from https://www.nasm.us/." },
        @{ Name = "swig";  Hint = "Install hint: winget install --id SWIG.SWIG -e, choco install swig, or install from https://www.swig.org/download.html." },
        @{ Name = "perl";  Hint = "Install hint: winget install --id StrawberryPerl.StrawberryPerl -e, choco install strawberryperl, or install Strawberry Perl from https://strawberryperl.com/." }
    )

    foreach ($command in $requiredCommands) {
        if (-not (Test-Command $command.Name)) {
            $missing += "$($command.Name) not found on PATH. $($command.Hint)"
        }
    }

    if ($missing.Count -gt 0) {
        throw "Missing native build prerequisites:`n - $($missing -join "`n - ")"
    }

    Write-Host "Native build prerequisites found: VS vcvars64.bat, git, cmake, nasm, swig, perl." -ForegroundColor Green
    Write-Host "Using MSVC environment: $script:VcVars" -ForegroundColor Green
}

function Assert-PythonExecutable([string]$Executable) {
    try {
        $pythonVersion = & $Executable --version 2>&1
    } catch {
        throw "Python executable '$Executable' is not callable. Install Python 3.11 or pass -PythonExe with a valid python.exe path. $($_.Exception.Message)"
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Python executable '$Executable' failed version check: $pythonVersion"
    }

    Write-Host "Python executable found: $pythonVersion" -ForegroundColor Green
}

function Invoke-External([string]$Description, [scriptblock]$Command) {
    & $Command
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $Description"
    }
}

function Invoke-VcCmd([string]$cmd) {
    # Runs a command inside the MSVC x64 environment.
    cmd /c "`"$VcVars`" && $cmd"
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $cmd" }
}

Add-KnownNativeToolPaths
$script:VcVars = Find-VcVars

if ($PreflightOnly) {
    Write-Header "Running build preflight"
    Assert-PythonExecutable $PythonExe
    if (-not $SkipNativeBuild) {
        Assert-NativePrerequisites
    } else {
        Write-Host "Native build prerequisite check skipped because -SkipNativeBuild was set." -ForegroundColor Yellow
    }
    Write-Host "Preflight checks passed. No dependencies installed, native builds run, or PyInstaller invoked." -ForegroundColor Green
    exit 0
}

if ($Clean) {
    Write-Header "Cleaning build artifacts"
    Remove-Item -Recurse -Force "$RepoRoot\dist", "$RepoRoot\build\work", "$ThirdParty" -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------------
# 1. Native build (PJSIP + OpenSSL + BCG729)
# ------------------------------------------------------------------
$NativeOut = Join-Path $RepoRoot "src\noc_beam\_native\pjsua2"
if (-not $SkipNativeBuild) {
    Assert-NativePrerequisites
}

if (-not $SkipNativeBuild -and -not (Test-Path "$NativeOut\_pjsua2.pyd")) {

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
        # WASAPI is intentionally OFF -- PJSIP 2.14.1's wasapi_dev.c leaves
        # pjmedia_wasapi_factory unresolved at link time, breaking every
        # downstream binary. WMME is the older audio API but it works.
        @"
#define PJ_HAS_IPV6                 1
#define PJ_HAS_SSL_SOCK             1
#define PJMEDIA_HAS_SRTP            1
#define PJMEDIA_HAS_BCG729          1
#define PJMEDIA_HAS_OPUS_CODEC      1
#define PJMEDIA_AUDIO_DEV_HAS_WMME  1
#define PJ_ENABLE_EXTRA_CHECK       1
#define PJSUA_MAX_ACC               32
#define PJSUA_MAX_CALLS             16
#define PJSIP_HAS_100REL            0
#include <pj/config_site_sample.h>
"@ | Set-Content -Encoding ASCII $ConfigSite
    }

    $OpusCodecSource = "pjmedia\src\pjmedia-codec\opus.c"
    $opusCodecText = Get-Content -Raw -Path $OpusCodecSource
    $opusCodecText = $opusCodecText.Replace('#   pragma comment(lib, "libopus.a")', '#   pragma comment(lib, "opus.lib")')
    Set-Content -Encoding ASCII -Path $OpusCodecSource -Value $opusCodecText

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
      <PreprocessorDefinitions>BCG729_STATIC;%(PreprocessorDefinitions)</PreprocessorDefinitions>
    </ClCompile>
    <Link>
      <AdditionalLibraryDirectories>$externLibs;%(AdditionalLibraryDirectories)</AdditionalLibraryDirectories>
    </Link>
  </ItemDefinitionGroup>
</Project>
"@ | Set-Content -Encoding UTF8 -Path "Directory.Build.props"

    # pjproject 2.14.1 vcxproj files target PlatformToolset v141 (VS2017) which
    # isn't installed on most modern Windows hosts -- override to v143 (VS2022).
    # Build the aggregate libpjproject static library; skip test/sample/CLI
    # binaries that aren't shipped.
    Invoke-VcCmd "msbuild pjproject-vs14.sln /t:libpjproject /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v143 /p:WindowsTargetPlatformVersion=10.0 /m"

    Write-Header "Building pjsua2 Python extension"
    Push-Location pjsip-apps\src\swig
    Invoke-External "Generate pjsua2 SWIG wrapper" {
        swig "-I..\..\..\pjlib\include" `
             "-I..\..\..\pjlib-util\include" `
             "-I..\..\..\pjmedia\include" `
             "-I..\..\..\pjsip\include" `
             "-I..\..\..\pjnath\include" `
             -c++ -w312 -threads -DSWIG_NO_EXPORT_ITERATOR_METHODS `
             -python -o python\pjsua2_wrap.cpp pjsua2.i
    }

    $SetupMsvc = @"
from setuptools import Extension, setup

pjdir = r"$($PWD.Path)\..\..\.."
openssl_dir = r"$env:OPENSSL_DIR"
bcg729_dir = r"$env:BCG729_DIR"
opus_dir = r"$env:OPUS_DIR"

include_dirs = [
    pjdir + r"\pjlib\include",
    pjdir + r"\pjlib-util\include",
    pjdir + r"\pjmedia\include",
    pjdir + r"\pjsip\include",
    pjdir + r"\pjnath\include",
    openssl_dir + r"\include",
    bcg729_dir + r"\include",
    opus_dir + r"\include",
]
library_dirs = [
    pjdir + r"\lib",
    openssl_dir + r"\lib",
    bcg729_dir + r"\lib",
    opus_dir + r"\lib",
]
libraries = [
    "libpjproject-x86_64-x64-vc14-Release",
    "libssl",
    "libcrypto",
    "bcg729",
    "opus",
    "ws2_32",
    "winmm",
    "ole32",
    "dsound",
    "iphlpapi",
    "secur32",
    "crypt32",
    "advapi32",
    "setupapi",
    "user32",
]
extra_compile_args = [
    "/EHsc",
    "/MD",
    "/DWIN64",
    "/DPJ_WIN64=1",
    "/DPJ_M_X86_64=1",
]

setup(
    name="pjsua2",
    version="2.14.1",
    ext_modules=[
        Extension(
            "_pjsua2",
            ["pjsua2_wrap.cpp"],
            include_dirs=include_dirs,
            library_dirs=library_dirs,
            libraries=libraries,
            extra_compile_args=extra_compile_args,
            language="c++",
        )
    ],
    py_modules=["pjsua2"],
)
"@
    Set-Content -Encoding UTF8 -Path python\setup_msvc.py -Value $SetupMsvc
    Push-Location python
    Invoke-VcCmd "`"$PythonExe`" setup_msvc.py build_ext --inplace"
    Pop-Location
    Pop-Location

    New-Item -ItemType Directory -Force -Path $NativeOut | Out-Null
    $BuiltPyd = Get-ChildItem pjsip-apps\src\swig\python -Filter "_pjsua2*.pyd" | Select-Object -First 1
    if ($null -eq $BuiltPyd) {
        throw "pjsua2 Python extension build did not produce _pjsua2*.pyd"
    }
    Copy-Item -Force $BuiltPyd.FullName (Join-Path $NativeOut "_pjsua2.pyd")
    Copy-Item -Force pjsip-apps\src\swig\python\pjsua2.py (Join-Path $NativeOut "pjsua2.py")
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
    Invoke-External "Create virtualenv using $PythonExe" { & $PythonExe -m venv $VenvDir }
}

$Py = Join-Path $VenvDir "Scripts\python.exe"
Write-Header "Installing Python dependencies"
Invoke-External "Upgrade pip in $VenvDir" { & $Py -m pip install --upgrade pip }
Invoke-External "Install project dependencies from $RepoRoot[dev]" { & $Py -m pip install -e "$RepoRoot[dev]" }

# ------------------------------------------------------------------
# 3. FAS detection assets (ONNX models + Chromaprint fpcalc)
# ------------------------------------------------------------------
if (-not $SkipFasFetch) {
    Write-Header "Fetching FAS detection models + binaries"
    Push-Location $RepoRoot
    try {
        # Non-fatal: a missing AASIST/PANNs model only means the resulting
        # bundle won't include FAS assets. --fas-smoke on the built exe will
        # report which ones are missing.
        & $Py "build\fetch_fas_models.py"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "FAS asset fetch reported missing items (exit $LASTEXITCODE). Continuing build." -ForegroundColor Yellow
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "FAS asset fetch skipped (-SkipFasFetch)." -ForegroundColor Yellow
}

# ------------------------------------------------------------------
# 4. PyInstaller (--onedir)
# ------------------------------------------------------------------
Write-Header "Running PyInstaller"
Push-Location $RepoRoot
try {
    Invoke-External "Run PyInstaller for build\noc_beam.spec" { & $Py -m PyInstaller --clean --noconfirm build\noc_beam.spec }
} finally {
    Pop-Location
}

$DistDir = Join-Path $RepoRoot "dist\NOC_Beam"
$ExePath = Join-Path $DistDir "NOC_Beam.exe"
if (-not (Test-Path $ExePath)) {
    throw "PyInstaller did not produce $ExePath"
}

if (-not $SkipPackagedSmoke -and -not $SkipNativeBuild) {
    Write-Header "Running packaged SIP smoke"
    $SmokeOut = Join-Path $RepoRoot "build\sip-smoke.json"
    Remove-Item -Force $SmokeOut -ErrorAction SilentlyContinue

    $SmokeProc = Start-Process `
        -FilePath $ExePath `
        -ArgumentList @("--sip-smoke", "--sip-smoke-output", $SmokeOut) `
        -PassThru `
        -WindowStyle Hidden

    if (-not $SmokeProc.WaitForExit($PackagedSmokeTimeoutSeconds * 1000)) {
        try {
            Stop-Process -Id $SmokeProc.Id -Force -ErrorAction SilentlyContinue
        } catch {
        }
        $TimeoutReport = @{
            ok = $false
            source = "timeout"
            errors = @(
                "Packaged SIP smoke timed out after $PackagedSmokeTimeoutSeconds seconds"
            )
        } | ConvertTo-Json -Depth 4
        Set-Content -Encoding UTF8 -Path $SmokeOut -Value $TimeoutReport
        throw "Packaged SIP smoke timed out after $PackagedSmokeTimeoutSeconds seconds"
    }

    if (Test-Path $SmokeOut) {
        Get-Content -Raw -Path $SmokeOut | Write-Host
    }

    if ($null -eq $SmokeProc.ExitCode -or $SmokeProc.ExitCode -ne 0) {
        throw "Packaged SIP smoke failed with exit code $($SmokeProc.ExitCode)"
    }

    if (-not (Test-Path $SmokeOut)) {
        throw "Packaged SIP smoke did not write $SmokeOut"
    }

    $SmokeJson = Get-Content -Raw -Path $SmokeOut | ConvertFrom-Json
    if (-not $SmokeJson.ok) {
        throw "Packaged SIP smoke report is not ok"
    }
    if ($SmokeJson.source -ne "native") {
        throw "Packaged SIP smoke loaded '$($SmokeJson.source)' instead of native"
    }
    if (-not $SmokeJson.endpoint_created -or -not $SmokeJson.endpoint_destroyed) {
        throw "Packaged SIP smoke did not create and destroy a PJSIP endpoint"
    }
    if (-not $SmokeJson.lib_initialized -or -not $SmokeJson.lib_started) {
        throw "Packaged SIP smoke did not initialize and start PJSIP"
    }
    foreach ($transport in @("udp", "tcp", "tls")) {
        if (-not $SmokeJson.transports.$transport.ok) {
            throw "Packaged SIP smoke transport '$transport' failed"
        }
    }
    foreach ($codec in @("g729", "opus")) {
        if (-not $SmokeJson.required_codecs.$codec) {
            throw "Packaged SIP smoke did not find required codec '$codec'"
        }
    }
    if (-not $SmokeJson.pjsip_version) {
        throw "Packaged SIP smoke did not report a PJSIP version"
    }
}

$ChecksumPath = "$ExePath.sha256"
$ExeHash = Get-FileHash -LiteralPath $ExePath -Algorithm SHA256
"$($ExeHash.Hash)  NOC_Beam.exe" | Set-Content -Encoding ASCII -Path $ChecksumPath
Write-Host "SHA256: $($ExeHash.Hash)" -ForegroundColor Green

# ------------------------------------------------------------------
# 5. Packaged FAS smoke
# ------------------------------------------------------------------
if (-not $SkipPackagedSmoke) {
    Write-Header "Running packaged FAS smoke"
    $FasSmokeOut = Join-Path $RepoRoot "build\fas-smoke.json"
    Remove-Item -Force $FasSmokeOut -ErrorAction SilentlyContinue

    $FasProc = Start-Process `
        -FilePath $ExePath `
        -ArgumentList @("--fas-smoke", "--fas-smoke-output", $FasSmokeOut) `
        -PassThru `
        -WindowStyle Hidden

    if (-not $FasProc.WaitForExit($PackagedSmokeTimeoutSeconds * 1000)) {
        try { Stop-Process -Id $FasProc.Id -Force -ErrorAction SilentlyContinue } catch {}
        Write-Host "FAS smoke timed out after $PackagedSmokeTimeoutSeconds seconds (continuing build)." -ForegroundColor Yellow
    } elseif (Test-Path $FasSmokeOut) {
        Get-Content -Raw -Path $FasSmokeOut | Write-Host
        if ($FasProc.ExitCode -ne 0) {
            Write-Host "FAS smoke exit $($FasProc.ExitCode) -- assets missing or failed to load. Continuing build." -ForegroundColor Yellow
        }
    }
}

# ------------------------------------------------------------------
# 6. Distribution zip
# ------------------------------------------------------------------
if (-not $SkipDistZip) {
    Write-Header "Zipping dist\NOC_Beam for distribution"
    $ZipPath = Join-Path $RepoRoot "dist\NOC_Beam.zip"
    Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue
    Compress-Archive -Path "$DistDir\*" -DestinationPath $ZipPath -CompressionLevel Optimal
    $ZipHash = Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256
    "$($ZipHash.Hash)  NOC_Beam.zip" | Set-Content -Encoding ASCII -Path "$ZipPath.sha256"
    $ZipSizeMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
    Write-Host "Distribution zip: $ZipPath ($ZipSizeMB MB)" -ForegroundColor Green
    Write-Host "Zip SHA256: $($ZipHash.Hash)" -ForegroundColor Green
}

Write-Header "SUCCESS"
Write-Host "Built: $ExePath" -ForegroundColor Green
Write-Host "Dist folder: $DistDir" -ForegroundColor Green
Write-Host "Checksum: $ChecksumPath" -ForegroundColor Green
