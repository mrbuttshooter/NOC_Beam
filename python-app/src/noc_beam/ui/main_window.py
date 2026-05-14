"""NOC_Beam main window."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from noc_beam import __app_name__, __version__
from noc_beam.config.store import (
    AccountConfig,
    GlobalSettings,
    load_accounts,
    load_settings,
    save_accounts,
    save_settings,
)
from noc_beam.sip.endpoint import SipEndpoint
from noc_beam.sip.events import sip_events
from noc_beam.ui.account_dialog import AccountDialog
from noc_beam.ui.call_widget import CallWidget
from noc_beam.ui.dialpad import DialPad
from noc_beam.ui.settings_dialog import SettingsDialog
from noc_beam.ui.trace_view import TraceView

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{__app_name__} {__version__}")
        self.resize(1100, 720)

        self.settings: GlobalSettings = load_settings()
        self.accounts: list[AccountConfig] = load_accounts()
        self._active_call = None  # current SipCall in the call widget

        self._build_ui()
        self._connect_events()
        self._refresh_account_list()

        # Defer SIP endpoint startup until the event loop is running so that
        # any error dialog can be displayed asynchronously.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, self._start_sip)

    def _start_sip(self) -> None:
        SipEndpoint.instance().start(self.settings)
        for acc in self.accounts:
            if acc.enabled:
                self._add_account_to_endpoint(acc)
        self._refresh_account_list()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        # Toolbar
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        act_add_acc = QAction("Add account", self)
        act_add_acc.triggered.connect(self._on_add_account)
        tb.addAction(act_add_acc)

        act_edit_acc = QAction("Edit account", self)
        act_edit_acc.triggered.connect(self._on_edit_account)
        tb.addAction(act_edit_acc)

        act_remove_acc = QAction("Remove account", self)
        act_remove_acc.triggered.connect(self._on_remove_account)
        tb.addAction(act_remove_acc)

        tb.addSeparator()

        act_settings = QAction("Settings", self)
        act_settings.triggered.connect(self._on_settings)
        tb.addAction(act_settings)

        # Left: accounts + dialpad
        self.account_list = QListWidget()
        self.account_list.setObjectName("AccountList")

        self.active_account = QComboBox()
        self.active_account.setObjectName("ActiveAccount")

        left_top = QVBoxLayout()
        left_top.addWidget(QLabel("Accounts"))
        left_top.addWidget(self.account_list, 1)
        left_top.addWidget(QLabel("Place call from:"))
        left_top.addWidget(self.active_account)

        self.dialpad = DialPad()
        left_layout = QVBoxLayout()
        left_layout.addLayout(left_top, 1)
        left_layout.addWidget(self.dialpad)
        left_widget = QWidget()
        left_widget.setLayout(left_layout)
        left_widget.setMaximumWidth(360)

        # Right: tabbed call view + trace
        self.call_widget = CallWidget()
        self.trace_view = TraceView()

        right_tabs = QTabWidget()
        right_tabs.addTab(self.call_widget, "Call")
        right_tabs.addTab(self.trace_view, "SIP trace")
        self.right_tabs = right_tabs

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(right_tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        # Status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Starting…")

    def _connect_events(self) -> None:
        ev = sip_events()
        ev.endpoint_started.connect(lambda: self.status.showMessage("SIP endpoint started"))
        ev.endpoint_stopped.connect(lambda: self.status.showMessage("SIP endpoint stopped"))
        ev.endpoint_error.connect(self._on_endpoint_error)
        ev.registration_changed.connect(self._on_registration_changed)
        ev.call_incoming.connect(self._on_call_incoming)
        ev.call_state_changed.connect(self._on_call_state)
        ev.call_media_active.connect(self._on_call_media)
        ev.call_ended.connect(self._on_call_ended)

        self.dialpad.call_requested.connect(self._on_call_requested)
        self.dialpad.hangup_requested.connect(self._on_hangup_requested)
        self.dialpad.digit_pressed.connect(self._on_digit_pressed)

        self.call_widget.answer_clicked.connect(self._on_answer)
        self.call_widget.reject_clicked.connect(self._on_reject)
        self.call_widget.hangup_clicked.connect(self._on_hangup_by_id)

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------
    def _refresh_account_list(self) -> None:
        self.account_list.clear()
        self.active_account.clear()
        for acc in self.accounts:
            label = acc.display_name or f"{acc.username}@{acc.domain}"
            item = QListWidgetItem(f"{label}  [{acc.transport.upper()}]")
            item.setData(Qt.UserRole, acc.id)
            self.account_list.addItem(item)
            if acc.enabled:
                self.active_account.addItem(label, acc.id)

    def _add_account_to_endpoint(self, cfg: AccountConfig) -> None:
        try:
            SipEndpoint.instance().add_account(cfg)
        except Exception as e:
            log.exception("Failed to add account %s", cfg.id)
            QMessageBox.warning(self, "Account error", str(e))

    def _on_add_account(self) -> None:
        dlg = AccountDialog(parent=self)
        if dlg.exec() == AccountDialog.Accepted:
            cfg = dlg.result_account()
            self.accounts.append(cfg)
            save_accounts(self.accounts)
            if cfg.enabled:
                self._add_account_to_endpoint(cfg)
            self._refresh_account_list()

    def _selected_account(self) -> AccountConfig | None:
        item = self.account_list.currentItem()
        if item is None:
            return None
        aid = item.data(Qt.UserRole)
        return next((a for a in self.accounts if a.id == aid), None)

    def _on_edit_account(self) -> None:
        acc = self._selected_account()
        if acc is None:
            return
        dlg = AccountDialog(account=acc, parent=self)
        if dlg.exec() == AccountDialog.Accepted:
            new_cfg = dlg.result_account()
            self.accounts = [new_cfg if a.id == acc.id else a for a in self.accounts]
            save_accounts(self.accounts)
            SipEndpoint.instance().remove_account(acc.id)
            if new_cfg.enabled:
                self._add_account_to_endpoint(new_cfg)
            self._refresh_account_list()

    def _on_remove_account(self) -> None:
        acc = self._selected_account()
        if acc is None:
            return
        if QMessageBox.question(self, "Remove account", f"Remove {acc.username}@{acc.domain}?") != QMessageBox.Yes:
            return
        SipEndpoint.instance().remove_account(acc.id)
        self.accounts = [a for a in self.accounts if a.id != acc.id]
        save_accounts(self.accounts)
        self._refresh_account_list()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _on_settings(self) -> None:
        dlg = SettingsDialog(self.settings, parent=self)
        if dlg.exec() == SettingsDialog.Accepted:
            codec_map = dlg.apply_to(self.settings)
            save_settings(self.settings)
            # Apply codec changes live
            from noc_beam.codecs.manager import set_priority

            for cid, prio in codec_map.items():
                set_priority(cid, prio)
            from noc_beam.audio.devices import set_active_devices

            set_active_devices(
                self.settings.audio.input_device,
                self.settings.audio.output_device,
            )
            self.status.showMessage("Settings applied", 3000)

    # ------------------------------------------------------------------
    # SIP event handlers
    # ------------------------------------------------------------------
    def _on_endpoint_error(self, msg: str) -> None:
        log.error("Endpoint error: %s", msg)
        self.status.showMessage(f"Endpoint error: {msg}")
        # Only nag the user with a dialog if they've already configured accounts
        if self.accounts:
            QMessageBox.warning(self, "SIP endpoint error", msg)

    def _on_registration_changed(self, account_id: str, code: int, reason: str) -> None:
        acc = next((a for a in self.accounts if a.id == account_id), None)
        label = acc.display_name if acc and acc.display_name else (acc.username if acc else account_id)
        self.status.showMessage(f"[{label}] registration: {code} {reason}", 5000)

    def _on_call_incoming(self, account_id: str, call_id: int, remote: str, is_in: bool) -> None:
        self.call_widget.show_incoming(call_id, remote)
        self.right_tabs.setCurrentWidget(self.call_widget)
        ep = SipEndpoint.instance()
        acc = ep.get_account(account_id)
        if acc:
            for c in acc.calls:
                if c.getInfo().id == call_id:
                    self._active_call = c
                    break

    def _on_call_state(self, account_id: str, call_id: int, state: str, code: int, reason: str) -> None:
        self.call_widget.update_state(state, code, reason)
        self.dialpad.set_in_call(state not in ("DISCONNECTED", "NULL"))

    def _on_call_media(self, call_id: int, codec: str, clock: int, channels: int) -> None:
        self.call_widget.update_media(codec, clock, channels)

    def _on_call_ended(self, call_id: int) -> None:
        self.call_widget.show_idle()
        self.dialpad.set_in_call(False)
        self._active_call = None

    # ------------------------------------------------------------------
    # Dialpad actions
    # ------------------------------------------------------------------
    def _on_call_requested(self, target: str) -> None:
        acc_id = self.active_account.currentData()
        if not acc_id:
            QMessageBox.information(self, "No account", "Add a SIP account first.")
            return
        try:
            call = SipEndpoint.instance().make_call(acc_id, target)
            self._active_call = call
            self.call_widget.show_outgoing(-1, target)
            self.dialpad.set_in_call(True)
            self.right_tabs.setCurrentWidget(self.call_widget)
        except Exception as e:
            log.exception("make_call failed")
            QMessageBox.warning(self, "Call failed", str(e))

    def _on_hangup_requested(self) -> None:
        if self._active_call:
            try:
                SipEndpoint.instance().hangup_call(self._active_call)
            except Exception:
                log.exception("hangup failed")

    def _on_hangup_by_id(self, _call_id: int) -> None:
        self._on_hangup_requested()

    def _on_answer(self, _call_id: int) -> None:
        if self._active_call:
            SipEndpoint.instance().answer_call(self._active_call)

    def _on_reject(self, _call_id: int) -> None:
        if self._active_call:
            SipEndpoint.instance().hangup_call(self._active_call, code=603)

    def _on_digit_pressed(self, digit: str) -> None:
        if self._active_call is None:
            return
        acc_id = self.active_account.currentData()
        acc_cfg = next((a for a in self.accounts if a.id == acc_id), None)
        if acc_cfg is None:
            return
        try:
            SipEndpoint.instance().send_dtmf(self._active_call, digit, acc_cfg)
        except Exception:
            log.exception("send_dtmf failed")

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802, ANN001
        try:
            SipEndpoint.instance().stop()
        except Exception:
            log.exception("Endpoint stop error")
        super().closeEvent(event)
