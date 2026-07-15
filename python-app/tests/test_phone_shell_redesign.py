from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication
_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])

from noc_beam.ui.bottom_tabs import BOTTOM_NAV_HEIGHT, Tab  # noqa: E402
from noc_beam.ui.phone_shell import PhoneShell  # noqa: E402


@pytest.fixture
def qt_app() -> QApplication:
    return _APP


def test_phone_shell_uses_operator_width_and_critical_regions(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("noc_beam.ui.phone_shell.QTimer.singleShot", lambda _ms, _fn: None)
    shell = PhoneShell()
    qt_app.processEvents()

    try:
        # 360 floor per owner feedback round 2 (compact default footprint);
        # was 380 in the original operator-width contract.
        assert shell.minimumWidth() >= 360
        assert shell.findChild(type(shell.account_chip), "AccountChip") is not None
        assert shell.findChild(type(shell.status_banner), "StatusBanner") is not None
        assert shell.findChild(type(shell.bottom_tabs), "BottomTabs") is not None
    finally:
        shell.close()


def test_bottom_tabs_are_compact_and_include_existing_pages(qt_app: QApplication):
    from noc_beam.ui.bottom_tabs import BottomTabs

    tabs = BottomTabs()
    qt_app.processEvents()

    try:
        assert tabs.height() == BOTTOM_NAV_HEIGHT
        assert tabs._buttons[int(Tab.DIALPAD)].text().startswith("Dial")
        # Trace was removed from the bottom tabs (now popup-only via
        # View -> NOC Trace... or Ctrl+Shift+T). Assert the four
        # remaining tabs match Bria's softphone tab structure.
        assert tabs._buttons[int(Tab.CONTACTS)].text().startswith("Contacts")
        assert tabs._buttons[int(Tab.FAVORITES)].text().startswith("Favorites")
        assert tabs._buttons[int(Tab.HISTORY)].text().startswith("History")
    finally:
        tabs.close()


def test_dial_surface_has_compact_controls(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("noc_beam.ui.phone_shell.QTimer.singleShot", lambda _ms, _fn: None)
    shell = PhoneShell()
    qt_app.processEvents()

    try:
        assert shell.dial_input.objectName() == "DialInput"
        assert shell.call_btn.objectName() == "CallButton"
        assert shell.call_btn.minimumHeight() <= 40
        assert shell.findChild(type(shell.dialpad), "DialPad") is not None
    finally:
        shell.close()


def test_supplier_changed_before_endpoint_start_is_a_silent_no_op(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: _on_supplier_changed used to call update_account ->
    add_account during PhoneShell construction, which raised
    RuntimeError("Endpoint not started") and logged an ERROR-level
    traceback on every cold start. The early-return gate should make
    this a silent INFO-level skip.
    """
    import logging

    # Stub QTimer.singleShot so _start_sip never runs; this keeps the
    # SipEndpoint singleton in its not-started state for the duration
    # of the test.
    monkeypatch.setattr("noc_beam.ui.phone_shell.QTimer.singleShot", lambda _ms, _fn: None)

    from noc_beam.sip.endpoint import SipEndpoint

    # Force the singleton into the cold-start state the bug fires in:
    # instance exists (a previous test may have built it) but
    # _started is False.
    endpoint = SipEndpoint.instance()
    monkeypatch.setattr(endpoint, "_started", False, raising=False)

    shell = PhoneShell()
    qt_app.processEvents()

    try:
        # Build a fake Teles account so we exercise the post-kind-check
        # code path that used to call update_account. _selected_account
        # is what _on_supplier_changed reads.
        fake_acc = type(
            "FakeAcc",
            (),
            {
                "id": "test-acc-id",
                "switch_type": "teles",
                "routing_format": "U{id}",
                "username": "U",
                "auth_user": "U",
                "domain": "example.test",
            },
        )()
        monkeypatch.setattr(shell, "_selected_account", lambda: fake_acc)

        # Make sure supplier_combo has an index >= 0 so we get past
        # the first guard. We don't actually need a real supplier list
        # — the endpoint-not-started gate fires before the supplier
        # lookup.
        shell.supplier_combo.blockSignals(True)
        shell.supplier_combo.set_items([("Test - C080", "080")], "080")
        shell.supplier_combo.setCurrentIndex(0)
        shell.supplier_combo.blockSignals(False)

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="noc_beam.ui.phone_shell"):
            # This used to raise RuntimeError("Endpoint not started")
            # which was swallowed by the inner try/except and logged
            # as ERROR with a full traceback.
            shell._on_supplier_changed(0)

        # No ERROR-level records from the phone_shell logger.
        error_records = [
            r for r in caplog.records
            if r.name == "noc_beam.ui.phone_shell"
            and r.levelno >= logging.ERROR
        ]
        assert not error_records, (
            "Expected no ERROR logs from _on_supplier_changed when the "
            f"endpoint is not started, got: "
            f"{[(r.levelname, r.getMessage()) for r in error_records]}"
        )

        # And the friendly INFO skip message must be present.
        assert any(
            "endpoint not started yet" in r.getMessage().lower()
            for r in caplog.records
        ), (
            "Expected an INFO-level 'endpoint not started yet' skip "
            "message, got: "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )
    finally:
        shell.close()
