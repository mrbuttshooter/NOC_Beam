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
from noc_beam.config import destinations as destinations_module
from noc_beam.ui import phone_shell as phone_shell_module
from noc_beam.ui import test_runner_view as test_runner_view_module
from noc_beam.ui.phone_shell import PhoneShell
from noc_beam.ui.test_runner_view import TestRunnerView as RunnerWindow


@pytest.fixture
def qt_app() -> QApplication:
    return _APP


def test_constructs_with_exact_title_and_disabled_run_button(qt_app: QApplication) -> None:
    view = RunnerWindow([])

    assert view.windowTitle() == "NOC_Beam test runner"
    # Run button no longer prefixes the call count with a play glyph
    # (boss feedback: looked like a "playable" media control).
    assert view.run_btn.text() == "Run 0 calls"
    assert not view.run_btn.isEnabled()

    view.close()


def test_test_runner_uses_operator_object_names(qt_app: QApplication) -> None:
    view = RunnerWindow([])

    # Legacy back-compat object names are still emitted (as hidden
    # markers) so style selectors and existing test infra keep working.
    assert view.findChild(QtWidgets.QFrame, "TestRunnerPasteGrid") is not None
    assert view.findChild(QtWidgets.QFrame, "OperatorToolbar") is not None
    assert view.table.objectName() == "TestRunnerResults"
    assert view.run_btn.objectName() == "RunTestButton"

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

    # CSV was trimmed to 5 columns (operator request — 13-col version
    # was too noisy for billing review). Header is fixed; the timestamp
    # is rendered in local time so we just check stable substrings.
    contents = path.read_text(encoding="utf-8")
    lines = contents.splitlines()
    assert lines[0] == "A Number,B Number,Date,Duration (s),FAS Verdict"
    assert lines[1].startswith("acc-1,2001,")
    # Date column ends with `,1.2,` (duration + empty FAS verdict).
    assert lines[1].endswith(",1.2,")

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

    class _FakeSignal:
        def connect(self, _slot) -> None:
            pass

    class FakeTimer:
        # Accept an optional parent arg so QTimer(self) works.
        def __init__(self, *_args, **_kwargs) -> None:
            self.timeout = _FakeSignal()

        def setInterval(self, _ms) -> None:  # noqa: N802
            pass

        def setSingleShot(self, _flag) -> None:  # noqa: N802
            pass

        def start(self, *_args) -> None:
            pass

        def stop(self) -> None:
            pass

        def isActive(self) -> bool:  # noqa: N802
            return False

        @staticmethod
        def singleShot(_msec, callback) -> None:  # noqa: N802
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

        # "Account settings..." was removed (duplicated Edit account); the
        # menu now only exposes Add / Edit / Remove for accounts.
        assert "Add account..." in softphone_labels
        assert "Test Runner..." in labels
        assert "Always on Top" in labels
        # PhoneShell defers _start_sip and _check_dpapi_status via QTimer.singleShot.
        assert len(scheduled_callbacks) >= 1
        assert sip_startups == 0
    finally:
        shell._really_quitting = True
        shell.close()


def test_phone_shell_empty_account_chip_offers_add_account(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    class FakeRinger:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class _FakeSignal:
        def connect(self, _slot) -> None:
            pass

    class FakeTimer:
        def __init__(self, *_args, **_kwargs) -> None:
            self.timeout = _FakeSignal()

        def setInterval(self, _ms) -> None:  # noqa: N802
            pass

        def setSingleShot(self, _flag) -> None:  # noqa: N802
            pass

        def start(self, *_args) -> None:
            pass

        def stop(self) -> None:
            pass

        def isActive(self) -> bool:  # noqa: N802
            return False

        @staticmethod
        def singleShot(_msec, _callback) -> None:  # noqa: N802
            pass

    monkeypatch.setattr(phone_shell_module, "load_settings", GlobalSettings)
    monkeypatch.setattr(phone_shell_module, "load_accounts", lambda: [])
    monkeypatch.setattr(phone_shell_module, "Ringer", FakeRinger)
    monkeypatch.setattr(phone_shell_module, "QTimer", FakeTimer)
    monkeypatch.setattr(PhoneShell, "_start_sip", lambda _self: None)

    shell = PhoneShell()

    try:
        labels = [action.text() for action in shell.account_chip.menu().actions()]

        assert "No accounts" in labels
        assert "Add account..." in labels
    finally:
        shell._really_quitting = True
        shell.close()


def test_phone_shell_add_account_from_clean_start_saves_and_selects(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    new_account = AccountConfig(
        id="new-acct",
        display_name="NOC Lab",
        username="1001",
        domain="sip.example.test",
        enabled=True,
    )
    saved: list[list[AccountConfig]] = []
    added: list[AccountConfig] = []

    class FakeRinger:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class _FakeSignal:
        def connect(self, _slot) -> None:
            pass

    class FakeTimer:
        def __init__(self, *_args, **_kwargs) -> None:
            self.timeout = _FakeSignal()

        def setInterval(self, _ms) -> None:  # noqa: N802
            pass

        def setSingleShot(self, _flag) -> None:  # noqa: N802
            pass

        def start(self, *_args) -> None:
            pass

        def stop(self) -> None:
            pass

        def isActive(self) -> bool:  # noqa: N802
            return False

        @staticmethod
        def singleShot(_msec, _callback) -> None:  # noqa: N802
            pass

    class FakeDialog:
        Accepted = 1

        def __init__(self, parent=None) -> None:
            self.parent = parent

        def result_account(self) -> AccountConfig:
            return new_account

    class FakeEndpoint:
        def add_account(self, cfg: AccountConfig) -> None:
            added.append(cfg)

    class FakeSipEndpoint:
        @staticmethod
        def instance() -> FakeEndpoint:
            return fake_endpoint

    fake_endpoint = FakeEndpoint()

    monkeypatch.setattr(phone_shell_module, "load_settings", GlobalSettings)
    monkeypatch.setattr(phone_shell_module, "load_accounts", lambda: [])
    monkeypatch.setattr(
        phone_shell_module,
        "save_accounts",
        lambda accounts: saved.append(list(accounts)),
    )
    monkeypatch.setattr(phone_shell_module, "Ringer", FakeRinger)
    monkeypatch.setattr(phone_shell_module, "QTimer", FakeTimer)
    monkeypatch.setattr(phone_shell_module, "AccountDialog", FakeDialog)
    monkeypatch.setattr(phone_shell_module, "SipEndpoint", FakeSipEndpoint)
    monkeypatch.setattr(phone_shell_module, "_open_modal", lambda _dlg: True)
    monkeypatch.setattr(PhoneShell, "_start_sip", lambda _self: None)

    shell = PhoneShell()

    try:
        add_action = next(
            action for action in shell.account_chip.menu().actions()
            if action.text() == "Add account..."
        )
        add_action.trigger()

        assert saved == [[new_account]]
        assert added == [new_account]
        assert shell.accounts == [new_account]
        assert shell._active_account_id == "new-acct"
        # Chip text now carries a leading registration-health dot and a
        # smaller chevron glyph -- the assertion checks the substring so
        # cosmetic dot/chevron tweaks don't have to round-trip through
        # this test.
        assert "NOC Lab" in shell.account_chip.text()
    finally:
        shell._really_quitting = True
        shell.close()


def test_phone_shell_add_account_save_failure_keeps_account_out_of_memory(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    new_account = AccountConfig(
        id="new-acct",
        display_name="NOC Lab",
        username="1001",
        domain="sip.example.test",
        enabled=True,
    )
    warnings: list[tuple[str, str]] = []
    added: list[AccountConfig] = []

    class FakeRinger:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class _FakeSignal:
        def connect(self, _slot) -> None:
            pass

    class FakeTimer:
        def __init__(self, *_args, **_kwargs) -> None:
            self.timeout = _FakeSignal()

        def setInterval(self, _ms) -> None:  # noqa: N802
            pass

        def setSingleShot(self, _flag) -> None:  # noqa: N802
            pass

        def start(self, *_args) -> None:
            pass

        def stop(self) -> None:
            pass

        def isActive(self) -> bool:  # noqa: N802
            return False

        @staticmethod
        def singleShot(_msec, _callback) -> None:  # noqa: N802
            pass

    class FakeDialog:
        Accepted = 1

        def __init__(self, parent=None) -> None:
            self.parent = parent

        def result_account(self) -> AccountConfig:
            return new_account

    class FakeEndpoint:
        def add_account(self, cfg: AccountConfig) -> None:
            added.append(cfg)

    class FakeSipEndpoint:
        @staticmethod
        def instance() -> FakeEndpoint:
            return fake_endpoint

    fake_endpoint = FakeEndpoint()

    monkeypatch.setattr(phone_shell_module, "load_settings", GlobalSettings)
    monkeypatch.setattr(phone_shell_module, "load_accounts", lambda: [])
    monkeypatch.setattr(
        phone_shell_module,
        "save_accounts",
        lambda _accounts: (_ for _ in ()).throw(OSError("disk denied")),
    )
    monkeypatch.setattr(
        phone_shell_module,
        "accounts_file",
        lambda: "C:/Users/User/AppData/Roaming/NOC_Beam/accounts.json",
    )
    monkeypatch.setattr(phone_shell_module, "Ringer", FakeRinger)
    monkeypatch.setattr(phone_shell_module, "QTimer", FakeTimer)
    monkeypatch.setattr(phone_shell_module, "AccountDialog", FakeDialog)
    monkeypatch.setattr(phone_shell_module, "SipEndpoint", FakeSipEndpoint)
    monkeypatch.setattr(phone_shell_module, "_open_modal", lambda _dlg: True)
    monkeypatch.setattr(
        phone_shell_module.QMessageBox,
        "warning",
        lambda _parent, title, body: warnings.append((title, body)),
    )
    monkeypatch.setattr(PhoneShell, "_start_sip", lambda _self: None)

    shell = PhoneShell()

    try:
        shell._on_add_account()

        assert shell.accounts == []
        assert added == []
        assert warnings
        assert warnings[0][0] == "Account save failed"
        assert "accounts.json" in warnings[0][1]
        assert "disk denied" in warnings[0][1]
        # Status banner now leads with a dot glyph; substring check.
        assert "Account save failed" in shell.status_banner.text()
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

    class _FakeSignal:
        def connect(self, _slot) -> None:
            pass

    class FakeTimer:
        def __init__(self, *_args, **_kwargs) -> None:
            self.timeout = _FakeSignal()

        def setInterval(self, _ms) -> None:  # noqa: N802
            pass

        def setSingleShot(self, _flag) -> None:  # noqa: N802
            pass

        def start(self, *_args) -> None:
            pass

        def stop(self) -> None:
            pass

        def isActive(self) -> bool:  # noqa: N802
            return False

        @staticmethod
        def singleShot(_msec, _callback) -> None:  # noqa: N802
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

    class _FakeSignal:
        def connect(self, _slot) -> None:
            pass

    class FakeTimer:
        def __init__(self, *_args, **_kwargs) -> None:
            self.timeout = _FakeSignal()

        def setInterval(self, _ms) -> None:  # noqa: N802
            pass

        def setSingleShot(self, _flag) -> None:  # noqa: N802
            pass

        def start(self, *_args) -> None:
            pass

        def stop(self) -> None:
            pass

        def isActive(self) -> bool:  # noqa: N802
            return False

        @staticmethod
        def singleShot(_msec, _callback) -> None:  # noqa: N802
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
        assert shell.status_banner.accessibleName() == "Registration and call status"
        assert shell.dial_input.accessibleName() == "Dial target"
        assert shell.call_btn.accessibleName() == "Place call"

        tab_names = {
            button.accessibleName()
            for button in shell.bottom_tabs.findChildren(QToolButton)
        }
        # SIP signalling trace tab was retired (lived in Settings now).
        assert {"Dialpad", "Contacts and groups", "Starred contacts", "Call history"}.issubset(tab_names)

        dialpad_names = {
            button.accessibleName()
            for button in shell.dialpad.findChildren(QtWidgets.QPushButton)
        }
        assert "Dial 5" in dialpad_names
        # `#` button accessible name was tightened from "Dial # key" to "Dial #".
        assert "Dial #" in dialpad_names
    finally:
        shell._really_quitting = True
        shell.close()


def test_phone_shell_settings_apply_theme_live(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    settings = GlobalSettings()
    applied: list[tuple[bool, str]] = []

    class FakeRinger:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class _FakeSignal:
        def connect(self, _slot) -> None:
            pass

    class FakeTimer:
        def __init__(self, *_args, **_kwargs) -> None:
            self.timeout = _FakeSignal()

        def setInterval(self, _ms) -> None:  # noqa: N802
            pass

        def setSingleShot(self, _flag) -> None:  # noqa: N802
            pass

        def start(self, *_args) -> None:
            pass

        def stop(self) -> None:
            pass

        def isActive(self) -> bool:  # noqa: N802
            return False

        @staticmethod
        def singleShot(_msec, _callback) -> None:  # noqa: N802
            pass

    class FakeDialog:
        Accepted = 1

        def __init__(self, dialog_settings, account=None, parent=None) -> None:
            self.dialog_settings = dialog_settings
            self.account = account
            self.parent = parent

        def apply_to(self, dialog_settings) -> dict[str, int]:
            dialog_settings.appearance.high_contrast = True
            dialog_settings.appearance.reduced_motion = True
            return {}

    monkeypatch.setattr(phone_shell_module, "load_settings", lambda: settings)
    monkeypatch.setattr(phone_shell_module, "load_accounts", lambda: [])
    monkeypatch.setattr(phone_shell_module, "save_settings", lambda _settings: None)
    monkeypatch.setattr(phone_shell_module, "Ringer", FakeRinger)
    monkeypatch.setattr(phone_shell_module, "QTimer", FakeTimer)
    monkeypatch.setattr(phone_shell_module, "SettingsDialog", FakeDialog)
    monkeypatch.setattr(phone_shell_module, "_open_modal", lambda _dlg: True)
    monkeypatch.setattr(phone_shell_module, "set_active_devices", lambda *_args: None, raising=False)
    monkeypatch.setattr(
        phone_shell_module,
        "apply_theme",
        lambda _app, high_contrast, *, theme="light": applied.append(
            (high_contrast, theme)
        ),
    )
    monkeypatch.setattr(PhoneShell, "_start_sip", lambda _self: None)

    shell = PhoneShell()

    try:
        shell._on_settings()

        # Dark is the redesign default (AppearanceSettings.theme now defaults
        # to "dark"), so a settings-apply with no explicit theme change
        # re-applies "dark".
        assert applied == [(True, "dark")]
        assert shell.settings.appearance.high_contrast is True
        assert shell.settings.appearance.reduced_motion is True
    finally:
        shell._really_quitting = True
        shell.close()


# ---------------------------------------------------------------------------
# New-field coverage (Times, Parallel-hidden, FAS Sweep mode, dest pickers)
# ---------------------------------------------------------------------------


def test_times_spin_defaults_to_one_and_propagates_to_spec(qt_app):
    view = RunnerWindow([])
    try:
        assert view.times_spin.value() == 1
        view.times_spin.setValue(7)
        spec = view._spec_from_ui()
        assert spec.times == 7
    finally:
        view.close()


def test_parallel_field_is_hidden_in_ui(qt_app):
    view = RunnerWindow([])
    try:
        # The spinbox exists (spec.parallel still needs a value) but
        # must not be visible to the operator (hidden via setVisible(False)).
        assert view.parallel_spin.isHidden()
        # No non-hidden widget should be labelled Parallel.
        labels = view.findChildren(QtWidgets.QLabel)
        for lbl in labels:
            if not lbl.isHidden():
                assert 'Parallel' not in lbl.text()
    finally:
        view.close()


def test_parallel_is_pinned_to_ten_in_spec(qt_app):
    view = RunnerWindow([])
    try:
        view.callers_edit.setPlainText('1001')
        view.targets_edit.setPlainText('2001')
        spec = view._spec_from_ui()
        assert spec.parallel == 10
    finally:
        view.close()


def test_picking_fas_sweep_mode_swaps_pass_to_fas_verdict_and_disables(qt_app):
    view = RunnerWindow([])
    try:
        view.mode_combo.setCurrentIndex(view.mode_combo.findData('fas-sweep'))
        assert view.pass_combo.currentData() == 'fas-verdict'
        assert not view.pass_combo.isEnabled()
        # Tries-per-pair radios become visible (via the times stack swap).
        assert view._times_stack.currentIndex() == 1
        # Jitter row visible (use isHidden() since the window isn't shown in tests).
        assert not view.jitter_row.isHidden()
    finally:
        view.close()


def test_reverting_from_fas_sweep_restores_times_spinbox(qt_app):
    view = RunnerWindow([])
    try:
        view.mode_combo.setCurrentIndex(view.mode_combo.findData('fas-sweep'))
        assert view._times_stack.currentIndex() == 1
        view.mode_combo.setCurrentIndex(view.mode_combo.findData('matrix'))
        assert view._times_stack.currentIndex() == 0
        assert view.pass_combo.isEnabled()
        # Jitter row hidden again.
        assert view.jitter_row.isHidden()
    finally:
        view.close()


def test_fas_sweep_tries_per_pair_quick_thorough_custom(qt_app):
    view = RunnerWindow([])
    try:
        view.mode_combo.setCurrentIndex(view.mode_combo.findData('fas-sweep'))
        # Default Quick (2)
        assert view._tries_per_pair_value() == 2
        view.tries_thorough_radio.setChecked(True)
        assert view._tries_per_pair_value() == 4
        view.tries_custom_radio.setChecked(True)
        assert view.tries_custom_spin.isEnabled()
        view.tries_custom_spin.setValue(7)
        assert view._tries_per_pair_value() == 7
    finally:
        view.close()


def test_origination_destination_rows_show_hint_when_no_zone_has_numbers(qt_app, monkeypatch):
    """When the destinations library has zero zones with numbers, the
    ORIGINATION + DESTINATION rows are still visible — but the dropdowns
    are disabled and an inline hint points the operator at Settings →
    Destinations to seed the first number. Engineers need to see the
    feature exists; previously the rows were hidden entirely, which made
    them un-discoverable on a fresh install."""
    empty_only = [
        destinations_module.Destination(country='X', zone='X-Z', numbers=tuple()),
    ]
    monkeypatch.setattr(
        test_runner_view_module.destinations_module, 'load_destinations',
        lambda: empty_only,
    )
    view = RunnerWindow([])
    try:
        # Rows are NOT explicitly hidden — they're discoverable when the
        # window is shown. (Use isHidden() not isVisible() because the
        # offscreen test window isn't show()ed.)
        assert not view.origination_row.isHidden()
        assert not view.destination_row.isHidden()
        # Dropdowns are disabled (can't pick from an empty library).
        assert not view.origination_country.isEnabled()
        assert not view.origination_zone.isEnabled()
        assert not view.destination_country.isEnabled()
        assert not view.destination_zone.isEnabled()
        # The empty-state hint label is intentionally NOT shown: it used to
        # render as a phantom standalone "Configure in Settings" window in
        # the taskbar / Alt+Tab, so _apply_destination_empty_state now keeps
        # it hidden and the DISABLED dropdowns (asserted above) are the
        # empty-state signal. The label still exists and carries its text.
        assert view.origination_hint.isHidden()
        assert view.destination_hint.isHidden()
        assert "Settings" in view.origination_hint.text()
        assert "Settings" in view.destination_hint.text()
    finally:
        view.close()


def test_origination_destination_hint_hides_after_zone_gets_number(qt_app, monkeypatch):
    """When the operator adds the first real number (via Settings or by
    JSON-editing the destinations file and clicking the reload button),
    the empty-state hint disappears and the dropdowns become usable."""
    populated = [
        destinations_module.Destination(
            country='Egypt', zone='Egypt-Mobile (Vodafone)', numbers=('201001234567',)
        ),
    ]
    monkeypatch.setattr(
        test_runner_view_module.destinations_module, 'load_destinations',
        lambda: populated,
    )
    view = RunnerWindow([])
    try:
        assert view.origination_country.isEnabled()
        assert view.destination_country.isEnabled()
        # Hint is explicitly hidden once the library has populated zones.
        assert view.origination_hint.isHidden()
        assert view.destination_hint.isHidden()
    finally:
        view.close()


def test_origination_zone_writes_to_callers_edit(qt_app, monkeypatch):
    items = [
        destinations_module.Destination(
            country='Egypt',
            zone='Egypt-Mobile (Vodafone)',
            numbers=('201001234567', '201002345678'),
        ),
    ]
    monkeypatch.setattr(
        test_runner_view_module.destinations_module, 'load_destinations',
        lambda: items,
    )
    view = RunnerWindow([])
    try:
        # Pick the country (only one), then activate the zone.
        view.origination_country.setCurrentIndex(0)
        # Find the zone index in the zone combo.
        zone_idx = view.origination_zone.findData('Egypt-Mobile (Vodafone)')
        assert zone_idx > 0
        # callers edit starts blank so no confirm prompt.
        view._on_origination_zone_activated(zone_idx)
        text = view.callers_edit.toPlainText().strip().splitlines()
        assert text == ['201001234567', '201002345678']
    finally:
        view.close()


def test_destination_zone_writes_to_targets_edit(qt_app, monkeypatch):
    items = [
        destinations_module.Destination(
            country='Egypt',
            zone='Egypt-Mobile (Vodafone)',
            numbers=('201001234567',),
        ),
    ]
    monkeypatch.setattr(
        test_runner_view_module.destinations_module, 'load_destinations',
        lambda: items,
    )
    view = RunnerWindow([])
    try:
        view.destination_country.setCurrentIndex(0)
        zone_idx = view.destination_zone.findData('Egypt-Mobile (Vodafone)')
        assert zone_idx > 0
        view._on_destination_zone_activated(zone_idx)
        assert view.targets_edit.toPlainText().strip().splitlines() == ['201001234567']
    finally:
        view.close()


def test_destination_replace_confirm_cancel_reverts_zone(qt_app, monkeypatch):
    items = [
        destinations_module.Destination(
            country='Egypt',
            zone='Egypt-Mobile (Vodafone)',
            numbers=('201001234567',),
        ),
    ]
    monkeypatch.setattr(
        test_runner_view_module.destinations_module, 'load_destinations',
        lambda: items,
    )
    # Pre-existing content in targets box -> replace prompt should fire.
    monkeypatch.setattr(
        test_runner_view_module.QMessageBox,
        'question',
        lambda *_args, **_kwargs: test_runner_view_module.QMessageBox.StandardButton.Cancel,
    )
    view = RunnerWindow([])
    try:
        view.targets_edit.setPlainText('999999')
        view.destination_country.setCurrentIndex(0)
        zone_idx = view.destination_zone.findData('Egypt-Mobile (Vodafone)')
        assert zone_idx > 0
        # Simulate the activated signal — Cancel must keep the old content.
        view.destination_zone.setCurrentIndex(zone_idx)
        view._on_destination_zone_activated(zone_idx)
        assert view.targets_edit.toPlainText().strip() == '999999'
        # And the zone combo should have been reverted to index 0 (blank).
        assert view.destination_zone.currentIndex() == 0
    finally:
        view.close()


def test_tabwidget_has_configure_running_results(qt_app):
    view = RunnerWindow([])
    try:
        labels = [view.tabs.tabText(i) for i in range(view.tabs.count())]
        assert labels[0] == 'Configure'
        assert labels[1].startswith('Running')
        assert labels[2].startswith('Results')
    finally:
        view.close()
