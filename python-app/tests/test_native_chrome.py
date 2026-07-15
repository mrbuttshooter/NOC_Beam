"""Tests for native_chrome.py (dark DWM title bars).

DWM itself can't be meaningfully asserted offscreen (there is no real native
chrome under QT_QPA_PLATFORM=offscreen), so these tests pin the *contract*:

  1. apply_dark_title_bar never raises and returns a bool -- graceful no-op
     off-Windows / when DWM isn't reachable.
  2. DarkTitleBarFilter applies chrome on Show of top-level widgets only,
     re-queries the theme per Show, and treats "dark"/"dark-hc" as dark.
  3. The filter never consumes events and never lets exceptions escape.

Headless-safe: offscreen platform, filter logic driven with fake widgets and
synthesized events, DWM call monkeypatched out.
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import noc_beam.ui.native_chrome as nc  # noqa: E402
from noc_beam.ui.native_chrome import DarkTitleBarFilter, apply_dark_title_bar  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ---------------------------------------------------------------------------
# apply_dark_title_bar -- must be a graceful no-op when DWM can't cooperate
# ---------------------------------------------------------------------------
def test_apply_dark_title_bar_never_raises(qapp):
    """Offscreen there is no real DWM chrome; the call must not raise and
    must return a bool either way."""
    w = QWidget()
    result = apply_dark_title_bar(w, True)
    assert isinstance(result, bool)
    result = apply_dark_title_bar(w, False)
    assert isinstance(result, bool)


def test_apply_dark_title_bar_noop_off_windows(qapp, monkeypatch):
    monkeypatch.setattr(nc.sys, "platform", "linux")
    w = QWidget()
    assert apply_dark_title_bar(w, True) is False


# ---------------------------------------------------------------------------
# DarkTitleBarFilter -- Show-event logic with the DWM call stubbed out
# ---------------------------------------------------------------------------
class _ShowEvent(QEvent):
    def __init__(self):
        super().__init__(QEvent.Type.Show)


@pytest.fixture()
def dwm_spy(monkeypatch):
    """Stub out the real DWM call; record (widget, enabled) invocations."""
    calls: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        nc, "apply_dark_title_bar", lambda widget, enabled: calls.append((widget, enabled)) or True
    )
    return calls


def test_filter_applies_on_show_when_dark(qapp, dwm_spy):
    filt = DarkTitleBarFilter(lambda: "dark")
    w = QWidget()  # top-level: no parent -> isWindow() is True
    consumed = filt.eventFilter(w, _ShowEvent())
    assert consumed is False  # observe-only, never eat the event
    assert dwm_spy == [(w, True)]


def test_filter_skips_when_light(qapp, dwm_spy):
    filt = DarkTitleBarFilter(lambda: "light")
    w = QWidget()
    filt.eventFilter(w, _ShowEvent())
    assert dwm_spy == []


def test_filter_skips_non_toplevel_widgets(qapp, dwm_spy):
    filt = DarkTitleBarFilter(lambda: "dark")
    parent = QWidget()
    child = QWidget(parent)  # has a parent -> not a window
    assert not child.isWindow()
    filt.eventFilter(child, _ShowEvent())
    assert dwm_spy == []


def test_filter_ignores_non_show_events(qapp, dwm_spy):
    filt = DarkTitleBarFilter(lambda: "dark")
    w = QWidget()
    filt.eventFilter(w, QEvent(QEvent.Type.Hide))
    assert dwm_spy == []


def test_filter_rechecks_theme_per_show(qapp, dwm_spy):
    """A runtime theme switch must affect windows shown afterwards."""
    theme = {"value": "light"}
    filt = DarkTitleBarFilter(lambda: theme["value"])
    w = QWidget()

    filt.eventFilter(w, _ShowEvent())
    assert dwm_spy == []  # light: untouched

    theme["value"] = "dark"  # user switches theme at runtime
    filt.eventFilter(w, _ShowEvent())
    assert dwm_spy == [(w, True)]  # next Show gets dark chrome


def test_filter_dark_variants_count_as_dark(qapp, dwm_spy):
    filt = DarkTitleBarFilter(lambda: "dark-hc")
    w = QWidget()
    filt.eventFilter(w, _ShowEvent())
    assert dwm_spy == [(w, True)]


def test_filter_survives_broken_theme_provider(qapp, dwm_spy):
    """Provider raising must not propagate; fail toward dark (app default)."""
    def _boom() -> str:
        raise RuntimeError("settings store unavailable")

    filt = DarkTitleBarFilter(_boom)
    w = QWidget()
    consumed = filt.eventFilter(w, _ShowEvent())
    assert consumed is False
    assert dwm_spy == [(w, True)]
