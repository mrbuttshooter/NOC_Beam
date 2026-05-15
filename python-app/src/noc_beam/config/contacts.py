"""Persistent contact storage for the Contacts tab."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from noc_beam.config.paths import config_dir

log = logging.getLogger(__name__)


@dataclass
class Contact:
    id: str
    name: str
    number: str
    group: str = "Work"
    favorite: bool = False


def contacts_file() -> Path:
    return config_dir() / "contacts.json"


def load_contacts() -> list[Contact]:
    path = contacts_file()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Contact(**item) for item in raw]
    except Exception:
        log.exception("Failed to load contacts")
        return []


def save_contacts(contacts: list[Contact]) -> None:
    path = contacts_file()
    payload = [asdict(contact) for contact in contacts]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_contact(
    contacts: list[Contact],
    name: str,
    number: str,
    group: str = "Work",
    favorite: bool = False,
) -> Contact:
    clean_name = _required_str(name, "name")
    clean_number = _required_str(number, "number")
    clean_group = _clean_group(group)
    contact = Contact(
        id=uuid.uuid4().hex,
        name=clean_name,
        number=clean_number,
        group=clean_group,
        favorite=bool(favorite),
    )
    contacts.append(contact)
    return contact


def update_contact(contacts: list[Contact], contact_id: str, **fields: Any) -> Contact:
    contact = next((item for item in contacts if item.id == contact_id), None)
    if contact is None:
        raise KeyError(contact_id)

    allowed = {"name", "number", "group", "favorite"}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "name":
            setattr(contact, key, _required_str(value, key))
        elif key == "number":
            setattr(contact, key, _required_str(value, key))
        elif key == "group":
            setattr(contact, key, _clean_group(value))
        elif key == "favorite":
            setattr(contact, key, bool(value))
    return contact


def delete_contact(contacts: list[Contact], contact_id: str) -> bool:
    for idx, contact in enumerate(contacts):
        if contact.id == contact_id:
            del contacts[idx]
            return True
    return False


def _required_str(value: Any, field_name: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    return cleaned


def _clean_group(value: Any) -> str:
    cleaned = str(value).strip()
    return cleaned or "Work"
