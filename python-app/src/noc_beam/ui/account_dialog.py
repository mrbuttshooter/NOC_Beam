"""Dialog to add/edit a single SIP account."""
from __future__ import annotations

import logging
import re
import uuid

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from noc_beam.config.store import AccountConfig
from noc_beam.ui.components import FooterActionBar, FormSection

log = logging.getLogger(__name__)


# How long to wait for a registration response before declaring timeout.
TEST_TIMEOUT_MS = 8000

# Reject anything that isn't a normal host name. Specifically blocks
# CR/LF and other control chars that could smuggle SIP headers when
# the URI is interpolated into an outgoing request.
_DOMAIN_RX = re.compile(r"^[A-Za-z0-9._:\[\]\-]+$")


class AccountDialog(QDialog):
    def __init__(self, account: AccountConfig | None = None, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("Edit SIP account" if account is not None else "Add SIP account")
        # 720px wide -- 2-column grid (Identity | Connection) keeps the
        # 80% case visible without scroll. Bottom row carries Switch +
        # routing as a single full-width card. Advanced/rare fields
        # collapse into a per-card disclosure.
        self.setMinimumWidth(720)
        self.resize(720, 540)

        self._editing = account is not None
        if account is None:
            account = AccountConfig(id=str(uuid.uuid4()))

        self.label = QLineEdit(getattr(account, "label", ""))
        self.label.setPlaceholderText("e.g. Production main, Test trunk #1")
        self.display_name = QLineEdit(account.display_name)
        self.username = QLineEdit(account.username)
        self.auth_user = QLineEdit(account.auth_user)
        self.domain = QLineEdit(account.domain)
        self.password = QLineEdit(account.password)
        self.password.setEchoMode(QLineEdit.Password)
        self.proxy = QLineEdit(account.proxy)
        self.stun_server = QLineEdit(account.stun_server)
        # Optional port. Blank or 0 = transport default (5060 / 5061).
        # Many real ITSPs publish on non-default ports.
        self.port = QLineEdit("" if not getattr(account, "port", 0) else str(account.port))
        self.port.setPlaceholderText("default (5060 / 5061)")
        self.port.setMaximumWidth(180)

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

        # ---- Switch type + per-account routing -----------------------
        # Determines whether the dial view's Supplier dropdown shows up
        # and which routing field gets the supplier substitution.
        self.switch_type = QComboBox()
        self.switch_type.addItems(["other", "teles", "genband"])
        self.switch_type.setCurrentText(getattr(account, "switch_type", "other"))
        self.switch_type.setToolTip(
            "Teles: supplier id becomes the auth username (re-register).\n"
            "Genband: supplier id becomes a dial prefix (no re-register).\n"
            "Other: no supplier picker; dial works as today."
        )
        self.dial_prefix = QLineEdit(getattr(account, "dial_prefix", ""))
        self.dial_prefix.setPlaceholderText("e.g. 00")
        self.dial_prefix.setToolTip(
            "Prefix auto-prepended to every dialled number on this "
            "account. Common case: Teles needs '00' before every number."
        )
        self.routing_format = QLineEdit(getattr(account, "routing_format", ""))
        self.routing_format.setPlaceholderText("e.g. U{id} or 000{id}")
        self.routing_format.setToolTip(
            "How a supplier's id is turned into the actual routing string.\n"
            "Use {id} as the placeholder.\n"
            "  Teles UK example: U{id}   -- supplier 303 becomes auth U303\n"
            "  Teles NY example: N{id}   -- supplier 303 becomes auth N303\n"
            "  Genband example:  000{id} -- supplier 303 becomes prefix 000303"
        )

        # ===== Identity card =====
        identity = FormSection("Identity", self)
        identity_form = QFormLayout()
        identity_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # `Account name` is the UI nickname (chip + picker label),
        # distinct from `Display name` which carries the A-number on
        # the SIP wire per operator workflow.
        identity_form.addRow("Account name", self.label)
        identity_form.addRow("Display name", self.display_name)
        identity_form.addRow("Username *", self.username)
        identity_form.addRow("Auth user", self.auth_user)
        identity.body.addLayout(identity_form)

        # ===== Connection card (main fields + Advanced disclosure) =====
        connection = FormSection("Connection", self)
        connection_form = QFormLayout()
        connection_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        connection_form.addRow("Domain *", self.domain)
        # Port + Transport inline (single visual row) -- saves a slot.
        from PySide6.QtWidgets import QHBoxLayout as _HBox, QWidget as _W
        port_transport_row = _W()
        ptr_l = _HBox(port_transport_row)
        ptr_l.setContentsMargins(0, 0, 0, 0)
        ptr_l.setSpacing(6)
        ptr_l.addWidget(self.port)
        ptr_l.addWidget(QLabel("Transport"))
        ptr_l.addWidget(self.transport, 1)
        connection_form.addRow("Port", port_transport_row)
        connection_form.addRow("Password", self.password)
        connection.body.addLayout(connection_form)

        # --- Advanced disclosure inside Connection card -------------
        # Hides the 80%-never-touched fields by default. Click to expand.
        # Summary chip shows the current values so even when collapsed
        # you can see what's set.
        from PySide6.QtWidgets import QFrame as _Frame, QToolButton as _TBtn
        self._adv_toggle = _TBtn()
        self._adv_toggle.setText("▸ Advanced")
        self._adv_toggle.setObjectName("AccountAdvancedToggle")
        self._adv_toggle.setCheckable(True)
        self._adv_toggle.setChecked(False)
        self._adv_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._adv_toggle.setAutoRaise(True)
        self._adv_summary = QLabel("")
        self._adv_summary.setObjectName("AccountAdvancedSummary")
        adv_header_row = QHBoxLayout()
        adv_header_row.setContentsMargins(0, 6, 0, 0)
        adv_header_row.addWidget(self._adv_toggle)
        adv_header_row.addWidget(self._adv_summary, 1)

        self._advanced_body = _Frame()
        self._advanced_body.setObjectName("AccountAdvancedBody")
        adv_body_form = QFormLayout(self._advanced_body)
        adv_body_form.setContentsMargins(0, 6, 0, 0)
        adv_body_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        adv_body_form.addRow("SRTP", self.srtp)
        adv_body_form.addRow("DTMF method", self.dtmf_method)
        adv_body_form.addRow("Outbound proxy", self.proxy)
        adv_body_form.addRow("STUN server", self.stun_server)
        adv_body_form.addRow(self.register)
        adv_body_form.addRow(self.enabled)
        self._advanced_body.setVisible(False)

        def _refresh_adv_summary():
            parts = [
                f"SRTP: {self.srtp.currentText()}",
                f"DTMF: {self.dtmf_method.currentText()}",
            ]
            if self.stun_server.text().strip():
                parts.append(f"STUN: {self.stun_server.text().strip()[:20]}")
            else:
                parts.append("STUN: —")
            if not self.register.isChecked():
                parts.append("no register")
            if not self.enabled.isChecked():
                parts.append("disabled")
            self._adv_summary.setText(" · ".join(parts))

        def _toggle_adv(*_a):
            on = self._adv_toggle.isChecked()
            self._advanced_body.setVisible(on)
            self._adv_toggle.setText("▾ Advanced" if on else "▸ Advanced")
            self._adv_summary.setVisible(not on)
        self._adv_toggle.toggled.connect(_toggle_adv)
        # Wire summary refresh so it always reflects current state.
        self.srtp.currentTextChanged.connect(_refresh_adv_summary)
        self.dtmf_method.currentTextChanged.connect(_refresh_adv_summary)
        self.stun_server.textChanged.connect(_refresh_adv_summary)
        self.register.toggled.connect(_refresh_adv_summary)
        self.enabled.toggled.connect(_refresh_adv_summary)
        _refresh_adv_summary()
        _toggle_adv()

        connection.body.addLayout(adv_header_row)
        connection.body.addWidget(self._advanced_body)

        # ===== Switch & supplier routing (full-width below) =====
        routing = FormSection("Switch & supplier routing", self)
        routing_form = QFormLayout()
        routing_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        routing_form.addRow("Switch type", self.switch_type)
        routing_form.addRow("Dial prefix", self.dial_prefix)
        routing_form.addRow("Routing format", self.routing_format)
        routing.body.addLayout(routing_form)

        # Hide routing_format when switch_type is "other" since suppliers
        # don't apply there. Re-show when user picks teles/genband.
        def _toggle_routing(*_a):
            kind = self.switch_type.currentText()
            show = kind in ("teles", "genband")
            self.routing_format.setEnabled(show)
            if not show:
                self.routing_format.setPlaceholderText("(not used for 'other' accounts)")
            elif kind == "teles":
                self.routing_format.setPlaceholderText("e.g. U{id} or N{id}")
            else:
                self.routing_format.setPlaceholderText("e.g. 000{id}")
        self.switch_type.currentTextChanged.connect(_toggle_routing)
        _toggle_routing()

        self.error = QLabel("", self)
        self.error.setObjectName("DialogError")
        self.error.setWordWrap(True)

        # Test row — its label is reused for live + final status.
        self.test_btn = QPushButton("Test registration")
        self.test_btn.clicked.connect(self._on_test)
        self.test_status = QLabel("")
        self.test_status.setObjectName("AccountTestStatus")
        self.test_status.setWordWrap(True)
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_status, 1, Qt.AlignVCenter)

        self.footer = FooterActionBar("Save" if self._editing else "Add account", "Cancel", self)
        self.footer.primary_button.clicked.connect(self.accept)
        self.footer.secondary_button.clicked.connect(self.reject)

        # ===== Layout: 2-col grid for Identity + Connection,
        #               full-width Switch routing, sticky footer  =====
        from PySide6.QtWidgets import QGridLayout
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(14)
        # Pin both cards to the TOP of the grid row. Without AlignTop
        # Qt vertically centres the shorter card -- so when the user
        # expands ADVANCED on the Connection side, the Identity card
        # drifts down to keep its centre aligned with the now-taller
        # Connection card. AlignTop keeps Identity anchored.
        grid.addWidget(identity, 0, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(connection, 0, 1, Qt.AlignmentFlag.AlignTop)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        root.addLayout(grid)
        root.addWidget(routing)
        root.addWidget(self.error)
        # Test registration row collapses into the footer rather than
        # taking its own line. Footer order: ghost-secondary (test) on
        # left, Cancel + primary on right.
        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.addWidget(self.test_btn)
        footer_row.addWidget(self.test_status, 1, Qt.AlignVCenter)
        footer_row.addWidget(self.footer.secondary_button)
        footer_row.addWidget(self.footer.primary_button)
        # Re-style Test reg as a ghost button so it doesn't compete with
        # the orange primary visually.
        self.test_btn.setObjectName("AccountTestGhostBtn")
        root.addLayout(footer_row)
        # Hide the FooterActionBar -- we re-mounted its buttons above.
        self.footer.setVisible(False)

        self._account_id = account.id

        # Per-test bookkeeping
        self._test_id: str | None = None
        self._test_timer: QTimer | None = None
        self._test_subscribed: bool = False

    def result_account(self) -> AccountConfig:
        port_txt = self.port.text().strip()
        try:
            port_val = int(port_txt) if port_txt else 0
        except ValueError:
            port_val = 0
        return AccountConfig(
            id=self._account_id,
            label=self.label.text().strip(),
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
            port=port_val,
            switch_type=self.switch_type.currentText(),
            dial_prefix=self.dial_prefix.text().strip(),
            routing_format=self.routing_format.text().strip(),
        )

    def accept(self) -> None:
        missing = []
        if not self.username.text().strip():
            missing.append("Username")
        if not self.domain.text().strip():
            missing.append("Domain")
        if missing:
            self.error.setText(", ".join(missing) + " required.")
            first = self.username if "Username" in missing else self.domain
            first.setFocus()
            return
        # Domain field can carry SIP-injection if it contains \r, \n,
        # or other control chars -- the URI is interpolated into a
        # SIP message later. Reject anything that isn't host-allowed.
        domain = self.domain.text().strip()
        if not _DOMAIN_RX.match(domain):
            self.error.setText(
                "Domain must be a host name (letters, digits, dots, dashes only)."
            )
            self.domain.setFocus()
            return
        # Optional port: if filled, must be 1..65535.
        port_txt = self.port.text().strip()
        if port_txt:
            try:
                port_val = int(port_txt)
                if not (1 <= port_val <= 65535):
                    raise ValueError
            except ValueError:
                self.error.setText("Port must be a number between 1 and 65535.")
                self.port.setFocus()
                return
        # Tear down any in-flight test-registration before accepting --
        # the test account would otherwise leak past dialog close.
        self._cleanup_test()
        super().accept()

    def reject(self) -> None:
        # Same cleanup on Cancel: without this, a test-registration
        # that was issued and then Cancel'd leaves a __test__* PJSIP
        # account live AND keeps the registration_changed signal
        # connected to a slot on a deleted QDialog -> SIGSEGV.
        self._cleanup_test()
        super().reject()

    def closeEvent(self, event):  # noqa: ANN001
        self._cleanup_test()
        super().closeEvent(event)

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

        sip_events().registration_changed.connect(self._on_reg_event)
        self._test_subscribed = True
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

        if self._test_timer is not None:
            self._test_timer.stop()
            self._test_timer = None
        # Only attempt a disconnect when we actually wired up the slot.
        # _cleanup_test is now also called from reject() / closeEvent()
        # before any test was started; the previous unconditional
        # disconnect would raise RuntimeError (caught silently) on
        # every dialog cancel, masking real wiring bugs.
        if self._test_subscribed:
            try:
                from noc_beam.sip.events import sip_events
                sip_events().registration_changed.disconnect(self._on_reg_event)
            except Exception:
                pass
            self._test_subscribed = False
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
