# Native SIP Startup Smoke + STUN Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the packaged executable smoke test bounded and representative of real native SIP startup, while wiring configured STUN servers into PJSIP.

**Architecture:** Keep the smoke logic in `noc_beam.sip.smoke`, keep STUN collection in `noc_beam.sip.endpoint`, and pass account data from existing UI startup points into endpoint startup. The build script owns process timeout behavior because it is the release gate that can otherwise hang.

**Tech Stack:** Python 3.12, PySide6, pjsua2/PJSIP, PyInstaller, PowerShell, pytest, ruff.

---

### Task 1: STUN Collection And Endpoint Startup

**Files:**
- Modify: `python-app/src/noc_beam/sip/endpoint.py`
- Modify: `python-app/src/noc_beam/ui/main_window.py`
- Modify: `python-app/src/noc_beam/ui/phone_shell.py`
- Test: `python-app/tests/test_stun_config.py`

- [ ] **Step 1: Write STUN collection tests**

Create `python-app/tests/test_stun_config.py`:

```python
from __future__ import annotations

from noc_beam.config.store import AccountConfig
from noc_beam.sip.endpoint import collect_stun_servers


def test_collect_stun_servers_strips_dedupes_and_skips_disabled() -> None:
    accounts = [
        AccountConfig(id="a", stun_server=" stun1.example.com "),
        AccountConfig(id="b", stun_server=""),
        AccountConfig(id="c", stun_server="stun2.example.com"),
        AccountConfig(id="d", stun_server="stun1.example.com"),
        AccountConfig(id="e", stun_server="stun3.example.com", enabled=False),
    ]

    assert collect_stun_servers(accounts) == [
        "stun1.example.com",
        "stun2.example.com",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_stun_config.py -q`

Expected: import failure for `collect_stun_servers`.

- [ ] **Step 3: Implement helper and endpoint parameter**

In `python-app/src/noc_beam/sip/endpoint.py`, add:

```python
def collect_stun_servers(accounts: list[AccountConfig] | None) -> list[str]:
    if not accounts:
        return []
    seen: set[str] = set()
    servers: list[str] = []
    for account in accounts:
        if not account.enabled:
            continue
        server = account.stun_server.strip()
        if not server or server in seen:
            continue
        seen.add(server)
        servers.append(server)
    return servers
```

Change `SipEndpoint.start` to:

```python
def start(self, settings: GlobalSettings, accounts: list[AccountConfig] | None = None) -> None:
```

Before `self._ep.libInit(ep_cfg)`, add:

```python
for server in collect_stun_servers(accounts):
    ep_cfg.uaConfig.stunServer.append(server)
ep_cfg.uaConfig.stunIgnoreFailure = True
```

- [ ] **Step 4: Pass accounts from UI startup**

In `python-app/src/noc_beam/ui/main_window.py`, update endpoint startup calls that currently pass only settings:

```python
SipEndpoint.instance().start(self.settings, accounts=self.accounts)
```

In `python-app/src/noc_beam/ui/phone_shell.py`, update the same pattern:

```python
SipEndpoint.instance().start(self.settings, accounts=self.accounts)
```

- [ ] **Step 5: Run STUN focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_stun_config.py -q`

Expected: `1 passed`.

### Task 2: Native Startup Smoke Depth

**Files:**
- Modify: `python-app/src/noc_beam/sip/smoke.py`
- Modify: `python-app/tests/test_sip_smoke.py`

- [ ] **Step 1: Extend smoke tests for startup depth**

Update fake PJSIP in `python-app/tests/test_sip_smoke.py` with fake `EpConfig`,
`TransportConfig`, codecs, and methods for `libInit`, `transportCreate`,
`codecEnum2`, and `libStart`. Add assertions:

```python
def test_sip_smoke_runs_full_startup_path(monkeypatch) -> None:
    # fake endpoint records libCreate, libInit, UDP/TCP/TLS transportCreate,
    # codecEnum2, libStart, and libDestroy.
    ...
    exit_code, report = smoke.run_sip_smoke(require_native=True, stun_servers=["stun.example.com"])
    assert exit_code == 0
    assert report["lib_initialized"] is True
    assert report["lib_started"] is True
    assert report["transports"]["udp"]["ok"] is True
    assert report["transports"]["tcp"]["ok"] is True
    assert report["transports"]["tls"]["ok"] is True
    assert report["required_codecs"]["g729"] is True
    assert report["required_codecs"]["opus"] is True
    assert report["stun_servers"] == ["stun.example.com"]


def test_sip_smoke_fails_when_required_native_codecs_are_missing(monkeypatch) -> None:
    # fake endpoint returns only PCMU.
    ...
    exit_code, report = smoke.run_sip_smoke(require_native=True)
    assert exit_code == 1
    assert report["required_codecs"]["g729"] is False
    assert report["required_codecs"]["opus"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_sip_smoke.py -q`

Expected: failures for missing `lib_initialized`, transport, codec, and STUN
fields.

- [ ] **Step 3: Implement full startup smoke**

In `python-app/src/noc_beam/sip/smoke.py`, add report fields:

```python
"lib_initialized": False,
"lib_started": False,
"transports": {},
"codecs": [],
"required_codecs": {"g729": False, "opus": False},
"stun_servers": list(stun_servers or []),
```

Change the signature:

```python
def run_sip_smoke(*, require_native: bool = True, stun_servers: list[str] | None = None) -> tuple[int, dict[str, Any]]:
```

After `libCreate()`, create and initialize config:

```python
ep_cfg = _pjsua2_loader.pj.EpConfig()
ep_cfg.uaConfig.userAgent = "NOC_Beam sip-smoke"
ep_cfg.uaConfig.maxCalls = 16
for server in stun_servers or []:
    ep_cfg.uaConfig.stunServer.append(server)
ep_cfg.uaConfig.stunIgnoreFailure = True
endpoint.libInit(ep_cfg)
report["lib_initialized"] = True
```

Create transports with ephemeral ports:

```python
for name, transport_type in (
    ("udp", _pjsua2_loader.pj.PJSIP_TRANSPORT_UDP),
    ("tcp", _pjsua2_loader.pj.PJSIP_TRANSPORT_TCP),
    ("tls", _pjsua2_loader.pj.PJSIP_TRANSPORT_TLS),
):
    cfg = _pjsua2_loader.pj.TransportConfig()
    cfg.port = 0
    try:
        transport_id = endpoint.transportCreate(transport_type, cfg)
        report["transports"][name] = {"ok": True, "id": transport_id, "error": ""}
    except Exception as exc:
        report["transports"][name] = {"ok": False, "id": None, "error": str(exc)}
```

Enumerate codecs and required codec presence:

```python
codecs = [str(c.codecId) for c in endpoint.codecEnum2()]
report["codecs"] = codecs
lower = [c.lower() for c in codecs]
report["required_codecs"]["g729"] = any(c.startswith("g729/") for c in lower)
report["required_codecs"]["opus"] = any(c.startswith("opus/") for c in lower)
```

Call:

```python
endpoint.libStart()
report["lib_started"] = True
```

Compute `ok` only when native source, endpoint created/destroyed, initialized,
started, all transports ok, and all required codecs present.

- [ ] **Step 4: Run smoke tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_sip_smoke.py -q`

Expected: all smoke tests pass.

### Task 3: Bounded Packaged Smoke In Build Script

**Files:**
- Modify: `python-app/build/build_windows.ps1`

- [ ] **Step 1: Add timeout parameter**

Add to the PowerShell `param` block:

```powershell
[int]$PackagedSmokeTimeoutSeconds = 30,
```

- [ ] **Step 2: Replace unbounded wait**

Replace the current `Start-Process ... -Wait` smoke call with:

```powershell
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
        errors = @("Packaged SIP smoke timed out after $PackagedSmokeTimeoutSeconds seconds")
    } | ConvertTo-Json -Depth 4
    Set-Content -Encoding UTF8 -Path $SmokeOut -Value $TimeoutReport
    throw "Packaged SIP smoke timed out after $PackagedSmokeTimeoutSeconds seconds"
}
```

Keep the existing JSON validation after the process exits.

- [ ] **Step 3: Validate script syntax**

Run:

```powershell
powershell -NoProfile -Command "$null = [scriptblock]::Create((Get-Content -Raw .\build\build_windows.ps1)); 'syntax-ok'"
```

Expected: `syntax-ok`.

### Task 4: Verification And Commit

**Files:**
- All modified files above.

- [ ] **Step 1: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_sip_smoke.py tests\test_stun_config.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check src\noc_beam\sip\smoke.py src\noc_beam\sip\endpoint.py tests\test_sip_smoke.py tests\test_stun_config.py
```

Expected: `All checks passed!`

- [ ] **Step 3: Run full tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: full suite passes.

- [ ] **Step 4: Rebuild and smoke packaged executable**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\build\build_windows.ps1 -PythonExe .\.venv\Scripts\python.exe
```

Expected: build succeeds and prints a smoke JSON with `ok: true`, `source:
native`, `lib_initialized: true`, `lib_started: true`, native transports, and
G.729/Opus present.

- [ ] **Step 5: Commit and push**

Run:

```powershell
git add -- python-app/src/noc_beam/sip/endpoint.py python-app/src/noc_beam/sip/smoke.py python-app/src/noc_beam/ui/main_window.py python-app/src/noc_beam/ui/phone_shell.py python-app/tests/test_sip_smoke.py python-app/tests/test_stun_config.py python-app/build/build_windows.ps1
git commit -m "fix: deepen native sip smoke and stun wiring"
git push origin claude/debug-error-Dlc4I
```

Expected: commit and push succeed.

## Self-Review

- Spec coverage: timeout handling is Task 3; startup depth is Task 2; STUN wiring is Task 1; rebuild verification is Task 4.
- Placeholder scan: no TODO/TBD placeholders remain.
- Type consistency: `run_sip_smoke` takes `require_native` and `stun_servers`; `collect_stun_servers` takes `list[AccountConfig] | None`; `SipEndpoint.start` takes `accounts`.
