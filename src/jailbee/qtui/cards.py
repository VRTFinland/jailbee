"""Card-grid view for the Qt dashboard — a width-adaptive alternative to the
wide table. Renders the same snapshot as the tree via ``card_content`` and
emits the same ``actionRequested(verb, name)`` contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from jailbee.dashboard import actions_for_container, view_only_note, visible_fields
from jailbee.qtui.flow_layout import FlowLayout
from jailbee.qtui.model import (
    STATE_COLORS,
    CardContent,
    card_content,
    card_field,
    compact_meta,
    git_segments,
    group_header,
    job_badge,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QContextMenuEvent

    from jailbee.dashboard import RepoGroup

# Minimum card width — the FlowLayout fits as many columns as this allows.
_CARD_MIN_WIDTH = 260

# Selection highlight applied to a card's *own* stylesheet (scoped to
# ``#card`` so it doesn't cascade to the child labels). This is deliberately
# per-widget rather than an ancestor QSS rule toggled via ``setProperty`` +
# ``style().unpolish/polish``: that idiom re-resolved the style but did not
# schedule an on-screen repaint on Wayland, so the highlight only appeared on
# the next ``set_groups`` rebuild. ``setStyleSheet`` reliably repaints.
_SELECTED_QSS = "#card { border: 2px solid #1565c0; }"

_GIT_SEGMENT_COLORS = {"ahead": "#1565c0", "diff": "#2e7d32", "conflict": "#c62828"}
_DIM = "#6b6b6b"

# Job badge colours by `model.job_badge` kind.
_JOB_BADGE_COLORS = {"failed": "#c62828", "running": "#ef6c00"}


def _clear_layout(layout: QLayout) -> None:
    """Recursively remove and delete every item in a Qt layout."""
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            _clear_layout(child)
            child.deleteLater()


# Verb dispatched when the PR pill is clicked — mirrors the "Open PR" context
# action (dashboard.menu_actions), so the pill runs `jailbee pr --open <name>`.
_OPEN_PR_VERB = "pr --open"


class _ClickableLabel(QLabel):
    """A QLabel that emits ``clicked`` on a left mouse press and swallows the
    event, so clicking it doesn't also trigger the parent card's selection."""

    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class _Card(QFrame):
    """One container, shown as a bordered card. Emits clicks and context
    requests keyed by container name."""

    clicked = Signal(str)
    contextRequested = Signal(str, object)  # noqa: N815 - (name, global QPoint)
    prClicked = Signal(str)  # noqa: N815 - the PR pill was clicked; payload: container name

    def __init__(
        self, name: str, content: CardContent, *, style: str = "compact", selected: bool
    ) -> None:
        super().__init__()
        self._name = name
        self._content = content
        self._style = style
        self.setObjectName("card")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.set_selected(selected)
        self.setMinimumWidth(_CARD_MIN_WIDTH)
        self._outer = QVBoxLayout(self)
        self._build()

    def update_content(self, content: CardContent) -> None:
        """Rebuild this card's inner layout when its snapshot changed.

        A no-op when nothing changed — the common case on the periodic
        refresh — so an unchanged card does zero work."""
        if content == self._content:
            return
        self._content = content
        self._build()

    def _build(self) -> None:
        _clear_layout(self._outer)
        self._add_header()
        if self._style == "grid":
            self._build_grid()
        else:
            self._build_compact()

    def _add_header(self) -> None:
        row = QHBoxLayout()
        title = QLabel(self._content.name)
        title.setStyleSheet("font-weight: 600;")
        row.addWidget(title)
        row.addStretch(1)
        # The job badge lives in the header (not in the body) so it is visible
        # in both card styles — the compact body renders no job field at all.
        badge = job_badge(self._content)
        if badge is not None:
            text, kind = badge
            pill = QLabel(text)
            pill.setStyleSheet(
                f"background:{_JOB_BADGE_COLORS[kind]}; color:white; "
                "border-radius:8px; padding:1px 8px;"
            )
            if self._content.job_error:
                pill.setToolTip(f"{self._content.job_error}\n\nRight-click → Clear failed job")
            row.addWidget(pill)
        state = QLabel(self._content.state)
        color = STATE_COLORS.get(self._content.state, "#9e9e9e")
        state.setStyleSheet(f"background:{color}; color:white; border-radius:8px; padding:1px 8px;")
        row.addWidget(state)
        self._outer.addLayout(row)

    def _build_compact(self) -> None:
        meta = compact_meta(self._content)
        if meta:
            lbl = QLabel("  ·  ".join(meta))
            lbl.setStyleSheet(f"color:{_DIM};")
            self._outer.addWidget(lbl)
        ip = card_field(self._content, "ip")
        mem = card_field(self._content, "mem")
        if ip or mem:
            res = QHBoxLayout()
            res.addWidget(QLabel(ip or "—"))
            if mem:
                m = QLabel(f"▪ {mem}")
                m.setStyleSheet(f"color:{_DIM};")
                res.addWidget(m)
            res.addStretch(1)
            self._outer.addLayout(res)
        segs = git_segments(self._content)
        if segs:
            git = QHBoxLayout()
            for text, kind in segs:
                s = QLabel(text)
                s.setStyleSheet(f"color:{_GIT_SEGMENT_COLORS[kind]}; font-weight:500;")
                git.addWidget(s)
            git.addStretch(1)
            self._outer.addLayout(git)
        pr = card_field(self._content, "pr")
        if pr:
            row = QHBoxLayout()
            row.addStretch(1)
            pill = _ClickableLabel(f"PR {pr}")
            pill.setStyleSheet(
                "background:#e3f2fd; color:#1565c0; border-radius:8px; padding:1px 8px;"
            )
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            pill.setToolTip("Open this PR")
            pill.clicked.connect(lambda: self.prClicked.emit(self._name))
            row.addWidget(pill)
            self._outer.addLayout(row)

    def _build_grid(self) -> None:
        from jailbee.qtui.model import grid_rows

        for header, value in grid_rows(self._content):
            row = QHBoxLayout()
            key = QLabel(header)
            key.setStyleSheet(f"color:{_DIM};")
            key.setFixedWidth(72)
            row.addWidget(key)
            row.addWidget(QLabel(value))
            row.addStretch(1)
            self._outer.addLayout(row)

    def set_selected(self, selected: bool) -> None:
        """Toggle the selection highlight by setting the card's own stylesheet.

        Setting the widget's stylesheet re-resolves its style *and* schedules a
        repaint — unlike ``setProperty`` + ``style().unpolish/polish``, which
        left the on-screen card unpainted on Wayland."""
        self.setStyleSheet(_SELECTED_QSS if selected else "")

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._name)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802 - Qt override
        self.contextRequested.emit(self._name, event.globalPos())


class _GroupHeader(QLabel):
    """Clickable repo section header that toggles its group's cards."""

    clicked = Signal(str)  # Qt signal; payload: repo prefix

    def __init__(self, prefix: str, label: str, count: int, *, collapsed: bool) -> None:
        super().__init__()
        self._prefix = prefix
        self._label = label
        self._count = count
        self.setStyleSheet("font-weight: bold; margin-top: 8px;")
        self.set_collapsed(collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        arrow = "▸" if collapsed else "▾"
        self.setText(f"{arrow}  {self._label}   {self._count} containers")

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._prefix)
        super().mousePressEvent(event)


class CardView(QScrollArea):
    """Scrollable, wrapping grid of container cards grouped by repo."""

    actionRequested = Signal(str, str)  # noqa: N815 - (verb, container_name)
    collapsedChanged = Signal()  # noqa: N815 - Qt signal; a group was expanded/collapsed

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self._groups: list[RepoGroup] = []
        self._selected: str | None = None
        # Live card widgets keyed by container name, reused across refreshes.
        self._cards: dict[str, _Card] = {}
        # Last rendered structure: (repo prefix, group label, container names
        # in order) per group. When a refresh matches it, only card *content*
        # is pushed — no widget churn, no reparenting, no flicker. The label
        # must be part of the key: it is not a pure function of prefix (a
        # repo can flip registered<->orphan while prefix + containers stay
        # the same), so dropping it would let the header text go stale.
        self._structure: list[tuple[str, str, tuple[str, ...]]] = []
        self._card_style: str = "compact"
        self._now: datetime | None = None
        self._columns: Sequence[str] | None = None
        # Repo prefixes currently collapsed, and the live header/grid-host
        # widgets per prefix (rebuilt on every reconcile).
        self._collapsed: set[str] = set()
        self._headers: dict[str, _GroupHeader] = {}
        self._grid_hosts: dict[str, QWidget] = {}
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.addStretch(1)  # trailing spacer keeps cards top-aligned
        self.setWidget(self._body)

    def selected_name(self) -> str | None:
        return self._selected

    def collapsed(self) -> set[str]:
        return set(self._collapsed)

    def set_collapsed(self, prefixes: set[str]) -> None:
        self._collapsed = set(prefixes)

    def _toggle_group(self, prefix: str) -> None:
        collapsed = prefix not in self._collapsed
        if collapsed:
            self._collapsed.add(prefix)
        else:
            self._collapsed.discard(prefix)
        host = self._grid_hosts.get(prefix)
        if host is not None:
            host.setVisible(not collapsed)
        header = self._headers.get(prefix)
        if header is not None:
            header.set_collapsed(collapsed)
        self.collapsedChanged.emit()

    def set_groups(
        self,
        groups: list[RepoGroup],
        *,
        now: datetime,
        columns: Sequence[str] | None = None,
    ) -> None:
        self._now = now
        self._columns = columns
        self._render(groups, now, columns)

    def card_style(self) -> str:
        return self._card_style

    def set_card_style(self, style: str) -> None:
        """Switch card style; a full rebuild in the new style (rare action)."""
        if style == self._card_style:
            return
        self._card_style = style
        for card in list(self._cards.values()):
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._structure = []
        if self._now is not None:
            self._render(self._groups, self._now, self._columns)

    def _render(
        self, groups: list[RepoGroup], now: datetime, columns: Sequence[str] | None = None
    ) -> None:
        self._groups = groups
        all_containers = [c for g in groups for c in g.containers]
        fields = visible_fields(now, all_containers, enabled=columns)
        names = {c.name for c in all_containers}
        if self._selected not in names:
            self._selected = None

        desired: list[tuple[str, str, list[tuple[str, CardContent]]]] = []
        for g in groups:
            label, _is_orphan = group_header(g)
            entries = [(c.name, card_content(c, fields)) for c in g.containers]
            desired.append((g.prefix, label, entries))
        structure = [
            (prefix, label, tuple(n for n, _ in entries)) for prefix, label, entries in desired
        ]

        if structure == self._structure:
            # Fast path: same groups/containers/order. Push content only.
            for _prefix, _label, entries in desired:
                for name, content in entries:
                    self._cards[name].update_content(content)
            return

        self._reconcile(desired, structure)

    def _reconcile(
        self,
        desired: list[tuple[str, str, list[tuple[str, CardContent]]]],
        structure: list[tuple[str, str, tuple[str, ...]]],
    ) -> None:
        """Rebuild the group scaffolding, reusing card widgets by name.

        Detaches existing cards before clearing so the scaffolding teardown
        doesn't take them with it; survivors are updated in place and re-added,
        vanished containers' cards are dropped. Wrapped in a paint freeze so
        the whole change lands as one repaint."""
        scroll = self.verticalScrollBar().value()
        self.setUpdatesEnabled(False)
        try:
            for existing in self._cards.values():
                existing.setParent(None)  # keep alive via self._cards; detach from layout
            self._clear()
            self._headers.clear()
            self._grid_hosts.clear()

            seen: set[str] = set()
            for prefix, label, entries in desired:
                collapsed = prefix in self._collapsed
                header = _GroupHeader(prefix, label, len(entries), collapsed=collapsed)
                header.clicked.connect(self._toggle_group)
                self._headers[prefix] = header
                self._insert(header)

                grid_host = QWidget()
                grid = FlowLayout(grid_host)
                for name, content in entries:
                    card = self._cards.get(name)
                    if card is None:
                        card = _Card(
                            name,
                            content,
                            style=self._card_style,
                            selected=(name == self._selected),
                        )
                        card.clicked.connect(self._on_clicked)
                        card.contextRequested.connect(self._on_context)
                        card.prClicked.connect(self._on_pr_clicked)
                        self._cards[name] = card
                    else:
                        card.update_content(content)
                        card.set_selected(name == self._selected)
                    grid.addWidget(card)
                    seen.add(name)
                self._grid_hosts[prefix] = grid_host
                grid_host.setVisible(not collapsed)
                self._insert(grid_host)

            for name in list(self._cards):
                if name not in seen:
                    self._cards.pop(name).deleteLater()

            self._structure = structure
        finally:
            self.setUpdatesEnabled(True)
        self.verticalScrollBar().setValue(scroll)

    def _insert(self, widget: QWidget) -> None:
        # Insert before the trailing stretch (always the last item).
        self._body_layout.insertWidget(self._body_layout.count() - 1, widget)

    def _clear(self) -> None:
        while self._body_layout.count() > 1:  # keep the trailing stretch
            item = self._body_layout.takeAt(0)
            if item is None:  # defensive; count() > 1 guarantees a real item
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _on_clicked(self, name: str) -> None:
        self._selected = name
        self._apply_selection(name)

    def _on_pr_clicked(self, name: str) -> None:
        """The PR pill was clicked — dispatch the same action as the
        right-click "Open PR" menu entry."""
        self.actionRequested.emit(_OPEN_PR_VERB, name)

    def _apply_selection(self, name: str) -> None:
        """Update each card's highlight to match ``name`` so it changes
        immediately (shared by click + right-click, which both change
        ``self._selected`` outside of ``set_groups``)."""
        for card in self._cards.values():
            card.set_selected(card._name == name)

    def _on_context(self, name: str, pos: QPoint) -> None:
        self._selected = name
        self._apply_selection(name)
        actions = actions_for_container(self._groups, name)
        menu = QMenu(self)
        if not actions:
            # View-only container: say so rather than declining to open, which
            # is what made this look like a broken right-click. `None` means
            # the container isn't on screen — then there is nothing to say.
            note = view_only_note(self._groups, name)
            if note is None:
                return
            menu.addAction(note).setEnabled(False)
        for label, verb in actions:
            act = menu.addAction(label)
            act.triggered.connect(
                lambda _checked=False, v=verb, n=name: self.actionRequested.emit(v, n)
            )
        menu.exec(pos)
