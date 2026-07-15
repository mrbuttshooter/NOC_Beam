"""Modal showing every field of a single CDR plus Redial / Export actions.

Opened from HistoryView when the user double-clicks a row. The export
writes a single-row CSV with all fields so the user can drop it into
ticketing / analytics tools.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.history import CdrEntry
from noc_beam.ui.components import StatusPill

# NOTE: history_view imports CdrDetailDialog at module scope, so we
# must defer importing from it (resolve_account_label / show_export_toast
# / default_export_dir) until call-time to avoid a circular import.


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_duration(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:d}:{sec:02d}"


def _direction_label(entry: CdrEntry) -> str:
    if entry.direction == "in":
        return "Incoming (answered)" if entry.was_answered else "Incoming (missed)"
    return "Outgoing (answered)" if entry.was_answered else "Outgoing (failed)"


def _display_peer(entry: CdrEntry) -> str:
    clean = (getattr(entry, "dialed_uri", "") or "").strip()
    if clean:
        return clean
    raw = entry.peer_uri or ""
    s = raw.strip()
    for scheme in ("sip:", "sips:", "tel:"):
        if s.lower().startswith(scheme):
            s = s[len(scheme):]
            break
    s = s.split(";", 1)[0]
    if "@" in s:
        s = s.split("@", 1)[0] or s
    return s or "Unknown peer"


def _redial_target(entry: CdrEntry) -> str:
    return (getattr(entry, "dialed_uri", "") or "").strip() or (entry.peer_uri or "")


def _supplier_display(entry: CdrEntry) -> str:
    label = (getattr(entry, "supplier_label", "") or "").strip()
    if label:
        return label
    sid = (getattr(entry, "supplier_id", "") or "").strip()
    return f"C{sid}" if sid else "—"


class CdrDetailDialog(QDialog):
    """Single-CDR detail view. Emits redial_requested(peer_uri) on Redial."""

    redial_requested = Signal(str)

    def __init__(self, entry: CdrEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entry = entry
        self.setObjectName("CdrDetailDialog")
        self.setWindowTitle("Call detail")
        self.setMinimumWidth(420)

        # Header: peer URI big + direction underneath
        peer_lbl = QLabel(_display_peer(entry))
        peer_lbl.setObjectName("CdrDetailPeer")
        peer_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if entry.peer_uri:
            peer_lbl.setToolTip(f"Wire target: {entry.peer_uri}")

        if entry.was_answered:
            level = "ok"
        elif entry.direction == "in":
            level = "danger"
        else:
            level = "warn"
        direction_lbl = StatusPill(_direction_label(entry), level, self)

        # Field grid
        form = QFormLayout()
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Resolve the raw account UUID to its human label (display_name →
        # username → UUID fallback). Operators see meaningful A-numbers
        # instead of "7f3a8c44-..."; the raw UUID is preserved as a
        # tooltip for engineers who still need it.
        raw_account_id = entry.account_id or ""
        if raw_account_id:
            from noc_beam.ui.history_view import _resolve_account_label
            account_label = _resolve_account_label(raw_account_id)
        else:
            account_label = "—"
        account_tooltip = raw_account_id or ""

        # `mono` marks fields that are numbers / URIs / codes / timestamps
        # (rule 3 -> monospace). The Account field is a human name, so it
        # renders in the sans "CdrDetailValueText" role instead.
        for label, value, tooltip, mono in (
            ("Call ID", str(entry.call_id), "", True),
            ("Account",  account_label, account_tooltip, False),
            ("Supplier", _supplier_display(entry), "", True),
            ("Wire target", entry.peer_uri or "—", "", True),
            ("Started",  _fmt_ts(entry.started_at), "", True),
            ("Connected", _fmt_ts(entry.connected_at) if entry.connected_at else "—", "", True),
            ("Ended",    _fmt_ts(entry.ended_at), "", True),
            ("Duration", _fmt_duration(entry.duration_s), "", True),
            ("End code", f"{entry.end_code} {entry.end_reason}".strip() or "—", "", True),
            ("Codec",    entry.codec or "—", "", True),
        ):
            v = QLabel(value)
            v.setObjectName("CdrDetailValue" if mono else "CdrDetailValueText")
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setWordWrap(True)
            if tooltip:
                v.setToolTip(tooltip)
            form.addRow(label, v)

        # FAS analysis (shown only when a verdict was captured at end-of-call).
        self._fas_section_widgets: list = []
        fas_verdict = getattr(entry, "fas_verdict", "") or ""
        if fas_verdict:
            from noc_beam.ui.components import FasBadge

            fas_header = QLabel("FAS Analysis")
            fas_header.setObjectName("CdrDetailSectionHeader")
            self._fas_section_widgets.append(fas_header)

            fas_badge = FasBadge(fas_verdict, self)
            fas_badge.update_verdict(
                fas_verdict,
                float(getattr(entry, "fas_confidence", 0.0) or 0.0),
                getattr(entry, "fas_reasons", "") or "",
            )

            fas_form = QFormLayout()
            fas_form.setSpacing(6)
            fas_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            fas_form.addRow("Verdict:", fas_badge)
            conf = float(getattr(entry, "fas_confidence", 0.0) or 0.0)
            conf_lbl = QLabel(f"{conf:.0%}")
            conf_lbl.setObjectName("CdrDetailValue")
            fas_form.addRow("Confidence:", conf_lbl)
            reasons = getattr(entry, "fas_reasons", "") or "—"
            reasons_lbl = QLabel(reasons)
            reasons_lbl.setObjectName("CdrDetailValue")
            reasons_lbl.setWordWrap(True)
            reasons_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            fas_form.addRow("Signals:", reasons_lbl)
            self._fas_section_widgets.append(fas_form)

        # Action row: Redial (left) + Export CSV (right) + Close
        self.redial_btn = QPushButton("Redial")
        self.redial_btn.setObjectName("PrimaryAction")
        self.redial_btn.setEnabled(bool(_redial_target(entry)))
        self.redial_btn.clicked.connect(self._on_redial)

        # Redial is the single indigo primary in this dialog; Export is a
        # quiet secondary (outline/ghost) per the design system.
        self.export_btn = QPushButton("Export CSV…")
        self.export_btn.setObjectName("SecondaryAction")
        self.export_btn.clicked.connect(self._on_export)

        actions = QHBoxLayout()
        actions.addWidget(self.redial_btn)
        actions.addWidget(self.export_btn)
        actions.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(peer_lbl)
        layout.addWidget(direction_lbl)
        layout.addSpacing(8)
        layout.addLayout(form)
        # Inject FAS Analysis section below the field grid when present.
        for item in self._fas_section_widgets:
            layout.addSpacing(8)
            if hasattr(item, "addRow"):
                layout.addLayout(item)
            else:
                layout.addWidget(item)
        layout.addSpacing(8)
        layout.addLayout(actions)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    def _on_redial(self) -> None:
        target = _redial_target(self._entry)
        if target:
            self.redial_requested.emit(target)
            self.accept()

    @staticmethod
    def _csv_safe(value):
        """Prefix `'` when a CSV field starts with a character Excel/Sheets
        would interpret as a formula trigger. A malicious caller-id
        (peer_uri=`=cmd|...`) becomes a live formula on open otherwise --
        classic CSV-injection vector. Per OWASP CSV-injection guidance."""
        if value is None:
            return ""
        s = str(value)
        if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + s
        return s

    def _on_export(self) -> None:
        # Deferred import: history_view imports CdrDetailDialog at
        # module scope, so importing it at module scope here would cycle.
        from noc_beam.ui.history_view import _show_export_toast, default_export_dir
        default_name = f"cdr-{self._entry.call_id}-{int(self._entry.ended_at)}.csv"
        # Seed the dialog in Documents/NOC_BEAM/ to match every other
        # export entry-point in the app (history_view, test_runner_view).
        default_path = str(default_export_dir() / default_name)
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export CDR", default_path, "CSV (*.csv);;All Files (*.*)"
        )
        if not path:
            return
        safe = self._csv_safe
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "call_id", "account_id", "peer_uri", "direction",
                    "started_at", "connected_at", "ended_at",
                    "duration_s", "end_code", "end_reason", "codec",
                ])
                writer.writerow([
                    self._entry.call_id,
                    safe(self._entry.account_id),
                    safe(self._entry.peer_uri),
                    safe(self._entry.direction),
                    _fmt_ts(self._entry.started_at),
                    _fmt_ts(self._entry.connected_at) if self._entry.connected_at else "",
                    _fmt_ts(self._entry.ended_at),
                    f"{self._entry.duration_s:.1f}",
                    self._entry.end_code,
                    safe(self._entry.end_reason),
                    safe(self._entry.codec),
                ])
            # Non-blocking floating toast (click to open folder), matching
            # the UX everywhere else in the app — operators exporting many
            # CDRs shouldn't have to dismiss N modal popups.
            _show_export_toast(self, Path(path), 1)
        except Exception:
            _show_export_toast(self, Path(path), 0, failed=True)
