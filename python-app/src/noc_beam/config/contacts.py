"""Persistent contact storage for the Contacts tab."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, fields
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
    except Exception:
        # Quarantine the corrupt file rather than silently returning [] --
        # otherwise the very next save_contacts would overwrite it and
        # destroy the user's entire contact list. Quarantined file is
        # timestamped so multiple corruptions don't collide.
        try:
            quarantine = path.with_name(
                f"{path.stem}.corrupt-{int(time.time())}{path.suffix}"
            )
            path.rename(quarantine)
            log.error(
                "contacts.json was unreadable; quarantined to %s",
                quarantine.name,
            )
        except Exception:
            log.exception(
                "Failed to read contacts AND failed to quarantine; "
                "leaving file in place to prevent overwrite"
            )
        return []
    if not isinstance(raw, list):
        # Top-level shape is wrong (someone hand-edited it, wrote a dict,
        # etc.). Same risk as a JSON parse failure: returning [] here
        # would let the next save wipe recoverable data. Quarantine it.
        try:
            quarantine = path.with_name(
                f"{path.stem}.corrupt-{int(time.time())}{path.suffix}"
            )
            path.rename(quarantine)
            log.error(
                "contacts.json top-level was not a list; quarantined to %s",
                quarantine.name,
            )
        except Exception:
            log.exception(
                "contacts.json top-level was not a list AND failed to "
                "quarantine; leaving file in place to prevent overwrite"
            )
        return []
    # Filter unknown keys per row so a stale field on disk (e.g. one
    # written by a newer build with extra dataclass fields) doesn't take
    # out the whole list. Skip individual malformed rows rather than
    # returning [] for the whole file -- preserves recoverable contacts.
    known = {f.name for f in fields(Contact)}
    out: list[Contact] = []
    for item in raw:
        if not isinstance(item, dict):
            log.warning("Skipping non-dict contact row: %r", item)
            continue
        clean = {k: v for k, v in item.items() if k in known}
        try:
            out.append(Contact(**clean))
        except TypeError:
            log.warning("Skipping malformed contact row: %s", item)
            continue
    return out


def save_contacts(contacts: list[Contact]) -> None:
    path = contacts_file()
    payload = [asdict(contact) for contact in contacts]
    tmp = path.with_suffix(path.suffix + ".tmp")
    # fsync before replace so a power loss can't leave a zero-length
    # contacts.json: the rename may be journaled ahead of the data blocks.
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2))
        f.flush()
        os.fsync(f.fileno())
    # Windows: tmp.replace can fail if the target is held open by an
    # antivirus / file watcher. Retry a couple of times before giving up.
    last_err: BaseException | None = None
    for _ in range(3):
        try:
            tmp.replace(path)
            return
        except Exception as exc:
            last_err = exc
            time.sleep(0.05)
    # Clean up the orphaned tmp so it doesn't poison the next save.
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    log.error("Failed to atomically save contacts after retries: %s", last_err)
    raise last_err  # type: ignore[misc]


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
