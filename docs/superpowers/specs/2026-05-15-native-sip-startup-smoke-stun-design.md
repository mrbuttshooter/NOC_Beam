# Native SIP Startup Smoke + STUN Wiring

## Context

The packaged executable now has a `--sip-smoke` diagnostic, but it only proves
that the bundled native `pjsua2` can create and destroy an endpoint. The real
application startup path also runs `libInit`, creates UDP/TCP/TLS transports,
enumerates codecs, applies codec priority rules, and starts PJSIP. A release
artifact can pass the current smoke while still failing the actual SIP runtime
path.

The build script also waits for the packaged smoke without a timeout, which can
stall a local release or CI run indefinitely. Separately, account settings expose
`stun_server`, but the endpoint never adds those servers to `EpConfig.uaConfig`,
so NAT behavior is implied by the UI but not configured in PJSIP.

Approval: self-approved under the user's delegated instruction on 2026-05-15 to
keep upgrading without waiting for another approval gate.

## Goals

- Make packaged smoke bounded: it must fail cleanly on timeout instead of
  hanging the build.
- Make smoke mirror real SIP startup enough to catch runtime packaging failures:
  `libCreate`, `libInit`, UDP/TCP/TLS transport creation, codec enumeration,
  `libStart`, and `libDestroy`.
- Record transport and codec facts in the JSON smoke report.
- Assert native codec/runtime expectations that matter for this product: at
  least G.729 and Opus codec presence when the native build is used.
- Wire configured STUN servers from account settings into endpoint startup.
- Keep the smoke offline and credential-free.

## Non-Goals

- Registering real SIP accounts.
- Sending SIP traffic to external networks.
- Solving the full Diagnostics UI threading issue.
- Installer/signing/version metadata.

## Design

### Shared STUN Server Collection

Add a small helper in `noc_beam.sip.endpoint`:

```python
def collect_stun_servers(accounts: list[AccountConfig]) -> list[str]
```

It strips whitespace, skips blank values and disabled accounts, preserves first
seen order, and de-duplicates duplicate STUN values. `SipEndpoint.start()` will
accept an optional `accounts` list and append those servers to
`ep_cfg.uaConfig.stunServer` before `libInit()`. Callers that do not pass
accounts keep the existing behavior.

The main UI startup paths will pass their loaded accounts into endpoint startup.

### Runtime Smoke

`noc_beam.sip.smoke.run_sip_smoke()` will perform a native startup smoke:

1. Verify the loader source is `native` when `require_native=True`.
2. Create `pj.Endpoint()`.
3. Create `pj.EpConfig()` and set minimal UA/log/media fields.
4. Optionally append supplied STUN servers to `uaConfig.stunServer`.
5. Run `libInit()`.
6. Create UDP, TCP, and TLS transports on ephemeral ports.
7. Enumerate codecs with `codecEnum2()`.
8. Run `libStart()`.
9. Run `libDestroy()` in cleanup.

The JSON report will include:

- `lib_initialized`
- `lib_started`
- `transports`: per-transport success/error/id
- `codecs`: codec IDs discovered
- `required_codecs`: map of required codec name to present boolean
- `stun_servers`

Transport failures should be recorded per transport. UDP is required; TCP/TLS
failures are reported and make the smoke fail for a native release because TLS
is part of the product promise. Codec enumeration failure also fails the smoke.

### Build Timeout

Add a small PowerShell helper in `build_windows.ps1` that starts
`NOC_Beam.exe --sip-smoke`, waits up to a fixed timeout, kills the process on
timeout, writes a failure JSON if the executable did not produce one, and fails
the build with a clear message.

CI continues to use the shared script and validates the JSON report after the
script returns.

## Testing

- Unit test STUN collection: strip, skip blanks, skip disabled, de-duplicate,
  preserve order.
- Unit test `SipEndpoint.start(settings, accounts=...)` appends STUN servers to
  the fake `EpConfig`.
- Unit test smoke success with fake PJSIP that records `libInit`,
  transport creation, codec enumeration, `libStart`, and `libDestroy`.
- Unit test smoke failure when required codecs are absent.
- Unit test the build timeout helper by code inspection is enough locally;
  release verification runs the rebuilt packaged executable.
- Run focused pytest, full pytest, full Windows build, direct packaged smoke,
  and GUI launch smoke.

## Self-Review

- No placeholders remain.
- The slice is bounded to startup smoke, timeout handling, and STUN config.
- It does not require credentials or external network access.
- It closes the build-hang risk and makes the release smoke match real SIP
  startup more closely.
