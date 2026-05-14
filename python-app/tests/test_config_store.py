"""Round-trip tests for the config store (no pjsua2 needed)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from noc_beam.config import store


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(store, "settings_file", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(store, "accounts_file", lambda: tmp_path / "accounts.json")
    yield


def test_default_settings_loads_when_no_file() -> None:
    s = store.load_settings()
    assert s.sip_port == 0
    assert "PCMA/8000" in s.codecs.priorities


def test_settings_roundtrip() -> None:
    s = store.load_settings()
    s.sip_port = 5070
    s.audio.input_device = 3
    s.codecs.priorities["custom/1234"] = 99
    store.save_settings(s)

    s2 = store.load_settings()
    assert s2.sip_port == 5070
    assert s2.audio.input_device == 3
    assert s2.codecs.priorities["custom/1234"] == 99


def test_accounts_roundtrip_protects_password() -> None:
    acc = store.AccountConfig(
        id="abc",
        username="alice",
        domain="sip.example.com",
        password="hunter2",
        transport="tls",
        srtp="mandatory",
    )
    store.save_accounts([acc])

    raw = json.loads(store.accounts_file().read_text())
    assert raw[0]["password"] != "hunter2"
    assert raw[0]["password"].startswith(("dpapi:", "b64:"))

    loaded = store.load_accounts()
    assert len(loaded) == 1
    assert loaded[0].password == "hunter2"
    assert loaded[0].transport == "tls"
    assert loaded[0].srtp == "mandatory"


def test_empty_password_is_empty_in_storage() -> None:
    acc = store.AccountConfig(id="no-pw", username="x", domain="y")
    store.save_accounts([acc])
    raw = json.loads(store.accounts_file().read_text())
    assert raw[0]["password"] == ""
    assert store.load_accounts()[0].password == ""
