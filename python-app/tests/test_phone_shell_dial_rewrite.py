"""Regression tests for PhoneShell._rewrite_dial_target.

Specifically guards the Teles dial_prefix path against mangling
alphabetic SIP usernames like `echo` -- the bug that turned
`echo@iptel.org` (or just `echo`) into `00echo` and caused the
registrar to reject the INVITE."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication
_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])

from noc_beam.config.store import AccountConfig  # noqa: E402
from noc_beam.ui.phone_shell import PhoneShell  # noqa: E402


@pytest.fixture
def teles_account() -> AccountConfig:
    return AccountConfig(
        id="acct-teles",
        switch_type="teles",
        dial_prefix="00",
    )


@pytest.fixture
def shell(monkeypatch: pytest.MonkeyPatch, teles_account: AccountConfig) -> PhoneShell:
    monkeypatch.setattr(
        "noc_beam.ui.phone_shell.QTimer.singleShot", lambda _ms, _fn: None
    )
    s = PhoneShell()
    # Force _selected_account() to return our teles fixture regardless
    # of the configured-accounts list so the rewrite logic runs.
    monkeypatch.setattr(s, "_selected_account", lambda: teles_account)
    yield s
    s.close()


def test_dial_rewrite_skips_alpha_username(shell: PhoneShell) -> None:
    """Alphabetic SIP usernames must NOT receive the Teles `00` prefix.
    Before the fix `echo` was rewritten to `00echo` which the registrar
    rejected with a 4xx response."""
    assert shell._rewrite_dial_target("echo") == "echo"


def test_dial_rewrite_skips_uri_with_at_sign(shell: PhoneShell) -> None:
    """Targets containing @ (already a SIP URI fragment) are passed
    through unchanged regardless of prefix configuration."""
    assert shell._rewrite_dial_target("echo@iptel.org") == "echo@iptel.org"


def test_dial_rewrite_applies_prefix_to_pure_digits(shell: PhoneShell) -> None:
    """Pure digit strings are the original/intended use case for the
    Teles `00` international-prefix rule."""
    assert shell._rewrite_dial_target("12345") == "0012345"


def test_dial_rewrite_passes_through_e164_plus_form(shell: PhoneShell) -> None:
    """Numbers already in E.164 form (leading `+`) should not be
    re-prefixed -- `00+12345` would be an invalid hybrid. The `+`
    already signals the international break."""
    assert shell._rewrite_dial_target("+12345") == "+12345"


def test_dial_rewrite_skips_mixed_alnum(shell: PhoneShell) -> None:
    """Mixed alphanumeric targets (e.g. service identifiers) must not
    receive the numeric prefix either."""
    assert shell._rewrite_dial_target("test1234abc") == "test1234abc"
