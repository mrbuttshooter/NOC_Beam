from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        paste_frame = QFrame(central)
        paste_frame.setObjectName("TestRunnerPasteGrid")
        paste_grid = QGridLayout(paste_frame)
        paste_grid.setContentsMargins(0, 0, 0, 0)
        paste_grid.setColumnStretch(0, 1)
        paste_grid.setColumnStretch(1, 1)
        paste_grid.addWidget(QLabel("CALLERS"), 0, 0)
        paste_grid.addWidget(QLabel("TARGETS"), 0, 1)
        paste_grid.addWidget(self.callers_edit, 1, 0)
        paste_grid.addWidget(self.targets_edit, 1, 1)
        layout.addWidget(paste_frame, 1)

        controls_frame = QFrame(central)
        controls_frame.setObjectName("OperatorToolbar")
        controls = QHBoxLayout(controls_frame)
        controls.setContentsMargins(6, 6, 6, 6)
        controls.setSpacing(8)
        self._add_labeled_control(controls, "Mode", self.mode_combo)
        self._add_labeled_control(controls, "Pass", self.pass_combo)
        self._add_labeled_control(controls, "Parallel", self.parallel_spin)
        self._add_labeled_control(controls, "Hold", self.hold_spin)
        self._add_labeled_control(controls, "Timeout", self.timeout_spin)
        controls.addStretch(1)
        layout.addWidget(controls_frame)

        run_row = QHBoxLayout()
        run_row.addWidget(self.run_btn, 1)
        run_row.addWidget(self.stop_btn)
        run_row.addWidget(self.clear_btn)
        layout.addLayout(run_row)

        layout.addWidget(self.table, 3)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addWidget(self.summary_passed)
        footer.addWidget(self.summary_failed)
        footer.addWidget(self.summary_running)
        footer.addWidget(self.summary_pending)
        footer.addStretch(1)
        footer.addWidget(self.cancel_btn)
        footer.addWidget(self.export_btn)
        layout.addLayout(footer)

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

        self.export_btn.setEnabled(False)
        # Stop (header) toggles with run state; Close (footer) stays
        # always enabled -- user should always be able to close the
        # window even between runs.
        self.stop_btn.setEnabled(True)
        self._refresh_summary()

        self.runner = Runner(spec, self.accounts, self)
        self.runner.call_started.connect(self._on_call_started)
        self.runner.call_completed.connect(self._on_call_completed)
        self.runner.run_complete.connect(self._on_run_complete)
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
        self._refresh_summary()

    def _on_call_completed(self, result: RunnerResult) -> None:
        self.results.append(result)
        row = self._row_by_call_index.get(result.call.index)
        if row is None:
            row = self._append_call_row(result.call)
        self._populate_result_row(row, result)
        self.export_btn.setEnabled(True)
        self._refresh_summary()

    def _on_run_complete(self, results: list[RunnerResult]) -> None:
        self.results = list(results)
        self.runner = None
        self.stop_btn.setEnabled(False)
        self.export_btn.setEnabled(bool(self.results))
        self._refresh_plan_preview()
        self._refresh_summary()

    def _on_cancel_clicked(self) -> None:
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

    def export_csv(self, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(CSV_HEADER)
            for result in self.results:
                started = self._started_at_datetime(result.started_at)
                writer.writerow(
                    [
                        f"nb-{started:%Y%m%d-%H%M%S}-{result.call.index:03d}",
                        self._format_started_at(started),
                        result.from_account,
                        result.to_uri,
                        result.result,
                        "" if result.sip_code is None else result.sip_code,
                        result.sip_reason,
                        "" if result.rtt_ms is None else int(result.rtt_ms),
                        f"{result.duration_s:.1f}",
                        result.notes,
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
        # Running / pending now read from the RESULT cell widget (a QLabel
        # badge) rather than a text item, since we switched to coloured pills.
        running = 0
        for row in range(self.table.rowCount()):
            w = self.table.cellWidget(row, 3)
            if w is not None:
                badge = w.findChild(QLabel, "TestRunnerBadge")
                if badge is not None and badge.text() == "RUNNING":
                    running += 1
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
