"""Tests for the single-instance activation IPC (single_instance.py).

The Win32 mutex in app.py is the authoritative guard and is not exercised
here (it's a machine-global side effect). What we test is the *activation*
side channel that replaces the old "already running" modal:

  1. The first instance's QLocalServer comes up listening.
  2. The stale-endpoint cleanup path (removeServer before listen) runs.
  3. A second instance's message triggers the activation callback.
  4. signal_existing_instance() returns True when a server answers, and
     False (never raising, never a dialog) when nothing is listening.

Headless-safe: forces the offscreen Qt platform like the other UI tests, and
uses QLocalServer + QLocalSocket in a single process -- no display required.
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from noc_beam.single_instance import (  # noqa: E402
    ACTIVATION_PAYLOAD,
    ActivationServer,
    signal_existing_instance,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture()
def server_name():
    # Unique per test so leftover named pipes / socket files from one test
    # can never collide with another.
    return f"NOC_Beam_Test_{os.getpid()}_{uuid.uuid4().hex[:8]}"


def _pump_until(qapp, predicate, timeout=2.0):
    """Spin the Qt event loop until predicate() is true or we time out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return predicate()


def test_server_listens(qapp, server_name):
    server = ActivationServer(lambda: None, name=server_name)
    try:
        assert server.is_listening
        assert server.name == server_name
    finally:
        server.close()


def test_stale_server_cleanup_before_listen(qapp, server_name, monkeypatch):
    """The server must call QLocalServer.removeServer(name) before listening.

    That's what clears an endpoint orphaned by a crashed prior run. We spy on
    removeServer (still delegating to the real one) to prove the cleanup runs
    with the right name, and confirm the server ends up listening.
    """
    calls: list[str] = []
    real_remove = QLocalServer.removeServer

    def _spy_remove(name):
        calls.append(name)
        return real_remove(name)

    monkeypatch.setattr(QLocalServer, "removeServer", staticmethod(_spy_remove))

    server = ActivationServer(lambda: None, name=server_name)
    try:
        assert server_name in calls, "removeServer was not called before listen"
        assert server.is_listening
    finally:
        server.close()


def test_stale_endpoint_is_reclaimed(qapp, server_name):
    """A brand-new server on a name a prior server used still comes up.

    Simulates the crash-and-relaunch case: a first server exists, then a
    second is created on the same name. The second must reclaim the name and
    listen rather than failing.
    """
    first = ActivationServer(lambda: None, name=server_name)
    assert first.is_listening
    first.close()

    second = ActivationServer(lambda: None, name=server_name)
    try:
        assert second.is_listening
    finally:
        second.close()


def test_activation_callback_fires_on_message(qapp, server_name):
    fired = {"count": 0}
    server = ActivationServer(lambda: fired.__setitem__("count", fired["count"] + 1),
                              name=server_name)
    try:
        assert server.is_listening

        client = QLocalSocket()
        client.connectToServer(server_name)
        assert client.waitForConnected(1000), client.errorString()
        client.write(ACTIVATION_PAYLOAD)
        client.flush()
        # Note: waitForBytesWritten() returns False when flush() already
        # drained the buffer, so we don't assert on it -- we assert on the
        # observable effect (the callback firing) instead.
        if client.bytesToWrite() > 0:
            client.waitForBytesWritten(1000)

        assert _pump_until(qapp, lambda: fired["count"] >= 1), "callback never fired"
        assert fired["count"] == 1

        client.disconnectFromServer()
        client.close()
    finally:
        server.close()


def test_callback_exception_does_not_propagate(qapp, server_name):
    """A throwing activation callback must be swallowed (never crash the loop).

    Uses the real server + a plain client socket (no cross-thread blocking),
    driving the handshake through the event loop.
    """
    fired = {"hit": False}

    def _boom():
        fired["hit"] = True
        raise RuntimeError("callback blew up")

    server = ActivationServer(_boom, name=server_name)
    try:
        assert server.is_listening
        client = QLocalSocket()
        client.connectToServer(server_name)
        assert client.waitForConnected(1000), client.errorString()
        client.write(ACTIVATION_PAYLOAD)
        client.flush()
        # The event loop must keep pumping without the exception propagating.
        assert _pump_until(qapp, lambda: fired["hit"]), "callback never ran"
        client.disconnectFromServer()
        client.close()
    finally:
        server.close()


# ---------------------------------------------------------------------------
# signal_existing_instance() unit tests -- the client side is exercised with a
# fake QLocalSocket so we can assert its logic deterministically without a
# cross-thread live server (QLocalSocket blocking calls can't be driven from
# the same thread as an in-process server without racing the pipe on Windows).
# ---------------------------------------------------------------------------
class _FakeSocket:
    """Minimal stand-in for QLocalSocket capturing what the client did."""

    def __init__(self, connect_ok=True):
        self._connect_ok = connect_ok
        self.written = b""
        self.flushed = False
        self.disconnected = False
        self.closed = False

    def connectToServer(self, name):  # noqa: N802
        self.name = name

    def waitForConnected(self, timeout):  # noqa: N802
        return self._connect_ok

    def write(self, data):
        self.written += bytes(data)
        return len(data)

    def flush(self):
        self.flushed = True
        return True

    def bytesToWrite(self):  # noqa: N802
        return 0  # flush() drained everything

    def disconnectFromServer(self):  # noqa: N802
        self.disconnected = True

    def state(self):
        return QLocalSocket.LocalSocketState.UnconnectedState

    def errorString(self):  # noqa: N802
        return "fake error"

    def close(self):
        self.closed = True


def _patch_socket_factory(monkeypatch, fake):
    """Replace single_instance.QLocalSocket with a factory returning `fake`.

    signal_existing_instance() references both ``QLocalSocket()`` (the
    constructor) and ``QLocalSocket.LocalSocketState`` (the enum), so the
    replacement must be callable AND carry the enum.
    """
    import noc_beam.single_instance as si

    def _factory():
        return fake

    _factory.LocalSocketState = QLocalSocket.LocalSocketState
    monkeypatch.setattr(si, "QLocalSocket", _factory)
    return si


def test_signal_writes_payload_on_success(monkeypatch, server_name):
    fake = _FakeSocket(connect_ok=True)
    si = _patch_socket_factory(monkeypatch, fake)
    assert si.signal_existing_instance(server_name) is True
    assert fake.written == ACTIVATION_PAYLOAD
    assert fake.name == server_name
    assert fake.disconnected is True
    assert fake.closed is True


def test_signal_returns_false_when_connect_fails(monkeypatch, server_name):
    fake = _FakeSocket(connect_ok=False)
    si = _patch_socket_factory(monkeypatch, fake)
    assert si.signal_existing_instance(server_name, timeout_ms=50) is False
    assert fake.written == b""  # never wrote when we couldn't connect
    assert fake.closed is True  # still cleaned up


def test_signal_existing_instance_no_server_returns_false(qapp, server_name):
    """Real end-to-end: with nothing listening we return False, never raise.

    waitForConnected fails fast (ServerNotFoundError), safe to run inline.
    """
    assert signal_existing_instance(server_name, timeout_ms=300) is False
