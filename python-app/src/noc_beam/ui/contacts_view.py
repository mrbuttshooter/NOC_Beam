"""Contacts tab with persistent Bria-style grouped contacts."""
from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.contacts import (
    Contact,
    add_contact,
    delete_contact,
    load_contacts,
    save_contacts,
    update_contact,
)
from noc_beam.ui.rail_icons import rail_icon


def _group_avatar(letter: str, color_hex: str = "#E85D04", px: int = 28) -> QPixmap:
    """Square rounded avatar with the group's first letter, Bria-style."""
    pix = QPixmap(QSize(px, px))
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(color_hex))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 0, px, px, 4, 4)
    painter.setPen(QColor("#FFFFFF"))
    f = painter.font()
    f.setPointSize(11)
    f.setBold(True)
    painter.setFont(f)
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, letter.upper())
    painter.end()
    return pix


def _open_modal(dlg: QDialog) -> bool:
    runner = getattr(dlg, "exec")
    return int(runner()) == int(QDialog.DialogCode.Accepted)


class ContactDialog(QDialog):
    def __init__(
        self,
        contact: Contact | None = None,
        group: str = "Work",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Contact")
        self.setMinimumWidth(320)

        group_value = contact.group if contact is not None else group
        self.name_edit = QLineEdit(contact.name if contact is not None else "", self)
        self.number_edit = QLineEdit(contact.number if contact is not None else "", self)
        self.group_edit = QLineEdit(group_value, self)
        self.favorite_check = QCheckBox("Favorite", self)
        self.favorite_check.setChecked(contact.favorite if contact is not None else False)
        self.error = QLabel("", self)
        self.error.setObjectName("DialogError")
        self.error.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Number", self.number_edit)
        form.addRow("Group", self.group_edit)
        form.addRow(self.favorite_check)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(self.error)
        root.addWidget(self.buttons)

    def values(self) -> dict[str, str | bool]:
        return {
            "name": self.name_edit.text(),
            "number": self.number_edit.text(),
            "group": self.group_edit.text(),
            "favorite": self.favorite_check.isChecked(),
        }

    def accept(self) -> None:
        name = self.name_edit.text().strip()
        number = self.number_edit.text().strip()
        if not name or not number:
            self.error.setText("Name and number are required.")
            return
        super().accept()


class GroupRow(QFrame):
    """One Bria-style group row: avatar + name + count + chevron."""

    clicked = Signal(str)

    def __init__(
        self,
        letter: str,
        name: str,
        count: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ContactGroupRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.name = name

        avatar = QLabel(self)
        avatar.setObjectName("GroupAvatar")
        avatar.setPixmap(_group_avatar(letter))
        avatar.setFixedSize(28, 28)

        name_lbl = QLabel(name, self)
        name_lbl.setObjectName("GroupName")

        count_lbl = QLabel(str(count), self)
        count_lbl.setObjectName("GroupCount")

        chev = QToolButton(self)
        chev.setObjectName("GroupChevron")
        chev.setIcon(rail_icon("chevron-down", color="#94A0AD", px=12))
        chev.setIconSize(QSize(12, 12))
        chev.setAutoRaise(True)
        chev.clicked.connect(lambda: self.clicked.emit(self.name))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)
        layout.addWidget(avatar)
        layout.addWidget(name_lbl, 1)
        layout.addWidget(count_lbl)
        layout.addWidget(chev)

    def mousePressEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.name)
        super().mousePressEvent(event)


class ContactRow(QFrame):
    call_requested = Signal(str)
    edit_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, contact: Contact, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DenseListRow")
        self.contact = contact
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        marker = QLabel("*" if contact.favorite else "", self)
        marker.setObjectName("DenseRowMarker")
        marker.setFixedWidth(20)
        marker.setAlignment(Qt.AlignmentFlag.AlignCenter)

        name_lbl = QLabel(contact.name, self)
        name_lbl.setObjectName("DenseRowTitle")
        number_lbl = QLabel(contact.number, self)
        number_lbl.setObjectName("DenseRowSubtitle")

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        text_col.addWidget(name_lbl)
        text_col.addWidget(number_lbl)

        call_btn = QToolButton(self)
        call_btn.setObjectName("IconActionButton")
        call_btn.setIcon(rail_icon("calls", color="#2DA44E", px=16))
        call_btn.setIconSize(QSize(16, 16))
        call_btn.setToolTip("Call")
        call_btn.clicked.connect(lambda: self.call_requested.emit(self.contact.number))

        edit_btn = QToolButton(self)
        edit_btn.setObjectName("IconActionButton")
        edit_btn.setIcon(rail_icon("settings", color="#57606A", px=16))
        edit_btn.setIconSize(QSize(16, 16))
        edit_btn.setToolTip("Edit")
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.contact.id))

        delete_btn = QToolButton(self)
        delete_btn.setObjectName("IconActionButton")
        delete_btn.setIcon(rail_icon("close", color="#CF222E", px=16))
        delete_btn.setIconSize(QSize(16, 16))
        delete_btn.setToolTip("Delete")
        delete_btn.clicked.connect(lambda: self.delete_requested.emit(self.contact.id))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 7, 8, 7)
        layout.setSpacing(8)
        layout.addWidget(marker)
        layout.addLayout(text_col, 1)
        layout.addWidget(call_btn)
        layout.addWidget(edit_btn)
        layout.addWidget(delete_btn)

    def mouseDoubleClickEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.call_requested.emit(self.contact.number)
        super().mouseDoubleClickEvent(event)


class ContactsView(QWidget):
    add_group_requested = Signal()
    add_contact_requested = Signal()
    call_requested = Signal(str)
    contact_saved = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ContactsView")
        self._contacts: list[Contact] = []
        self._group_widgets: dict[str, list[QWidget]] = {}
        self._expanded_groups: set[str] = set()

        self.search = QLineEdit(self)
        self.search.setObjectName("ContactsSearch")
        self.search.setPlaceholderText("Search Contacts")
        self.search.textChanged.connect(self._render)

        self.add_group_btn = QToolButton(self)
        self.add_group_btn.setObjectName("ContactsActionBtn")
        self.add_group_btn.setIcon(rail_icon("users", color="#57606A", px=18))
        self.add_group_btn.setIconSize(QSize(18, 18))
        self.add_group_btn.setToolTip("New group")
        self.add_group_btn.clicked.connect(self._on_add_group)

        self.add_contact_btn = QToolButton(self)
        self.add_contact_btn.setObjectName("ContactsActionBtn")
        self.add_contact_btn.setIcon(rail_icon("user-plus", color="#57606A", px=18))
        self.add_contact_btn.setIconSize(QSize(18, 18))
        self.add_contact_btn.setToolTip("Add contact")
        self.add_contact_btn.clicked.connect(lambda: self._on_add_contact())

        bar = QHBoxLayout()
        bar.setContentsMargins(12, 12, 12, 8)
        bar.setSpacing(6)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.add_group_btn)
        bar.addWidget(self.add_contact_btn)

        self._rows_holder = QFrame(self)
        self._rows_holder.setObjectName("ContactsBody")
        self._rows_layout = QVBoxLayout(self._rows_holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setObjectName("ContactsScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._rows_holder)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(bar)
        outer.addWidget(scroll, 1)

        self.reload()

    def reload(self) -> None:
        self._contacts = load_contacts()
        self._expanded_groups.update(contact.group for contact in self._contacts)
        self._render()

    def _render(self) -> None:
        self._clear_rows()
        self._group_widgets = {}
        needle = self.search.text().strip().lower()
        contacts = [contact for contact in self._contacts if self._matches(contact, needle)]

        if not contacts:
            text = "No contacts yet." if not needle else "No contacts match your search."
            empty = QLabel(text, self._rows_holder)
            empty.setObjectName("ViewEmpty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            self._rows_layout.addWidget(empty, 1)
            return

        grouped: dict[str, list[Contact]] = defaultdict(list)
        for contact in contacts:
            grouped[contact.group].append(contact)

        for group in sorted(grouped, key=str.lower):
            group_contacts = sorted(grouped[group], key=lambda item: item.name.lower())
            row = GroupRow(group[:1] or "W", group, len(group_contacts), self._rows_holder)
            row.clicked.connect(self._toggle_group)
            self._rows_layout.addWidget(row)
            child_widgets: list[QWidget] = []
            for contact in group_contacts:
                contact_row = ContactRow(contact, self._rows_holder)
                contact_row.call_requested.connect(self.call_requested.emit)
                contact_row.edit_requested.connect(self._on_edit_contact)
                contact_row.delete_requested.connect(self._on_delete_contact)
                contact_row.setVisible(bool(needle) or group in self._expanded_groups)
                self._rows_layout.addWidget(contact_row)
                child_widgets.append(contact_row)
            self._group_widgets[group] = child_widgets
        self._rows_layout.addStretch(1)

    def _clear_rows(self) -> None:
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _matches(self, contact: Contact, needle: str) -> bool:
        if not needle:
            return True
        haystack = f"{contact.name} {contact.number} {contact.group}".lower()
        return needle in haystack

    def _toggle_group(self, group: str) -> None:
        if group in self._expanded_groups:
            self._expanded_groups.remove(group)
        else:
            self._expanded_groups.add(group)
        force_visible = bool(self.search.text().strip())
        for widget in self._group_widgets.get(group, []):
            widget.setVisible(force_visible or group in self._expanded_groups)

    def _on_add_group(self) -> None:
        self.add_group_requested.emit()
        group, ok = QInputDialog.getText(self, "New group", "Group name")
        group = group.strip()
        if ok and group:
            self._on_add_contact(group)

    def _on_add_contact(self, group: str = "Work") -> None:
        self.add_contact_requested.emit()
        dlg = ContactDialog(group=group, parent=self)
        while _open_modal(dlg):
            if self._save_new_contact(dlg):
                return

    def _on_edit_contact(self, contact_id: str) -> None:
        contact = next((item for item in self._contacts if item.id == contact_id), None)
        if contact is None:
            return
        dlg = ContactDialog(contact=contact, parent=self)
        while _open_modal(dlg):
            if self._save_existing_contact(dlg, contact_id):
                return

    def _on_delete_contact(self, contact_id: str) -> None:
        contacts = load_contacts()
        if delete_contact(contacts, contact_id):
            try:
                save_contacts(contacts)
            except OSError as exc:
                self._warn_save_failed("delete contact", exc)
                return
            self._after_contacts_saved()

    def _save_new_contact(self, dlg: ContactDialog) -> bool:
        try:
            contacts = load_contacts()
            add_contact(contacts, **dlg.values())
            save_contacts(contacts)
        except ValueError as exc:
            dlg.error.setText(str(exc))
            return False
        except OSError as exc:
            self._warn_save_failed("save contact", exc)
            return False
        self._after_contacts_saved()
        return True

    def _save_existing_contact(self, dlg: ContactDialog, contact_id: str) -> bool:
        try:
            contacts = load_contacts()
            update_contact(contacts, contact_id, **dlg.values())
            save_contacts(contacts)
        except ValueError as exc:
            dlg.error.setText(str(exc))
            return False
        except (KeyError, OSError) as exc:
            self._warn_save_failed("save contact", exc)
            return False
        self._after_contacts_saved()
        return True

    def _after_contacts_saved(self) -> None:
        self.contact_saved.emit()
        self.reload()

    def _warn_save_failed(self, action: str, exc: Exception) -> None:
        QMessageBox.warning(
            self,
            "Contacts",
            f"Could not {action}.\n\n{exc}",
        )
