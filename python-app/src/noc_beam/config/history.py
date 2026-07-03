"""Persistent CDR (call history).

The live call state lives in `noc_beam.sip.call_manager`. Once a call ends we
write a final record here so it survives restarts. Stored as a JSON list under
the user's data dir; capped at MAX_ENTRIES to keep the file bounded.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from noc_beam.config.paths import data_dir

log = logging.getLogger(__name__)

MAX_ENTRIES = 10000

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
    dialed_uri: str = ""
    supplier_id: str = ""
    supplier_label: str = ""
    # FAS (False Answer Supervision) verdict captured at disconnect.
    # Empty when FAS is disabled or the call never reached CONFIRMED.
    fas_verdict: str = ""
    fas_confidence: float = 0.0
    fas_reasons: str = ""

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


def _archive_file(year: int, month: int) -> Path:
    return data_dir() / f"call_history.{year:04d}-{month:02d}.json"


def _archive_unknown_file() -> Path:
    """Catch-all archive for entries whose timestamp is missing/unparseable."""
    return data_dir() / "call_history.archive.json"


def _archive_key(entry: CdrEntry) -> Path:
    """Pick the archive file for an entry based on ended_at (fallback started_at).
    Unparseable / zero timestamps go to the catch-all archive."""
    ts = entry.ended_at if entry.ended_at else entry.started_at
    if not ts or ts <= 0:
        return _archive_unknown_file()
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return _archive_unknown_file()
    return _archive_file(dt.year, dt.month)


def _atomic_write_json(path: Path, payload: list[dict]) -> None:
    """Mirror save_history's tempfile + replace + 3-retry-backoff pattern.
    Raises the last exception if all retries fail."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    # fsync before replace so a power loss can't leave a zero-length
    # archive file: the rename may be journaled ahead of the data blocks.
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2))
        f.flush()
        os.fsync(f.fileno())
    last_err: Exception | None = None
    for _ in range(3):
        try:
            tmp.replace(path)
            return
        except Exception as exc:
            last_err = exc
            time.sleep(0.05)
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    assert last_err is not None
    raise last_err


def _append_to_archive(overflow: list[CdrEntry]) -> None:
    """Append trimmed-off entries to monthly archive files. A failure here
    must NOT block the live save -- we'd rather lose deep history than
    drop the live CDR. Each target archive file is read, extended, and
    rewritten atomically (same pattern as save_history)."""
    if not overflow:
        return
    # Bucket by target path so we read+rewrite each archive file at most once.
    buckets: dict[Path, list[CdrEntry]] = {}
    for e in overflow:
        buckets.setdefault(_archive_key(e), []).append(e)
    for arc_path, new_entries in buckets.items():
        try:
            existing: list[dict] = []
            if arc_path.exists():
                try:
                    raw = json.loads(arc_path.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        existing = [r for r in raw if isinstance(r, dict)]
                except Exception:
                    # Don't quarantine the archive; just log and overwrite with
                    # what we can salvage from this batch. Losing one archive
                    # file is preferable to dropping the live save.
                    log.warning(
                        "Archive %s unreadable; rewriting with current batch",
                        arc_path.name,
                    )
                    existing = []
            payload = existing + [asdict(e) for e in new_entries]
            _atomic_write_json(arc_path, payload)
        except Exception:
            log.exception(
                "Failed to append %d CDR(s) to archive %s; continuing",
                len(new_entries),
                arc_path.name,
            )


def save_history(entries: list[CdrEntry]) -> None:
    global _cache, _cache_path
    path = history_file()
    # Normalise to chronological order (oldest first) before capping so
    # the trailing slice consistently keeps the NEWEST entries regardless
    # of caller input ordering. Previously, the delete path passed a
    # newest-first list (HistoryView sorts reverse=True), and entries[-N:]
    # silently kept the OLDEST N -- dropping the most recent calls. Fixed
    # by sorting here.
    entries = sorted(entries, key=lambda e: getattr(e, "ended_at", 0) or 0)
    if len(entries) > MAX_ENTRIES:
        overflow = entries[:-MAX_ENTRIES]
        trimmed = entries[-MAX_ENTRIES:]
        # Best-effort: route the dropped entries to monthly archive files
        # before they fall off the hot cache. _append_to_archive swallows
        # its own errors so a flaky archive write never blocks the live
        # save.
        _append_to_archive(overflow)
    else:
        trimmed = entries
    payload = [asdict(e) for e in trimmed]
    tmp = path.with_suffix(path.suffix + ".tmp")
    # fsync before replace so a power loss can't leave a zero-length
    # call_history.json (the rename may be journaled ahead of the data).
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2))
        f.flush()
        os.fsync(f.fileno())
    # Windows: tmp.replace can fail if the target is held open by an
    # antivirus / file watcher. Retry a couple of times before giving up.
    last_err = None
    for _ in range(3):
        try:
            tmp.replace(path)
            # The module cache is the source of truth between calls, so it
            # MUST equal exactly what's now on disk. Two failure modes this
            # fixes together:
            #   (2) Previously save_history trimmed only `trimmed` (a local)
            #       and left _cache holding the full untrimmed list. Once
            #       the cache exceeded MAX_ENTRIES, EVERY subsequent
            #       append_entry re-computed the same overflow slice and
            #       re-archived it -- duplicate archive rows, O(n^2) archive
            #       growth, unbounded RAM. Rebinding _cache to `trimmed`
            #       caps it so the overflow is archived exactly once.
            #   (3) HistoryView's delete path builds a NEW list (minus the
            #       deleted CDR) and calls save_history on it; _cache still
            #       held the stale list, so the next append_entry wrote the
            #       deleted CDR back to disk (resurrection). Rebinding
            #       _cache here makes cache == disk after every save.
            _cache = list(trimmed)
            _cache_path = path
            return
        except Exception as exc:
            last_err = exc
            time.sleep(0.05)
    # Clean up the orphaned tmp so it doesn't poison the next save.
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    log.error("Failed to atomically save call history after retries: %s", last_err)
    raise last_err  # type: ignore[misc]


def load_archive(year: int, month: int) -> list[CdrEntry]:
    """Read the monthly archive file for the given year/month. Returns []
    if missing or unreadable. Schema-tolerant: unknown keys are dropped,
    malformed rows are skipped (mirrors load_history())."""
    path = _archive_file(year, month)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("Archive %s unreadable; returning empty list", path.name)
        return []
    if not isinstance(raw, list):
        return []
    known = {f.name for f in fields(CdrEntry)}
    out: list[CdrEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        clean = {k: v for k, v in item.items() if k in known}
        try:
            out.append(CdrEntry(**clean))
        except TypeError:
            log.warning("Skipping malformed archived CDR row: %s", item)
            continue
    return out


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
    try:
        save_history(entries)
    except Exception:
        # Roll back the in-memory append so cache and disk stay in sync.
        # save_history() retries 3x then raises (AV lock, disk full, etc.);
        # if we left the entry in _cache, disk would be missing it and the
        # next successful save would trim from a divergent list, silently
        # dropping real CDRs. Re-raise so the caller knows the save failed.
        if _cache is not None and _cache and _cache[-1] is entry:
            _cache.pop()
        raise


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
        # Use the same tmp + fsync + replace pattern as the rest of the
        # module. A direct write_text here is non-atomic: a crash mid-write
        # leaves a truncated/zero-length meta file, and on next launch
        # load_last_seen_ended_at() swallows the parse error and returns
        # 0.0 -- re-lighting every prior missed-call badge.
        _atomic_write_json_obj(path, {"last_seen_ended_at": float(ts)})
    except Exception:
        log.exception("Failed to save history meta (last_seen_ended_at)")


def _atomic_write_json_obj(path: Path, payload: dict) -> None:
    """Atomic write for the tiny JSON meta side-file (a dict, not a list).
    Mirrors _atomic_write_json's fsync-before-replace + retry pattern."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload))
        f.flush()
        os.fsync(f.fileno())
    last_err: Exception | None = None
    for _ in range(3):
        try:
            tmp.replace(path)
            return
        except Exception as exc:
            last_err = exc
            time.sleep(0.05)
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    assert last_err is not None
    raise last_err
