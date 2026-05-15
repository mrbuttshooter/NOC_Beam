from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication
_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])

from noc_beam.ui.components import (  # noqa: E402
    DenseListRow,
    FooterActionBar,
    SipCodeBadge,
    StatusPill,
)


@pytest.fixture
def qt_app() -> QApplication:
    return _APP


def test_status_pill_exposes_text_level_and_accessible_name(qt_app: QApplication):
    pill = StatusPill("Registered", "ok")

    assert pill.text() == "Registered"
    assert pill.objectName() == "StatusPill"
    assert pill.property("level") == "ok"
    assert "Registered" in pill.accessibleName()

    pill.close()


def test_sip_code_badge_uses_fixed_level_and_tooltip(qt_app: QApplication):
    badge = SipCodeBadge(180, "Ringing")

    assert badge.text() == "180"
    assert badge.objectName() == "SipCodeBadge"
    assert badge.property("level") == "progress"
    assert badge.toolTip() == "180 Ringing"

    badge.close()


def test_footer_action_bar_keeps_primary_last(qt_app: QApplication):
    bar = FooterActionBar(primary_text="Save", secondary_text="Cancel")

    buttons = bar.findChildren(type(bar.primary_button))
    assert bar.primary_button.text() == "Save"
    assert bar.secondary_button.text() == "Cancel"
    assert buttons[-1] is bar.primary_button

    bar.close()


def test_dense_list_row_has_fixed_action_column(qt_app: QApplication):
    row = DenseListRow(title="Alice", subtitle="sip:alice@example.com", marker="*")

    assert row.objectName() == "DenseListRow"
    assert row.title_label.text() == "Alice"
    assert row.subtitle_label.text() == "sip:alice@example.com"
    assert row.marker_label.text() == "*"

    row.close()
