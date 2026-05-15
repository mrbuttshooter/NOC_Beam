from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QtGui = pytest.importorskip("PySide6.QtGui")
QtCore = pytest.importorskip("PySide6.QtCore")
QApplication = QtWidgets.QApplication
QToolButton = QtWidgets.QToolButton
QCloseEvent = QtGui.QCloseEvent
Qt = QtCore.Qt
_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])

from noc_beam.testing.plan import TestCall as PlanCall
from noc_beam.testing.runner import TestResult as RunnerResult
from noc_beam.config.store import AccountConfig, GlobalSettings
from noc_beam.ui import phone_shell as phone_shell_module
from noc_beam.ui.phone_shell import PhoneShell
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


class FakeRunner:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def test_close_event_cancels_active_runner_and_ignores_event(
    qt_app: QApplication,
) -> None:
    view = RunnerWindow([])
    fake_runner = FakeRunner()
    view.runner = fake_runner  # type: ignore[assignment]
    event = QCloseEvent()

    view.closeEvent(event)

    assert fake_runner.cancelled
    assert not event.isAccepted()

    view.runner = None
    view.close()


def test_close_event_without_active_runner_is_accepted(qt_app: QApplication) -> None:
    view = RunnerWindow([])
    event = QCloseEvent()
    event.ignore()

    view.closeEvent(event)

    assert event.isAccepted()

    view.close()


def test_phone_shell_has_test_runner_menu_action(qt_app: QApplication, monkeypatch) -> None:
    sip_startups = 0
    scheduled_callbacks = []

    class FakeRinger:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeTimer:
        @staticmethod
        def singleShot(_msec, callback) -> None:
            scheduled_callbacks.append(callback)

    def fake_start_sip(_self) -> None:
        nonlocal sip_startups
        sip_startups += 1

    monkeypatch.setattr(phone_shell_module, "load_settings", GlobalSettings)
    monkeypatch.setattr(phone_shell_module, "load_accounts", lambda: [])
    monkeypatch.setattr(phone_shell_module, "Ringer", FakeRinger)
    monkeypatch.setattr(phone_shell_module, "QTimer", FakeTimer)
    monkeypatch.setattr(PhoneShell, "_start_sip", fake_start_sip)

    shell = PhoneShell()

    try:
        view_actions = next(items for group, items in shell._menu_actions if group == "View")
        labels = [label for label, _slot in view_actions]
        softphone_actions = next(
            items for group, items in shell._menu_actions if group == "Softphone"
        )
        softphone_labels = [label for label, _slot in softphone_actions]

        assert "Account settings..." in softphone_labels
        assert "Test Runner..." in labels
        assert "Always on Top" in labels
        assert len(scheduled_callbacks) == 1
        assert sip_startups == 0
    finally:
        shell._really_quitting = True
        shell.close()


def test_phone_shell_account_settings_updates_selected_account(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    original = AccountConfig(
        id="acct-1",
        display_name="Primary",
        username="1001",
        domain="old.example.test",
        enabled=True,
    )
    updated = AccountConfig(
        id="acct-1",
        display_name="Primary Updated",
        username="2002",
        domain="new.example.test",
        enabled=True,
    )
    saved: list[list[AccountConfig]] = []
    removed: list[str] = []
    added: list[AccountConfig] = []

    class FakeRinger:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeTimer:
        @staticmethod
        def singleShot(_msec, _callback) -> None:
            pass

    class FakeDialog:
        Accepted = 1

        def __init__(self, account, parent=None) -> None:
            self.account = account
            self.parent = parent

        def result_account(self) -> AccountConfig:
            return updated

    class FakeEndpoint:
        def remove_account(self, account_id: str) -> None:
            removed.append(account_id)

        def add_account(self, cfg: AccountConfig) -> None:
            added.append(cfg)

    class FakeSipEndpoint:
        @staticmethod
        def instance() -> FakeEndpoint:
            return fake_endpoint

    fake_endpoint = FakeEndpoint()

    monkeypatch.setattr(phone_shell_module, "load_settings", GlobalSettings)
    monkeypatch.setattr(phone_shell_module, "load_accounts", lambda: [original])
    monkeypatch.setattr(phone_shell_module, "save_accounts", lambda accounts: saved.append(list(accounts)))
    monkeypatch.setattr(phone_shell_module, "Ringer", FakeRinger)
    monkeypatch.setattr(phone_shell_module, "QTimer", FakeTimer)
    monkeypatch.setattr(phone_shell_module, "AccountSettingsDialog", FakeDialog)
    monkeypatch.setattr(phone_shell_module, "SipEndpoint", FakeSipEndpoint)
    monkeypatch.setattr(phone_shell_module, "_open_modal", lambda _dlg: True)
    monkeypatch.setattr(PhoneShell, "_start_sip", lambda _self: None)

    shell = PhoneShell()

    try:
        shell._on_account_settings()

        assert saved == [[updated]]
        assert shell.accounts == [updated]
        assert removed == ["acct-1"]
        assert added == [updated]
        assert shell._active_account_id == "acct-1"
    finally:
        shell._really_quitting = True
        shell.close()


def test_phone_shell_always_on_top_action_toggles_window_flag(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    class FakeRinger:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeTimer:
        @staticmethod
        def singleShot(_msec, _callback) -> None:
            pass

    monkeypatch.setattr(phone_shell_module, "load_settings", GlobalSettings)
    monkeypatch.setattr(phone_shell_module, "load_accounts", lambda: [])
    monkeypatch.setattr(phone_shell_module, "Ringer", FakeRinger)
    monkeypatch.setattr(phone_shell_module, "QTimer", FakeTimer)
    monkeypatch.setattr(PhoneShell, "_start_sip", lambda _self: None)

    shell = PhoneShell()

    try:
        action = shell._always_on_top_action
        assert action is not None
        assert action.isCheckable()
        assert not bool(shell.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

        action.setChecked(True)
        assert bool(shell.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        assert shell._always_on_top

        action.setChecked(False)
        assert not bool(shell.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        assert not shell._always_on_top
    finally:
        shell._really_quitting = True
        shell.close()


def test_phone_shell_primary_controls_have_accessible_names(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    class FakeRinger:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeTimer:
        @staticmethod
        def singleShot(_msec, _callback) -> None:
            pass

    monkeypatch.setattr(phone_shell_module, "load_settings", GlobalSettings)
    monkeypatch.setattr(phone_shell_module, "load_accounts", lambda: [])
    monkeypatch.setattr(phone_shell_module, "Ringer", FakeRinger)
    monkeypatch.setattr(phone_shell_module, "QTimer", FakeTimer)
    monkeypatch.setattr(PhoneShell, "_start_sip", lambda _self: None)

    shell = PhoneShell()

    try:
        assert shell.menu_btn.accessibleName() == "Application menu"
        assert shell.account_chip.accessibleName() == "Active SIP account"
        assert shell.status_banner.accessibleName() == "SIP status"
        assert shell.dial_input.accessibleName() == "Dial target"
        assert shell.call_btn.accessibleName() == "Place call"

        tab_names = {
            button.accessibleName()
            for button in shell.bottom_tabs.findChildren(QToolButton)
        }
        assert tab_names == {
            "Dialpad",
            "Contacts and groups",
            "Starred contacts",
            "Call history",
        }

        dialpad_names = {
            button.accessibleName()
            for button in shell.dialpad.findChildren(QtWidgets.QPushButton)
        }
        assert "Dial 5" in dialpad_names
        assert "Dial # key" in dialpad_names
    finally:
        shell._really_quitting = True
        shell.close()
