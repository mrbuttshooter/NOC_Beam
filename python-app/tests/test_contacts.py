"""Tests for persistent contacts storage."""
from __future__ import annotations

from pathlib import Path

import pytest

from noc_beam.config import contacts


@pytest.fixture(autouse=True)
def isolated_contacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(contacts, "contacts_file", lambda: tmp_path / "contacts.json")
    yield


def test_load_missing_file_returns_empty() -> None:
    assert contacts.load_contacts() == []


def test_malformed_file_logs_and_returns_empty() -> None:
    contacts.contacts_file().write_text("{not json", encoding="utf-8")

    assert contacts.load_contacts() == []


def test_add_strips_defaults_and_allows_duplicates() -> None:
    rows: list[contacts.Contact] = []

    first = contacts.add_contact(rows, " Alice ", " 1001 ", group=" ", favorite=True)
    second = contacts.add_contact(rows, " Alice ", " 1001 ", group=" ", favorite=False)

    assert first.name == "Alice"
    assert first.number == "1001"
    assert first.group == "Work"
    assert first.favorite is True
    assert second.name == "Alice"
    assert second.number == "1001"
    assert first.id != second.id
    assert len(rows) == 2


def test_add_requires_name_and_number() -> None:
    rows: list[contacts.Contact] = []

    with pytest.raises(ValueError):
        contacts.add_contact(rows, "", "1001")
    with pytest.raises(ValueError):
        contacts.add_contact(rows, "Alice", " ")


def test_update_validates_and_raises_key_error() -> None:
    rows = [contacts.Contact(id="c1", name="Alice", number="1001")]

    updated = contacts.update_contact(
        rows,
        "c1",
        name=" Bob ",
        number=" 2002 ",
        group=" ",
        favorite=True,
    )

    assert updated.name == "Bob"
    assert updated.number == "2002"
    assert updated.group == "Work"
    assert updated.favorite is True

    with pytest.raises(ValueError):
        contacts.update_contact(rows, "c1", name=" ")
    with pytest.raises(ValueError):
        contacts.update_contact(rows, "c1", number="")
    with pytest.raises(KeyError):
        contacts.update_contact(rows, "missing", name="Nobody")


def test_delete_returns_true_or_false() -> None:
    rows = [contacts.Contact(id="c1", name="Alice", number="1001")]

    assert contacts.delete_contact(rows, "missing") is False
    assert contacts.delete_contact(rows, "c1") is True
    assert rows == []


def test_save_load_roundtrip() -> None:
    rows = [
        contacts.Contact(
            id="c1",
            name="Alice",
            number="1001",
            group="NOC",
            favorite=True,
        ),
        contacts.Contact(id="c2", name="Bob", number="2002"),
    ]

    contacts.save_contacts(rows)

    assert contacts.load_contacts() == rows
