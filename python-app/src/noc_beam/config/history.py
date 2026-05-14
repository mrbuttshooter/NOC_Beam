"""Persistent CDR (call history).

The live call state lives in `noc_beam.sip.call_manager`. Once a call ends we
write a final record here so it survives restarts. Stored as a JSON list under
the user's data dir; capped at MAX_ENTRIES to keep the file bounded.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from noc_beam.config.paths import data_dir

log = logging.getLogger(__name__)

MAX_ENTRIES = 1000


def history_file() -> Path:
    return data_dir() / "call_history.json"


@dataclass
class CdrEntry:
    call_id: int
    account_id: str
    peer_uri: str
    direction: str          # "in" | "out"
    started_at: float
    connected_at: float | None
    ended_at: float
    end_code: int = 0
    end_reason: str = ""
    codec: str = ""

    @property
    def duration_s(self) -> float:
        if self.connected_at is None:
            return 0.0
        return max(0.0, self.ended_at - self.connected_at)

    @property
    def was_answered(self) -> bool:
        return self.connected_at is not None


def load_history() -> list[CdrEntry]:
    path = history_file()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [CdrEntry(**item) for item in raw]
    except Exception:
        log.exception("Failed to read call history; starting fresh")
        return []


def save_history(entries: list[CdrEntry]) -> None:
    path = history_file()
    # Keep newest-first ordering and cap.
    trimmed = entries[-MAX_ENTRIES:]
    payload = [asdict(e) for e in trimmed]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_entry(entry: CdrEntry) -> None:
    """Append a single CDR row. Safe to call from any UI handler."""
    entries = load_history()
    entries.append(entry)
    save_history(entries)


def clear_history() -> None:
    path = history_file()
    if path.exists():
        path.unlink()
