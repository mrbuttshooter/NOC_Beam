"""Dialog to add/edit a single SIP account."""
from __future__ import annotations

import logging
import uuid

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from noc_beam.config.store import AccountConfig

log = logging.getLogger(__name__)


# How long to wait for a registration response before declaring timeout.
TEST_TIMEOUT_MS = 8000


class AccountDialog(QDialog):
    def __init__(self, account: AccountConfig | None = None, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("SIP account")
        self.setMinimumWidth(420)

        self._editing = account is not None
        if account is None:
            account = AccountConfig(id=str(uuid.uuid4()))

        self.display_name = QLineEdit(account.display_name)
        self.username = QLineEdit(account.username)
        self.auth_user = QLineEdit(account.auth_user)
        self.domain = QLineEdit(account.domain)
        self.password = QLineEdit(account.password)
        self.password.setEchoMode(QLineEdit.Password)
        self.proxy = QLineEdit(account.proxy)
        self.stun_server = QLineEdit(account.stun_server)

        self.transport = QComboBox()
        self.transport.addItems(["udp", "tcp", "tls"])
        self.transport.setCurrentText(account.transport)

        self.srtp = QComboBox()
        self.srtp.addItems(["disabled", "optional", "mandatory"])
        self.srtp.setCurrentText(account.srtp)

        self.dtmf_method = QComboBox()
        self.dtmf_method.addItems(["rfc2833", "info", "inband"])
        self.dtmf_method.setCurrentText(account.dtmf_method)

        self.register = QCheckBox("Register on add")
        self.register.setChecked(account.register)

        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(account.enabled)

        form = QFormLayout()
        form.addRow("Display name", self.display_name)
        form.addRow("Username", self.username)
        form.addRow("Auth user (if different)", self.auth_user)
        form.addRow("Domain / registrar", self.domain)
        form.addRow("Password", self.password)
        form.addRow("Outbound proxy (optional)", self.proxy)
        form.addRow("STUN server (optional)", self.stun_server)
        form.addRow("Transport", self.transport)
        form.addRow("SRTP", self.srtp)
        form.addRow("DTMF method", self.dtmf_method)
        form.addRow(self.register)
        form.addRow(self.enabled)

        # Test row — its label is reused for live + final status.
        self.test_btn = QPushButton("Test registration")
        self.test_btn.clicked.connect(self._on_test)
        self.test_status = QLabel("")
        self.test_status.setObjectName("AccountTestStatus")
        self.test_status.setWordWrap(True)
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_status, 1, Qt.AlignVCenter)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addLayout(test_row)
        root.addWidget(buttons)

        self._account_id = account.id

        # Per-test bookkeeping
        self._test_id: str | None = None
        self._test_timer: QTimer | None = None
        self._test_conn = None

    def result_account(self) -> AccountConfig:
        return AccountConfig(
            id=self._account_id,
            display_name=self.display_name.text().strip(),
            username=self.username.text().strip(),
            auth_user=self.auth_user.text().strip(),
            domain=self.domain.text().strip(),
            password=self.password.text(),
            proxy=self.proxy.text().strip(),
            transport=self.transport.currentText(),
            register=self.register.isChecked(),
            srtp=self.srtp.currentText(),
            dtmf_method=self.dtmf_method.currentText(),
            stun_server=self.stun_server.text().strip(),
            enabled=self.enabled.isChecked(),
        )

    # ------------------------------------------------------------------
    # Test registration
    # ------------------------------------------------------------------
    def _on_test(self) -> None:
        username = self.username.text().strip()
        domain = self.domain.text().strip()
        if not username or not domain:
            self._set_status("Username and domain are required.", ok=False)
            return

        # Late import to avoid pulling SIP at dialog construction time.
        from noc_beam.sip.endpoint import SipEndpoint
        from noc_beam.sip.events import sip_events

        ep = SipEndpoint.instance()
        if not ep.is_started():
            self._set_status("SIP endpoint isn't running yet.", ok=False)
            return

        # Build a throw-away account with a sentinel id so we can match the
        # incoming registration_changed signal without colliding with any
        # real account.
        self._test_id = f"__test__{uuid.uuid4().hex[:8]}"
        cfg = self.result_account()
        cfg.id = self._test_id
        cfg.register = True
        cfg.enabled = True

        self._set_status("Registering…", ok=None)
        self.test_btn.setEnabled(False)

        self._test_conn = sip_events().registration_changed.connect(self._on_reg_event)
        self._test_timer = QTimer(self)
        self._test_timer.setSingleShot(True)
        self._test_timer.timeout.connect(self._on_test_timeout)
        self._test_timer.start(TEST_TIMEOUT_MS)

        try:
            ep.add_account(cfg)
        except Exception as e:
            log.exception("test add_account failed")
            self._set_status(f"Could not start test: {e}", ok=False)
            self._cleanup_test()

    def _on_reg_event(self, account_id: str, code: int, reason: str) -> None:
        if account_id != self._test_id:
            return
        # 0 with empty reason is the initial "not yet" event; ignore it.
        if code == 0 and not reason:
            return
        ok = 200 <= code < 300
        verdict = "OK" if ok else "FAIL"
        self._set_status(f"{verdict} — {code} {reason}", ok=ok)
        self._cleanup_test()

    def _on_test_timeout(self) -> None:
        self._set_status("Timeout — no response within "
                         f"{TEST_TIMEOUT_MS // 1000}s.", ok=False)
        self._cleanup_test()

    def _cleanup_test(self) -> None:
        from noc_beam.sip.endpoint import SipEndpoint
        from noc_beam.sip.events import sip_events

        if self._test_timer is not None:
            self._test_timer.stop()
            self._test_timer = None
        if self._test_conn is not None:
            try:
                sip_events().registration_changed.disconnect(self._on_reg_event)
            except Exception:
                pass
            self._test_conn = None
        if self._test_id is not None:
            try:
                SipEndpoint.instance().remove_account(self._test_id)
            except Exception:
                log.exception("could not remove test account %s", self._test_id)
            self._test_id = None
        self.test_btn.setEnabled(True)

    def _set_status(self, text: str, ok: bool | None) -> None:
        self.test_status.setText(text)
        color = {
            True:  "#66D19E",   # success
            False: "#FF5C7A",   # danger
            None:  "#B7C0CC",   # in-progress / neutral
        }[ok]
        self.test_status.setStyleSheet(f"color: {color};")
