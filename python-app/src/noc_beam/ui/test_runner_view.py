from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class _SupplierComboFocusFilter(QObject):
    """Select-all-on-focus + clear-proxy-filter for the SUPPLIER combo.

    Mirrors the dial-bar combo in phone_shell: click or tab in ->
    existing supplier name is wiped, leftover proxy filter is reset.
    """

    def __init__(self, combo):
        super().__init__(combo)
        self._combo = combo

    def eventFilter(self, obj, event):
        et = event.type()
        if et in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            QTimer.singleShot(0, obj.selectAll)
            try:
                from PySide6.QtCore import QSortFilterProxyModel as _QSFPM
                model = self._combo.model()
                if isinstance(model, _QSFPM):
                    QTimer.singleShot(0, lambda m=model: m.setFilterFixedString(""))
            except Exception:
                pass
        return False


class _PasteAtEndTextEdit(QTextEdit):
    """QTextEdit that always pastes at the end of the document, with a
    trailing newline appended if the pasted chunk wasn't already
    terminated.

    Rationale: in the Test Runner, engineers paste phone numbers one
    at a time from chat / spreadsheet / notes. With a standard textarea
    a misclick can paste mid-line and split an existing number. This
    subclass guarantees every paste lands at the bottom as its own
    line, regardless of where the cursor was.
    """

    def insertFromMimeData(self, source) -> None:  # noqa: ANN001, N802
        text = source.text() if source is not None else ""
        if not text:
            return
        if not text.endswith("\n"):
            text += "\n"
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.setTextCursor(cursor)
        # Ensure visible focus stays on the new bottom line.
        self.ensureCursorVisible()

from noc_beam.config.store import AccountConfig
from noc_beam.testing.plan import TestCall as PlanCall
from noc_beam.testing.plan import TestSpec as PlanSpec
from noc_beam.testing.plan import expand, normalise_lines
from noc_beam.testing.runner import TestResult as RunnerResult
from noc_beam.testing.runner import TestRunner as Runner


CSV_HEADER = [
    "test_run_id",
    "started_at",
    "from_account",
    "to_uri",
    "result",
    "sip_code",
    "sip_reason",
    "rtt_ms",
    "duration_s",
    "notes",
    "fas_verdict",
    "fas_confidence",
    "fas_reasons",
]

__test__ = False


class TestRunnerView(QMainWindow):
    def __init__(self, accounts: list[AccountConfig], parent=None) -> None:
        super().__init__(parent)
        self.accounts = accounts
        self.results: list[RunnerResult] = []
        self.runner: Runner | None = None
        self._row_by_call_index: dict[int, int] = {}

        self.setWindowTitle("NOC_Beam test runner")
        # Wider default so toolbar fits horizontally + results table
        # has room for all columns without horizontal scroll.
        self.resize(960, 600)
        self.setMinimumSize(820, 480)

        self.callers_edit = _PasteAtEndTextEdit()
        self.callers_edit.setObjectName("TestRunnerPasteBox")
        self.callers_edit.setAcceptRichText(False)
        # Leaving the callers field blank dispatches via the first
        # enabled account. Otherwise paste one account username per
        # line (or `*` / `auto`). Stops the silent "no matching
        # account" failure when users paste dial-target lists into
        # the callers box by mistake.
        self.callers_edit.setPlaceholderText(
            "Account usernames (one per line) — leave blank to use the active account"
        )
        self.targets_edit = _PasteAtEndTextEdit()
        self.targets_edit.setObjectName("TestRunnerPasteBox")
        self.targets_edit.setAcceptRichText(False)
        self.targets_edit.setPlaceholderText(
            "Target numbers or full SIP URIs (one per line)"
        )

        self.mode_combo = QComboBox()
        # Fan-out first = default. Matches the most common wholesale
        # workflow: leave Callers blank (active account), paste 30
        # targets, hit Run. Tooltips explain each mode for new users.
        for label, value, tooltip in (
            ("Fan-out — 1 account dials all targets",  "fan-out",
             "Default. The first caller (or active account if blank) dials every target in the list."),
            ("Matrix — every caller × every target",   "matrix",
             "Each caller dials every target. Use for full coverage testing."),
            ("Paired — caller 1↔target 1, line by line", "paired",
             "Pairs callers and targets one-to-one by line number. Cuts off at the shorter list."),
            ("Fan-in — all callers dial 1 target",     "fan-in",
             "Every caller dials the first target. Use to stress one destination from multiple sources."),
        ):
            self.mode_combo.addItem(label, value)
            idx = self.mode_combo.count() - 1
            self.mode_combo.setItemData(idx, tooltip, Qt.ItemDataRole.ToolTipRole)
        self.mode_combo.setToolTip(
            "Test plan mode. Fan-out is the default (leave Callers blank "
            "to dial every target from the active account)."
        )
        # The combo lives in a narrow toolbar grid cell, but the labels
        # ("Fan-out -- 1 account dials all targets") are long. Force the
        # *popup* (which is independent of the combo's own width) to be
        # wide enough that no item shows ellipsis on drop-down.
        self.mode_combo.view().setMinimumWidth(360)

        self.pass_combo = QComboBox()
        for label, value in (
            ("Reachability", "reachability"),
            ("Full call", "full-call"),
        ):
            self.pass_combo.addItem(label, value)

        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 16)
        self.parallel_spin.setValue(4)

        self.hold_spin = QSpinBox()
        self.hold_spin.setRange(0, 3600)
        self.hold_spin.setValue(5)
        self.hold_spin.setSuffix(" s")

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 3600)
        self.timeout_spin.setValue(30)
        self.timeout_spin.setSuffix(" s")

        self.run_btn = QPushButton("Run 0 calls")
        self.run_btn.setObjectName("RunTestButton")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("TestRunnerStopBtn")
        self.stop_btn.setEnabled(False)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("TestRunnerClearBtn")
        self.table = QTableWidget(0, 9)
        self.table.setObjectName("TestRunnerResults")
        self.table.setHorizontalHeaderLabels(
            ["#", "FROM", "TO", "RESULT", "FAS", "CODE", "RTT", "TIME", "NOTES"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            8, QHeaderView.ResizeMode.Stretch
        )
        # FAS column needs a fixed-min width so the pill badge text
        # ("Suspicious", "Likely FAS") doesn't get truncated to
        # "Suspici…" by ResizeToContents on the underlying QLabel.
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Fixed
        )
        self.table.setColumnWidth(4, 110)

        # Footer counter pills (mockup panel 8: "12 passed · 3 failed
        # · 1 running · 0 pending"). Each is a coloured chip.
        self.summary_passed = QLabel("0 passed")
        self.summary_passed.setObjectName("TestRunnerCounter")
        self.summary_passed.setProperty("level", "passed")
        self.summary_failed = QLabel("0 failed")
        self.summary_failed.setObjectName("TestRunnerCounter")
        self.summary_failed.setProperty("level", "failed")
        self.summary_running = QLabel("0 running")
        self.summary_running.setObjectName("TestRunnerCounter")
        self.summary_running.setProperty("level", "running")
        self.summary_pending = QLabel("0 pending")
        self.summary_pending.setObjectName("TestRunnerCounter")
        self.summary_pending.setProperty("level", "pending")
        # Legacy `summary_label` alias preserved for any callers that
        # touch it.
        self.summary_label = self.summary_passed
        # Footer button is "Close" -- the run-control Stop lives in the
        # header row next to Run. Two buttons firing the same cancel
        # was just confusing (UX audit blocker 5).
        self.cancel_btn = QPushButton("Close")
        self.cancel_btn.setObjectName("SecondaryAction")
        self.export_btn = QPushButton("Export CSV")
        self.export_btn.setObjectName("PrimaryAction")
        self.export_btn.setEnabled(False)

        self._build_ui()
        self._connect_ui()
        self._refresh_hold_enabled()
        self._refresh_plan_preview()
        self._refresh_summary()

    def _build_ui(self) -> None:
        """Modern Test Runner layout.

        Top:    horizontal toolbar with supplier + mode + pass + parallel
                + hold + timeout (replaces the tall CONFIGURATION card).
        Strip:  one-line pre-flight summary -- "Will run N calls via X
                @ parallel=Y ≈ ETA" + counter chips. Catches the classic
                "I meant parallel=4 not 14" mistake before a 100-call
                batch hits a real carrier.
        Body:   35/65 split. Left: tabbed Targets/Callers textarea.
                Right: live-streaming Results table.
        Footer: sticky -- ghost actions on left, primary Run + Stop on right.
        """
        from PySide6.QtWidgets import (
            QButtonGroup, QComboBox as _QComboBox, QSplitter, QStackedWidget,
            QToolButton, QWidget as _QWidget,
        )
        central = QWidget(self)
        central.setObjectName("TestRunnerRoot")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        # Title row
        title = QLabel("Test Runner")
        title.setObjectName("SettingsTitle")
        subtitle = QLabel(
            "Place N concurrent test calls from your registered accounts "
            "to one or more targets. Results stream live; export when done."
        )
        subtitle.setObjectName("SettingsSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(title)
        outer.addWidget(subtitle)

        # ===== Toolbar: supplier + config (all in one row) ============
        toolbar = QFrame()
        toolbar.setObjectName("TestRunnerToolbar")
        # Legacy marker for backwards-compat tests
        _legacy_toolbar = QFrame(toolbar)
        _legacy_toolbar.setObjectName("OperatorToolbar")
        _legacy_toolbar.setFixedSize(0, 0)
        _legacy_toolbar.setVisible(False)
        # Use a grid layout so labels + widgets can wrap cleanly when
        # the window is narrow. Each field is a fixed unit; the supplier
        # picker gets its own full row when visible (its content is the
        # longest -- carrier names won't fit inline with config).
        from PySide6.QtWidgets import QGridLayout, QSizePolicy as _SP
        tb_l = QGridLayout(toolbar)
        tb_l.setContentsMargins(14, 10, 14, 10)
        tb_l.setHorizontalSpacing(14)
        tb_l.setVerticalSpacing(10)

        # SUPPLIER picker -- top row, full width, only shown when
        # active account is teles/genband.
        self.supplier_row = _QWidget()
        _supp_l = QHBoxLayout(self.supplier_row)
        _supp_l.setContentsMargins(0, 0, 0, 0)
        _supp_l.setSpacing(8)
        self.supplier_label = QLabel("SUPPLIER")
        self.supplier_label.setObjectName("TestRunnerToolbarLabel")
        self.supplier_label.setMinimumWidth(80)
        self.supplier_combo = _QComboBox()
        self.supplier_combo.setObjectName("TestRunnerSupplier")
        self.supplier_combo.setEditable(True)
        self.supplier_combo.setInsertPolicy(_QComboBox.InsertPolicy.NoInsert)
        self.supplier_combo.setMinimumWidth(280)
        self.supplier_combo.setSizePolicy(_SP.Policy.Expanding, _SP.Policy.Fixed)
        # ARCHITECTURE: ditch QCompleter, use QSortFilterProxyModel +
        # the combo's OWN popup. The QCompleter popup is impossible to
        # dismiss programmatically after a commit; the combo's popup
        # obeys hidePopup() reliably. See phone_shell for full notes.
        from PySide6.QtCore import QSortFilterProxyModel as _QSFPM
        from PySide6.QtGui import QStandardItemModel as _QSIM
        self._supplier_source_model = _QSIM(self.supplier_combo)
        self._supplier_proxy = _QSFPM(self.supplier_combo)
        self._supplier_proxy.setSourceModel(self._supplier_source_model)
        self._supplier_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.supplier_combo.setModel(self._supplier_proxy)
        self.supplier_combo.setCompleter(None)
        self._all_suppliers: list[tuple[str, str]] = []
        _le = self.supplier_combo.lineEdit()
        if _le is not None:
            _le.setCompleter(None)
            self._supplier_combo_filter = _SupplierComboFocusFilter(self.supplier_combo)
            _le.installEventFilter(self._supplier_combo_filter)
            _le.textEdited.connect(self._on_supplier_text_edited)
            _le.returnPressed.connect(self._on_supplier_return_pressed)
        self.supplier_combo.currentIndexChanged.connect(self._on_supplier_changed)
        _supp_l.addWidget(self.supplier_label)
        _supp_l.addWidget(self.supplier_combo, 1)
        self.supplier_row.setVisible(False)
        tb_l.addWidget(self.supplier_row, 0, 0, 1, 6)
        self._batch_supplier_id: str = ""

        # Separator no longer needed in a grid layout; keep a hidden
        # widget so _refresh_supplier_picker() still finds the attr.
        self._supplier_sep = QLabel("")
        self._supplier_sep.setVisible(False)

        # Bottom row: 5 config fields evenly spaced. Each field is a
        # tiny QWidget (label-on-top-of-widget) so labels and inputs
        # stay associated when widths change.
        def _config_field(label_text: str, widget: QWidget, min_width: int) -> _QWidget:
            f = _QWidget()
            fl = QVBoxLayout(f)
            fl.setContentsMargins(0, 0, 0, 0)
            fl.setSpacing(4)
            lbl = QLabel(label_text)
            lbl.setObjectName("TestRunnerToolbarLabel")
            widget.setMinimumWidth(min_width)
            widget.setSizePolicy(_SP.Policy.Expanding, _SP.Policy.Fixed)
            fl.addWidget(lbl)
            fl.addWidget(widget)
            return f

        config_fields = [
            _config_field("Mode", self.mode_combo, 120),
            _config_field("Pass criteria", self.pass_combo, 140),
            _config_field("Parallel", self.parallel_spin, 80),
            _config_field("Hold (s)", self.hold_spin, 80),
            _config_field("Timeout (s)", self.timeout_spin, 80),
        ]
        for col, widget in enumerate(config_fields):
            tb_l.addWidget(widget, 1, col)
            tb_l.setColumnStretch(col, 1)
        outer.addWidget(toolbar)

        # ===== Pre-flight strip ======================================
        # Shows what's ABOUT to run + live counters. Replaces the
        # bottom STATUS card.
        preflight = QFrame()
        preflight.setObjectName("TestRunnerPreflight")
        pf_l = QHBoxLayout(preflight)
        pf_l.setContentsMargins(12, 8, 12, 8)
        pf_l.setSpacing(10)
        self._preflight_label = QLabel("Configure targets to preview the run")
        self._preflight_label.setObjectName("TestRunnerPreflightLabel")
        pf_l.addWidget(self._preflight_label, 1)
        # Counter chips inline (replace the old STATUS card grid).
        pf_l.addWidget(self.summary_passed)
        pf_l.addWidget(self.summary_failed)
        pf_l.addWidget(self.summary_running)
        pf_l.addWidget(self.summary_pending)
        outer.addWidget(preflight)

        # ===== Body: split 35/65 (Targets | Results) =================
        split = QSplitter(Qt.Orientation.Horizontal, central)
        split.setObjectName("TestRunnerSplit")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(8)

        # ---- LEFT: tabbed Targets/Callers ----
        left_card = QFrame()
        left_card.setObjectName("SettingsCard")
        # Hidden legacy marker for back-compat selectors
        _legacy_paste_grid = QFrame(left_card)
        _legacy_paste_grid.setObjectName("TestRunnerPasteGrid")
        _legacy_paste_grid.setFixedSize(0, 0)
        _legacy_paste_grid.setVisible(False)
        lc_l = QVBoxLayout(left_card)
        lc_l.setContentsMargins(0, 0, 0, 0)
        lc_l.setSpacing(0)

        # Tab strip
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(10, 8, 10, 0)
        tabs_row.setSpacing(4)
        self._tab_targets_btn = QToolButton()
        self._tab_targets_btn.setObjectName("TestRunnerTab")
        self._tab_targets_btn.setText("Targets (0)")
        self._tab_targets_btn.setCheckable(True)
        self._tab_targets_btn.setChecked(True)
        self._tab_targets_btn.setAutoRaise(True)
        self._tab_targets_btn.setProperty("active", True)
        self._tab_callers_btn = QToolButton()
        self._tab_callers_btn.setObjectName("TestRunnerTab")
        self._tab_callers_btn.setText("Callers (auto)")
        self._tab_callers_btn.setCheckable(True)
        self._tab_callers_btn.setAutoRaise(True)
        _grp = QButtonGroup(left_card)
        _grp.setExclusive(True)
        _grp.addButton(self._tab_targets_btn)
        _grp.addButton(self._tab_callers_btn)
        tabs_row.addWidget(self._tab_targets_btn)
        tabs_row.addWidget(self._tab_callers_btn)
        tabs_row.addStretch(1)
        # Run count badge here (it's the legacy name)
        self._run_count_badge = QLabel("0 calls")
        self._run_count_badge.setObjectName("TestRunnerCountBadge")
        self._run_count_badge.setVisible(False)  # info now in preflight
        tabs_row.addWidget(self._run_count_badge)
        lc_l.addLayout(tabs_row)

        # Stacked widget for tab content
        self._target_stack = QStackedWidget()
        self._target_stack.addWidget(self.targets_edit)
        self._target_stack.addWidget(self.callers_edit)
        lc_l.addWidget(self._target_stack, 1)
        self._tab_targets_btn.toggled.connect(
            lambda checked: checked and self._target_stack.setCurrentIndex(0)
        )
        self._tab_callers_btn.toggled.connect(
            lambda checked: checked and self._target_stack.setCurrentIndex(1)
        )
        split.addWidget(left_card)

        # ---- RIGHT: results table ----
        right_card = QFrame()
        right_card.setObjectName("SettingsCard")
        right_l = QVBoxLayout(right_card)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(0)
        results_header = QHBoxLayout()
        results_header.setContentsMargins(14, 10, 14, 8)
        results_label = QLabel("RESULTS")
        results_label.setObjectName("SettingsCardLabel")
        results_header.addWidget(results_label)
        results_header.addStretch(1)
        right_l.addLayout(results_header)
        self.table.setObjectName("TestRunnerResults")
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(36)
        right_l.addWidget(self.table, 1)
        split.addWidget(right_card)

        split.setStretchFactor(0, 35)
        split.setStretchFactor(1, 65)
        split.setSizes([320, 600])
        outer.addWidget(split, 1)

        # ===== Sticky footer ==========================================
        # Layout: ghost-secondary actions on left, Cancel + destructive
        # + primary on right. Run button label shows the call count so
        # the operator sees what they're about to fire.
        footer = QFrame(central)
        footer.setObjectName("TestRunnerFooter")
        f_l = QHBoxLayout(footer)
        f_l.setContentsMargins(0, 4, 0, 0)
        f_l.setSpacing(8)
        f_l.addWidget(self.clear_btn)
        f_l.addWidget(self.export_btn)
        f_l.addStretch(1)
        f_l.addWidget(self.cancel_btn)
        f_l.addWidget(self.stop_btn)
        f_l.addWidget(self.run_btn)
        outer.addWidget(footer)

        self.setCentralWidget(central)

        # Populate supplier picker now that all widgets exist.
        self._refresh_supplier_picker()

    def _refresh_supplier_picker(self) -> None:
        """Show/hide + populate the supplier picker based on the first
        enabled account's switch_type. Also toggles the toolbar
        separator that follows the supplier row in the new layout.
        Test Runner uses the first enabled account by default; if none
        has teles/genband the picker stays hidden and the runner
        behaves as before."""
        acc = next((a for a in self.accounts if getattr(a, "enabled", True)), None)
        # Match separator visibility to the supplier row's visibility.
        sep = getattr(self, "_supplier_sep", None)
        kind = (getattr(acc, "switch_type", "other") or "other").lower() if acc else "other"
        if not acc or kind not in ("teles", "genband"):
            self.supplier_row.setVisible(False)
            if sep is not None:
                sep.setVisible(False)
            self._batch_supplier_id = ""
            return
        try:
            from noc_beam.config.suppliers import load_valid_suppliers
            # Only valid suppliers reach the picker; full list is in Settings.
            suppliers = load_valid_suppliers()
        except Exception:
            self.supplier_row.setVisible(False)
            return
        from PySide6.QtGui import QStandardItem
        self.supplier_combo.blockSignals(True)
        self._supplier_source_model.clear()
        self._all_suppliers.clear()
        for s in suppliers:
            item = QStandardItem(s.display())
            item.setData(s.id, Qt.ItemDataRole.UserRole)
            self._supplier_source_model.appendRow(item)
            self._all_suppliers.append((s.display(), s.id))
        self._supplier_proxy.setFilterFixedString("")
        if self.supplier_combo.count():
            self.supplier_combo.setCurrentIndex(0)
            self._batch_supplier_id = self.supplier_combo.itemData(0) or ""
        self.supplier_combo.blockSignals(False)
        self.supplier_label.setText(
            "SUPPLIER (auth)" if kind == "teles" else "SUPPLIER (prefix)"
        )
        self.supplier_row.setVisible(True)
        if sep is not None:
            sep.setVisible(True)

    def _on_supplier_return_pressed(self) -> None:
        """Enter commits the visible supplier match.

        Resolves via _all_suppliers cache (the combo's model is the
        filtered proxy view, so iterating it would miss rows the
        current filter hides). See phone_shell version for full notes.
        """
        try:
            text = self.supplier_combo.lineEdit().text().strip()
        except Exception:
            return
        if not text:
            return
        text_lower = text.lower()
        target_id = None
        for display, sid in self._all_suppliers:
            if display.lower() == text_lower:
                target_id = sid
                break
        if target_id is None:
            for display, sid in self._all_suppliers:
                if text_lower in display.lower():
                    target_id = sid
                    break
        if target_id is None:
            return
        try:
            self._supplier_proxy.setFilterFixedString("")
        except Exception:
            pass
        resolved_idx = self.supplier_combo.findData(target_id)
        if resolved_idx >= 0:
            self.supplier_combo.setCurrentIndex(resolved_idx)
        try:
            self.supplier_combo.hidePopup()
        except Exception:
            pass
        try:
            self.targets_edit.setFocus(Qt.FocusReason.TabFocusReason)
        except Exception:
            pass
        self._supplier_last_fill = ""
        self._supplier_typed_len = 0

    def _on_supplier_text_edited(self, text: str) -> None:
        """UNIQUE-match autofill + filter combo's own popup.

        Uses _all_suppliers cache for unique-match detection (proxy-
        filter-independent). Filter mutation is wrapped in line-edit
        signal blocking + state-restore so the line edit doesn't get
        cleared when the currently-selected row gets filtered out.
        """
        if not text:
            self._supplier_last_fill = ""
            self._supplier_typed_len = 0
            self._supplier_proxy.setFilterFixedString("")
            try:
                self.supplier_combo.hidePopup()
            except Exception:
                pass
            return
        last_fill = getattr(self, "_supplier_last_fill", "")
        typed_len = getattr(self, "_supplier_typed_len", 0)
        text_lower = text.lower()
        is_backspace_collapse = (
            last_fill and len(text) == typed_len
            and text.lower() == last_fill.lower()[:typed_len]
        )
        if is_backspace_collapse:
            self._supplier_last_fill = ""
            self._supplier_typed_len = len(text)
        else:
            unique_match = None
            for display, _sid in self._all_suppliers:
                if text_lower in display.lower():
                    if unique_match is not None:
                        unique_match = None
                        break
                    unique_match = display
            if unique_match is not None and unique_match != text:
                le = self.supplier_combo.lineEdit()
                if le is not None:
                    idx = unique_match.lower().find(text_lower)
                    cursor_at = idx + len(text)
                    le.blockSignals(True)
                    try:
                        le.setText(unique_match)
                        le.setCursorPosition(cursor_at)
                        if cursor_at < len(unique_match):
                            le.setSelection(cursor_at, len(unique_match) - cursor_at)
                    finally:
                        le.blockSignals(False)
                    self._supplier_last_fill = unique_match
                    self._supplier_typed_len = cursor_at
            elif unique_match is None:
                self._supplier_last_fill = ""
                self._supplier_typed_len = len(text)
        # Filter combo's popup, preserving line edit state across the swap.
        le = self.supplier_combo.lineEdit()
        if le is not None:
            saved_text = le.text()
            saved_cursor = le.cursorPosition()
            saved_sel_start = le.selectionStart()
            saved_sel_len = len(le.selectedText())
            try:
                self.supplier_combo.blockSignals(True)
                le.blockSignals(True)
                self._supplier_proxy.setFilterFixedString(text)
                if le.text() != saved_text:
                    le.setText(saved_text)
                    le.setCursorPosition(saved_cursor)
                    if saved_sel_start >= 0 and saved_sel_len > 0:
                        le.setSelection(saved_sel_start, saved_sel_len)
            finally:
                le.blockSignals(False)
                self.supplier_combo.blockSignals(False)
            try:
                if self._supplier_proxy.rowCount() > 0:
                    self.supplier_combo.hidePopup()
                    self.supplier_combo.showPopup()
                    le.setFocus(Qt.FocusReason.OtherFocusReason)
                else:
                    self.supplier_combo.hidePopup()
            except Exception:
                pass

    def _on_supplier_changed(self, index: int) -> None:
        if index < 0:
            return
        self._batch_supplier_id = self.supplier_combo.itemData(index) or ""
        # Reflect new supplier in the preflight line.
        try:
            self._refresh_plan_preview()
        except Exception:
            pass

    @staticmethod
    def _add_labeled_control(layout: QHBoxLayout, label: str, widget: QWidget) -> None:
        layout.addWidget(QLabel(label))
        layout.addWidget(widget)

    def _connect_ui(self) -> None:
        self.callers_edit.textChanged.connect(self._refresh_plan_preview)
        self.targets_edit.textChanged.connect(self._refresh_plan_preview)
        self.mode_combo.currentIndexChanged.connect(self._refresh_plan_preview)
        self.pass_combo.currentIndexChanged.connect(self._refresh_hold_enabled)
        self.pass_combo.currentIndexChanged.connect(self._refresh_plan_preview)
        self.parallel_spin.valueChanged.connect(self._refresh_plan_preview)
        self.hold_spin.valueChanged.connect(self._refresh_plan_preview)
        self.timeout_spin.valueChanged.connect(self._refresh_plan_preview)
        self.run_btn.clicked.connect(self._on_run_clicked)
        self.stop_btn.clicked.connect(self._on_cancel_clicked)
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        self.cancel_btn.clicked.connect(self.close)
        self.export_btn.clicked.connect(self._on_export_clicked)

    def _spec_from_ui(self) -> PlanSpec:
        return PlanSpec(
            callers=normalise_lines(self.callers_edit.toPlainText()),
            targets=normalise_lines(self.targets_edit.toPlainText()),
            mode=self.mode_combo.currentData(),
            pass_criterion=self.pass_combo.currentData(),
            parallel=self.parallel_spin.value(),
            hold_seconds=float(self.hold_spin.value()),
            timeout_seconds=float(self.timeout_spin.value()),
        )

    def _refresh_plan_preview(self) -> None:
        spec = self._spec_from_ui()
        count = len(expand(spec))
        self.run_btn.setText(
            "▶ Run 1 call" if count == 1 else f"▶ Run {count} calls"
        )
        self.run_btn.setEnabled(count > 0 and self.runner is None)
        # Update the targets-tab badge with the live count.
        if hasattr(self, "_tab_targets_btn"):
            self._tab_targets_btn.setText(
                "Targets (1)" if count == 1 else f"Targets ({count})"
            )
        # Update Callers tab badge -- "(auto)" when blank, otherwise count.
        if hasattr(self, "_tab_callers_btn"):
            caller_lines = [
                l for l in (self.callers_edit.toPlainText() or "").splitlines() if l.strip()
            ]
            self._tab_callers_btn.setText(
                "Callers (auto)" if not caller_lines else f"Callers ({len(caller_lines)})"
            )
        # Legacy count badge (kept hidden but updated for any consumer).
        if hasattr(self, "_run_count_badge"):
            self._run_count_badge.setText(
                "1 call" if count == 1 else f"{count} calls"
            )
        # Pre-flight summary -- the headline.
        if hasattr(self, "_preflight_label"):
            self._preflight_label.setText(self._preflight_text(count, spec))

    def _preflight_text(self, count: int, spec) -> str:
        if count == 0:
            return "Paste destinations into Targets to preview the run"
        parallel = max(1, int(spec.parallel))
        hold = max(0, int(spec.hold_seconds))
        timeout = max(0, int(spec.timeout_seconds))
        # Reachability tests run roughly until ringing; full-call adds hold.
        per_call_s = (hold if spec.pass_criterion == "full-call" else 0) + 4
        per_call_s = max(per_call_s, 2)
        per_call_s = min(per_call_s, timeout + 2)
        # Total wall time = ceil(count / parallel) * per_call duration
        import math
        eta_s = max(per_call_s, math.ceil(count / parallel) * per_call_s)
        m, s = divmod(int(eta_s), 60)
        eta_str = f"{m}m {s:02d}s" if m else f"{s}s"
        # Active supplier label (if any)
        supplier_part = ""
        try:
            if self._batch_supplier_id and self.supplier_combo.count():
                supplier_part = " via " + (self.supplier_combo.currentText() or "")
        except Exception:
            pass
        plural = "call" if count == 1 else "calls"
        return (
            f"Will run {count} {plural}{supplier_part} @ "
            f"parallel={parallel}, hold={hold}s  ≈  {eta_str}"
        )

    def _refresh_hold_enabled(self) -> None:
        self.hold_spin.setEnabled(self.pass_combo.currentData() == "full-call")

    def _on_run_clicked(self) -> None:
        spec = self._spec_from_ui()
        calls = expand(spec)
        if not calls or self.runner is not None:
            self._refresh_plan_preview()
            return

        self.results = []
        self._row_by_call_index = {}
        self.table.setRowCount(0)
        for call in calls:
            self._append_call_row(call)

        # Reset the integer running counter at the start of every run.
        # If a stale call_started signal from a previous run arrived
        # after run_complete (queued Qt signals + deferred deleteLater
        # of the parent-pinned runner), _running_count could leak a
        # non-zero baseline into the new run -- the "running" chip
        # would be wrong from the first tick.
        self._running_count = 0

        self.export_btn.setEnabled(False)
        # Stop (header) toggles with run state; Close (footer) stays
        self._refresh_summary()

        # Stash the picked supplier id on the relevant accounts so the
        # runner's _apply_routing_to_target() picks it up. Only meaningful
        # for teles/genband accounts; "other" accounts ignore it.
        for acc in self.accounts:
            kind = (getattr(acc, "switch_type", "other") or "other").lower()
            if kind in ("teles", "genband"):
                setattr(acc, "_active_supplier_id", self._batch_supplier_id)

        # Construct the runner BEFORE enabling Stop. If Runner.__init__
        # raises (e.g. endpoint=None resolution path) the Stop button
        # used to stick in the enabled state with self.runner=None and
        # clicking it AttributeError'd on .cancel(). Now Stop only
        # turns on once we have an actual Runner to cancel.
        self.runner = Runner(spec, self.accounts, self)
        self.runner.call_started.connect(self._on_call_started)
        self.runner.call_completed.connect(self._on_call_completed)
        self.runner.run_complete.connect(self._on_run_complete)
        self.stop_btn.setEnabled(True)
        self._refresh_plan_preview()
        self.runner.start()

    def _on_call_started(self, call_index: int) -> None:
        row = self._row_by_call_index.get(call_index)
        if row is not None:
            # Install a proper RUNNING badge widget; previous code wrote
            # plain text "running" which the footer counter (which scans
            # for badge text "RUNNING") never matched, so the "running"
            # chip was stuck at 0 throughout the run.
            self._set_result_badge(row, "running")
        # Integer counter -- see _refresh_summary docstring for why
        # this beats scanning every table row's findChild on each event.
        self._running_count = getattr(self, "_running_count", 0) + 1
        self._refresh_summary()

    def _on_call_completed(self, result: RunnerResult) -> None:
        self.results.append(result)
        row = self._row_by_call_index.get(result.call.index)
        if row is None:
            row = self._append_call_row(result.call)
        self._populate_result_row(row, result)
        self.export_btn.setEnabled(True)
        self._running_count = max(0, getattr(self, "_running_count", 0) - 1)
        self._refresh_summary()

    def _on_run_complete(self, results: list[RunnerResult]) -> None:
        self.results = list(results)
        # Reclaim the Runner promptly. parent=self pins it to the
        # window until window-close otherwise; deleteLater fires the
        # destroyed signal NOW so subscribers tear down before the
        # next Run cycle stacks a fresh Runner.
        if self.runner is not None:
            try:
                self.runner.deleteLater()
            except Exception:
                pass
        self.runner = None
        # Reset the running counter for the next cycle.
        self._running_count = 0
        self.stop_btn.setEnabled(False)
        self.export_btn.setEnabled(bool(self.results))
        self._refresh_plan_preview()
        self._refresh_summary()

    def _on_cancel_clicked(self) -> None:
        # Disable Stop immediately so a double-click can't re-trigger
        # cancel() while the first cancel is still unwinding the
        # _active dict (which iterates with a list() snapshot but
        # any handler that runs in between via processEvents could
        # otherwise re-enter and misbehave).
        self.stop_btn.setEnabled(False)
        if self.runner is not None:
            self.runner.cancel()

    def _on_clear_clicked(self) -> None:
        """Wipe the results table without cancelling a live run."""
        if self.runner is not None:
            return
        self.results = []
        self._row_by_call_index = {}
        self.table.setRowCount(0)
        self.export_btn.setEnabled(False)
        self._refresh_summary()

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001
        if self.runner is not None:
            self.runner.cancel()
            event.ignore()
            return
        super().closeEvent(event)

    def _on_export_clicked(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export CSV",
            "",
            "CSV files (*.csv);;All files (*)",
        )
        if filename:
            self.export_csv(Path(filename))

    @staticmethod
    def _csv_safe(value):
        """Prefix `'` when a CSV field starts with a character Excel/Sheets
        would interpret as a formula trigger. Same logic the
        cdr_detail_dialog.py export already uses -- mirrors OWASP
        CSV-injection guidance. A malicious dial-string like
        ``=cmd|'/c calc'!A1`` would otherwise execute on open."""
        if value is None:
            return ""
        s = str(value)
        if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + s
        return s

    def export_csv(self, path: Path) -> None:
        safe = self._csv_safe
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(CSV_HEADER)
            for result in self.results:
                started = self._started_at_datetime(result.started_at)
                writer.writerow(
                    [
                        f"nb-{started:%Y%m%d-%H%M%S}-{result.call.index:03d}",
                        self._format_started_at(started),
                        safe(result.from_account),
                        safe(result.to_uri),
                        safe(result.result),
                        "" if result.sip_code is None else result.sip_code,
                        safe(result.sip_reason),
                        "" if result.rtt_ms is None else int(result.rtt_ms),
                        f"{result.duration_s:.1f}",
                        safe(result.notes),
                        safe(getattr(result, "fas_verdict", "") or ""),
                        f"{getattr(result, 'fas_confidence', 0.0):.2f}"
                            if getattr(result, "fas_verdict", "") else "",
                        safe(getattr(result, "fas_reasons", "") or ""),
                    ]
                )

    def _append_call_row(self, call: PlanCall) -> int:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._row_by_call_index[call.index] = row
        for column, text in enumerate(
            [
                str(call.index),
                call.caller_number,
                call.target_number,
                "queued",
                "",      # FAS (column 4)
                "",
                "",
                "",
                "",
            ]
        ):
            self._set_text(row, column, text)
        return row

    # ------------------------------------------------------------------
    def _populate_result_row(self, row: int, result: RunnerResult) -> None:
        code = "" if result.sip_code is None else str(result.sip_code)
        if result.sip_reason:
            code = f"{code} {result.sip_reason}".strip()
        rtt = "" if result.rtt_ms is None else f"{int(result.rtt_ms)} ms"
        # Columns: # / FROM / TO / RESULT(3) / FAS(4) / CODE(5) / RTT(6) / TIME(7) / NOTES(8)
        text_columns = {
            0: str(result.call.index),
            1: result.from_account,
            2: result.to_uri,
            5: code,
            6: rtt,
            7: f"{result.duration_s:.1f} s",
            8: result.notes,
        }
        for column, value in text_columns.items():
            self._set_text(row, column, value)
        self._set_result_badge(row, result.result)
        self._set_fas_badge(
            row,
            getattr(result, "fas_verdict", "") or "",
            float(getattr(result, "fas_confidence", 0.0) or 0.0),
            getattr(result, "fas_reasons", "") or "",
        )

    def _set_fas_badge(self, row: int, verdict: str, confidence: float, reasons: str) -> None:
        """Render FAS column as a coloured pill, mirroring the FasBadge in
        the call card. Empty verdict renders an em-dash so the column
        doesn't look broken on calls that never reached CONFIRMED."""
        from noc_beam.ui.components import FasBadge

        self.table.takeItem(row, 4)
        if not verdict:
            placeholder = QLabel("—")
            placeholder.setObjectName("TestRunnerFasPlaceholder")
            placeholder.setAlignment(
                __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.AlignmentFlag.AlignCenter
            )
            wrapper = QWidget()
            wl = QHBoxLayout(wrapper)
            wl.setContentsMargins(6, 2, 6, 2)
            wl.addWidget(placeholder)
            self.table.setCellWidget(row, 4, wrapper)
            return
        badge = FasBadge(verdict)
        badge.update_verdict(verdict, confidence, reasons)
        wrapper = QWidget()
        wl = QHBoxLayout(wrapper)
        wl.setContentsMargins(6, 2, 6, 2)
        wl.addWidget(badge)
        self.table.setCellWidget(row, 4, wrapper)

    def _set_result_badge(self, row: int, result: str) -> None:
        """Render the RESULT column as a coloured pill badge."""
        # Normalise the level for QSS branching.
        level = result.lower() if result else "queued"
        if level not in ("pass", "fail", "running", "queued"):
            level = "queued"
        # Clear any previous text item so the cell widget owns the cell.
        self.table.takeItem(row, 3)
        badge = QLabel(result.upper() if result else "QUEUED")
        badge.setObjectName("TestRunnerBadge")
        badge.setProperty("level", level)
        badge.setAlignment(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.AlignmentFlag.AlignCenter)
        # Wrap the label in a small frame so the cell has padding.
        wrapper = QWidget()
        wl = QHBoxLayout(wrapper)
        wl.setContentsMargins(6, 2, 6, 2)
        wl.addWidget(badge)
        self.table.setCellWidget(row, 3, wrapper)

    def _refresh_summary(self) -> None:
        passed = sum(1 for result in self.results if result.result == "PASS")
        failed = sum(1 for result in self.results if result.result == "FAIL")
        # Running count maintained as an integer (incremented in
        # _on_call_started, decremented in _on_call_completed). Was
        # scanning every table row's findChild(QLabel) on every signal
        # tick -- O(N) per event, quadratic over the whole run.
        running = getattr(self, "_running_count", 0)
        completed = passed + failed + running
        pending = max(0, self.table.rowCount() - completed)
        self.summary_passed.setText(f"{passed} passed")
        self.summary_failed.setText(f"{failed} failed")
        self.summary_running.setText(f"{running} running")
        self.summary_pending.setText(f"{pending} pending")

    def _set_text(self, row: int, column: int, text: str) -> None:
        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, column, item)
        item.setText(text)

    @staticmethod
    def _started_at_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        return datetime.fromtimestamp(float(value), UTC)

    @staticmethod
    def _format_started_at(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")
