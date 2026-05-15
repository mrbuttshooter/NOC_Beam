"""Regression test for _open_modal.

PySide6 6.7+ removed QDialog.Accepted as a class attribute on the
instance namespace; only QDialog.DialogCode.Accepted survives. The
old `runner() == dlg.Accepted` form raised AttributeError on every
modal dismissal, which silently aborted Add Account / Edit Account
/ etc. (the dialog closed but no save happened).

This test pins the API:
  1. _open_modal returns True when the dialog is accepted
  2. _open_modal returns False when the dialog is rejected
  3. The implementation does not reference dlg.Accepted
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from noc_beam.ui.phone_shell import _open_modal as phone_open_modal  # noqa: E402
from noc_beam.ui.contacts_view import _open_modal as contacts_open_modal  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.mark.parametrize("opener", [phone_open_modal, contacts_open_modal])
def test_open_modal_returns_true_on_accept(qapp, opener):
    dlg = QDialog()
    QTimer.singleShot(0, dlg.accept)
    assert opener(dlg) is True


@pytest.mark.parametrize("opener", [phone_open_modal, contacts_open_modal])
def test_open_modal_returns_false_on_reject(qapp, opener):
    dlg = QDialog()
    QTimer.singleShot(0, dlg.reject)
    assert opener(dlg) is False


@pytest.mark.parametrize(
    "opener", [phone_open_modal, contacts_open_modal],
    ids=["phone_shell", "contacts_view"],
)
def test_open_modal_does_not_use_instance_Accepted(opener):
    src = inspect.getsource(opener)
    assert "dlg.Accepted" not in src, (
        "_open_modal must use QDialog.DialogCode.Accepted, not dlg.Accepted "
        "(removed in PySide6 6.7+). See ui/phone_shell.py and ui/contacts_view.py."
    )
