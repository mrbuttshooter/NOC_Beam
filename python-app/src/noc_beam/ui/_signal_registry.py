"""Signal registry: bind once, unbind together.

The recurring footgun in this codebase is connecting a slot to a
long-lived QObject (sip_events singleton, call_manager singleton,
etc.) from a short-lived widget and forgetting to disconnect when
the widget dies. Symptoms range from "the singleton fires into a
deleted C++ object" RuntimeError to (worse) the v3 audit's actual
test-hang root cause: every PhoneShell instance stacked another
lambda on the call_manager singleton until every CallRecord
mutation fanned out to 30+ dead PhoneShells.

This helper keeps the connect/disconnect pairs symmetric. Use it
like:

    self._signals = SignalRegistry()
    self._signals.bind(sip_events().registration_changed,
                       self._on_registration_changed)
    # ... at teardown:
    self._signals.unbind_all()

It also accepts lambdas (which the bound-method form of disconnect
can't reach by hashable identity, hence the historical "store the
lambda on self" pattern); the registry stores the original slot
reference so unbind_all can pass it back into disconnect().
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)


class SignalRegistry:
    """Holds a list of (signal, slot) pairs; symmetric bind/unbind."""

    def __init__(self) -> None:
        self._bindings: list[tuple[Any, Callable[..., Any]]] = []

    def bind(self, signal: Any, slot: Callable[..., Any]) -> None:
        """Connect slot to signal and remember the pairing for unbind."""
        try:
            signal.connect(slot)
        except Exception:
            log.exception("SignalRegistry.bind failed")
            return
        self._bindings.append((signal, slot))

    def unbind_all(self) -> None:
        """Disconnect every previously-bound (signal, slot) pair.

        Best-effort: a failure on any single disconnect (e.g. signal
        owner already destroyed, slot reference stale) is logged at
        debug and we continue with the rest. The registry is cleared
        either way so unbind_all() is idempotent.
        """
        for signal, slot in self._bindings:
            try:
                signal.disconnect(slot)
            except Exception:
                # Common during shutdown: the C++ owner of the signal
                # may already be gone. Not worth a warning at INFO+.
                log.debug("SignalRegistry: disconnect raised", exc_info=True)
        self._bindings.clear()

    def __len__(self) -> int:
        return len(self._bindings)
