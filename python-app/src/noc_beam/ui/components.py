from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.ui.design_tokens import ICON_BUTTON_SIZE


def sip_level(code: int | None) -> str:
    if code is None:
        return "muted"
    if 100 <= code < 200:
        return "progress"
    if 200 <= code < 300:
        return "ok"
    if 300 <= code < 400:
        return "warn"
    if code >= 400:
        return "danger"
    return "muted"


# Map raw SIP final-response codes to a single-word operator label.
# Used in History rows, Recent Calls chips, and the SipCodeBadge --
# the operator preference is "just type busy or reject" instead of
# "486" / "603" etc. The number stays in the tooltip for power
# users who want the exact code.
_SIP_CODE_LABEL = {
    # 2xx success
    200: "Answered",
    202: "Accepted",
    # 4xx client errors -- common SIP rejection paths
    400: "Bad Request",
    401: "Auth",
    402: "Payment Req",
    403: "Forbidden",
    404: "Not Found",
    405: "Not Allowed",
    406: "Not Acceptable",
    407: "Auth",
    408: "Timeout",
    410: "Gone",
    413: "Too Large",
    415: "Bad Media",
    416: "Bad URI",
    420: "Bad Extension",
    421: "Extension Req",
    423: "Too Brief",
    480: "Unavailable",
    481: "No Dialog",
    482: "Loop Detected",
    483: "Too Many Hops",
    484: "Bad Address",
    485: "Ambiguous",
    486: "Busy",
    487: "Cancelled",
    488: "Not Acceptable",
    491: "Pending",
    493: "Undecipherable",
    # 5xx server errors
    500: "Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Unavailable",
    504: "Gateway Timeout",
    505: "Bad Version",
    513: "Too Large",
    # 6xx global failures
    600: "Busy",
    603: "Declined",
    604: "No User",
    606: "Not Acceptable",
}


def sip_label(code: int | None) -> str:
    """Return a one- or two-word human label for a SIP response code.

    Used in History / Recents / call badges so the operator sees
    'Busy' / 'Cancelled' / 'Declined' instead of '486' / '487' / '603'.
    Unknown codes fall back to the raw number so debug isn't lost.
    """
    if code is None or code == 0:
        return ""
    name = _SIP_CODE_LABEL.get(int(code))
    if name:
        return name
    # Unknown -- emit the number so debugging still works.
    return str(code)


class StatusPill(QLabel):
    def __init__(self, text: str, level: str = "muted", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("StatusPill")
        self.setProperty("level", level)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAccessibleName(f"Status: {text}")


class SipCodeBadge(QLabel):
    def __init__(
        self,
        code: int | None,
        reason: str = "",
        parent: QWidget | None = None,
    ) -> None:
        # Operator preference: pill text reads as the human label
        # ("Busy", "Cancelled", "Declined") instead of the raw code
        # ("486"). Full "<code> <reason>" stays as the tooltip so the
        # exact wire-level data is one hover away for debugging.
        text = sip_label(code)
        super().__init__(text, parent)
        self.setObjectName("SipCodeBadge")
        self.setProperty("level", sip_level(code))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if code is not None:
            tip = f"{code} {reason}".strip()
            self.setToolTip(tip)
            self.setAccessibleName(f"SIP code {tip}")


class FasBadge(QLabel):
    """Compact verdict badge for False Answer Supervision detection.

    Maps the FAS engine's verdict enum to colour levels that pick up the
    same QSS palette as StatusPill (ok/warn/danger/muted/progress). The
    widget is hidden when verdict is empty so non-FAS rows render flush.

    Use ``update_verdict(verdict, confidence, reasons)`` to refresh. Empty
    string verdict hides the badge.
    """

    # Map FAS verdict -> (display text, QSS level)
    _LEVELS = {
        "":              ("",            "muted"),
        "ANALYZING":     ("Analyzing",   "progress"),
        "INCONCLUSIVE":  ("Inconclusive", "muted"),
        "LIKELY_REAL":   ("Real",        "ok"),
        "HUMAN_LIKELY":  ("Human",       "ok"),
        "MACHINE_OR_VOICEMAIL": ("Machine", "warn"),
        "IVR_OR_ANNOUNCEMENT": ("IVR",    "warn"),
        "SUSPICIOUS":    ("Suspicious",  "warn"),
        "LIKELY_FAS":    ("Likely FAS",  "danger"),
        "PROBABLE_FAS":  ("Probable FAS", "danger"),
        "CONFIRMED_FAS": ("FAS",         "danger"),
    }

    def __init__(self, verdict: str = "", parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("FasBadge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.update_verdict(verdict)

    def update_verdict(self, verdict: str, confidence: float = 0.0, reasons: str = "") -> None:
        text, level = self._LEVELS.get(verdict, ("", "muted"))
        if not text:
            self.setVisible(False)
            self.setText("")
            return
        self.setVisible(True)
        self.setText(text)
        self.setProperty("level", level)
        # Re-evaluate the QSS so the new level paints immediately.
        self.style().unpolish(self)
        self.style().polish(self)
        tip = f"FAS: {text}"
        if confidence > 0:
            tip += f" ({confidence:.0%} confidence)"
        if reasons:
            tip += f"\n{reasons}"
        self.setToolTip(tip)
        self.setAccessibleName(tip)


class MetricChip(QLabel):
    def __init__(self, text: str, level: str = "muted", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("MetricChip")
        self.setProperty("level", level)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAccessibleName(text)


class IconActionButton(QToolButton):
    def __init__(self, tooltip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("IconActionButton")
        self.setFixedSize(ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class SectionHeader(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text.upper(), parent)
        self.setObjectName("SectionHeader")


class FormSection(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FormSection")
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(SectionHeader(title, self))
        layout.addLayout(self.body)


class FooterActionBar(QFrame):
    def __init__(
        self,
        primary_text: str,
        secondary_text: str = "Cancel",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("FooterActionBar")
        self.secondary_button = QPushButton(secondary_text, self)
        self.secondary_button.setObjectName("SecondaryAction")
        self.primary_button = QPushButton(primary_text, self)
        self.primary_button.setObjectName("PrimaryAction")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.addStretch(1)
        layout.addWidget(self.secondary_button)
        layout.addWidget(self.primary_button)


class DenseListRow(QFrame):
    def __init__(
        self,
        title: str,
        subtitle: str = "",
        marker: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DenseListRow")
        self.marker_label = QLabel(marker, self)
        self.marker_label.setObjectName("DenseRowMarker")
        self.marker_label.setFixedWidth(20)
        self.marker_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("DenseRowTitle")
        self.subtitle_label = QLabel(subtitle, self)
        self.subtitle_label.setObjectName("DenseRowSubtitle")

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.subtitle_label)

        self.action_holder = QFrame(self)
        self.action_holder.setObjectName("DenseRowActions")
        self.action_layout = QHBoxLayout(self.action_holder)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(4)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)
        layout.addWidget(self.marker_label)
        layout.addLayout(text_col, 1)
        layout.addWidget(self.action_holder)
