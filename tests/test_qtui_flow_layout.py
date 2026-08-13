import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QWidget

from jailbee.qtui.flow_layout import FlowLayout


def _fixed(qtbot, w=100, h=40):
    widget = QWidget()
    widget.setFixedSize(w, h)
    qtbot.addWidget(widget)
    return widget


def test_wraps_to_more_rows_when_narrow(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    layout = FlowLayout(host, margin=0, spacing=6)
    for _ in range(3):
        layout.addWidget(_fixed(qtbot))
    assert layout.count() == 3
    # 3 cards of width 100: narrow width fits 1 per row (taller),
    # wide width fits all on one row (shorter).
    narrow = layout.heightForWidth(100)
    wide = layout.heightForWidth(400)
    assert narrow > wide


def test_take_at_empties_the_layout(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    layout = FlowLayout(host)
    layout.addWidget(_fixed(qtbot))
    assert layout.count() == 1
    layout.takeAt(0)
    assert layout.count() == 0
    assert layout.itemAt(0) is None
