from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication
_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])

from noc_beam.testing.plan import TestCall as PlanCall
from noc_beam.testing.runner import TestResult as RunnerResult
from noc_beam.ui.test_runner_view import TestRunnerView as RunnerWindow


@pytest.fixture
def qt_app() -> QApplication:
    return _APP


def test_constructs_with_exact_title_and_disabled_run_button(qt_app: QApplication) -> None:
    view = RunnerWindow([])

    assert view.windowTitle() == "NOC_Beam test runner"
    assert view.run_btn.text() == "Run 0 calls"
    assert not view.run_btn.isEnabled()

    view.close()


def test_run_count_updates_for_matrix(qt_app: QApplication) -> None:
    view = RunnerWindow([])

    view.callers_edit.setPlainText("1001\n1002\n")
    view.targets_edit.setPlainText("2001\n2002\n2003\n")
    view.mode_combo.setCurrentIndex(view.mode_combo.findData("matrix"))

    assert view.run_btn.text() == "Run 6 calls"
    assert view.run_btn.isEnabled()

    view.close()


def test_hold_spinner_enabled_only_for_full_call(qt_app: QApplication) -> None:
    view = RunnerWindow([])

    view.pass_combo.setCurrentIndex(view.pass_combo.findData("reachability"))
    assert not view.hold_spin.isEnabled()

    view.pass_combo.setCurrentIndex(view.pass_combo.findData("full-call"))
    assert view.hold_spin.isEnabled()

    view.pass_combo.setCurrentIndex(view.pass_combo.findData("reachability"))
    assert not view.hold_spin.isEnabled()

    view.close()


def test_export_csv_writes_header_and_result_row(
    qt_app: QApplication,
    tmp_path,
) -> None:
    view = RunnerWindow([])
    started_at = datetime(2026, 5, 15, 12, 34, 56, tzinfo=UTC).timestamp()
    view.results = [
        RunnerResult(
            call=PlanCall(index=7, caller_number="1001", target_number="2001"),
            result="PASS",
            sip_code=180,
            sip_reason="Ringing",
            rtt_ms=123.9,
            duration_s=1.25,
            notes="",
            started_at=started_at,
            from_account="acc-1",
            to_uri="sip:2001@example.test",
        )
    ]

    path = tmp_path / "results.csv"
    view.export_csv(path)

    assert path.read_text(encoding="utf-8") == (
        "test_run_id,started_at,from_account,to_uri,result,sip_code,"
        "sip_reason,rtt_ms,duration_s,notes\n"
        "nb-20260515-123456-007,2026-05-15T12:34:56Z,acc-1,"
        "sip:2001@example.test,PASS,180,Ringing,123,1.2,\n"
    )

    view.close()
