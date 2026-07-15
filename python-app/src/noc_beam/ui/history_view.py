"""Call history as a Bria-style row list (not a table).

Each row shows the peer URI prominently with a small meta line
underneath. Double-click redials the peer (Bria parity); the per-row
info button or right-click context menu opens CdrDetailDialog with
every field plus Export CSV.
"""
from __future__ import annotations

import time

from datetime import datetime, timedelta

import csv
import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)


def _csv_safe(value) -> str:
    """Prefix `'` when a CSV field starts with a character Excel/Sheets
    treats as a formula trigger -- OWASP CSV-injection guidance."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _show_export_toast(parent: QWidget, path: Path, count: int, failed: bool = False) -> None:
    """Non-blocking floating notification after a CSV export.

    Sits at the bottom of the parent view for ~3.5s, then fades out.
    Click anywhere on it to open Windows Explorer at the file's
    location (selected). Avoids QMessageBox -- the operator runs many
    exports per session and a modal popup is friction.
    """
    if failed:
        text = f"✗ Failed to export to {path.name}"
    elif count == 0:
        text = f"Exported (empty) to {path.name}"
    else:
        text = f"✓ Exported {count} {'row' if count == 1 else 'rows'} to {path.name}\nClick to open folder"
    toast = QLabel(text, parent)
    toast.setObjectName("ExportToast")
    toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
    toast.setWordWrap(True)
    toast.setCursor(Qt.CursorShape.PointingHandCursor)
    # Inline style (palette hexes) -- a dark floating toast that reads on
    # both themes. Inline styles bypass the light->dark QSS substitution,
    # so this deliberately uses the dark-chrome surface for both modes.
    toast.setStyleSheet(
        "background-color: #1B2130; color: #E3E9F2; "
        "border: 1px solid #3B4557; border-radius: 8px; padding: 12px 18px; "
        "font-size: 12px; font-weight: 500;"
    )
    # Size + position: bottom-centre of parent, fixed width.
    toast.adjustSize()
    pw = parent.width()
    ph = parent.height()
    tw = min(360, max(toast.width(), 280))
    th = toast.height() + 4
    toast.setFixedSize(tw, th)
    toast.move((pw - tw) // 2, ph - th - 24)
    toast.show()
    toast.raise_()
    # Click to open the containing folder in Explorer with the new
    # file pre-selected (Windows: explorer /select,"path").
    def _open_in_explorer(_ev) -> None:
        if not failed:
            try:
                import subprocess
                subprocess.Popen(["explorer", "/select,", str(path)])
            except Exception:
                pass
        toast.hide()
        toast.deleteLater()
    toast.mousePressEvent = _open_in_explorer  # type: ignore[assignment]
    # Auto-dismiss after 3.5s if user doesn't click. If the user already
    # clicked the toast, _open_in_explorer ran deleteLater() and the C++
    # QLabel is gone; calling deleteLater() again on the dead wrapper
    # raises RuntimeError inside the event loop. Guard the delayed call
    # with shiboken6.isValid so it fires only while the object still lives.
    from PySide6.QtCore import QTimer as _QT

    def _dismiss_if_alive() -> None:
        try:
            import shiboken6
            if not shiboken6.isValid(toast):
                return
        except Exception:
            # If shiboken6 is somehow unavailable, fall back to attempting
            # the deletion but swallow the double-delete RuntimeError.
            pass
        try:
            toast.deleteLater()
        except RuntimeError:
            pass

    _QT.singleShot(3500, _dismiss_if_alive)


def default_export_dir() -> Path:
    """Resolve (and create) the default folder for CSV exports.

    Lands under the user's Documents folder as a 'NOC_BEAM' subdir.
    Handles OneDrive Documents redirect by reading the Windows
    'Personal' shell folder (= Documents) from the registry; falls
    back to home/Documents otherwise. The user can still navigate to
    any other folder via the Save-As dialog this seeds.
    """
    docs: Path | None = None
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Personal")
            cand = Path(value)
            if cand.exists():
                docs = cand
    except Exception:
        pass
    if docs is None:
        # Try the common OneDrive Documents path before plain home.
        od = Path.home() / "OneDrive" / "Documents"
        docs = od if od.exists() else (Path.home() / "Documents")
    target = docs / "NOC_BEAM"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception:
        # If we can't create the subdir, fall back to Documents root.
        return docs
    return target


def _peer_userpart(uri: str) -> str:
    """Strip sip:/sips:/tel: scheme and @domain from a URI so the CSV
    shows '33415835' instead of 'sip:33415835@iptel.org'. Operator
    reviewing the CSV cares about the number, not the routing detail.
    Falls back to the original string if it doesn't look like a URI.
    """
    if not uri:
        return ""
    s = uri.strip()
    for scheme in ("sip:", "sips:", "tel:"):
        if s.lower().startswith(scheme):
            s = s[len(scheme):]
            break
    # Strip URI params like ;transport=udp
    if ";" in s:
        s = s.split(";", 1)[0]
    # Strip @domain part
    if "@" in s:
        s = s.split("@", 1)[0]
    return s


def _display_peer(entry: CdrEntry) -> str:
    """Operator-facing called/calling number for lists and CSV."""
    clean = (getattr(entry, "dialed_uri", "") or "").strip()
    if clean:
        return _peer_userpart(clean)
    return _peer_userpart(entry.peer_uri or "")


def _redial_target(entry: CdrEntry) -> str:
    return (getattr(entry, "dialed_uri", "") or "").strip() or (entry.peer_uri or "")


def _supplier_display(entry: CdrEntry) -> str:
    label = (getattr(entry, "supplier_label", "") or "").strip()
    if label:
        return label
    sid = (getattr(entry, "supplier_id", "") or "").strip()
    return f"C{sid}" if sid else ""


def _resolve_account_label(account_id: str) -> str:
    """Resolve a CDR's stored account_id (an internal UUID) to the
    A-number to show in CSV exports.

    Operator convention (lifted from their Eyebeam setup): the
    Display Name field carries the A-number (e.g. "33415835"), while
    Username holds the per-supplier Uid (e.g. "U080"). For CSV the
    operator wants A-number, so prefer display_name; fall back to
    username; fall back to the UUID if neither is set.
    """
    if not account_id:
        return ""
    try:
        # NB: GlobalSettings has NO `.accounts` field — accounts live in
        # accounts.json via load_accounts(). The previous version called
        # load_settings().accounts which raised AttributeError that the
        # bare `except Exception: pass` silently swallowed, causing every
        # CSV export to show raw UUIDs instead of the operator's display
        # names. Caught in pre-rollout audit; load_accounts() is the
        # correct call.
        from noc_beam.config.store import load_accounts
        for acc in load_accounts():
            if acc.id == account_id:
                return acc.display_name or acc.username or account_id
    except Exception:
        log.exception("Could not resolve account label for %s", account_id)
    return account_id


def _ab_numbers(entry: CdrEntry) -> tuple[str, str]:
    """Return (A-number, B-number) for a CDR.

    For OUTGOING calls: A = our account's username (resolved from id),
    B = the peer userpart (called party).
    For INCOMING calls: A = peer userpart (caller), B = our username.
    """
    own = _resolve_account_label(entry.account_id or "")
    peer = _display_peer(entry)
    if entry.direction == "out":
        return (own, peer)
    return (peer, own)

from noc_beam.config.history import (
    CdrEntry,
    clear_history,
    load_history,
    load_last_seen_ended_at,
    save_last_seen_ended_at,
)
from noc_beam.ui.cdr_detail_dialog import CdrDetailDialog
from noc_beam.ui.components import SipCodeBadge


def _bucket_label(ts: float) -> str:
    """Human bucket name for a timestamp."""
    if ts <= 0:
        return "Earlier"
    when = datetime.fromtimestamp(ts).date()
    today = datetime.now().date()
    if when == today:
        return "Today"
    if when == today - timedelta(days=1):
        return "Yesterday"
    if today - when < timedelta(days=7):
        return when.strftime("%A")
    return when.strftime("%b %d, %Y")


class _DateDivider(QLabel):
    """Section header label between groups of CDR rows on different dates."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("HistoryDivider")
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def _fmt_when(ts: float) -> str:
    now = time.time()
    if ts <= 0:
        return "-"
    same_day = time.strftime("%Y-%m-%d", time.localtime(now)) == \
               time.strftime("%Y-%m-%d", time.localtime(ts))
    if same_day:
        return time.strftime("%H:%M", time.localtime(ts))
    return time.strftime("%m/%d %H:%M", time.localtime(ts))


def _fmt_duration(seconds: float) -> str:
    if seconds <= 0:
        return ""
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _arrow(entry: CdrEntry) -> tuple[str, str]:
    """Return (icon_name, hex_color) for the per-row direction marker.

    iOS-style convention: keep the direction arrow even on failed/missed
    so the user still sees in/out at a glance — color carries the status
    signal (palette status fg: ok green, danger red, warn amber).
    Missed-incoming gets the dedicated `call-missed` icon (phone + X)
    because that's the most semantically loaded state.
    """
    # Palette status foregrounds (design_tokens). These are baked into the
    # icon pixmap; the saturated status hues read on both light + dark.
    from noc_beam.ui.design_tokens import (
        STATUS_OK_LIGHT as OK,
        STATUS_DANGER_LIGHT as DANGER,
        STATUS_WARN_LIGHT as WARN,
    )
    if entry.direction == "in":
        if entry.was_answered:
            return ("call-incoming", OK)
        return ("call-missed", DANGER)     # missed incoming
    if entry.was_answered:
        return ("call-outgoing", OK)
    return ("call-outgoing", WARN)         # failed outgoing


def _result_class(entry: CdrEntry) -> str:
    if entry.direction == "in" and not entry.was_answered:
        return "missed"
    if entry.direction == "out" and not entry.was_answered:
        return "failed"
    return "ok"


class HistoryRow(QFrame):
    """One CDR row.

    The WHOLE ROW is the redial affordance: double-click OR Enter redials
    the peer, and a quiet ghost phone icon appears at the row end on hover
    as an explicit click target (no more per-row filled circle button).

    - The (i) button opens CdrDetailDialog (quiet, hover-emphasised).
    - The selection checkbox is hidden unless the parent view is in
      bulk-select mode (set_select_mode).
    - Right-click opens context menu (Redial / Detail / Copy URI / Delete).
    """

    activated = Signal(int)            # entry index in the parent's list
    redial = Signal(str)               # peer_uri
    delete_requested = Signal(int)
    copy_requested = Signal(str)

    def __init__(self, entry: CdrEntry, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._index = index
        self._entry = entry
        self.setObjectName("HistoryRow")
        self.setProperty("result", _result_class(entry))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Row is keyboard-focusable so Enter can redial it (whole-row
        # affordance parity with double-click).
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Required so QSS background-color paints the full row
        # (including the layout's contents-margin area). Without
        # WA_StyledBackground, QFrame's stylesheet bg only paints
        # an inner rect, leaving the row's edges showing the
        # parent's bg through the hover.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # Selection checkbox: lets the user pick N rows for the bulk CSV
        # export. Hidden by default -- only shown when the parent view
        # enters bulk-select mode via the toolbar toggle. When 0 are
        # checked the export falls back to "all currently visible".
        self._select_cb = QCheckBox(self)
        self._select_cb.setObjectName("HistoryRowSelect")
        self._select_cb.setToolTip("Select for CSV export")
        self._select_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_cb.setVisible(False)

        from noc_beam.ui.rail_icons import rail_icon as _rail_icon
        icon_name, icon_color = _arrow(entry)
        arrow_lbl = QLabel(self)
        arrow_lbl.setObjectName("HistoryRowArrow")
        arrow_lbl.setProperty("result", _result_class(entry))
        arrow_lbl.setFixedSize(20, 20)
        arrow_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow_lbl.setPixmap(
            _rail_icon(icon_name, color=icon_color, px=18).pixmap(18, 18)
        )

        # Peer + meta labels get Ignored horizontal size policy so the
        # text column can shrink when the window is narrow. Without
        # this, the labels enforce their full text-width as a hard
        # minimum and the badge / info / call buttons get clipped off
        # the right edge (same root cause as the call-card overflow
        # fixed in 11c7ca6). The tooltip carries the FULL URI so the
        # @domain is one hover away when needed.
        from PySide6.QtWidgets import QSizePolicy as _SP
        peer_full = entry.peer_uri or "(unknown)"
        redial_target = _redial_target(entry)
        supplier_text = _supplier_display(entry)
        peer_display = _display_peer(entry) or peer_full
        peer_lbl = QLabel(peer_display)
        peer_lbl.setObjectName("HistoryRowPeer")
        peer_lbl.setProperty("result", _result_class(entry))
        tooltip = f"Wire target: {peer_full}"
        if supplier_text:
            tooltip += f"\nSupplier: {supplier_text}"
        peer_lbl.setToolTip(tooltip)
        peer_lbl.setSizePolicy(_SP.Policy.Ignored, _SP.Policy.Preferred)

        # Meta line carries date/time · supplier · duration ONLY. The
        # status ("Busy" / "Cancelled" / ...) lives solely in the
        # SipCodeBadge chip on the right -- one status carrier per row
        # (brief rule 3); repeating it here read as noise.
        when = _fmt_when(entry.ended_at or entry.started_at)
        dur = _fmt_duration(entry.duration_s)
        bits = [when]
        if supplier_text:
            bits.append(supplier_text)
        if dur:
            bits.append(dur)
        meta_lbl = QLabel(" · ".join(b for b in bits if b))
        meta_lbl.setObjectName("HistoryRowMeta")
        meta_lbl.setSizePolicy(_SP.Policy.Ignored, _SP.Policy.Preferred)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        text_col.addWidget(peer_lbl)
        text_col.addWidget(meta_lbl)

        self._info_btn = QToolButton(self)
        self._info_btn.setObjectName("HistoryRowInfo")
        self._info_btn.setText("i")
        self._info_btn.setToolTip("Show full call detail (codec, duration, end code)")
        self._info_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._info_btn.clicked.connect(lambda: self.activated.emit(self._index))

        # Redial affordance: the whole row redials (double-click / Enter).
        # This ghost phone icon is a quiet, explicit click target that only
        # appears on hover -- replaces the old filled circle button that
        # repeated down every row. Kept as #HistoryRowCall so the object
        # name / signal wiring is stable, but styled ghost.
        from noc_beam.ui.rail_icons import rail_icon as _rail_icon2
        self._call_btn = QToolButton(self)
        self._call_btn.setObjectName("HistoryRowCall")
        self._call_btn.setIcon(_rail_icon2("call-outgoing", color="#8A93A5", px=16))
        self._call_btn.setIconSize(QSize(16, 16))
        self._call_btn.setToolTip(
            f"Call {redial_target}" if redial_target else "No peer URI to call back"
        )
        self._call_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._call_btn.setEnabled(bool(redial_target))
        self._call_btn.clicked.connect(self._emit_redial)
        # Hidden until hover (the row itself is the primary affordance).
        self._call_btn.setVisible(False)

        code = entry.end_code if entry.end_code else (200 if entry.was_answered else None)
        badge = SipCodeBadge(code, entry.end_reason, self)

        outer = QHBoxLayout(self)
        # contentsMargins=0 so the QFrame's QSS background paints the
        # FULL widget rect on hover (Qt bg paint follows the layout's
        # content region). Visual padding moved to QSS `padding` on
        # #HistoryRow.
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        outer.addWidget(self._select_cb, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(arrow_lbl, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(text_col, 1)
        outer.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self._info_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self._call_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _emit_redial(self) -> None:
        target = _redial_target(self._entry)
        if target:
            self.redial.emit(target)

    def is_checked(self) -> bool:
        """Whether this row's selection checkbox is ticked."""
        return self._select_cb.isChecked()

    def set_select_mode(self, on: bool) -> None:
        """Show/hide the bulk-select checkbox. When leaving select mode the
        row's tick is cleared so a stale selection can't leak into the next
        export."""
        self._select_cb.setVisible(bool(on))
        if not on:
            self._select_cb.setChecked(False)

    def enterEvent(self, event) -> None:  # noqa: N802, ANN001
        # Reveal the quiet ghost redial icon while hovering (only when it
        # actually has a target to dial).
        if self._call_btn.isEnabled():
            self._call_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802, ANN001
        self._call_btn.setVisible(False)
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802, ANN001
        # Enter / Return on a focused row redials it (whole-row affordance).
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._emit_redial()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802, ANN001
        # Bria parity: double-click redials. Don't redial if the click
        # landed on a child button (info/call) or the selection checkbox.
        ch = self.childAt(event.pos())
        if ch in (self._call_btn, self._info_btn, self._select_cb):
            event.accept()
            return
        self._emit_redial()
        event.accept()

    def _show_context_menu(self, pos) -> None:
        menu = QMenu(self)
        redial_target = _redial_target(self._entry)
        if redial_target:
            act_call = QAction(f"Call {redial_target}", menu)
            act_call.triggered.connect(self._emit_redial)
            menu.addAction(act_call)
        act_detail = QAction("Show full detail…", menu)
        act_detail.triggered.connect(lambda: self.activated.emit(self._index))
        menu.addAction(act_detail)
        if self._entry.peer_uri:
            act_copy = QAction("Copy peer URI", menu)
            act_copy.triggered.connect(
                lambda: self.copy_requested.emit(self._entry.peer_uri)
            )
            menu.addAction(act_copy)
        menu.addSeparator()
        act_del = QAction("Delete entry", menu)
        act_del.triggered.connect(lambda: self.delete_requested.emit(self._index))
        menu.addAction(act_del)
        menu.popup(self.mapToGlobal(pos))


class HistoryView(QWidget):
    """List of HistoryRow widgets backed by the on-disk CDR store."""

    redial_requested = Signal(str)
    missed_count_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[CdrEntry] = []
        self._visible: list[CdrEntry] = []
        self._rows: list[HistoryRow] = []
        # Persisted across restarts so the badge doesn't re-light every
        # prior missed call on next launch.
        self._last_seen_ended_at: float = load_last_seen_ended_at()

        self._search = QLineEdit()
        self._search.setObjectName("HistorySearch")
        self._search.setAccessibleName("History search")
        self._search.setPlaceholderText("Search peer URI / number…")
        self._search.setClearButtonEnabled(True)
        # 150ms debounce: _refresh_rows tears down + rebuilds every row +
        # date divider, which on a 1000-CDR history was stalling the UI
        # mid-keystroke. Coalesce a burst of typing into one rebuild.
        from PySide6.QtCore import QTimer as _QT
        self._search_debounce = _QT(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._refresh_rows)
        self._search.textChanged.connect(
            lambda _t: self._search_debounce.start()
        )
        # The clear-button QLineEdit creates internally is a QToolButton
        # with no text/icon/accessibleName -- which breaks the a11y
        # contract test that walks every QPushButton+QToolButton in the
        # shell. Tag it with a tooltip + accessible name so it counts.
        from PySide6.QtWidgets import QToolButton as _QTB
        for tb in self._search.findChildren(_QTB):
            if not tb.accessibleName():
                tb.setAccessibleName("Clear search")
                tb.setToolTip("Clear search")

        self._dir_filter = QComboBox()
        self._dir_filter.setObjectName("HistoryFilter")
        self._dir_filter.addItem("All Calls", "all")
        self._dir_filter.addItem("Incoming", "in")
        self._dir_filter.addItem("Outgoing", "out")
        self._dir_filter.addItem("Missed", "missed")
        self._dir_filter.currentIndexChanged.connect(self._refresh_rows)

        self._range_filter = QComboBox()
        self._range_filter.setObjectName("HistoryFilter")
        self._range_filter.addItem("All time", "all")
        self._range_filter.addItem("Today", "today")
        self._range_filter.addItem("Yesterday", "yesterday")
        self._range_filter.addItem("Last 7 days", "week")
        self._range_filter.addItem("Last 30 days", "month")
        self._range_filter.currentIndexChanged.connect(self._refresh_rows)

        # Let both combos shrink below their longest item at narrow window
        # widths (the app's default is ~436px) instead of forcing the
        # toolbar wider and clipping the buttons at the right edge.
        for combo in (self._dir_filter, self._range_filter):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(8)

        # Bulk-select mode toggle. Off by default -> row checkboxes stay
        # hidden and the list reads calm. Toggling it on reveals the
        # per-row checkboxes so the operator can pick a subset to export.
        self._select_mode = False
        self._bulk_btn = QToolButton()
        self._bulk_btn.setObjectName("HistoryIconBtn")
        self._bulk_btn.setText("☑")
        self._bulk_btn.setCheckable(True)
        self._bulk_btn.setToolTip("Select rows for export")
        self._bulk_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bulk_btn.toggled.connect(self._on_toggle_select_mode)

        self._reload_btn = QToolButton()
        self._reload_btn.setObjectName("HistoryIconBtn")
        self._reload_btn.setText("⟳")
        self._reload_btn.setToolTip("Reload from disk")
        self._reload_btn.clicked.connect(self.reload)
        # Export selected (or all visible if nothing checked) to a CSV.
        # Icon-only quiet button (same treatment as reload): the text
        # label "Export CSV" was truncating to "xport CS" at the app's
        # default ~436px width, and operators use export rarely enough
        # that a tooltip carries the label fine.
        self._export_btn = QToolButton()
        self._export_btn.setObjectName("HistoryIconBtn")
        self._export_btn.setText("⤓")
        self._export_btn.setAccessibleName("Export CSV")
        self._export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._export_btn.setToolTip(
            "Export CSV — checked rows, or all currently-visible "
            "(filtered) rows when nothing is checked."
        )
        self._export_btn.clicked.connect(self._on_export)
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setObjectName("HistoryClearBtn")
        self._clear_btn.clicked.connect(self._on_clear)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(8, 6, 8, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._search, 1)

        controls = QHBoxLayout()
        controls.setContentsMargins(8, 4, 8, 4)
        controls.setSpacing(6)
        controls.addWidget(self._dir_filter)
        controls.addWidget(self._range_filter)
        controls.addStretch(1)
        controls.addWidget(self._bulk_btn)
        controls.addWidget(self._reload_btn)
        controls.addWidget(self._export_btn)
        controls.addWidget(self._clear_btn)

        self._empty_label = QLabel(
            "No call history yet.\n\n"
            "Placed and received calls will appear here.\n"
            "Double-click a row to call back, or use the (i) button for full detail.",
            self,
        )
        # parent=self prevents the label flashing as a top-level NOC_Beam
        # window during HistoryView init. QStackedLayout.addWidget on the
        # FIRST widget added auto-sets it as the current page (visible);
        # before the parent QStackedLayout is installed on this widget,
        # _empty_label has no parent of its own -- the resulting Show
        # event fires while isWindow()==True, so Windows briefly renders
        # an empty NOC_Beam frame next to the main shell.
        self._empty_label.setObjectName("ViewEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)

        self._rows_holder = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("HistoryScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidget(self._rows_holder)

        self._stack = QStackedLayout()
        self._stack.addWidget(self._empty_label)
        self._stack.addWidget(self._scroll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(search_row)
        layout.addLayout(controls)
        layout.addLayout(self._stack, 1)

        self.reload()

    def mark_all_seen(self) -> None:
        """Reset the unread-missed counter AND persist the new
        high-water-mark so the badge doesn't re-light every prior
        missed call after restart."""
        if self._entries:
            self._last_seen_ended_at = max(e.ended_at for e in self._entries)
            try:
                save_last_seen_ended_at(self._last_seen_ended_at)
            except Exception:
                pass
        self.missed_count_changed.emit(0)

    def unread_missed_count(self) -> int:
        return sum(
            1 for e in self._entries
            if e.direction == "in" and not e.was_answered
            and e.ended_at > self._last_seen_ended_at
        )

    def reload(self) -> None:
        self._entries = sorted(load_history(), key=lambda e: e.ended_at, reverse=True)
        self._refresh_rows()
        self.missed_count_changed.emit(self.unread_missed_count())

    def _refresh_rows(self) -> None:
        # Tear down everything (rows AND date dividers).
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                try:
                    w.blockSignals(True)
                except Exception:
                    pass
                w.deleteLater()
        self._rows.clear()

        self._visible = [e for e in self._entries if self._matches_filters(e)]
        if not self._visible:
            self._stack.setCurrentIndex(0)
            return

        self._stack.setCurrentIndex(1)
        last_bucket: str | None = None
        insert_at = 0
        for i, entry in enumerate(self._visible):
            bucket = _bucket_label(entry.ended_at or entry.started_at)
            if bucket != last_bucket:
                divider = _DateDivider(bucket, self._rows_holder)
                self._rows_layout.insertWidget(insert_at, divider)
                insert_at += 1
                last_bucket = bucket
            row = HistoryRow(entry, i, self._rows_holder)
            row.activated.connect(self._open_detail)
            row.redial.connect(self.redial_requested.emit)
            row.delete_requested.connect(self._on_delete_one)
            row.copy_requested.connect(self._on_copy_uri)
            row.set_select_mode(self._select_mode)
            self._rows_layout.insertWidget(insert_at, row)
            insert_at += 1
            self._rows.append(row)

    def _on_toggle_select_mode(self, on: bool) -> None:
        self._select_mode = bool(on)
        for row in self._rows:
            row.set_select_mode(self._select_mode)

    def _matches_filters(self, entry: CdrEntry) -> bool:
        # Search filter (peer URI substring, case-insensitive).
        needle = self._search.text().strip().lower()
        if needle:
            haystack = " ".join(
                s for s in (
                    entry.peer_uri or "",
                    getattr(entry, "dialed_uri", "") or "",
                    _display_peer(entry),
                    _supplier_display(entry),
                )
                if s
            ).lower()
            if needle not in haystack:
                return False
        # Direction filter
        dir_key = self._dir_filter.currentData()
        if dir_key == "in" and entry.direction != "in":
            return False
        if dir_key == "out" and entry.direction != "out":
            return False
        if dir_key == "missed" and not (
            entry.direction == "in" and not entry.was_answered
        ):
            return False
        # Range filter -- fall back to started_at when ended_at is 0.
        rng = self._range_filter.currentData()
        ts = entry.ended_at or entry.started_at
        if rng != "all" and ts:
            now = datetime.now().date()
            when = datetime.fromtimestamp(ts).date()
            if rng == "today" and when != now:
                return False
            if rng == "yesterday" and when != now - timedelta(days=1):
                return False
            if rng == "week" and now - when > timedelta(days=7):
                return False
            if rng == "month" and now - when > timedelta(days=30):
                return False
        return True

    def _open_detail(self, index: int) -> None:
        # HistoryRow emits its visible-list index (the `i` from the
        # enumerate in _refresh_rows). Previously this method indexed
        # self._entries (the FULL list) with that visible-list value,
        # so opening detail on a filtered row showed the WRONG CDR.
        # Resolve through the cached visible projection so a CDR appended
        # between the row click and this handler can't shift indices.
        if not (0 <= index < len(self._visible)):
            return
        entry = self._visible[index]
        dlg = CdrDetailDialog(entry, parent=self)
        dlg.redial_requested.connect(self.redial_requested.emit)
        runner = getattr(dlg, "exec")
        runner()

    def _on_delete_one(self, index: int) -> None:
        from noc_beam.config.history import save_history
        if not (0 <= index < len(self._visible)):
            return
        target = self._visible[index]
        self._entries = [e for e in self._entries if e is not target]
        try:
            save_history(self._entries)
        except Exception:
            return
        self._refresh_rows()
        self.missed_count_changed.emit(self.unread_missed_count())

    def _on_copy_uri(self, uri: str) -> None:
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(uri)

    def _on_export(self) -> None:
        """Export selected (or all visible) CDR rows to a CSV.

        Selection rules:
        - If any rows have their checkbox ticked -> export only those.
        - Otherwise export all currently-visible (filtered) rows.

        Save-As dialog opens at Documents/NOC_BEAM/ with an auto-named
        filename; the user can navigate elsewhere or rename. Schema:
        A Number, B Number, Date, Duration (s), FAS Verdict.
        """
        chosen = [r._entry for r in self._rows if r.is_checked()]
        if not chosen:
            chosen = [r._entry for r in self._rows]
        if not chosen:
            return  # nothing visible to export
        from datetime import datetime as _dt
        from PySide6.QtWidgets import QFileDialog as _QFD
        default_name = f"noc_beam_history_{_dt.now():%Y%m%d_%H%M}.csv"
        default_path = str(default_export_dir() / default_name)
        chosen_path, _selected_filter = _QFD.getSaveFileName(
            self,
            "Export call history to CSV",
            default_path,
            "CSV files (*.csv);;All files (*)",
        )
        if not chosen_path:
            return  # user cancelled
        out_path = Path(chosen_path)
        try:
            with out_path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh, lineterminator="\n")
                writer.writerow(["A Number", "B Number", "Date", "Duration (s)", "FAS Verdict"])
                for e in chosen:
                    a, b = _ab_numbers(e)
                    when = e.started_at or e.ended_at or 0
                    date_str = _dt.fromtimestamp(when).strftime("%Y-%m-%d %H:%M:%S") if when else ""
                    writer.writerow([
                        _csv_safe(a),
                        _csv_safe(b),
                        date_str,
                        f"{e.duration_s:.1f}",
                        _csv_safe(e.fas_verdict or ""),
                    ])
            log.info("History CSV exported: %s (%d rows)", out_path, len(chosen))
            _show_export_toast(self, out_path, len(chosen))
        except Exception:
            log.exception("Failed to export history CSV to %s", out_path)
            _show_export_toast(self, out_path, 0, failed=True)

    def _on_clear(self) -> None:
        # Confirm before nuking everything.
        reply = QMessageBox.question(
            self,
            "Clear call history",
            "Delete all call history entries? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        clear_history()
        self.reload()
