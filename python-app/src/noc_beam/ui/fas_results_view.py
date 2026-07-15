"""FasResultsView -- the Test Runner's FAS Sweep results pane.

A self-contained ``QWidget`` (not a dialog) so Agent C can drop it into
a tab / stacked widget in the Test Runner window. The view is read-only
relative to the FAS engine -- it consumes rows from
:class:`noc_beam.audio.fas_sweep_db.FasSweepDb`, lets the operator
filter by verdict, play back the recorded WAV of any row, and export
the rows + audio as CSV / ZIP for distribution to the offending
supplier.

Layout (top to bottom):

    +-------------------------------------------------+
    | Run sweep_2026-05-25_14:33 -- 18/120 done, 3 FAS|  header
    +-------------------------------------------------+
    | [All] [Confirmed] [Suspicious] [Machine] ...    |  filter chips
    +-------------------------------------------------+
    | Verdict | Supplier | Dest | Try | Dur | Score | |  table
    +-------------------------------------------------+
    | <reasons + score breakdown for selected row>    |  detail
    +-------------------------------------------------+
    | [Play] [Export CSV] [Export evidence ZIP]       |  actions
    +-------------------------------------------------+

Audio playback uses ``QMediaPlayer`` + ``QAudioOutput``. Recorded WAVs
live on disk (paths are stored in the ``calls.wav_path`` column); the
player just hands the file path off to Qt's GStreamer/DirectShow
backend.

The view is designed to construct cleanly against an empty database
(important for the Test Runner opening before any sweep has finished)
and to refresh in-place when ``load_run`` is called with a different
``run_id``.
"""
from __future__ import annotations

import csv
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from noc_beam.audio.fas_sweep_db import CallRow, FasSweepDb


log = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Filter taxonomy
# ----------------------------------------------------------------------
# Maps the filter-chip button label -> the canonical fas_verdict string
# the row must equal to be visible (None = pass-through, show all).
FILTER_CHIPS: list[tuple[str, Optional[str]]] = [
    ("All",                       None),
    ("Confirmed FAS",             "CONFIRMED_FAS"),
    ("Suspicious",                "SUSPICIOUS"),
    ("Machine/Voicemail",         "MACHINE_OR_VOICEMAIL"),
    ("Human-likely",              "HUMAN_LIKELY"),
    ("Inconclusive",              "INCONCLUSIVE"),
    # Failure-mode buckets surfaced by the sweep runner. These are NOT
    # FAS verdicts -- they're sip_final_code / heuristic tags that the
    # runner writes into fas_verdict so a single column tells the whole
    # row's story. Kept in the same enum so the UI doesn't need a
    # second filter dimension.
    ("180 then BYE",              "180_THEN_BYE"),
    ("Early media, no 180",       "EARLY_MEDIA_ONLY_NO_180"),
    ("200 with no 180",           "200_NO_180_WARN"),
    ("Fail / timeout",            "FAIL_TIMEOUT"),
]

# Table columns (logical order matches the on-screen order so we don't
# need a column-index lookup table sprinkled through the code).
COL_VERDICT = 0
COL_SUPPLIER = 1
COL_DEST = 2
COL_TRY = 3
COL_DUR = 4
COL_SCORE = 5
COL_REASONS = 6

COLUMN_LABELS = ("Verdict", "Supplier", "Destination", "Try", "Dur", "Score", "Reasons")

# CSV header order (matches the spec).
CSV_HEADER = (
    "timestamp", "supplier", "destination", "try", "duration_s",
    "sip_code", "verdict", "score", "reasons", "wav_path",
)


def _verdict_text(verdict: Optional[str]) -> str:
    """Pretty display label for a raw verdict tag."""
    if not verdict:
        return ""
    mapping = {
        "CONFIRMED_FAS": "Confirmed FAS",
        "PROBABLE_FAS": "Probable FAS",
        "SUSPICIOUS": "Suspicious",
        "MACHINE_OR_VOICEMAIL": "Machine / Voicemail",
        "IVR_OR_ANNOUNCEMENT": "IVR / Announcement",
        "HUMAN_LIKELY": "Human likely",
        "INCONCLUSIVE": "Inconclusive",
        "ANALYZING": "Analyzing",
        "180_THEN_BYE": "180 then BYE",
        "EARLY_MEDIA_ONLY_NO_180": "Early media, no 180",
        "200_NO_180_WARN": "200, no 180",
        "FAIL_TIMEOUT": "Fail / timeout",
    }
    return mapping.get(verdict, verdict)


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


# Custom data role carrying each row's stable index into ``_all_rows``.
# Stored on the column-0 item so selection/play/export resolve to the
# right CallRow even after the user clicks a header to sort (which
# physically reorders the QTableWidget rows out from under the frozen
# populate order). Mirrors supplier_dropdown.py's setData(UserRole,...)
# identity pattern.
ROW_KEY_ROLE = Qt.ItemDataRole.UserRole
# Private role carrying the numeric sort key for Try/Dur/Score. Kept
# SEPARATE from EditRole on purpose: Qt's item.text() falls back to
# EditRole whenever DisplayRole is empty, so storing the key in EditRole
# made an empty-score cell render its key ("-inf") instead of "". A
# dedicated UserRole slot keeps the displayed text and the sort key
# fully decoupled.
NUM_SORT_ROLE = Qt.ItemDataRole.UserRole + 1


class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts by a numeric key rather than by its
    displayed text. Try/Dur/Score are rendered as strings ("10", "1:05",
    "7"); the default lexicographic ``__lt__`` sorts "10" < "2", which
    misranks the very columns an operator sorts on to find the worst
    offenders. We keep the human formatting for display (DisplayRole) and
    stash the real number in a private role so Qt's sort compares numbers.
    """

    def __init__(self, text: str, sort_key: float) -> None:
        super().__init__(text)
        self.setData(NUM_SORT_ROLE, float(sort_key))

    def __lt__(self, other: QTableWidgetItem) -> bool:
        try:
            return float(self.data(NUM_SORT_ROLE)) < float(
                other.data(NUM_SORT_ROLE)
            )
        except (TypeError, ValueError):
            return super().__lt__(other)


def _row_csv_record(row: CallRow) -> tuple[str, ...]:
    return (
        row.started_at,
        row.supplier_id,
        row.destination_e164,
        str(row.try_idx),
        "" if row.duration_s is None else f"{row.duration_s:.3f}",
        "" if row.sip_final_code is None else str(row.sip_final_code),
        row.fas_verdict or "",
        "" if row.fas_score is None else str(row.fas_score),
        row.fas_reasons or "",
        row.wav_path or "",
    )


class FasResultsView(QWidget):
    """Read-only browser for a single FAS sweep run."""

    # Emitted whenever ``load_run`` is called and the run actually changes.
    run_changed = Signal(str)

    def __init__(self, db: FasSweepDb, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._run_id: str = ""
        self._all_rows: list[CallRow] = []
        self._visible_rows: list[CallRow] = []
        self._filter_verdict: Optional[str] = None

        # Lazy QMediaPlayer / QAudioOutput init -- importing Qt
        # multimedia is non-free (loads the platform's backend DLLs);
        # tests that don't exercise playback shouldn't pay for it.
        self._player = None
        self._audio_output = None

        self._build_ui()
        self._populate_table()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def load_run(self, run_id: str) -> None:
        """Switch the view to a different run. Empty string = clear."""
        if run_id == self._run_id:
            # Same run; reload in case rows were appended.
            self._reload_rows()
            return
        self._run_id = run_id
        self._reload_rows()
        self.run_changed.emit(run_id)

    def current_run_id(self) -> str:
        return self._run_id

    def selected_rows(self) -> list[CallRow]:
        """Rows currently selected in the table. Falls back to all visible
        rows if the selection is empty (matches the export-action UX:
        export selected, or all if none).
        """
        out: list[CallRow] = []
        for idx in self.table.selectionModel().selectedRows():
            # Resolve through the stable key stored on the column-0 item
            # rather than positional indexing into _visible_rows: sorting
            # physically reorders the view rows, so idx.row() no longer
            # lines up with the frozen populate order. Indexing positionally
            # here is exactly what shipped the wrong supplier's audio/CSV.
            row = self._row_for_view_index(idx.row())
            if row is not None:
                out.append(row)
        return out or list(self._visible_rows)

    def _row_for_view_index(self, view_row: int) -> Optional[CallRow]:
        """Map a physical (possibly post-sort) table row back to its
        CallRow via the stable index stored on the column-0 item."""
        item = self.table.item(view_row, COL_VERDICT)
        if item is None:
            return None
        key = item.data(ROW_KEY_ROLE)
        if key is None:
            return None
        try:
            idx = int(key)
        except (TypeError, ValueError):
            return None
        if 0 <= idx < len(self._visible_rows):
            return self._visible_rows[idx]
        return None

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ----- Header -------------------------------------------------
        self.header_label = QLabel("No run selected")
        self.header_label.setObjectName("FasResultsHeader")
        self.header_label.setProperty("section", "results")
        layout.addWidget(self.header_label)

        # ----- Filter chips ------------------------------------------
        chips_row = QHBoxLayout()
        chips_row.setContentsMargins(0, 0, 0, 0)
        chips_row.setSpacing(4)
        self._chip_buttons: dict[Optional[str], QPushButton] = {}
        for label, verdict_key in FILTER_CHIPS:
            btn = QPushButton(label)
            btn.setObjectName("FasFilterChip")
            btn.setCheckable(True)
            btn.setProperty("verdictKey", verdict_key or "")
            btn.clicked.connect(lambda _checked=False, v=verdict_key: self._set_filter(v))
            chips_row.addWidget(btn)
            self._chip_buttons[verdict_key] = btn
        chips_row.addStretch(1)
        # Default = "All" pressed.
        all_btn = self._chip_buttons.get(None)
        if all_btn is not None:
            all_btn.setChecked(True)
        chips_wrap = QFrame()
        chips_wrap.setObjectName("FasFilterChipRow")
        chips_wrap.setLayout(chips_row)
        layout.addWidget(chips_wrap)

        # ----- Empty state (design system rule 8) --------------------
        # Muted one-line invitation shown when the run has no rows. The
        # table's own column headers stay visible below it; this hint
        # tells the operator where results come from.
        self._empty_hint = QLabel(
            "No sweep results yet — run a FAS Sweep from the Test Runner "
            "to populate this view."
        )
        self._empty_hint.setObjectName("FasResultsEmpty")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setWordWrap(True)
        layout.addWidget(self._empty_hint)

        # ----- Results table -----------------------------------------
        self.table = QTableWidget(0, len(COLUMN_LABELS))
        self.table.setObjectName("FasResultsTable")
        self.table.setHorizontalHeaderLabels(list(COLUMN_LABELS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_REASONS, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table, 1)

        # ----- Detail panel ------------------------------------------
        self.detail_text = QTextEdit()
        self.detail_text.setObjectName("FasResultsDetail")
        self.detail_text.setReadOnly(True)
        self.detail_text.setMaximumHeight(110)
        self.detail_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.detail_text.setPlaceholderText("Select a row to see its reasons + score breakdown")
        layout.addWidget(self.detail_text)

        # ----- Action row --------------------------------------------
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        self.play_btn = QPushButton("Play audio")
        self.play_btn.setObjectName("FasResultsPlayBtn")
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self._on_play_clicked)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("FasResultsStopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        self.export_csv_btn = QPushButton("Export selected CSV")
        self.export_csv_btn.setObjectName("FasResultsExportCsvBtn")
        self.export_csv_btn.clicked.connect(self._on_export_csv_clicked)
        self.export_zip_btn = QPushButton("Export evidence ZIP")
        self.export_zip_btn.setObjectName("FasResultsExportZipBtn")
        self.export_zip_btn.clicked.connect(self._on_export_zip_clicked)
        actions.addWidget(self.play_btn)
        actions.addWidget(self.stop_btn)
        actions.addStretch(1)
        actions.addWidget(self.export_csv_btn)
        actions.addWidget(self.export_zip_btn)
        actions_wrap = QFrame()
        actions_wrap.setObjectName("FasResultsActionsRow")
        actions_wrap.setLayout(actions)
        layout.addWidget(actions_wrap)

    # ------------------------------------------------------------------
    # Data flow
    # ------------------------------------------------------------------
    def _reload_rows(self) -> None:
        if self._run_id:
            try:
                self._all_rows = list(self._db.get_run(self._run_id))
            except Exception:
                log.exception("Failed to load run %s", self._run_id)
                self._all_rows = []
        else:
            self._all_rows = []
        self._populate_table()

    def _populate_table(self) -> None:
        # Apply filter.
        if self._filter_verdict is None:
            self._visible_rows = list(self._all_rows)
        else:
            self._visible_rows = [
                r for r in self._all_rows if (r.fas_verdict or "") == self._filter_verdict
            ]

        # Update header.
        total = len(self._all_rows)
        fas_count = sum(
            1 for r in self._all_rows if (r.fas_verdict or "") == "CONFIRMED_FAS"
        )
        if not self._run_id:
            self.header_label.setText("No run selected")
        else:
            self.header_label.setText(
                f"Run {self._run_id} — {len(self._visible_rows)} of {total} shown, "
                f"{fas_count} CONFIRMED_FAS"
            )

        # Disable sorting during bulk repopulate -- pyside trips up on
        # itemChanged callbacks during a sort pass.
        self.table.setSortingEnabled(False)
        # Clear selection BEFORE shrinking the model. Qt retains selected
        # row indices across setRowCount cycles; without this, a row that
        # was selected pre-reload (e.g. row 5) ends up auto-selected in
        # the new run -- and the user clicking Play hears audio from a
        # different call than they intended.
        sel = self.table.selectionModel()
        if sel is not None:
            sel.clearSelection()
        self.table.setRowCount(0)
        self.table.setRowCount(len(self._visible_rows))
        for row_idx, row in enumerate(self._visible_rows):
            # Stamp the stable index into _visible_rows onto the column-0
            # item so post-sort selection/play/export resolve to the right
            # CallRow (see selected_rows / _row_for_view_index).
            self._set_cell(row_idx, COL_VERDICT, _verdict_text(row.fas_verdict),
                           row_key=row_idx)
            self._set_cell(row_idx, COL_SUPPLIER, row.supplier_id)
            self._set_cell(row_idx, COL_DEST, row.destination_e164)
            # Try/Dur/Score use numeric sort keys so header-click sorting
            # ranks "10" after "2" instead of lexicographically before it.
            self._set_cell(row_idx, COL_TRY, str(row.try_idx),
                           sort_key=float(row.try_idx))
            self._set_cell(row_idx, COL_DUR, _format_duration(row.duration_s),
                           sort_key=(row.duration_s if row.duration_s is not None else -1.0))
            score_text = "" if row.fas_score is None else str(row.fas_score)
            # Missing scores sort below any real score (float('-inf')).
            self._set_cell(row_idx, COL_SCORE, score_text,
                           sort_key=(float(row.fas_score) if row.fas_score is not None
                                     else float("-inf")))
            self._set_cell(row_idx, COL_REASONS, row.fas_reasons or "")
        self.table.setSortingEnabled(True)

        # Toggle the muted empty-state hint: shown only when the current
        # (filtered) view has no rows to display.
        if hasattr(self, "_empty_hint"):
            self._empty_hint.setVisible(not self._visible_rows)

        # Clear the detail / action enablement on repopulate.
        self.detail_text.clear()
        self.play_btn.setEnabled(False)

    def _set_cell(
        self,
        row: int,
        col: int,
        text: str,
        sort_key: float | None = None,
        row_key: int | None = None,
    ) -> None:
        # Numeric columns get a _NumericItem so sorting compares numbers,
        # not the formatted display text (see _NumericItem docstring).
        if sort_key is not None:
            item: QTableWidgetItem = _NumericItem(text, sort_key)
        else:
            item = QTableWidgetItem(text)
        # Centre numeric columns; left-align text columns.
        if col in (COL_TRY, COL_DUR, COL_SCORE):
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        # Stash the stable row identity on the column-0 item so a sorted
        # view resolves back to the correct CallRow.
        if row_key is not None:
            item.setData(ROW_KEY_ROLE, int(row_key))
        self.table.setItem(row, col, item)

    # ------------------------------------------------------------------
    # Filter chips
    # ------------------------------------------------------------------
    def _set_filter(self, verdict_key: Optional[str]) -> None:
        self._filter_verdict = verdict_key
        # Toggle the chip checkstate so only one chip is pressed at a time.
        for key, btn in self._chip_buttons.items():
            btn.setChecked(key == verdict_key)
        self._populate_table()

    # ------------------------------------------------------------------
    # Selection / detail panel
    # ------------------------------------------------------------------
    def _on_selection_changed(self) -> None:
        rows = self.selected_rows()
        # selected_rows() falls back to "all visible" when nothing is
        # explicitly selected. The play button only makes sense for
        # exactly one row, so gate it on the model selection size.
        explicit = self.table.selectionModel().selectedRows()
        row = self._row_for_view_index(explicit[0].row()) if len(explicit) == 1 else None
        if row is not None:
            self._render_detail(row)
            self.play_btn.setEnabled(bool(row.wav_path))
        else:
            self.detail_text.clear()
            self.play_btn.setEnabled(False)
        # Keep the local variable referenced so static-analyzers don't
        # flag "unused" -- `rows` is the user-visible fallback set.
        del rows

    def _render_detail(self, row: CallRow) -> None:
        lines = [
            f"Supplier: {row.supplier_id}",
            f"Destination: {row.destination_e164}",
            f"Started: {row.started_at}    Try #{row.try_idx}",
            f"Duration: {_format_duration(row.duration_s)}    "
            f"SIP final: {row.sip_final_code if row.sip_final_code is not None else '-'}",
            f"Verdict: {_verdict_text(row.fas_verdict)}    "
            f"Score: {row.fas_score if row.fas_score is not None else '-'}",
            "",
            "Reasons:",
        ]
        for tag in (row.fas_reasons or "").split(","):
            tag = tag.strip()
            if tag:
                lines.append(f"  • {tag}")
        if row.wav_path:
            lines.append("")
            lines.append(f"Audio: {row.wav_path}")
        self.detail_text.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------
    # Audio playback
    # ------------------------------------------------------------------
    def _ensure_player(self) -> bool:
        """Lazy-init the Qt multimedia player. Returns False if the
        backend is unavailable (e.g. headless / missing codecs)."""
        if self._player is not None:
            return True
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            self._audio_output = QAudioOutput(self)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio_output)
            # Re-enable Stop only while playing.
            self._player.playbackStateChanged.connect(self._on_playback_state)
            return True
        except Exception:
            log.exception("QtMultimedia is unavailable; audio playback disabled")
            return False

    def _on_playback_state(self, state) -> None:
        try:
            from PySide6.QtMultimedia import QMediaPlayer

            is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        except Exception:
            is_playing = False
        self.stop_btn.setEnabled(is_playing)

    def _on_play_clicked(self) -> None:
        explicit = self.table.selectionModel().selectedRows()
        if not explicit:
            return
        # Resolve through the stable key, not the physical row index --
        # after a header-click sort the two diverge, and playing by
        # position would hand the accused supplier the wrong call's WAV.
        row = self._row_for_view_index(explicit[0].row())
        if row is None or not row.wav_path:
            return
        wav = Path(row.wav_path)
        if not wav.exists():
            QMessageBox.warning(self, "Audio missing", f"WAV not found:\n{wav}")
            return
        if not self._ensure_player():
            QMessageBox.warning(self, "Audio unavailable", "QtMultimedia is not available.")
            return
        try:
            self._player.setSource(QUrl.fromLocalFile(str(wav)))
            self._player.play()
        except Exception:
            log.exception("Playback failed for %s", wav)
            QMessageBox.warning(self, "Playback failed", f"Could not play:\n{wav}")

    def _on_stop_clicked(self) -> None:
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------
    def export_csv(self, dest: Path, rows: list[CallRow] | None = None) -> int:
        """Write rows to ``dest`` (CSV). Returns the number of rows written.

        Exposed as a method (not just a slot) so tests can drive it
        without round-tripping a file dialog.
        """
        rows = list(rows) if rows is not None else self.selected_rows()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
            for row in rows:
                writer.writerow(_row_csv_record(row))
        return len(rows)

    def _on_export_csv_clicked(self) -> None:
        rows = self.selected_rows()
        if not rows:
            QMessageBox.information(self, "Nothing to export", "No rows to export.")
            return
        default_name = self._suggested_export_name("csv")
        dest_str, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", default_name, "CSV files (*.csv)"
        )
        if not dest_str:
            return
        try:
            n = self.export_csv(Path(dest_str), rows)
            QMessageBox.information(self, "Export complete", f"Wrote {n} rows.")
        except Exception as exc:  # noqa: BLE001
            log.exception("CSV export failed")
            QMessageBox.warning(self, "Export failed", f"{exc}")

    # ------------------------------------------------------------------
    # ZIP export
    # ------------------------------------------------------------------
    def export_zip(self, dest: Path, rows: list[CallRow] | None = None) -> int:
        """Bundle rows + their WAV files into a ZIP. Returns number of
        WAV files added (the CSV always lands too).

        Missing WAVs are silently skipped so a partially-recorded sweep
        still exports cleanly. The CSV always contains every row.
        """
        rows = list(rows) if rows is not None else self.selected_rows()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Build the CSV in-memory first so the ZIP write is one shot.
        import io

        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow(CSV_HEADER)
        for row in rows:
            writer.writerow(_row_csv_record(row))

        wavs_added = 0
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("results.csv", csv_buf.getvalue())
            for row in rows:
                if not row.wav_path:
                    continue
                wav = Path(row.wav_path)
                if not wav.exists():
                    continue
                # Disambiguate identically-named WAVs from different
                # suppliers by prefixing with call_id.
                arcname = f"audio/{row.call_id:06d}_{wav.name}"
                try:
                    zf.write(str(wav), arcname=arcname)
                    wavs_added += 1
                except OSError:
                    log.exception("Failed to zip %s", wav)
        return wavs_added

    def _on_export_zip_clicked(self) -> None:
        rows = self.selected_rows()
        if not rows:
            QMessageBox.information(self, "Nothing to export", "No rows to export.")
            return
        default_name = self._suggested_export_name("zip")
        dest_str, _ = QFileDialog.getSaveFileName(
            self, "Export evidence ZIP", default_name, "ZIP files (*.zip)"
        )
        if not dest_str:
            return
        try:
            n = self.export_zip(Path(dest_str), rows)
            QMessageBox.information(self, "Export complete", f"Bundled {n} WAV file(s).")
        except Exception as exc:  # noqa: BLE001
            log.exception("ZIP export failed")
            QMessageBox.warning(self, "Export failed", f"{exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _suggested_export_name(self, ext: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run = self._run_id or "all"
        return f"evidence_{run}_{ts}.{ext}"
