"""Round-trip and bounds checks for the persistent call history."""
from __future__ import annotations

from pathlib import Path

import pytest

from noc_beam.config import history

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication
_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])

from noc_beam.ui.history_view import HistoryRow  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(history, "history_file", lambda: tmp_path / "call_history.json")
    yield


def _make(idx: int, *, connected: bool = True) -> history.CdrEntry:
    return history.CdrEntry(
        call_id=idx,
        account_id="acc1",
        peer_uri=f"sip:peer{idx}@x",
        direction="out" if idx % 2 else "in",
        started_at=1000.0 + idx,
        connected_at=(1001.0 + idx) if connected else None,
        ended_at=1020.0 + idx,
        end_code=200,
        end_reason="OK",
        codec="opus/48000/2",
    )


def test_empty_when_no_file() -> None:
    assert history.load_history() == []


def test_append_and_reload() -> None:
    history.append_entry(_make(1))
    history.append_entry(_make(2))
    loaded = history.load_history()
    assert [e.call_id for e in loaded] == [1, 2]
    assert loaded[0].codec == "opus/48000/2"


def test_unanswered_call_has_zero_duration() -> None:
    entry = _make(3, connected=False)
    history.append_entry(entry)
    loaded = history.load_history()[0]
    assert loaded.was_answered is False
    assert loaded.duration_s == 0.0


def test_answered_duration_is_end_minus_connected() -> None:
    entry = _make(4)
    history.append_entry(entry)
    loaded = history.load_history()[0]
    assert loaded.duration_s == pytest.approx(loaded.ended_at - loaded.connected_at)


def test_capped_at_max_entries() -> None:
    # Write more than MAX_ENTRIES; only the most recent should survive.
    over = history.MAX_ENTRIES + 5
    entries = [_make(i) for i in range(over)]
    history.save_history(entries)
    loaded = history.load_history()
    assert len(loaded) == history.MAX_ENTRIES
    # save_history trims by keeping the tail (newest), so highest indices remain.
    assert loaded[-1].call_id == over - 1
    assert loaded[0].call_id == over - history.MAX_ENTRIES


def test_clear_removes_file() -> None:
    history.append_entry(_make(1))
    history.clear_history()
    assert history.load_history() == []


def test_malformed_file_resets_gracefully(tmp_path: Path) -> None:
    history.history_file().write_text("{this is not json", encoding="utf-8")
    assert history.load_history() == []


def test_history_row_uses_result_badge_and_action_column() -> None:
    entry = history.CdrEntry(
        call_id=1,
        account_id="acc-1",
        peer_uri="sip:alice@example.com",
        direction="out",
        started_at=1.0,
        connected_at=2.0,
        ended_at=8.0,
        end_code=200,
        end_reason="OK",
        codec="PCMU",
    )
    row = HistoryRow(entry, 0)

    try:
        assert row.objectName() == "HistoryRow"
        assert row.findChild(QtWidgets.QLabel, "SipCodeBadge") is not None
        assert row.findChild(QtWidgets.QToolButton, "HistoryRowCall") is not None
    finally:
        row.close()
