"""Pure state -> plain-dict serializers for the web softphone bridge.

Everything here is deliberately Qt-free (no widgets, no signals) so it can
be unit-tested headlessly -- QtWebEngine may not render offscreen in CI, but
these functions turn CallRecord / account / history data into the JSON the
web UI consumes, and THAT is what we pin in tests/test_web_bridge.py.

The mappings mirror the existing Qt widgets so the web UI reads identically:
  * call chip text/level      -> ui/call_widget.py:update_state
  * recents arrow/badge       -> ui/quick_dial.py:_arrow/_chip
  * account health bucket     -> ui/phone_shell.py:_health_bucket
"""
from __future__ import annotations

from typing import Any

from noc_beam.sip.call_manager import CallRecord, CallState


# ----------------------------------------------------------------------
# Live RX/TX meter level (bridged from PhoneShell's AudioStrip poll)
# ----------------------------------------------------------------------
def clamp_level(value: Any) -> int:
    """Coerce an audio-meter reading to a 0..100 int for nb.levels().

    PhoneShell's AudioStrip already clamps set_tx_level/set_rx_level to
    0..100; this defends the JSON push against a None/garbage read so the
    payload is always a legal 0..100 the web meters can consume.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, v))


# ----------------------------------------------------------------------
# Peer / number display (mirror ui/quick_dial.py:_short_uri + call_widget)
# ----------------------------------------------------------------------
def short_peer(uri: str) -> str:
    """Strip sip:/sips: scheme, ;params and @domain -> compact user-part.

    Matches ui/quick_dial.py:_short_uri and call_widget._split_peer so the
    web card / rows show the same headline the Qt UI shows.
    """
    if not uri:
        return ""
    s = uri.strip()
    if s.startswith('"') or s[:1] == "<":
        # "Friendly Name" <sip:user@host>
        if "<" in s and ">" in s:
            _, _, rest = s.partition("<")
            s = rest.rstrip(">")
    elif "<" in s and ">" in s:
        _, _, rest = s.partition("<")
        s = rest.rstrip(">")
    if s.startswith("sip:"):
        s = s[4:]
    elif s.startswith("sips:"):
        s = s[5:]
    s = s.split(";", 1)[0]
    if "@" in s:
        user, _, host = s.partition("@")
        return user or host
    return s


# ----------------------------------------------------------------------
# Live call card (mirror ui/call_widget.py:update_state)
# ----------------------------------------------------------------------
def _call_chip(state: str, code: int, reason: str) -> tuple[str, str]:
    """Return (chip_text, chip_level) for a CallState.

    chip_level is a CSS class the web UI styles: ring (amber, breathing),
    ok (green), bad (red), prog (neutral indigo).
    """
    if state == "CALLING":
        return "Calling…", "prog"
    if state == "EARLY":
        return "Ringing", "ring"
    if state in ("CONNECTING",):
        return "Connecting", "prog"
    if state == "CONFIRMED":
        return "Connected", "ok"
    if state == "HELD":
        return "On hold", "hold"
    if state == "INCOMING":
        return "Incoming", "ring"
    if state == "DISCONNECTED":
        if code and not (200 <= code < 300):
            return (f"{code} {reason}".strip(), "bad")
        return "Ended", "bad"
    if code:
        level = "ok" if 200 <= code < 300 else ("bad" if code >= 400 else "prog")
        return (f"{code} {reason}".strip(), level)
    return (state.title(), "prog")


def serialize_call(rec: CallRecord | None) -> dict[str, Any] | None:
    """Turn the selected/active CallRecord into the web card payload.

    Returns None when there is no live call (card hidden). `anchorMs` is the
    epoch (ms) the web UI counts up from: connected_at once talking,
    started_at while still ringing -- so the card shows talk-time after
    answer and ring-time before, exactly like CallWidget's timer phase.
    """
    if rec is None:
        return None
    state = rec.state.value if isinstance(rec.state, CallState) else str(rec.state)
    if state in ("NULL", "DISCONNECTED"):
        return None
    chip_text, chip_level = _call_chip(state, rec.last_code, rec.last_reason)
    incoming = rec.direction == "in" and state == "INCOMING"
    connected = rec.connected_at is not None
    held = state == "HELD"
    phase = "hold" if held else ("talk" if connected else "ring")
    anchor = rec.connected_at if connected else rec.started_at
    # Context line: "via <account> · <supplier> · <codec>"
    parts = [p for p in (rec.account_label or "", rec.supplier_label or "") if p]
    via = " · ".join(parts)
    if rec.codec:
        via = f"{via} · {rec.codec}" if via else rec.codec
    return {
        "id": int(rec.call_id),
        "peer": short_peer(rec.remote_uri or rec.dialed_uri or ""),
        "state": state,
        "chip": chip_text,
        "level": chip_level,
        "phase": phase,
        "via": via,
        "direction": rec.direction,
        "incoming": incoming,
        "muted": bool(rec.muted),
        "held": held,
        # Controls become live only once media can exist.
        "canControl": state in ("CONFIRMED", "HELD"),
        "anchorMs": int((anchor or 0) * 1000),
        "showTimer": state in ("CALLING", "EARLY", "CONFIRMED", "HELD"),
    }


def serialize_calls(
    records: list[CallRecord],
    selected_id: int | None = None,
) -> list[dict[str, Any]]:
    """All ACTIVE calls as card payloads, oldest call first (stable stack
    order), each flagged with `selected` (the call holding audio focus --
    mirrors PhoneShell._selected_call_id / the Qt calls_strip).

    Phase-2 multi-call: the web UI renders ONE card per entry; terminal
    records (NULL/DISCONNECTED) are dropped by serialize_call.
    """
    out: list[dict[str, Any]] = []
    for rec in sorted(records, key=lambda r: r.call_id):
        d = serialize_call(rec)
        if d is None:
            continue
        d["selected"] = rec.call_id == selected_id
        out.append(d)
    return out


# ----------------------------------------------------------------------
# Recents (mirror ui/quick_dial.py:_arrow/_chip + _collect_targets dedupe)
# ----------------------------------------------------------------------
def _recent_row(entry: Any) -> dict[str, Any]:
    from noc_beam.ui.components import sip_label

    answered = bool(getattr(entry, "was_answered", False))
    direction = getattr(entry, "direction", "out") or "out"
    code = getattr(entry, "end_code", 0) or 0
    reason = getattr(entry, "end_reason", "") or ""
    label = sip_label(code) if code else (reason or "—")
    level = "ok" if answered else "bad"
    dialed = (getattr(entry, "dialed_uri", "") or "").strip()
    uri = dialed or (getattr(entry, "peer_uri", "") or "")
    import time as _time

    ended = getattr(entry, "ended_at", 0) or 0
    tstr = _time.strftime("%H:%M:%S", _time.localtime(ended)) if ended else ""
    return {
        "num": short_peer(uri),
        "uri": uri,
        "status": label,
        "level": level,
        "dir": direction,
        "answered": answered,
        "time": tstr,
    }


def serialize_recents(history: list[Any], limit: int = 10) -> list[dict[str, Any]]:
    """Last `limit` DISTINCT-peer CDR rows, newest first.

    Dedupe-by-peer + newest-first ordering match ui/quick_dial.py so the web
    recents list is identical to the Qt strip's.
    """
    ordered = sorted(history, key=lambda e: getattr(e, "ended_at", 0) or 0, reverse=True)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in ordered:
        dialed = (getattr(entry, "dialed_uri", "") or "").strip()
        uri = (dialed or (getattr(entry, "peer_uri", "") or "")).strip()
        if not uri or uri in seen:
            continue
        seen.add(uri)
        out.append(_recent_row(entry))
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------
# History view (phase 2: full searchable list + per-row detail)
# ----------------------------------------------------------------------
def serialize_history(
    entries: list[Any],
    accounts: list[Any] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Full history rows, newest first, NOT peer-deduped (unlike recents --
    the History tab is the log, recents is the shortcut strip). Each row
    carries a `detail` block for the expandable info panel; account ids are
    resolved to their human labels via the accounts list."""
    label_by_id: dict[str, str] = {}
    for a in accounts or []:
        label_by_id[str(getattr(a, "id", ""))] = (
            getattr(a, "label", "")
            or getattr(a, "display_name", "")
            or f"{getattr(a, 'username', '')}@{getattr(a, 'domain', '')}"
        )
    import time as _time

    ordered = sorted(entries, key=lambda e: getattr(e, "ended_at", 0) or 0, reverse=True)
    out: list[dict[str, Any]] = []
    for e in ordered[:limit]:
        row = _recent_row(e)
        dur = float(getattr(e, "duration_s", 0.0) or 0.0)
        m, s = divmod(int(dur), 60)
        h, m = divmod(m, 60)
        code = getattr(e, "end_code", 0) or 0
        reason = getattr(e, "end_reason", "") or ""
        ended = getattr(e, "ended_at", 0) or 0
        row["detail"] = {
            "dialed": getattr(e, "dialed_uri", "") or "",
            "peer": getattr(e, "peer_uri", "") or "",
            "account": label_by_id.get(str(getattr(e, "account_id", "")), ""),
            "supplier": getattr(e, "supplier_label", "") or "",
            "codec": getattr(e, "codec", "") or "",
            "result": (f"{code} {reason}".strip() if code else reason),
            "duration": (f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}") if dur else "",
            "when": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(ended)) if ended else "",
        }
        out.append(row)
    return out


# ----------------------------------------------------------------------
# Contacts / favorites (config/contacts.py Contact dataclass)
# ----------------------------------------------------------------------
def serialize_contacts(contacts: list[Any]) -> list[dict[str, Any]]:
    """All contacts, name-sorted. The favorites tab is the favorite=True
    subset (client-side filter, same payload)."""
    out: list[dict[str, Any]] = []
    for c in sorted(contacts, key=lambda c: (getattr(c, "name", "") or "").lower()):
        out.append(
            {
                "id": str(getattr(c, "id", "")),
                "name": getattr(c, "name", "") or "",
                "number": getattr(c, "number", "") or "",
                "group": getattr(c, "group", "") or "",
                "favorite": bool(getattr(c, "favorite", False)),
            }
        )
    return out


# ----------------------------------------------------------------------
# Accounts + registration health (mirror ui/phone_shell.py:_health_bucket)
# ----------------------------------------------------------------------
def health_bucket(code: int) -> str:
    """SIP registration code -> health bucket. Matches PhoneShell._health_bucket
    but with an explicit 'muted' for the never-registered sentinel so the web
    dot renders grey (not green) before the first registration event."""
    if 200 <= code < 300:
        return "ok"
    if code in (401, 403, 407, 423):
        return "warn"
    if code == 0:
        return "muted"
    return "danger"


def serialize_accounts(
    accounts: list[Any],
    active_id: str,
    reg_state: dict[str, int],
) -> dict[str, Any]:
    """Account switcher payload: the enabled accounts + per-account health,
    plus the active account's label/health for the chip."""

    def _label(a: Any) -> str:
        return (
            getattr(a, "label", "")
            or getattr(a, "display_name", "")
            or f"{getattr(a, 'username', '')}@{getattr(a, 'domain', '')}"
        )

    items = []
    active_label = "No account"
    active_health = "muted"
    for a in accounts:
        if not getattr(a, "enabled", True):
            continue
        aid = getattr(a, "id", "")
        health = health_bucket(reg_state.get(aid, 0))
        label = _label(a)
        items.append({"id": aid, "label": label, "health": health})
        if aid == active_id:
            active_label = label
            active_health = health
    if not items:
        active_label = "No account"
        active_health = "muted"
    return {
        "accounts": items,
        "activeId": active_id if any(i["id"] == active_id for i in items) else "",
        "activeLabel": active_label,
        "activeHealth": active_health,
    }


# ----------------------------------------------------------------------
# Suppliers (mirror ui/phone_shell.py:_refresh_supplier_picker model)
# ----------------------------------------------------------------------
def serialize_suppliers(
    all_suppliers: list[tuple[str, str]],
    active_id: str,
    visible: bool,
) -> dict[str, Any]:
    """Supplier select payload. all_suppliers is PhoneShell._all_suppliers:
    a list of (display, id) tuples."""
    return {
        "visible": bool(visible),
        "activeId": str(active_id or ""),
        "suppliers": [{"id": str(sid), "label": disp} for (disp, sid) in all_suppliers],
    }
