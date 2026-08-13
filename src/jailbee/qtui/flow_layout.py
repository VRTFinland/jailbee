"""A wrapping layout: children flow left-to-right and wrap to new rows.

Qt has no built-in wrapping layout; this is the canonical ``FlowLayout``
example (a ``QLayout`` subclass) adapted for PySide6. It gives the card view
its responsive grid — one column when narrow, several when wide.
"""

from __future__ import annotations

from typing import cast

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, margin: int = 0, spacing: int = 6) -> None:
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(QMargins(margin, margin, margin, margin))
        self.setSpacing(spacing)
        self._items: list[QLayoutItem] = []

    def __del__(self) -> None:
        while self.count():
            self.takeAt(0)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt override
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt override
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt override
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt override
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt override
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        # PySide6 stubs type getContentsMargins() as -> object (it's a
        # by-reference C++ signature); at runtime it returns a 4-tuple.
        left, top, right, bottom = cast("tuple[int, int, int, int]", self.getContentsMargins())
        effective = rect.adjusted(left, top, -right, -bottom)
        x = effective.x()
        y = effective.y()
        line_height = 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + bottom
