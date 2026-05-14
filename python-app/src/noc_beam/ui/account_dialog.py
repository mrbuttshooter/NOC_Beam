"""Dialog to add/edit a single SIP account."""
from __future__ import annotations

import uuid

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
)

from noc_beam.config.store import AccountConfig


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

        form = QFormLayout(self)
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

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._account_id = account.id

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
