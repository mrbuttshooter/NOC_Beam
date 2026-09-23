"""Loads the pjsua2 module from the best available source.

Order of preference:
  1. The custom-built native extension at noc_beam._native.pjsua2 (full
     feature set: G.729, SRTP, TLS, BCG729).
  2. The public 'pjsua2' pip wheel (limited features, useful for UI dev).
  3. A stub module — pjsua2 unavailable; UI still works, calls disabled.

`PJSUA2_AVAILABLE` is True only for cases 1 and 2.
`PJSUA2_SOURCE` is one of "native", "wheel", or "stub".
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)

pj: Any
PJSUA2_AVAILABLE: bool
PJSUA2_SOURCE: str
PJSUA2_LOAD_ERROR: str


def _try_load() -> tuple[Any, bool, str, str]:
    errors: list[str] = []

    # 1) custom-built native
    try:
        from noc_beam._native.pjsua2 import pjsua2 as _pj  # type: ignore

        log.info("pjsua2 loaded from custom native build (noc_beam._native.pjsua2)")
        return _pj, True, "native", ""
    except Exception as e:
        errors.append(f"native: {e}")
        log.debug("Custom pjsua2 not available: %s", e)

    # 2) public wheel
    try:
        import pjsua2 as _pj  # type: ignore

        log.info("pjsua2 loaded from public 'pjsua2' wheel")
        return _pj, True, "wheel", ""
    except Exception as e:
        errors.append(f"wheel: {e}")
        log.warning("pjsua2 not available — running in UI-only stub mode: %s", e)

    # 3) stub
    return _make_stub(), False, "stub", "; ".join(errors)


def _make_stub() -> Any:
    """Tiny stub so imports succeed; any real call raises a clear error."""

    class _StubBase:
        def __init__(self, *a: Any, **kw: Any) -> None:
            raise RuntimeError(
                "pjsua2 native module not loaded. Build PJSIP for your platform "
                "(see build/build_pjsip_windows.md) or install the 'pjsua2' pip wheel."
            )

    return SimpleNamespace(
        Endpoint=_StubBase,
        EpConfig=_StubBase,
        Account=_StubBase,
        AccountConfig=_StubBase,
        Call=_StubBase,
        CallOpParam=_StubBase,
        TransportConfig=_StubBase,
        AuthCredInfo=_StubBase,
        PJSIP_TRANSPORT_UDP=1,
        PJSIP_TRANSPORT_TCP=2,
        PJSIP_TRANSPORT_TLS=3,
        PJSUA_INVALID_ID=-1,
    )


def _pj_error_str(self: Any) -> str:
    """Readable text for a pjsua2.Error.

    SWIG's pjsua2.Error stringifies to "" -- the detail lives in its
    `reason` / `title` / `status` attributes. Every `log.exception(...)`
    in the app therefore ended with a bare "pjsua2.Error" line, and the
    "Call failed" dialog showed "Error (no message)". Render e.g.
    "INVITE session already terminated (PJSIP_ESESSIONTERMINATED)
    [pjsua_call_answer2, status 171140]".
    """
    try:
        reason = str(getattr(self, "reason", "") or "").strip()
        title = str(getattr(self, "title", "") or "").strip()
        status = int(getattr(self, "status", 0) or 0)
    except Exception:
        return "pjsua2 error"
    func = title.split("(", 1)[0].strip() if title else ""
    detail = ", ".join(p for p in (func, f"status {status}" if status else "") if p)
    text = reason or "pjsua2 error"
    return f"{text} [{detail}]" if detail else text


def _install_error_str(module: Any) -> None:
    err = getattr(module, "Error", None)
    if not isinstance(err, type):
        return
    try:
        err.__str__ = _pj_error_str
    except Exception:
        log.debug("could not attach readable __str__ to pjsua2.Error", exc_info=True)


pj, PJSUA2_AVAILABLE, PJSUA2_SOURCE, PJSUA2_LOAD_ERROR = _try_load()
if PJSUA2_AVAILABLE:
    _install_error_str(pj)
