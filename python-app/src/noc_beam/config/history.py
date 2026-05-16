"""Persistent CDR (call history).

The live call state lives in `noc_beam.sip.call_manager`. Once a call ends we
write a final record here so it survives restarts. Stored as a JSON list under
the user's data dir; capped at MAX_ENTRIES to keep the file bounded.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from noc_beam.config.paths import data_dir

log = logging.getLogger(__name__)

MAX_ENTRIES = 1000

# Module-level in-memory cache. append_entry() used to call
# load_history() on EVERY hangup, re-reading + re-deserializing the
# full 1000-entry JSON before appending one row -- O(n) IO per call
# ended. We now load once on first access and treat the cache as the
# source of truth between calls; save_history() still atomically
# writes the trimmed tail so the on-disk file stays bounded.
_cache: list["CdrEntry"] | None = None
_cache_path: Path | None = None  # which history_file() the cache belongs to


def _invalidate_cache() -> None:
    """Reset the in-memory cache. Tests use this between fixtures; in
    production it's only hit when history_file() changes (data dir
    swap, never happens at runtime)."""
    global _cache, _cache_path
    _cache = None
    _cache_path = None


def history_file() -> Path:
    return data_dir() / "call_history.json"


def history_meta_file() -> Path:
    """Tiny side-file storing user-level state about the history list
    (e.g. newest-seen timestamp for unread-missed badge persistence)."""
    return data_dir() / "call_history_meta.json"


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
    global _cache, _cache_path
    path = history_file()
    _cache_path = path
    if not path.exists():
        _cache = []
        return _cache
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # Quarantine the corrupt file rather than silently returning []
        # -- otherwise the very next append_entry would overwrite it
        # with a single record and destroy ALL prior CDRs. Quarantined
        # file is timestamped so multiple corruptions don't collide.
        try:
            quarantine = path.with_name(
                f"{path.stem}.corrupt-{int(time.time())}{path.suffix}"
            )
            path.rename(quarantine)
            log.error(
                "call_history.json was unreadable; quarantined to %s",
                quarantine.name,
            )
        except Exception:
            log.exception(
                "Failed to read call history AND failed to quarantine; "
                "leaving file in place to prevent overwrite"
            )
        _cache = []
        return _cache
    # Filter unknown keys per row so a stale field on disk doesn't take
    # out the whole list. Also drop any row missing required fields.
    known = {f.name for f in fields(CdrEntry)}
    out: list[CdrEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        clean = {k: v for k, v in item.items() if k in known}
        try:
            out.append(CdrEntry(**clean))
        except TypeError:
            log.warning("Skipping malformed CDR row: %s", item)
            continue
    _cache = out
    return out


def save_history(entries: list[CdrEntry]) -> None:
    path = history_file()
    # Keep newest-first ordering and cap.
    trimmed = entries[-MAX_ENTRIES:]
    payload = [asdict(e) for e in trimmed]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Windows: tmp.replace can fail if the target is held open by an
    # antivirus / file watcher. Retry a couple of times before giving up.
    last_err = None
    for _ in range(3):
        try:
            tmp.replace(path)
            return
        except Exception as exc:
            last_err = exc
            time.sleep(0.05)
    log.error("Failed to atomically save call history after retries: %s", last_err)
    raise last_err  # type: ignore[misc]


def _ensure_cache() -> list[CdrEntry]:
    global _cache, _cache_path
    path = history_file()
    if _cache is None or _cache_path != path:
        _cache = load_history()
        _cache_path = path
    return _cache


def append_entry(entry: CdrEntry) -> None:
    """Append a single CDR row. Safe to call from any UI handler."""
    entries = _ensure_cache()
    entries.append(entry)
    save_history(entries)


def clear_history() -> None:
    global _cache, _cache_path
    _cache = []
    _cache_path = history_file()
    path = history_file()
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------
# History meta: persist "newest CDR the user has seen" so the missed
# call badge doesn't re-light every prior missed call after restart.
# ---------------------------------------------------------------------
def load_last_seen_ended_at() -> float:
    path = history_meta_file()
    if not path.exists():
        return 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("last_seen_ended_at", 0.0))
    except Exception:
        return 0.0


def save_last_seen_ended_at(ts: float) -> None:
    path = history_meta_file()
    try:
        path.write_text(
            json.dumps({"last_seen_ended_at": float(ts)}),
            encoding="utf-8",
        )
    except Exception:
        log.exception("Failed to save history meta (last_seen_ended_at)")
