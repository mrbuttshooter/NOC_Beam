"""Single-instance *activation* IPC.

The authoritative single-instance guard lives in ``app.py`` and is a Win32
named mutex -- that stays the source of truth for "am I the first instance?".
This module adds only the *activation side channel* on top of it: a way for a
second launch to poke the already-running first instance so it can raise its
window, instead of the old behaviour of scolding the user with an
"already running, check your tray" modal and quitting.

Design:

* First instance: after its main window exists it starts an
  :class:`ActivationServer` -- a thin wrapper over ``QLocalServer`` listening
  on a fixed name. When a second instance connects and sends the activation
  token, the server invokes a caller-supplied callback (which raises/restores
  the main window). All of this runs on the GUI thread because ``QLocalServer``
  delivers ``newConnection`` on the thread whose event loop owns it, and we
  construct it from the GUI thread.

* Second instance: the mutex told it another instance is already running, so
  it calls :func:`signal_existing_instance`, which connects a short-lived
  ``QLocalSocket``, writes the token, flushes and disconnects, then the caller
  exits 0 silently. If the connection fails (first instance hung or still
  starting up) we log a warning and return False -- the caller still exits 0
  with no dialog. A second instance must NEVER surface UI.

Named local sockets can be left orphaned on disk (AF_UNIX path / named pipe)
if a prior run crashed without an orderly shutdown, which makes ``listen()``
fail with ``AddressInUseError``. We defensively ``removeServer`` before
listening to clear such a stale endpoint.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

# The rendezvous name shared by first (server) and second (client) instance.
# Kept distinct from the mutex name -- the mutex is the guard, this is the
# doorbell.
ACTIVATION_SERVER_NAME = "NOC_Beam_Activate"

# Wire payload the second instance writes. The trailing newline lets the
# server treat it as a complete, framed message; ``ACTIVATION_TOKEN`` is the
# substring we actually match on so we're tolerant of how the bytes arrive
# across one or more readyRead chunks.
ACTIVATION_PAYLOAD = b"activate\n"
ACTIVATION_TOKEN = b"activate"

# Default connect / write timeout for the second instance. Short on purpose:
# if the first instance can't answer the doorbell in half a second we give up
# and exit silently rather than making the user wait on a second launch.
_DEFAULT_TIMEOUT_MS = 500


class ActivationServer(QObject):
    """Listens for activation pokes from later launches and fires a callback.

    The first instance owns exactly one of these for the process lifetime.
    Construction performs the stale-socket cleanup + ``listen()``; check
    :attr:`is_listening` to confirm the endpoint came up (a failure is logged
    and non-fatal -- the app still runs, it just won't be poke-able).
    """

    def __init__(
        self,
        on_activate: Callable[[], None],
        name: str = ACTIVATION_SERVER_NAME,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._on_activate = on_activate
        self._name = name
        self._server = QLocalServer(self)
        # Per-connection byte buffers keyed by the QLocalSocket. Small payload,
        # but readyRead can in principle deliver the token split across chunks,
        # so we accumulate until we see the token.
        self._buffers: dict[QLocalSocket, bytes] = {}

        self._server.newConnection.connect(self._on_new_connection)
        self._listen()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self._name

    @property
    def is_listening(self) -> bool:
        return self._server.isListening()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _listen(self) -> None:
        """Clear any stale endpoint and start listening. Best-effort."""
        try:
            # Remove a socket left behind by a crashed prior run so listen()
            # doesn't fail with AddressInUseError. removeServer is a no-op if
            # nothing is there, and safe to call before every listen.
            QLocalServer.removeServer(self._name)
        except Exception:
            # removeServer failing is not fatal -- listen() below will tell us
            # whether the name is actually usable.
            log.exception("removeServer(%r) raised; continuing to listen", self._name)

        try:
            if not self._server.listen(self._name):
                # One more attempt after an explicit cleanup, in case another
                # (truly dead) endpoint was racing us. If it still fails we log
                # and carry on window-less-poke -- the mutex still guards data.
                QLocalServer.removeServer(self._name)
                if not self._server.listen(self._name):
                    log.warning(
                        "Activation server could not listen on %r: %s",
                        self._name,
                        self._server.errorString(),
                    )
                    return
            log.info("Activation server listening on %r", self._name)
        except Exception:
            log.exception("Activation server listen(%r) raised", self._name)

    def close(self) -> None:
        """Stop listening and drop buffers. Idempotent."""
        try:
            self._server.close()
        except Exception:
            log.exception("Activation server close raised")
        self._buffers.clear()

    # ------------------------------------------------------------------
    # Connection handling (all on the GUI thread)
    # ------------------------------------------------------------------
    def _on_new_connection(self) -> None:
        # Drain every pending connection; a burst of double-launches could
        # queue more than one.
        while True:
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            self._buffers[socket] = b""
            # Bind the socket into each slot so we know which connection fired.
            socket.readyRead.connect(lambda s=socket: self._on_ready_read(s))
            socket.disconnected.connect(lambda s=socket: self._cleanup_socket(s))
            # Data may already be buffered by the time newConnection lands.
            self._on_ready_read(socket)

    def _on_ready_read(self, socket: QLocalSocket) -> None:
        try:
            chunk = bytes(socket.readAll().data())
        except Exception:
            log.exception("Reading activation payload raised")
            return
        if not chunk:
            return

        buf = self._buffers.get(socket, b"") + chunk
        if ACTIVATION_TOKEN in buf:
            # Complete: consume the buffer, fire once, and close our side.
            self._buffers.pop(socket, None)
            self._invoke_callback()
            try:
                socket.disconnectFromServer()
            except Exception:
                pass
        else:
            self._buffers[socket] = buf

    def _cleanup_socket(self, socket: QLocalSocket) -> None:
        self._buffers.pop(socket, None)
        try:
            # nextPendingConnection() returns a socket parented to the server.
            # Detach it before deleteLater so ownership is unambiguous: the
            # deferred-delete frees it, and the server's own destruction can't
            # double-free a child whose DeferredDelete is still queued.
            socket.setParent(None)
            socket.deleteLater()
        except Exception:
            pass

    def _invoke_callback(self) -> None:
        # Never let a misbehaving callback take down the event loop / the app.
        try:
            self._on_activate()
        except Exception:
            log.exception("Activation callback raised")


def signal_existing_instance(
    name: str = ACTIVATION_SERVER_NAME,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
) -> bool:
    """Poke the already-running first instance to raise its window.

    Called by the *second* instance once the mutex has said another instance
    already owns the process. Connects a short-lived ``QLocalSocket``, writes
    the activation token, flushes, and disconnects.

    Returns True if the token was written, False on any failure (no server, a
    hung/starting-up first instance, a timeout). The caller exits 0 either way
    -- a False result must NOT produce a modal; the log line is the breadcrumb.

    A ``QCoreApplication`` (or ``QApplication``) must already exist so the
    socket has an event dispatcher for its blocking ``waitFor*`` calls.
    """
    socket = QLocalSocket()
    try:
        socket.connectToServer(name)
        if not socket.waitForConnected(timeout_ms):
            log.warning(
                "Could not reach running instance on %r (%s); exiting silently",
                name,
                socket.errorString(),
            )
            return False

        socket.write(ACTIVATION_PAYLOAD)
        # flush() pushes as much as possible synchronously. Only *wait* if
        # something is still queued -- waitForBytesWritten returns False when
        # nothing is pending (flush already drained it), so calling it
        # unconditionally would be a false-negative "write failed".
        socket.flush()
        if socket.bytesToWrite() > 0 and not socket.waitForBytesWritten(timeout_ms):
            log.warning("Timed out writing activation payload to %r", name)
            return False

        socket.disconnectFromServer()
        # Give the peer a moment to notice; harmless if already gone.
        if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            socket.waitForDisconnected(timeout_ms)
        log.info("Signalled running instance on %r to activate", name)
        return True
    except Exception:
        log.exception("Signalling existing instance on %r failed", name)
        return False
    finally:
        try:
            socket.close()
        except Exception:
            pass
