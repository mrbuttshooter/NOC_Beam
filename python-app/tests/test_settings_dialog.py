"""Regression tests for SettingsDialog.apply_to."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication
_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])

from noc_beam.config.store import GlobalSettings  # noqa: E402
from noc_beam.ui.settings_dialog import SettingsDialog  # noqa: E402


def test_apply_to_does_not_raise_unbound_local_when_modern_codec_ui_present() -> None:
    """Regression: SettingsDialog.apply_to used to raise
    UnboundLocalError on `spins` whenever the modern drag-drop codec UI
    branch was taken (the legacy `_codec_priority_spins` path was the
    only branch that bound `spins`). Operator "Apply" therefore failed
    silently. apply_to must complete cleanly regardless of which codec
    UI variant is in use."""
    dlg = SettingsDialog(GlobalSettings())
    try:
        # Force the modern UI branch: ensure the drag-drop columns are
        # present (the dialog wires these up in its normal build path,
        # so this assertion just documents the precondition the bug
        # report describes).
        assert hasattr(dlg, "_codec_enabled_list")
        assert hasattr(dlg, "_codec_disabled_list")

        settings = GlobalSettings()
        # Must not raise UnboundLocalError.
        codec_map = dlg.apply_to(settings)
        assert isinstance(codec_map, dict)
    finally:
        dlg.close()


def test_apply_to_legacy_spins_branch_returns_codec_map() -> None:
    """Force the legacy `_codec_priority_spins` branch by deleting the
    drag-drop column attrs, and verify the fallback path still works."""
    dlg = SettingsDialog(GlobalSettings())
    try:
        # Strip the modern-UI attrs to force the else-branch in apply_to.
        for attr in ("_codec_enabled_list", "_codec_disabled_list"):
            if hasattr(dlg, attr):
                delattr(dlg, attr)
        settings = GlobalSettings()
        codec_map = dlg.apply_to(settings)
        assert isinstance(codec_map, dict)
    finally:
        dlg.close()
