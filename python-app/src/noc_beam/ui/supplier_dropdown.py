from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class _SelectAllFilter(QObject):
    def eventFilter(self, obj, event):  # noqa: ANN001, N802
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            try:
                QTimer.singleShot(0, obj.selectAll)
            except Exception:
                pass
        return False


class SupplierDropdown(QWidget):
    """Small search dropdown for supplier selection.

    This replaces editable QComboBox for supplier picking. Editable combos
    mutate currentIndex while filtering, which can switch suppliers while the
    operator is still typing. This widget keeps those concerns separate:
    typing filters only; Enter or explicit list activation commits.
    """

    currentIndexChanged = Signal(int)
    activated = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[tuple[str, str]] = []
        self._filtered_indexes: list[int] = []
        self._current_index = -1
        self._max_visible_items = 18

        self._line = QLineEdit(self)
        self._line.setObjectName("SupplierDropdownLine")
        self._button = QToolButton(self)
        self._button.setObjectName("SupplierDropdownButton")
        self._button.setText("v")
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.setAutoRaise(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._line, 1)
        layout.addWidget(self._button)

        # Parent the popup to the dropdown widget (not None) so Windows
        # doesn't render it as a separate top-level NOC_Beam window with
        # its own title bar / taskbar entry. Qt.WindowType.Popup still
        # gives popup behaviour (focus grab, click-outside-to-dismiss,
        # not part of layout) — but with a real parent the OS treats it
        # as a child window of the dropdown's top-level, so no second
        # "NOC_Beam" sliver appears next to the Test Runner.
        self._popup = QFrame(self, Qt.WindowType.Popup)
        self._popup.setObjectName("SupplierDropdownPopup")
        self._popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        popup_layout = QVBoxLayout(self._popup)
        popup_layout.setContentsMargins(0, 0, 0, 0)
        popup_layout.setSpacing(0)
        self._list = QListWidget(self._popup)
        self._list.setObjectName("SupplierDropdownList")
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        popup_layout.addWidget(self._list)

        self._focus_filter = _SelectAllFilter(self)
        self._line.installEventFilter(self._focus_filter)
        self._popup.installEventFilter(self)
        self._list.installEventFilter(self)
        self._button.clicked.connect(self._toggle_popup)
        self._list.itemActivated.connect(self._on_item_activated)
        self._list.itemClicked.connect(self._on_item_activated)

    def eventFilter(self, obj, event):  # noqa: ANN001, N802
        if obj in (self._popup, self._list) and event.type() == QEvent.Type.KeyPress:
            return self._handle_popup_key(event)
        return super().eventFilter(obj, event)

    def lineEdit(self) -> QLineEdit:
        return self._line

    def view(self) -> QListWidget:
        return self._list

    def setMaxVisibleItems(self, count: int) -> None:
        self._max_visible_items = max(1, int(count))

    def setMinimumContentsLength(self, chars: int) -> None:
        metrics = self.fontMetrics()
        self.setMinimumWidth(max(self.minimumWidth(), metrics.horizontalAdvance("M") * int(chars)))

    def setAccessibleName(self, name: str) -> None:
        super().setAccessibleName(name)
        self._line.setAccessibleName(name)

    def setAccessibleDescription(self, description: str) -> None:
        super().setAccessibleDescription(description)
        self._line.setAccessibleDescription(description)

    def set_items(self, items: list[tuple[str, str]], current_id: str = "") -> None:
        self._items = [(str(display), str(sid)) for display, sid in items]
        self._filtered_indexes = list(range(len(self._items)))
        self._rebuild_list()
        idx = self.findData(current_id) if current_id else -1
        if idx < 0 and self._items:
            idx = 0
        self.setCurrentIndex(idx, emit=False)

    def count(self) -> int:
        return len(self._items)

    def currentIndex(self) -> int:
        return self._current_index

    def currentText(self) -> str:
        return self.itemText(self._current_index)

    def findData(self, data: str) -> int:
        needle = str(data or "")
        for idx, (_display, sid) in enumerate(self._items):
            if sid == needle:
                return idx
        return -1

    def itemData(self, index: int) -> str:
        if 0 <= index < len(self._items):
            return self._items[index][1]
        return ""

    def itemText(self, index: int) -> str:
        if 0 <= index < len(self._items):
            return self._items[index][0]
        return ""

    def setCurrentIndex(self, index: int, *, emit: bool = True) -> None:
        if index < 0 or index >= len(self._items):
            self._current_index = -1
            self._line.clear()
            return
        changed = index != self._current_index
        self._current_index = index
        display, _sid = self._items[index]
        old_block = self._line.blockSignals(True)
        try:
            self._line.setText(display)
        finally:
            self._line.blockSignals(old_block)
        self._sync_list_selection()
        if emit and changed and not self.signalsBlocked():
            self.currentIndexChanged.emit(index)

    def set_filter(self, text: str) -> int:
        needle = (text or "").strip().lower()
        if not needle:
            self._filtered_indexes = list(range(len(self._items)))
        else:
            code = needle[1:] if needle.startswith("c") else needle
            self._filtered_indexes = [
                idx
                for idx, (display, sid) in enumerate(self._items)
                if needle in display.lower() or code in sid.lower()
            ]
        self._rebuild_list()
        return len(self._filtered_indexes)

    def showPopup(self) -> None:
        if not self._filtered_indexes:
            self.hidePopup()
            return
        self._popup.setMinimumWidth(max(self.width(), 320))
        row_h = max(self._list.sizeHintForRow(0), self.fontMetrics().height() + 10)
        visible_rows = min(len(self._filtered_indexes), self._max_visible_items)
        self._list.setFixedHeight(max(row_h * visible_rows + 2, row_h + 2))
        self._popup.move(self.mapToGlobal(QPoint(0, self.height())))
        self._popup.show()
        self._line.setFocus(Qt.FocusReason.OtherFocusReason)
        QTimer.singleShot(0, lambda: self._line.setFocus(Qt.FocusReason.OtherFocusReason))

    def hidePopup(self) -> None:
        self._popup.hide()

    def _toggle_popup(self) -> None:
        if self._popup.isVisible():
            self.hidePopup()
        else:
            self.set_filter(self._line.text())
            self.showPopup()

    def _rebuild_list(self) -> None:
        self._list.clear()
        for source_idx in self._filtered_indexes:
            display, sid = self._items[source_idx]
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, source_idx)
            item.setToolTip(f"{display} - C{sid}")
            self._list.addItem(item)
        self._sync_list_selection()

    def _sync_list_selection(self) -> None:
        try:
            row = self._filtered_indexes.index(self._current_index)
        except ValueError:
            row = -1
        if row >= 0:
            self._list.setCurrentRow(row)

    def _handle_popup_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.hidePopup()
            self._line.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self._list.currentItem()
            if item is not None:
                self._on_item_activated(item)
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            row = self._list.currentRow()
            if key == Qt.Key.Key_Up:
                row = max(0, row - 1)
            elif key == Qt.Key.Key_Down:
                row = min(max(0, self._list.count() - 1), row + 1)
            elif key == Qt.Key.Key_PageUp:
                row = max(0, row - self._max_visible_items)
            elif key == Qt.Key.Key_PageDown:
                row = min(max(0, self._list.count() - 1), row + self._max_visible_items)
            self._list.setCurrentRow(row)
            return True

        forwarded = QKeyEvent(
            event.type(),
            event.key(),
            event.modifiers(),
            event.text(),
            event.isAutoRepeat(),
            event.count(),
        )
        self._line.setFocus(Qt.FocusReason.OtherFocusReason)
        QCoreApplication.sendEvent(self._line, forwarded)
        return True

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        source_idx = int(item.data(Qt.ItemDataRole.UserRole))
        self.hidePopup()
        self.setCurrentIndex(source_idx)
        if not self.signalsBlocked():
            self.activated.emit(source_idx)
