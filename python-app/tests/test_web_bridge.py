"""Headless tests for the web softphone bridge + serializers.

QtWebEngine may not render offscreen in CI, so we deliberately do NOT
instantiate WebShell / QWebEngineView here. Instead we pin:
  * the pure state->JSON serializers (webui/serializers.py), which mirror the
    Qt widgets and are the contract the page renders, and
  * WebBridge slot -> PhoneShell method routing, using fake phone/web objects.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication
_APP = QApplication.instance() or QApplication([])

from noc_beam.sip.call_manager import CallRecord, CallState  # noqa: E402
from noc_beam.webui import serializers as S  # noqa: E402
from noc_beam.webui.bridge import WebBridge  # noqa: E402


# ======================================================================
# short_peer
# ======================================================================
def test_short_peer_strips_scheme_params_and_domain() -> None:
    assert S.short_peer("sip:0035796109901@host;transport=udp") == "0035796109901"
    assert S.short_peer("sips:alice@example.com") == "alice"
    assert S.short_peer('"Bob" <sip:bob@pbx>') == "bob"
    assert S.short_peer("35799999999") == "35799999999"
    assert S.short_peer("") == ""


# ======================================================================
# serialize_call
# ======================================================================
def _rec(**kw) -> CallRecord:
    base = dict(call_id=7, account_id="a1", account_label="Teles UK",
                remote_uri="sip:0035796109901@x", supplier_label="AAA Tel",
                direction="out", state=CallState.CALLING, started_at=1000.0)
    base.update(kw)
    return CallRecord(**base)


def test_serialize_call_none_and_terminal_states() -> None:
    assert S.serialize_call(None) is None
    assert S.serialize_call(_rec(state=CallState.NULL)) is None
    assert S.serialize_call(_rec(state=CallState.DISCONNECTED)) is None


def test_serialize_call_chip_mapping() -> None:
    assert S.serialize_call(_rec(state=CallState.CALLING))["chip"] == "Calling…"
    assert S.serialize_call(_rec(state=CallState.CALLING))["level"] == "prog"
    ring = S.serialize_call(_rec(state=CallState.EARLY))
    assert (ring["chip"], ring["level"]) == ("Ringing", "ring")
    conf = S.serialize_call(_rec(state=CallState.CONFIRMED, connected_at=1002.0, codec="G729"))
    assert (conf["chip"], conf["level"], conf["phase"]) == ("Connected", "ok", "talk")
    held = S.serialize_call(_rec(state=CallState.HELD, connected_at=1002.0))
    assert (held["chip"], held["level"], held["phase"], held["held"]) == ("On hold", "hold", "hold", True)


def test_serialize_call_incoming_and_controls() -> None:
    inc = S.serialize_call(_rec(direction="in", state=CallState.INCOMING))
    assert inc["incoming"] is True
    assert inc["chip"] == "Incoming"
    assert inc["canControl"] is False
    # canControl only once media can exist.
    assert S.serialize_call(_rec(state=CallState.CONFIRMED, connected_at=1002.0))["canControl"] is True
    assert S.serialize_call(_rec(state=CallState.CALLING))["canControl"] is False


def test_serialize_call_anchor_and_via() -> None:
    # Ringing -> anchor is started_at.
    ring = S.serialize_call(_rec(state=CallState.EARLY, started_at=1000.0))
    assert ring["anchorMs"] == 1000 * 1000
    # Connected -> anchor is connected_at.
    conf = S.serialize_call(_rec(state=CallState.CONFIRMED, started_at=1000.0, connected_at=1002.0, codec="G729"))
    assert conf["anchorMs"] == 1002 * 1000
    assert conf["via"] == "Teles UK · AAA Tel · G729"
    # No supplier/codec -> via is just the account label.
    bare = S.serialize_call(_rec(supplier_label="", codec=""))
    assert bare["via"] == "Teles UK"


# ======================================================================
# health_bucket + serialize_accounts
# ======================================================================
def test_health_bucket() -> None:
    assert S.health_bucket(200) == "ok"
    assert S.health_bucket(204) == "ok"
    for c in (401, 403, 407, 423):
        assert S.health_bucket(c) == "warn"
    assert S.health_bucket(0) == "muted"
    assert S.health_bucket(408) == "danger"
    assert S.health_bucket(503) == "danger"


def _acc(aid: str, label: str, enabled: bool = True):
    return SimpleNamespace(id=aid, label=label, enabled=enabled,
                           display_name="", username="u", domain="d")


def test_serialize_accounts_active_and_health() -> None:
    accts = [_acc("a1", "Teles UK"), _acc("a2", "Genband"), _acc("a3", "Off", enabled=False)]
    out = S.serialize_accounts(accts, "a1", {"a1": 200, "a2": 408})
    ids = [a["id"] for a in out["accounts"]]
    assert ids == ["a1", "a2"]  # disabled a3 excluded
    assert out["activeId"] == "a1"
    assert out["activeLabel"] == "Teles UK"
    assert out["activeHealth"] == "ok"
    a2 = next(a for a in out["accounts"] if a["id"] == "a2")
    assert a2["health"] == "danger"


def test_serialize_accounts_empty() -> None:
    out = S.serialize_accounts([], "", {})
    assert out["accounts"] == []
    assert out["activeLabel"] == "No account"
    assert out["activeHealth"] == "muted"


def test_serialize_accounts_active_not_registered_yet() -> None:
    out = S.serialize_accounts([_acc("a1", "Teles UK")], "a1", {})
    assert out["activeHealth"] == "muted"  # 0 sentinel -> grey dot


# ======================================================================
# serialize_recents
# ======================================================================
def _cdr(peer: str, answered: bool, ended: float, code: int = 200, direction: str = "out", dialed: str = ""):
    return SimpleNamespace(
        peer_uri=peer, dialed_uri=dialed, direction=direction,
        end_code=code, end_reason="", ended_at=ended,
        was_answered=answered,
    )


def test_serialize_recents_order_dedupe_and_badge() -> None:
    hist = [
        _cdr("sip:111@x", True, 100.0, code=200),
        _cdr("sip:222@x", False, 300.0, code=486),
        _cdr("sip:111@x", True, 400.0, code=200),   # newer dup of 111
        _cdr("sip:333@x", False, 200.0, code=408, direction="in"),
    ]
    rows = S.serialize_recents(hist, limit=10)
    # Newest-first, deduped by peer: 111 (400) , 222 (300), 333 (200)
    assert [r["num"] for r in rows] == ["111", "222", "333"]
    assert rows[0]["level"] == "ok"      # answered
    assert rows[1]["level"] == "bad"     # not answered
    assert rows[2]["dir"] == "in"
    assert rows[0]["uri"] == "sip:111@x"


def test_serialize_recents_prefers_dialed_uri_and_limits() -> None:
    hist = [_cdr("sip:realpeer@x", True, float(i), dialed=f"{i}") for i in range(20)]
    rows = S.serialize_recents(hist, limit=5)
    # All share the same peer differing only by dialed_uri -> dedupe is by the
    # displayed target (dialed_uri preferred), so 5 distinct rows.
    assert len(rows) == 5
    assert rows[0]["num"] == "19"


# ======================================================================
# serialize_suppliers
# ======================================================================
def test_serialize_suppliers() -> None:
    out = S.serialize_suppliers([("AAA Tel — C080", "080"), ("BBB — C207", "207")], "207", True)
    assert out["visible"] is True
    assert out["activeId"] == "207"
    assert out["suppliers"] == [
        {"id": "080", "label": "AAA Tel — C080"},
        {"id": "207", "label": "BBB — C207"},
    ]
    assert S.serialize_suppliers([], "", False)["visible"] is False


# ======================================================================
# WebBridge routing
# ======================================================================
def _fake_phone(rec: CallRecord | None = None):
    phone = MagicMock()
    phone._selected_call_id = None if rec is None else rec.call_id
    phone.calls.get.return_value = rec
    phone.calls.first_active.return_value = rec
    phone.accounts = [_acc("a1", "Teles UK")]
    return phone


def test_place_call_routes_and_guards_empty() -> None:
    phone = _fake_phone()
    bridge = WebBridge(phone, MagicMock())
    bridge.place_call("35799999999")
    phone._on_call_requested.assert_called_once_with("35799999999")
    phone._on_call_requested.reset_mock()
    bridge.place_call("   ")
    phone._on_call_requested.assert_not_called()


def test_redial_hangup_answer_reject_route() -> None:
    rec = _rec(call_id=9)
    phone = _fake_phone(rec)
    bridge = WebBridge(phone, MagicMock())
    bridge.redial("200")
    phone._on_call_requested.assert_called_once_with("200")
    bridge.hangup()
    phone._on_hangup_requested.assert_called_once()
    bridge.answer()
    phone._on_answer.assert_called_once_with(9)
    bridge.reject()
    phone._on_reject.assert_called_once_with(9)


def test_toggle_mute_flips_record_state() -> None:
    rec = _rec(call_id=3, muted=False)
    phone = _fake_phone(rec)
    bridge = WebBridge(phone, MagicMock())
    bridge.toggle_mute()
    phone._on_mute_toggled.assert_called_once_with(3, True)


def test_toggle_hold_and_resume() -> None:
    rec = _rec(call_id=4, state=CallState.CONFIRMED, connected_at=1.0)
    phone = _fake_phone(rec)
    bridge = WebBridge(phone, MagicMock())
    bridge.toggle_hold()
    phone._on_hold.assert_called_once_with(4)
    # Now held -> toggling resumes.
    rec.state = CallState.HELD
    bridge.toggle_hold()
    phone._on_resume.assert_called_once_with(4)


def test_send_dtmf_noop_without_active_call() -> None:
    phone = _fake_phone()
    phone._selected_pjsua_call.return_value = None
    bridge = WebBridge(phone, MagicMock())
    bridge.send_dtmf("5")  # must not raise
    bridge.send_dtmf("")   # empty guard


def test_select_account_routes_and_pushes() -> None:
    phone = _fake_phone()
    phone._account_label.return_value = "Teles UK"
    web = MagicMock()
    bridge = WebBridge(phone, web)
    bridge.select_account("a1")
    phone._set_active_account.assert_called_once_with("a1", "Teles UK")
    web.push_account_and_supplier.assert_called_once()


def test_select_supplier_sets_combo_index() -> None:
    phone = _fake_phone()
    phone.supplier_combo.findData.return_value = 2
    web = MagicMock()
    bridge = WebBridge(phone, web)
    bridge.select_supplier("207")
    phone.supplier_combo.findData.assert_called_once_with("207")
    phone.supplier_combo.setCurrentIndex.assert_called_once_with(2)


def test_open_window_routes() -> None:
    phone = _fake_phone()
    web = MagicMock()
    bridge = WebBridge(phone, web)
    bridge.open_window("settings"); phone._on_settings.assert_called_once()
    bridge.open_window("accounts"); phone._on_open_accounts.assert_called_once()
    bridge.open_window("trace"); phone._on_open_trace.assert_called_once()
    bridge.open_window("test-runner"); phone._on_open_test_runner.assert_called_once()
    bridge.open_window("history"); web.open_history.assert_called_once()
    bridge.open_window("contacts"); web.open_contacts.assert_called_once()
    bridge.open_window("favorites"); web.open_favorites.assert_called_once()
    bridge.open_window("quit"); phone._on_quit.assert_called_once()
    # Unknown target is a harmless no-op.
    bridge.open_window("bogus")


def test_window_ops_route_to_webshell() -> None:
    phone = _fake_phone()
    web = MagicMock()
    bridge = WebBridge(phone, web)
    bridge.minimize(); web.showMinimized.assert_called_once()
    bridge.close_win(); web.close.assert_called_once()
    bridge.start_move(); web.windowHandle.return_value.startSystemMove.assert_called_once()


def test_ready_triggers_full_push() -> None:
    phone = _fake_phone()
    web = MagicMock()
    bridge = WebBridge(phone, web)
    bridge.ready()
    web.push_all.assert_called_once()
