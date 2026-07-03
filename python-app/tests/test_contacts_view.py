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
QToolButton = QtWidgets.QToolButton
Qt = QtCore.Qt
QTest = QtTest.QTest
_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])

from noc_beam.config import contacts
from noc_beam.ui import contacts_view as contacts_view_module
from noc_beam.ui.contacts_view import ContactRow, ContactsView, GroupRow


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


def test_contact_row_call_button_emits_number(qt_app: QApplication) -> None:
    rows: list[contacts.Contact] = []
    contacts.add_contact(rows, "Alice", "1001", group="NOC")
    contacts.save_contacts(rows)
    view = ContactsView()
    view.show()
    qt_app.processEvents()
    emitted: list[str] = []
    view.call_requested.connect(emitted.append)

    row = view.findChildren(ContactRow)[0]
    call_btn = next(
        button for button in row.findChildren(QToolButton)
        if button.toolTip() == "Call"
    )
    QTest.mouseClick(call_btn, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    try:
        assert emitted == ["1001"]
    finally:
        view.close()


def test_add_edit_delete_persist_and_emit_contact_saved(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs: list[object] = []

    class FakeDialog:
        def __init__(self, contact=None, group="Work", parent=None, seed=None, error="") -> None:  # noqa: ANN001
            self.contact = contact
            self.group = group
            self.error = QLabel("")
            self.values_data = {
                "name": "Alice" if contact is None else "Alice Ops",
                "number": "1001" if contact is None else "2002",
                "group": group if contact is None else "Escalation",
                "favorite": contact is None,
            }
            dialogs.append(self)

        def values(self) -> dict[str, str | bool]:
            return dict(self.values_data)

    monkeypatch.setattr(contacts_view_module, "ContactDialog", FakeDialog)
    monkeypatch.setattr(contacts_view_module, "_open_modal", lambda _dlg: True)
    # _on_delete_contact opens QMessageBox.question for confirmation;
    # without this monkeypatch the test process blocks indefinitely on
    # the modal — full pytest suite would hang forever.
    monkeypatch.setattr(
        contacts_view_module.QMessageBox,
        "question",
        lambda *_a, **_kw: contacts_view_module.QMessageBox.StandardButton.Yes,
    )

    view = ContactsView()
    view.show()
    saved: list[None] = []
    view.contact_saved.connect(lambda: saved.append(None))

    view._on_add_contact("NOC")
    qt_app.processEvents()

    loaded = contacts.load_contacts()
    assert [(item.name, item.number, item.group, item.favorite) for item in loaded] == [
        ("Alice", "1001", "NOC", True)
    ]

    view._on_edit_contact(loaded[0].id)
    qt_app.processEvents()
    edited = contacts.load_contacts()[0]
    assert (edited.name, edited.number, edited.group, edited.favorite) == (
        "Alice Ops",
        "2002",
        "Escalation",
        False,
    )

    view._on_delete_contact(edited.id)
    qt_app.processEvents()

    try:
        assert contacts.load_contacts() == []
        assert len(saved) == 3
    finally:
        view.close()


def test_add_group_prefills_contact_dialog_group(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_groups: list[str] = []

    class FakeDialog:
        def __init__(self, contact=None, group="Work", parent=None, seed=None, error="") -> None:  # noqa: ANN001
            captured_groups.append(group)
            self.error = QLabel("")

        def values(self) -> dict[str, str | bool]:
            return {"name": "Alice", "number": "1001", "group": "Tier 1", "favorite": False}

    monkeypatch.setattr(
        contacts_view_module.QInputDialog,
        "getText",
        lambda *_args, **_kwargs: ("Tier 1", True),
    )
    monkeypatch.setattr(contacts_view_module, "ContactDialog", FakeDialog)
    monkeypatch.setattr(contacts_view_module, "_open_modal", lambda _dlg: False)
    view = ContactsView()
    view.show()

    view._on_add_group()
    qt_app.processEvents()

    try:
        assert captured_groups == ["Tier 1"]
    finally:
        view.close()


def test_add_save_failure_warns_and_keeps_dialog_for_retry(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs: list[object] = []
    warnings: list[str] = []
    modal_results = iter([True, False])

    class FakeDialog:
        def __init__(self, contact=None, group="Work", parent=None, seed=None, error="") -> None:  # noqa: ANN001
            self.error = QLabel("")
            self.values_data = {
                "name": "Alice",
                "number": "1001",
                "group": group,
                "favorite": False,
            }
            dialogs.append(self)

        def values(self) -> dict[str, str | bool]:
            return dict(self.values_data)

    def fail_save(_contacts) -> None:  # noqa: ANN001
        raise PermissionError("locked")

    monkeypatch.setattr(contacts_view_module, "ContactDialog", FakeDialog)
    monkeypatch.setattr(contacts_view_module, "_open_modal", lambda _dlg: next(modal_results))
    monkeypatch.setattr(contacts_view_module, "save_contacts", fail_save)
    monkeypatch.setattr(
        contacts_view_module.QMessageBox,
        "warning",
        lambda _parent, _title, body: warnings.append(body),
    )
    view = ContactsView()
    view.show()
    saved: list[None] = []
    view.contact_saved.connect(lambda: saved.append(None))

    view._on_add_contact("NOC")
    qt_app.processEvents()

    try:
        assert dialogs[0].values_data["group"] == "NOC"
        assert "locked" in warnings[0]
        assert contacts.load_contacts() == []
        assert saved == []
    finally:
        view.close()


def test_edit_save_failure_warns_and_keeps_existing_contact(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[contacts.Contact] = []
    contact = contacts.add_contact(rows, "Alice", "1001", group="NOC")
    contacts.save_contacts(rows)
    warnings: list[str] = []
    modal_results = iter([True, False])

    class FakeDialog:
        def __init__(self, contact=None, group="Work", parent=None, seed=None, error="") -> None:  # noqa: ANN001
            self.error = QLabel("")
            self.values_data = {
                "name": "Changed",
                "number": "2002",
                "group": "Escalation",
                "favorite": True,
            }

        def values(self) -> dict[str, str | bool]:
            return dict(self.values_data)

    def fail_save(_contacts) -> None:  # noqa: ANN001
        raise OSError("disk full")

    monkeypatch.setattr(contacts_view_module, "ContactDialog", FakeDialog)
    monkeypatch.setattr(contacts_view_module, "_open_modal", lambda _dlg: next(modal_results))
    monkeypatch.setattr(contacts_view_module, "save_contacts", fail_save)
    monkeypatch.setattr(
        contacts_view_module.QMessageBox,
        "warning",
        lambda _parent, _title, body: warnings.append(body),
    )
    view = ContactsView()
    view.show()

    view._on_edit_contact(contact.id)
    qt_app.processEvents()

    try:
        loaded = contacts.load_contacts()[0]
        assert (loaded.name, loaded.number, loaded.group, loaded.favorite) == (
            "Alice",
            "1001",
            "NOC",
            False,
        )
        assert "disk full" in warnings[0]
    finally:
        view.close()


def test_delete_save_failure_warns_and_keeps_current_list(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[contacts.Contact] = []
    contact = contacts.add_contact(rows, "Alice", "1001", group="NOC")
    contacts.save_contacts(rows)
    warnings: list[str] = []

    def fail_save(_contacts) -> None:  # noqa: ANN001
        raise PermissionError("readonly")

    monkeypatch.setattr(contacts_view_module, "save_contacts", fail_save)
    monkeypatch.setattr(
        contacts_view_module.QMessageBox,
        "warning",
        lambda _parent, _title, body: warnings.append(body),
    )
    # _on_delete_contact also opens QMessageBox.question for confirmation
    # before the save is attempted. Without patching it the modal blocks
    # the test process indefinitely (full pytest suite hangs forever).
    monkeypatch.setattr(
        contacts_view_module.QMessageBox,
        "question",
        lambda *_a, **_kw: contacts_view_module.QMessageBox.StandardButton.Yes,
    )
    view = ContactsView()
    view.show()

    view._on_delete_contact(contact.id)
    qt_app.processEvents()

    try:
        assert [item.id for item in contacts.load_contacts()] == [contact.id]
        assert "readonly" in warnings[0]
    finally:
        view.close()


def test_search_shows_matches_inside_collapsed_group(qt_app: QApplication) -> None:
    rows: list[contacts.Contact] = []
    contacts.add_contact(rows, "Alice", "1001", group="NOC")
    contacts.add_contact(rows, "Bob", "2002", group="NOC")
    contacts.save_contacts(rows)
    view = ContactsView()
    view.show()
    qt_app.processEvents()

    view._toggle_group("NOC")
    qt_app.processEvents()
    assert _visible_contact_names(view) == []

    view.search.setText("1001")
    qt_app.processEvents()

    try:
        assert _visible_contact_names(view) == ["Alice"]
    finally:
        view.close()


def test_search_keeps_matches_visible_when_group_toggled(qt_app: QApplication) -> None:
    rows: list[contacts.Contact] = []
    contacts.add_contact(rows, "Alice", "1001", group="NOC")
    contacts.save_contacts(rows)
    view = ContactsView()
    view.show()
    qt_app.processEvents()

    view.search.setText("alice")
    qt_app.processEvents()
    view._toggle_group("NOC")
    qt_app.processEvents()

    try:
        assert _visible_contact_names(view) == ["Alice"]
    finally:
        view.close()


def test_group_chevron_click_toggles_group(qt_app: QApplication) -> None:
    rows: list[contacts.Contact] = []
    contacts.add_contact(rows, "Alice", "1001", group="NOC")
    contacts.save_contacts(rows)
    view = ContactsView()
    view.show()
    qt_app.processEvents()
    group_row = view.findChildren(GroupRow)[0]
    chevron = group_row.findChild(QToolButton, "GroupChevron")

    QTest.mouseClick(chevron, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    try:
        assert _visible_contact_names(view) == []
    finally:
        view.close()
