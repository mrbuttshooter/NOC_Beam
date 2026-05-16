"""SIP message trace viewer — Call-ID grouped (sngrep-style).

Each SIP dialog (group of messages sharing a Call-ID) collapses into ONE
row by default. The collapsed row shows a chip sequence like
``INVITE → 100 → 180 → 200 → ACK → BYE → 200``. Clicking expands to
individual message rows; clicking a message row reveals its full body.

Method-filter chips at the top (INVITE / REGISTER / OPTIONS / 4xx /
5xx) toggle whole dialogs in or out — the existing free-text filter
still works for finer-grained search.

Public API: chk_rx, chk_tx, filter_edit, export_btn, clear_btn are
exposed as instance attributes so TracePage (the wide-window wrapper)
can re-parent the toolbar widgets into its own toolbar row.

The phone_shell entry point uses TraceView directly (tab + pop-out
window). The wide-window dashboard wraps it via TracePage (with an
extra preset-chip bar) and TraceDrawer (slide-in companion on the
dial page).
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.paths import log_dir
from noc_beam.sip.events import sip_events


_trace_logger: logging.Logger | None = None
MAX_DIALOGS = 200       # cap at dialog level
MAX_MSGS_PER_DIALOG = 200  # also cap inside each dialog so long REGISTER refresh loops don't OOM

_CALLID_RX = re.compile(r"^Call-ID:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_STATUS_RX = re.compile(r"^SIP/2\.0\s+(\d{3})", re.IGNORECASE)
_METHOD_RX = re.compile(
    r"^(INVITE|REGISTER|ACK|BYE|CANCEL|OPTIONS|SUBSCRIBE|"
    r"NOTIFY|REFER|MESSAGE|PUBLISH|INFO|UPDATE|PRACK)\s+"
)
_FROM_RX = re.compile(r"^(?:From|f):\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_TO_RX = re.compile(r"^(?:To|t):\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _extract_header(rx: re.Pattern, body: str) -> str:
    if not body:
        return ""
    m = rx.search(body)
    return m.group(1).strip() if m else ""


def _persistent_trace_logger() -> logging.Logger:
    global _trace_logger
    if _trace_logger is not None:
        return _trace_logger
    logger = logging.getLogger("noc_beam.sip.trace.file")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_dir() / "sip_trace.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    _trace_logger = logger
    return logger


def _extract_call_id(body: str) -> str:
    if not body:
        return ""
    m = _CALLID_RX.search(body)
    return m.group(1).strip() if m else ""


def _extract_chip(body: str) -> tuple[str, str]:
    """Return (chip_text, level) where level is one of:
    'method' (request), 'ok' (2xx), 'progress' (1xx), 'warn' (3xx),
    'auth' (401/407), 'error' (4xx/5xx other), 'unknown'.
    """
    if not body:
        return ("?", "unknown")
    first = body.split("\n", 1)[0].strip()
    m_st = _STATUS_RX.match(first)
    if m_st:
        code = int(m_st.group(1))
        if 200 <= code < 300:
            return (str(code), "ok")
        if 100 <= code < 200:
            return (str(code), "progress")
        if 300 <= code < 400:
            return (str(code), "warn")
        if code in (401, 407):
            return (str(code), "auth")
        if 400 <= code < 600:
            return (str(code), "error")
        return (str(code), "unknown")
    m_meth = _METHOD_RX.match(first)
    if m_meth:
        return (m_meth.group(1), "method")
    return ("?", "unknown")


def _summarize(body: str) -> tuple[str, bool]:
    """First line of the SIP message + is-error flag."""
    if not body:
        return "(empty)", False
    first = body.split("\n", 1)[0].strip()
    is_error = False
    if first.startswith("SIP/"):
        parts = first.split(None, 2)
        if len(parts) >= 2:
            try:
                code = int(parts[1])
                is_error = code >= 400
            except ValueError:
                pass
    return first, is_error


@dataclass
class _Msg:
    ts: float
    direction: str
    peer: str
    body: str
    when: str
    summary: str
    is_error: bool
    chip: str
    chip_level: str


@dataclass
class _Dialog:
    call_id: str
    started_at: float
    msgs: list[_Msg] = field(default_factory=list)

    @property
    def short_id(self) -> str:
        return (self.call_id.split("@", 1)[0] or "?")[:10]

    @property
    def first_method(self) -> str:
        for m in self.msgs:
            if m.chip_level == "method":
                return m.chip
        return self.msgs[0].chip if self.msgs else "?"

    @property
    def has_error(self) -> bool:
        return any(m.is_error for m in self.msgs)

    @property
    def is_complete_ok(self) -> bool:
        # A dialog "ended cleanly" if it has a 2xx after a BYE or
        # finished with a 200 to its initial transaction.
        codes = [m.chip for m in self.msgs if m.chip_level == "ok"]
        return bool(codes)


# ---------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------
class _Chip(QLabel):
    """Tiny coloured chip used inside the dialog summary row."""

    def __init__(self, text: str, level: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("TraceChipPill")
        self.setProperty("level", level)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(18)


class TraceMsgRow(QFrame):
    """Single SIP message row inside an expanded dialog."""

    def __init__(
        self,
        msg: _Msg,
        prev_ts: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.msg = msg
        self.setObjectName("TraceMsgRow")
        self.setProperty("dir", "rx" if msg.direction == "RX" else "tx")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        ts = QLabel(msg.when, self)
        ts.setObjectName("TraceMsgTime")
        ts.setFixedWidth(64)

        # Delta-time vs previous message in the same dialog -- the
        # column NOC operators always reach for in sngrep.
        delta_text = ""
        if prev_ts is not None and prev_ts > 0:
            delta_ms = int(max(0.0, msg.ts - prev_ts) * 1000)
            if delta_ms < 1000:
                delta_text = f"+{delta_ms}ms"
            elif delta_ms < 60000:
                delta_text = f"+{delta_ms / 1000:.2f}s"
            else:
                delta_text = f"+{delta_ms // 60000}m{(delta_ms % 60000) // 1000}s"
        delta_lbl = QLabel(delta_text, self)
        delta_lbl.setObjectName("TraceMsgDelta")
        delta_lbl.setFixedWidth(64)
        delta_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        dir_lbl = QLabel(msg.direction, self)
        dir_lbl.setObjectName("TraceMsgDir")
        dir_lbl.setProperty("dir", "rx" if msg.direction == "RX" else "tx")
        dir_lbl.setFixedWidth(28)

        chip = _Chip(msg.chip, msg.chip_level, self)
        chip.setFixedWidth(48)

        summary = QLabel(msg.summary, self)
        summary.setObjectName("TraceMsgSummary")
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        summary.setMinimumWidth(120)
        # Operator-grade tooltip: From / To headers parsed out of the
        # body so the row is scannable without expanding.
        from_h = _extract_header(_FROM_RX, msg.body)
        to_h = _extract_header(_TO_RX, msg.body)
        tip_lines = [msg.summary]
        if from_h:
            tip_lines.append(f"From: {from_h}")
        if to_h:
            tip_lines.append(f"To: {to_h}")
        if msg.peer:
            tip_lines.append(f"Peer: {msg.peer}")
        summary.setToolTip("\n".join(tip_lines))

        head = QHBoxLayout()
        head.setContentsMargins(28, 4, 10, 4)
        head.setSpacing(8)
        head.addWidget(ts)
        head.addWidget(delta_lbl)
        head.addWidget(dir_lbl)
        head.addWidget(chip)
        head.addWidget(summary, 1)

        self.body = QTextEdit(self)
        self.body.setObjectName("TraceMsgBody")
        self.body.setReadOnly(True)
        self.body.setPlainText(msg.body)
        self.body.setVisible(False)
        self.body.setMaximumHeight(220)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(head)
        outer.addWidget(self.body)

        # Right-click context menu: Copy headers / Copy body / Copy
        # Call-ID. Operator basics.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    def mousePressEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.body.setVisible(not self.body.isVisible())
        super().mousePressEvent(event)

    def _show_menu(self, pos) -> None:
        menu = QMenu(self)
        cb = QGuiApplication.clipboard()
        act_body = QAction("Copy full message", menu)
        act_body.triggered.connect(lambda: cb.setText(self.msg.body))
        menu.addAction(act_body)
        # Headers only = strip body after first blank line.
        act_hdrs = QAction("Copy headers only", menu)
        def _copy_hdrs():
            hdrs = self.msg.body.split("\r\n\r\n", 1)[0].split("\n\n", 1)[0]
            cb.setText(hdrs)
        act_hdrs.triggered.connect(_copy_hdrs)
        menu.addAction(act_hdrs)
        cid = _extract_call_id(self.msg.body)
        if cid:
            act_cid = QAction(f"Copy Call-ID ({cid[:24]}…)" if len(cid) > 24 else f"Copy Call-ID ({cid})", menu)
            act_cid.triggered.connect(lambda: cb.setText(cid))
            menu.addAction(act_cid)
        menu.popup(self.mapToGlobal(pos))


class TraceDialogRow(QFrame):
    """Collapsible dialog row -- one per Call-ID."""

    def __init__(self, dialog: _Dialog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.dialog = dialog
        self._expanded = False
        self.setObjectName("TraceDialogRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_state_property()

        self._caret = QLabel("▸", self)
        self._caret.setObjectName("TraceCaret")
        self._caret.setFixedWidth(14)

        time_lbl = QLabel(dialog.msgs[0].when, self)
        time_lbl.setObjectName("TraceDialogTime")
        time_lbl.setFixedWidth(64)

        self._id_lbl = QLabel(dialog.short_id, self)
        self._id_lbl.setObjectName("TraceDialogId")
        self._id_lbl.setToolTip(dialog.call_id)
        self._id_lbl.setFixedWidth(88)

        self._chips_holder = QFrame(self)
        self._chips_holder.setObjectName("TraceChipsHolder")
        self._chips_layout = QHBoxLayout(self._chips_holder)
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        self._chips_layout.setSpacing(4)
        self._render_chips()

        head = QHBoxLayout()
        head.setContentsMargins(8, 4, 10, 4)
        head.setSpacing(8)
        head.addWidget(self._caret)
        head.addWidget(time_lbl)
        head.addWidget(self._id_lbl)
        head.addWidget(self._chips_holder, 1)

        # Expanded body holds individual TraceMsgRow widgets.
        self._body = QFrame(self)
        self._body.setObjectName("TraceDialogBody")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 4)
        self._body_layout.setSpacing(0)
        self._body.setVisible(False)
        self._msg_rows: list[TraceMsgRow] = []
        prev_ts: float | None = None
        for m in dialog.msgs:
            row = TraceMsgRow(m, prev_ts, self._body)
            self._msg_rows.append(row)
            self._body_layout.addWidget(row)
            prev_ts = m.ts

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(head)
        outer.addWidget(self._body)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    def append_msg(self, msg: _Msg) -> None:
        """Add a new message into this dialog (chip + sub-row).

        Caps msgs at MAX_MSGS_PER_DIALOG -- a long-running REGISTER
        refresh loop on a single Call-ID would otherwise grow the
        dialog forever and eventually OOM.
        """
        prev_ts = self.dialog.msgs[-1].ts if self.dialog.msgs else None
        self.dialog.msgs.append(msg)
        self._refresh_state_property()
        # Insert a new chip + arrow into the chips holder
        if self._chips_layout.count() > 0:
            arrow = QLabel("→", self._chips_holder)
            arrow.setObjectName("TraceChipArrow")
            self._chips_layout.addWidget(arrow)
        chip = _Chip(msg.chip, msg.chip_level, self._chips_holder)
        self._chips_layout.addWidget(chip)
        # Append a sub-row
        sub = TraceMsgRow(msg, prev_ts, self._body)
        self._msg_rows.append(sub)
        self._body_layout.addWidget(sub)
        # Trim oldest msgs (and their widgets) past the per-dialog cap.
        # hide() before deleteLater to avoid the PySide6 top-level-window
        # flash when a visible widget is reparented to None during
        # teardown.
        while len(self.dialog.msgs) > MAX_MSGS_PER_DIALOG:
            self.dialog.msgs.pop(0)
            old_row = self._msg_rows.pop(0)
            old_row.hide()
            old_row.deleteLater()
            # Drop the matching head chip + arrow pair.
            if self._chips_layout.count() >= 2:
                first = self._chips_layout.takeAt(0)
                w = first.widget()
                if w is not None:
                    w.hide()
                    w.deleteLater()
                second = self._chips_layout.itemAt(0)
                if second is not None:
                    sw = second.widget()
                    if sw is not None and sw.objectName() == "TraceChipArrow":
                        self._chips_layout.takeAt(0)
                        sw.hide()
                        sw.deleteLater()

    def _show_menu(self, pos) -> None:
        menu = QMenu(self)
        cb = QGuiApplication.clipboard()
        act_cid = QAction("Copy Call-ID", menu)
        act_cid.triggered.connect(lambda: cb.setText(self.dialog.call_id))
        menu.addAction(act_cid)
        act_dialog = QAction("Copy whole dialog", menu)
        def _copy_dialog():
            buf = [f"=== Call-ID: {self.dialog.call_id} ==="]
            for m in self.dialog.msgs:
                buf.append(f"\n[{m.when}] {m.direction}  {m.peer}")
                buf.append(m.body)
            cb.setText("\n".join(buf))
        act_dialog.triggered.connect(_copy_dialog)
        menu.addAction(act_dialog)
        menu.popup(self.mapToGlobal(pos))

    def _render_chips(self) -> None:
        # Initial render -- chips with arrows between
        for i, m in enumerate(self.dialog.msgs):
            if i > 0:
                arrow = QLabel("→", self._chips_holder)
                arrow.setObjectName("TraceChipArrow")
                self._chips_layout.addWidget(arrow)
            chip = _Chip(m.chip, m.chip_level, self._chips_holder)
            self._chips_layout.addWidget(chip)
        self._chips_layout.addStretch(1)

    def _refresh_state_property(self) -> None:
        if self.dialog.has_error:
            state = "error"
        elif self.dialog.is_complete_ok:
            state = "ok"
        else:
            state = "pending"
        self.setProperty("state", state)
        self.style().unpolish(self); self.style().polish(self)

    def mousePressEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self._expanded = not self._expanded
            self._body.setVisible(self._expanded)
            self._caret.setText("▾" if self._expanded else "▸")
        super().mousePressEvent(event)

    # Filter helpers --------------------------------------------------
    def matches_method(self, methods: set[str], statuses: set[str]) -> bool:
        """True if this dialog contains any message matching the active
        method/status filters. Empty filter sets mean 'show all'."""
        if not methods and not statuses:
            return True
        for m in self.dialog.msgs:
            if m.chip_level == "method" and m.chip in methods:
                return True
            if m.chip_level in {"ok", "auth", "error", "warn", "progress"}:
                if m.chip in statuses:
                    return True
                # 4xx / 5xx bucket matches
                try:
                    code = int(m.chip)
                    if "4xx" in statuses and 400 <= code < 500:
                        return True
                    if "5xx" in statuses and 500 <= code < 600:
                        return True
                except ValueError:
                    pass
        return False

    def matches_text(self, flt: str) -> bool:
        if not flt:
            return True
        flt_l = flt.lower()
        for m in self.dialog.msgs:
            if flt_l in m.body.lower() or flt_l in m.peer.lower():
                return True
        if flt_l in self.dialog.call_id.lower():
            return True
        return False

    def has_direction(self, rx: bool, tx: bool) -> bool:
        if rx and tx:
            return True
        for m in self.dialog.msgs:
            if rx and m.direction == "RX":
                return True
            if tx and m.direction == "TX":
                return True
        return False


# ---------------------------------------------------------------------
# Main view
# ---------------------------------------------------------------------
class TraceView(QWidget):
    export_failed = Signal(str)

    METHOD_CHIPS = ("INVITE", "REGISTER", "OPTIONS", "BYE")
    STATUS_BUCKETS = ("4xx", "5xx")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # ---- Existing toolbar widgets (kept for TracePage compat) ----
        self.chk_rx = QToolButton()
        self.chk_rx.setText("RX")
        self.chk_rx.setCheckable(True)
        self.chk_rx.setChecked(True)
        self.chk_rx.setObjectName("TraceDirToggle")
        self.chk_rx.setProperty("dir", "rx")
        self.chk_tx = QToolButton()
        self.chk_tx.setText("TX")
        self.chk_tx.setCheckable(True)
        self.chk_tx.setChecked(True)
        self.chk_tx.setObjectName("TraceDirToggle")
        self.chk_tx.setProperty("dir", "tx")
        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("TraceFilter")
        self.filter_edit.setPlaceholderText("Filter (Call-ID, body, peer)")

        # Pause/resume button -- demo blocker. While paused, incoming
        # messages buffer in self._paused_buffer and the screen does
        # not scroll-jump on every packet.
        self.pause_btn = QToolButton()
        self.pause_btn.setObjectName("TracePauseBtn")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setText("⏸ Pause")
        self.pause_btn.setToolTip("Pause auto-scroll + queue new messages")
        self.pause_btn.toggled.connect(self._on_pause_toggled)

        # LIVE indicator next to the pause button. Visible when streaming,
        # hidden (or "PAUSED") when paused. The pulse comes from QSS via
        # an animated property; if that's not present a static dot still
        # reads as live.
        self.live_label = QLabel("● LIVE")
        self.live_label.setObjectName("TraceLiveBadge")
        self.live_label.setProperty("state", "live")
        self.live_label.setToolTip("Streaming SIP traffic in real time")

        self.export_btn = QPushButton("Export…")
        self.clear_btn = QPushButton("Clear")

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 4, 8, 4)
        toolbar.setSpacing(6)
        toolbar.addWidget(self.chk_rx)
        toolbar.addWidget(self.chk_tx)
        toolbar.addWidget(self.filter_edit, 1)
        toolbar.addWidget(self.live_label)
        toolbar.addWidget(self.pause_btn)
        toolbar.addWidget(self.export_btn)
        toolbar.addWidget(self.clear_btn)

        # ---- Method-chip filter row ----------------------------------
        chip_bar = QHBoxLayout()
        chip_bar.setContentsMargins(8, 0, 8, 4)
        chip_bar.setSpacing(4)
        self._method_chips: dict[str, QToolButton] = {}
        for label in (*self.METHOD_CHIPS, *self.STATUS_BUCKETS):
            btn = QToolButton()
            btn.setObjectName("TraceFastChip")
            btn.setText(label)
            btn.setCheckable(True)
            btn.toggled.connect(self._reapply_filters)
            self._method_chips[label] = btn
            chip_bar.addWidget(btn)
        chip_bar.addStretch(1)

        # ---- Scroll area for the dialog list -------------------------
        self._dialogs: OrderedDict[str, TraceDialogRow] = OrderedDict()

        self._rows_holder = QFrame(self)
        self._rows_holder.setObjectName("TraceRowList")
        self._rows_layout = QVBoxLayout(self._rows_holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)

        self._empty = QLabel(
            "Waiting for SIP traffic.\n\n"
            "Once a SIP account registers or a call is placed,\n"
            "every signalling dialog will land here.",
            self._rows_holder,
        )
        self._empty.setObjectName("TraceEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._rows_layout.insertWidget(0, self._empty)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("TraceScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._rows_holder)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(toolbar)
        layout.addLayout(chip_bar)
        layout.addWidget(self._scroll, 1)

        # ---- Pause buffer --------------------------------------------
        # While paused, incoming messages accumulate here and flush on
        # resume. Bounded so a long pause can't OOM.
        from collections import deque
        self._paused: bool = False
        self._paused_buffer: deque = deque(maxlen=500)

        # ---- Wires ---------------------------------------------------
        self.clear_btn.clicked.connect(self._on_clear)
        self.export_btn.clicked.connect(self._on_export)
        self.chk_rx.toggled.connect(self._reapply_filters)
        self.chk_tx.toggled.connect(self._reapply_filters)
        # Debounce the per-keystroke filter pass: every char typed
        # used to walk the entire row collection (1000+ rows) and
        # toggle each one's visibility, freezing the GUI during
        # heavy traces. 150ms timer coalesces a burst of keystrokes
        # into one re-filter pass.
        self._filter_debounce = QTimer(self)
        self._filter_debounce.setSingleShot(True)
        self._filter_debounce.setInterval(150)
        self._filter_debounce.timeout.connect(self._reapply_filters)
        self.filter_edit.textChanged.connect(
            lambda _t: self._filter_debounce.start()
        )
        # CRITICAL: SIP messages arrive on PJSIP worker threads. Without
        # QueuedConnection the slot would mutate Qt widgets from the
        # wrong thread -> SIGSEGV / heap corruption. Forcing
        # QueuedConnection guarantees the slot runs on the main GUI
        # thread regardless of which thread emitted the signal.
        sip_events().sip_message.connect(
            self._on_sip_message,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # Disconnect on destruction. Without this, every TraceView
        # constructed (in-shell + popup + any future) leaves a permanent
        # subscriber on the singleton sip_events. A single open/close
        # cycle of the trace popup doubles per-message processing for
        # the rest of the session.
        self.destroyed.connect(self._disconnect_signals)

    def _disconnect_signals(self, *_args) -> None:
        try:
            sip_events().sip_message.disconnect(self._on_sip_message)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _on_sip_message(self, ts: float, direction: str, peer: str, body: str) -> None:
        if self._paused:
            # Buffer until resume. The deque is bounded so we don't OOM
            # during a long pause; the oldest dropped entries are still
            # in the on-disk RotatingFileHandler log.
            self._paused_buffer.append((ts, direction, peer, body))
            return
        self._ingest(ts, direction, peer, body)

    def _ingest(self, ts: float, direction: str, peer: str, body: str) -> None:
        when = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        try:
            _persistent_trace_logger().info(
                "[%s] %s  %s\n%s", when, direction, peer, body,
            )
        except Exception:
            pass

        summary, is_error = _summarize(body)
        chip, chip_level = _extract_chip(body)
        msg = _Msg(
            ts=ts, direction=direction, peer=peer, body=body,
            when=when, summary=summary, is_error=is_error,
            chip=chip, chip_level=chip_level,
        )
        call_id = _extract_call_id(body) or f"_orphan_{ts}"

        existing = self._dialogs.get(call_id)
        if existing is not None:
            existing.append_msg(msg)
            self._apply_filters_to(existing)
        else:
            dialog = _Dialog(call_id=call_id, started_at=ts, msgs=[msg])
            row = TraceDialogRow(dialog, self._rows_holder)
            self._dialogs[call_id] = row
            insert_at = self._rows_layout.count() - 1
            self._rows_layout.insertWidget(insert_at, row)
            self._apply_filters_to(row)
            # Cap dialog count. hide() before deleteLater to avoid the
            # top-level-window flash.
            while len(self._dialogs) > MAX_DIALOGS:
                old_id, old_row = next(iter(self._dialogs.items()))
                del self._dialogs[old_id]
                old_row.hide()
                old_row.deleteLater()

        self._empty.setVisible(False)

        # Auto-scroll to bottom ONLY when the user is already near the
        # bottom. If they've scrolled up to inspect an expanded SIP
        # message, jamming them back down on every incoming packet
        # closes their reading focus -- the "clicking expand collapses
        # the whole trace" UX bug.
        bar = self._scroll.verticalScrollBar()
        near_bottom = (bar.maximum() - bar.value()) < 80
        if near_bottom:
            bar.setValue(bar.maximum())

    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = bool(paused)
        if paused:
            self.pause_btn.setText("▶ Resume")
            self.pause_btn.setToolTip(
                "Resume streaming + flush queued messages"
            )
            self.live_label.setText("● PAUSED")
            self.live_label.setProperty("state", "paused")
        else:
            self.pause_btn.setText("⏸ Pause")
            self.pause_btn.setToolTip(
                "Pause auto-scroll + queue new messages"
            )
            self.live_label.setText("● LIVE")
            self.live_label.setProperty("state", "live")
            # Drain the buffer in arrival order.
            while self._paused_buffer:
                args = self._paused_buffer.popleft()
                self._ingest(*args)
        # Re-polish so the QSS attribute-selector applies.
        self.live_label.style().unpolish(self.live_label)
        self.live_label.style().polish(self.live_label)

    def _on_clear(self) -> None:
        # Confirm before nuking -- protection against demo mis-clicks.
        reply = QMessageBox.question(
            self,
            "Clear trace",
            "Discard all captured SIP dialogs from this session?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for row in self._dialogs.values():
            row.hide()
            row.deleteLater()
        self._dialogs.clear()
        self._paused_buffer.clear()
        self._empty.setVisible(True)

    # ------------------------------------------------------------------
    def _active_method_filters(self) -> tuple[set[str], set[str]]:
        methods = {m for m in self.METHOD_CHIPS if self._method_chips[m].isChecked()}
        statuses = {s for s in self.STATUS_BUCKETS if self._method_chips[s].isChecked()}
        return methods, statuses

    def _reapply_filters(self) -> None:
        for row in self._dialogs.values():
            self._apply_filters_to(row)

    def _apply_filters_to(self, row: TraceDialogRow) -> None:
        rx = self.chk_rx.isChecked()
        tx = self.chk_tx.isChecked()
        methods, statuses = self._active_method_filters()
        flt = self.filter_edit.text().strip()
        ok = (
            row.has_direction(rx, tx)
            and row.matches_method(methods, statuses)
            and row.matches_text(flt)
        )
        row.setVisible(ok)

    # ------------------------------------------------------------------
    def _on_export(self) -> None:
        default = log_dir() / f"sip_trace_export_{datetime.now():%Y%m%d_%H%M%S}.log"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export current trace", str(default), "Log files (*.log);;All files (*.*)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for row in self._dialogs.values():
                    if not row.isVisible():
                        continue
                    d = row.dialog
                    fh.write(f"=== Call-ID: {d.call_id} ===\n")
                    for m in d.msgs:
                        fh.write(f"[{m.when}] {m.direction}  {m.peer}\n{m.body}\n\n")
        except Exception as e:
            logging.getLogger(__name__).exception("Trace export failed")
            self.export_failed.emit(f"Export failed: {e}")
