"""Loads the pjsua2 module from the best available source.

Order of preference:
  1. The custom-built native extension at noc_beam._native.pjsua2 (full
     feature set: G.729, SRTP, TLS, BCG729).
  2. The public 'pjsua2' pip wheel (limited features, useful for UI dev).
  3. A stub module — pjsua2 unavailable; UI still works, calls disabled.

`PJSUA2_AVAILABLE` is True only for cases 1 and 2.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)

pj: Any
PJSUA2_AVAILABLE: bool


def _try_load() -> tuple[Any, bool]:
    # 1) custom-built native
    try:
        from noc_beam._native.pjsua2 import pjsua2 as _pj  # type: ignore

        log.info("pjsua2 loaded from custom native build (noc_beam._native.pjsua2)")
        return _pj, True
    except Exception as e:
        log.debug("Custom pjsua2 not available: %s", e)

    # 2) public wheel
    try:
        import pjsua2 as _pj  # type: ignore

        log.info("pjsua2 loaded from public 'pjsua2' wheel")
        return _pj, True
    except Exception as e:
        log.warning("pjsua2 not available — running in UI-only stub mode: %s", e)

    # 3) stub
    return _make_stub(), False


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


pj, PJSUA2_AVAILABLE = _try_load()
