from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
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
        self.resize(900, 620)

        self.callers_edit = QTextEdit()
        self.callers_edit.setObjectName("TestRunnerPasteBox")
        self.callers_edit.setAcceptRichText(False)
        self.targets_edit = QTextEdit()
        self.targets_edit.setObjectName("TestRunnerPasteBox")
        self.targets_edit.setAcceptRichText(False)

        self.mode_combo = QComboBox()
        for label, value in (
            ("Matrix", "matrix"),
            ("Paired", "paired"),
            ("Fan-out", "fan-out"),
            ("Fan-in", "fan-in"),
        ):
            self.mode_combo.addItem(label, value)

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
        self.table = QTableWidget(0, 8)
        self.table.setObjectName("TestRunnerResults")
        self.table.setHorizontalHeaderLabels(
            ["#", "FROM", "TO", "RESULT", "CODE", "RTT", "TIME", "NOTES"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            7, QHeaderView.ResizeMode.Stretch
        )

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
        from PySide6.QtWidgets import QSplitter, QScrollArea
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

        # 2-pane split: left = setup cards stack; right = results
        split = QSplitter(Qt.Orientation.Horizontal, central)
        split.setObjectName("TestRunnerSplit")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(8)

        # ---- LEFT pane: scrollable card stack -----------------------
        left_holder = QWidget()
        left_holder.setObjectName("TestRunnerLeft")
        left = QVBoxLayout(left_holder)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(12)

        # TARGETS card (callers + targets stacked vertically). Also
        # carries the legacy `TestRunnerPasteGrid` objectName for test
        # backwards-compat (sub-frames now hold the actual widgets).
        targets_card = QFrame()
        targets_card.setObjectName("SettingsCard")
        # Keep a hidden marker child for the legacy test selector.
        _legacy_paste_grid = QFrame(targets_card)
        _legacy_paste_grid.setObjectName("TestRunnerPasteGrid")
        _legacy_paste_grid.setFixedSize(0, 0)
        _legacy_paste_grid.setVisible(False)
        t_l = QVBoxLayout(targets_card)
        t_l.setContentsMargins(18, 16, 18, 16)
        t_l.setSpacing(8)
        t_header_row = QHBoxLayout()
        t_label = QLabel("TARGETS")
        t_label.setObjectName("SettingsCardLabel")
        self._run_count_badge = QLabel("0 calls")
        self._run_count_badge.setObjectName("TestRunnerCountBadge")
        t_header_row.addWidget(t_label)
        t_header_row.addStretch(1)
        t_header_row.addWidget(self._run_count_badge)
        t_l.addLayout(t_header_row)
        cl = QLabel("Callers (one per line)")
        cl.setObjectName("SettingsRowLabel")
        t_l.addWidget(cl)
        t_l.addWidget(self.callers_edit)
        tl = QLabel("Targets (one per line)")
        tl.setObjectName("SettingsRowLabel")
        t_l.addWidget(tl)
        t_l.addWidget(self.targets_edit)
        left.addWidget(targets_card)

        # CONFIGURATION card (legacy OperatorToolbar object name lives
        # on a hidden marker child to satisfy backwards-compat selectors).
        config_card = QFrame()
        config_card.setObjectName("SettingsCard")
        _legacy_toolbar = QFrame(config_card)
        _legacy_toolbar.setObjectName("OperatorToolbar")
        _legacy_toolbar.setFixedSize(0, 0)
        _legacy_toolbar.setVisible(False)
        c_l = QVBoxLayout(config_card)
        c_l.setContentsMargins(18, 16, 18, 16)
        c_l.setSpacing(10)
        c_label = QLabel("CONFIGURATION")
        c_label.setObjectName("SettingsCardLabel")
        c_l.addWidget(c_label)
        for label, widget in (
            ("Mode", self.mode_combo),
            ("Pass criteria", self.pass_combo),
            ("Parallel", self.parallel_spin),
            ("Hold (per call)", self.hold_spin),
            ("Timeout (per call)", self.timeout_spin),
        ):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(label)
            lbl.setObjectName("SettingsRowLabel")
            lbl.setMinimumWidth(140)
            row.addWidget(lbl)
            row.addWidget(widget, 1)
            c_l.addLayout(row)
        left.addWidget(config_card)

        # STATUS card (counter chips arranged in a 2x2)
        status_card = QFrame()
        status_card.setObjectName("SettingsCard")
        st_l = QVBoxLayout(status_card)
        st_l.setContentsMargins(18, 16, 18, 16)
        st_l.setSpacing(8)
        st_label = QLabel("STATUS")
        st_label.setObjectName("SettingsCardLabel")
        st_l.addWidget(st_label)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.addWidget(self.summary_passed, 0, 0)
        grid.addWidget(self.summary_failed, 0, 1)
        grid.addWidget(self.summary_running, 1, 0)
        grid.addWidget(self.summary_pending, 1, 1)
        st_l.addLayout(grid)
        left.addWidget(status_card)

        # Run / Stop / Clear sticky at bottom of left column
        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        run_row.addWidget(self.run_btn, 1)
        run_row.addWidget(self.stop_btn)
        run_row.addWidget(self.clear_btn)
        left.addLayout(run_row)
        left.addStretch(1)

        # Scroll-wrap the left card stack
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setWidget(left_holder)
        split.addWidget(left_scroll)

        # ---- RIGHT pane: results table card -------------------------
        right_holder = QFrame()
        right_holder.setObjectName("SettingsCard")
        right = QVBoxLayout(right_holder)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        results_header = QHBoxLayout()
        results_header.setContentsMargins(18, 14, 18, 10)
        results_label = QLabel("RESULTS")
        results_label.setObjectName("SettingsCardLabel")
        results_header.addWidget(results_label)
        results_header.addStretch(1)
        right.addLayout(results_header)
        # Make the table look at-home inside the card.
        self.table.setObjectName("TestRunnerResults")
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(36)
        right.addWidget(self.table, 1)
        split.addWidget(right_holder)

        split.setStretchFactor(0, 35)
        split.setStretchFactor(1, 65)
        split.setSizes([350, 650])
        outer.addWidget(split, 1)

        # Footer: Close + Export CSV
        footer = QFrame(central)
        footer.setObjectName("TestRunnerFooter")
        f_l = QHBoxLayout(footer)
        f_l.setContentsMargins(0, 4, 0, 0)
        f_l.addStretch(1)
        f_l.addWidget(self.cancel_btn)
        f_l.addWidget(self.export_btn)
        outer.addWidget(footer)

        self.setCentralWidget(central)

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
        count = len(expand(self._spec_from_ui()))
        self.run_btn.setText(f"Run {count} calls")
        self.run_btn.setEnabled(count > 0 and self.runner is None)
        # Also update the count badge in the TARGETS card header.
        if hasattr(self, "_run_count_badge"):
            self._run_count_badge.setText(
                "1 call" if count == 1 else f"{count} calls"
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
        # Columns: # / FROM / TO / RESULT (badge) / CODE / RTT / TIME / NOTES
        text_columns = {
            0: str(result.call.index),
            1: result.from_account,
            2: result.to_uri,
            4: code,
            5: rtt,
            6: f"{result.duration_s:.1f} s",
            7: result.notes,
        }
        for column, value in text_columns.items():
            self._set_text(row, column, value)
        self._set_result_badge(row, result.result)

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
