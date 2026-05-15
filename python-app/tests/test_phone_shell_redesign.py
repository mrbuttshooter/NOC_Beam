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
        assert shell.minimumWidth() >= 380
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
        assert tabs._buttons[int(Tab.TRACE)].text().startswith("Trace")
    finally:
        tabs.close()
