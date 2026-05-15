"""Qt smoke tests for the persistent Contacts view."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtCore = pytest.importorskip("PySide6.QtCore")
QtTest = pytest.importorskip("PySide6.QtTest")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication
QLabel = QtWidgets.QLabel
Qt = QtCore.Qt
QTest = QtTest.QTest
_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])

from noc_beam.config import contacts
from noc_beam.ui.contacts_view import ContactRow, ContactsView


@pytest.fixture
def qt_app() -> QApplication:
    return _APP


@pytest.fixture(autouse=True)
def isolated_contacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(contacts, "contacts_file", lambda: tmp_path / "contacts.json")
    yield


def _visible_contact_names(view: ContactsView) -> list[str]:
    return [
        row.contact.name
        for row in view.findChildren(ContactRow)
        if row.isVisible()
    ]


def test_constructs_with_empty_store(qt_app: QApplication) -> None:
    view = ContactsView()
    view.show()
    qt_app.processEvents()

    try:
        labels = [label.text() for label in view.findChildren(QLabel)]
        assert "No contacts yet." in labels
    finally:
        view.close()


def test_reload_displays_added_contact(qt_app: QApplication) -> None:
    view = ContactsView()
    view.show()
    rows: list[contacts.Contact] = []
    contacts.add_contact(rows, "Alice NOC", "1001", group="Escalation")
    contacts.save_contacts(rows)

    view.reload()
    qt_app.processEvents()

    try:
        labels = [label.text() for label in view.findChildren(QLabel)]
        assert "Escalation" in labels
        assert "Alice NOC" in labels
        assert "1001" in labels
    finally:
        view.close()


def test_search_by_number_filters_contacts(qt_app: QApplication) -> None:
    rows: list[contacts.Contact] = []
    contacts.add_contact(rows, "Alice", "1001", group="NOC")
    contacts.add_contact(rows, "Bob", "2999", group="NOC")
    contacts.save_contacts(rows)
    view = ContactsView()
    view.show()
    qt_app.processEvents()

    view.search.setText("2999")
    qt_app.processEvents()

    try:
        assert _visible_contact_names(view) == ["Bob"]
    finally:
        view.close()


def test_contact_row_call_emits_number(qt_app: QApplication) -> None:
    rows: list[contacts.Contact] = []
    contacts.add_contact(rows, "Alice", "1001", group="NOC")
    contacts.save_contacts(rows)
    view = ContactsView()
    view.show()
    qt_app.processEvents()
    emitted: list[str] = []
    view.call_requested.connect(emitted.append)

    row = view.findChildren(ContactRow)[0]
    QTest.mouseDClick(row, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    try:
        assert emitted == ["1001"]
    finally:
        view.close()
