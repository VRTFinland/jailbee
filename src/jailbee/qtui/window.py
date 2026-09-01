"""Main window for the Qt dashboard.

A repo-grouped QTreeWidget over the shared dashboard data layer. Actions come
from ``dashboard.actions_for_container`` so the GUI and TUI stay in sync. The
window is passive: it renders snapshots pushed via ``set_groups`` and emits
``actionRequested(verb, container_name)`` — the app layer performs the launch.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow,
    QMenu,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
)

from jailbee.dashboard import (
    all_column_names,
    default_columns,
    dynamic_column_names,
    view_only_note,
    visible_fields,
)
from jailbee.qtui.cards import CardView
from jailbee.qtui.model import (
    STATE_COLORS,
    column_headers,
    container_cells,
    group_header,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jailbee.dashboard import RepoGroup

# Custom role storing the full container name on a tree item.
_NAME_ROLE = int(Qt.ItemDataRole.UserRole)

# Built from the framework-free STATE_COLORS (shared with the card view).
_STATE_COLORS = {state: QColor(hex_) for state, hex_ in STATE_COLORS.items()}

# Numeric cadence presets offered in the Refresh menu, in seconds.
_CADENCE_PRESETS = (1.0, 2.0, 3.0, 5.0, 10.0, 30.0)

# Layout name -> QStackedWidget index.
_LAYOUT_INDEX = {"table": 0, "cards": 1}


def _filtered_columns(names: Sequence[str]) -> tuple[str, ...]:
    """``names`` reduced to real columns, in canonical order.

    Falls back to :func:`default_columns` when nothing survives — a stale
    or hand-edited set (a renamed/removed column, or a caller passing
    arbitrary names directly) must not be able to leave the window with
    zero enabled columns, the exact state the last-column guard in
    ``_toggle_column`` exists to prevent. Used by ``__init__`` when
    restoring a persisted column set via the ``enabled_columns`` keyword.
    """
    filtered = tuple(n for n in all_column_names() if n in set(names))
    return filtered or default_columns()


class MainWindow(QMainWindow):
    """Live container view; emits action requests for the app to execute."""

    actionRequested = Signal(str, str)  # noqa: N815 - Qt signal naming convention (camelCase); payload: (verb, container_name)
    refreshRequested = Signal()  # noqa: N815 - Qt signal naming convention (camelCase); "Refresh now" was triggered
    intervalChanged = Signal(float)  # noqa: N815 - Qt signal naming convention (camelCase); payload: new interval seconds
    autoRefreshDisabled = Signal()  # noqa: N815 - Qt signal naming convention (camelCase); "Off (manual)" was selected
    layoutChanged = Signal(str)  # noqa: N815 - Qt signal naming convention (camelCase); payload: "table" | "cards"
    cardStyleChanged = Signal(str)  # noqa: N815 - Qt signal naming; payload: "compact" | "grid"
    columnsChanged = Signal()  # noqa: N815 - Qt signal naming convention (camelCase); the enabled column set changed

    def __init__(
        self,
        *,
        git_enabled: bool,
        interval: float,
        paused: bool = False,
        layout: str = "cards",
        card_style: str = "compact",
        header_state: str | None = None,
        enabled_columns: Sequence[str] | None = None,
    ) -> None:
        super().__init__()
        self._git_enabled = git_enabled
        self._groups: list[RepoGroup] = []
        self._layout = layout if layout in _LAYOUT_INDEX else "cards"
        self._card_style = card_style if card_style in ("compact", "grid") else "compact"
        self._pending_header_state = header_state
        self._enabled_columns: tuple[str, ...] = (
            _filtered_columns(enabled_columns) if enabled_columns is not None else default_columns()
        )
        self.setWindowTitle("🐝 JailBee dashboard")
        self.resize(1000, 640)
        self.setMinimumSize(360, 420)  # narrow-friendly: card view needs little width

        self.tree = QTreeWidget()
        self.tree.setColumnCount(1)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)

        self.card_view = CardView()
        self.card_view.actionRequested.connect(self.actionRequested)  # re-emit
        self.card_view.set_card_style(self._card_style)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.tree)  # index 0 = table
        self.stack.addWidget(self.card_view)  # index 1 = cards
        self.stack.setCurrentIndex(_LAYOUT_INDEX[self._layout])
        self.setCentralWidget(self.stack)

        self.statusBar()
        self._build_view_menu(self._layout)
        self._build_columns_menu()
        self._build_card_style_menu(self._card_style)
        self._build_refresh_menu(interval, paused=paused)

    def _build_view_menu(self, layout: str) -> None:
        menu = self.menuBar().addMenu("&View")
        self.view_menu = menu
        group = QActionGroup(self)
        group.setExclusive(True)
        for name, label in (("table", "Table"), ("cards", "Cards")):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(name == layout)
            group.addAction(act)
            act.triggered.connect(lambda _checked=False, n=name: self._switch_layout(n))
        self._view_action_group = group

    def _build_card_style_menu(self, card_style: str) -> None:
        """The Compact/Grid switch — its own top-level menu, shown only in the
        card view (it has no meaning while the table is displayed)."""
        menu = self.menuBar().addMenu("&Card style")
        self.card_style_menu = menu
        group = QActionGroup(self)
        group.setExclusive(True)
        for name, label in (("compact", "Compact"), ("grid", "Grid")):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(name == card_style)
            group.addAction(act)
            act.triggered.connect(lambda _checked=False, n=name: self._switch_card_style(n))
        self._card_style_action_group = group
        menu.menuAction().setVisible(self._layout == "cards")

    def _build_columns_menu(self) -> None:
        """A checkable action per column, under View.

        The Qt counterpart of the TUI's settings overlay, and deliberately
        independent of it: the two front-ends keep separate `view_prefs`
        rows, so a wide table here and a narrow one there is a supported
        setup rather than a bug.
        """
        menu = self.view_menu.addMenu("&Columns")
        self.columns_menu = menu
        self._column_actions: dict[str, QAction] = {}
        dynamic = dynamic_column_names()
        for name in all_column_names():
            label = f"{name} (shown only when it applies)" if name in dynamic else name
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(name in self._enabled_columns)
            act.triggered.connect(lambda checked=False, n=name: self._toggle_column(n, checked))
            self._column_actions[name] = act

    def _toggle_column(self, name: str, checked: bool) -> None:
        """Flip one column, refusing to leave the table with none.

        A dashboard rendering zero columns reads as broken rather than as
        configured, so the last one is pinned and its action snaps back.
        """
        if not checked and len(self._enabled_columns) == 1:
            self._column_actions[name].setChecked(True)
            return
        current = set(self._enabled_columns)
        if checked:
            current.add(name)
        else:
            current.discard(name)
        self._enabled_columns = tuple(n for n in all_column_names() if n in current)
        self.columnsChanged.emit()

    def enabled_columns(self) -> tuple[str, ...]:
        return self._enabled_columns

    def _switch_layout(self, name: str) -> None:
        self._layout = name
        self.stack.setCurrentIndex(_LAYOUT_INDEX[name])
        self.card_style_menu.menuAction().setVisible(name == "cards")
        self.layoutChanged.emit(name)

    def current_layout(self) -> str:
        return self._layout

    def _switch_card_style(self, name: str) -> None:
        self._card_style = name
        self.card_view.set_card_style(name)
        self.cardStyleChanged.emit(name)

    def current_card_style(self) -> str:
        return self._card_style

    def collapsed_repos(self) -> set[str]:
        return self.card_view.collapsed()

    def table_header_state(self) -> str | None:
        """The persisted header layout: base64-encoded ``QHeaderView`` state.

        Before the first ``set_groups``, a restored ``header_state`` sits
        unapplied in ``self._pending_header_state`` (it's only applied to
        the live tree on the first snapshot, once real columns exist). If
        persistence runs before that first snapshot, reading the live
        tree's header would return the default single-column state and
        clobber the real persisted value — so return the pending value
        until it's consumed.
        """
        if self._pending_header_state is not None:
            return self._pending_header_state
        data = self.tree.header().saveState()
        return bytes(data.toBase64().data()).decode("ascii")

    def _build_refresh_menu(self, interval: float, *, paused: bool = False) -> None:
        """Build the Refresh menu: manual refresh-now plus an exclusive
        group of cadence presets (including an "Off (manual)" pause option).

        Exposed as ``self.refresh_menu`` (mirroring ``self.tree``) so tests
        can find its actions directly, rather than round-tripping through
        ``menuBar().actions()`` — PySide's Python wrapper for a submenu
        fetched via ``QAction.menu()`` is tied to the lifetime of the
        ``QAction`` it was fetched from, so it can look "already deleted"
        once that loop-local action wrapper is garbage-collected, even
        though the underlying C++ QMenu is still alive and parented."""
        menu = self.menuBar().addMenu("&Refresh")
        self.refresh_menu = menu

        refresh_now = menu.addAction("Refresh now")
        refresh_now.setShortcut(QKeySequence("F5"))
        refresh_now.triggered.connect(lambda: self.refreshRequested.emit())

        menu.addSeparator()

        group = QActionGroup(self)
        group.setExclusive(True)
        closest = min(_CADENCE_PRESETS, key=lambda v: abs(v - interval))
        for value in _CADENCE_PRESETS:
            preset_action = menu.addAction(f"{value:.0f}s")
            preset_action.setCheckable(True)
            preset_action.setChecked(value == closest and not paused)
            group.addAction(preset_action)
            preset_action.triggered.connect(
                lambda _checked=False, v=value: self.intervalChanged.emit(v)
            )

        manual_action = menu.addAction("Off (manual)")
        manual_action.setCheckable(True)
        manual_action.setChecked(paused)
        group.addAction(manual_action)
        manual_action.triggered.connect(lambda: self.autoRefreshDisabled.emit())

        # Kept alive on the window so the action group itself isn't GC'd
        # once this method returns.
        self._refresh_action_group = group

    def _selected_name(self) -> str | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        name = item.data(0, _NAME_ROLE)
        return str(name) if name else None

    def set_groups(
        self,
        groups: list[RepoGroup],
        *,
        now: datetime,
        columns: Sequence[str] | None = None,
    ) -> None:
        """(Re)populate the tree, preserving the selection by container name.

        ``columns``, when given, overrides the window's own enabled set for
        this call only (existing callers/tests rely on that); ``None`` (the
        common case — a periodic refresh) renders the live, menu-driven
        ``self._enabled_columns`` instead, so a toggle in the Columns menu
        takes effect on the next refresh without the caller having to know
        about it.
        """
        self._groups = groups
        prev = self._selected_name()
        active_columns = columns if columns is not None else self._enabled_columns

        all_containers = [c for g in groups for c in g.containers]
        fields = visible_fields(now, all_containers, enabled=active_columns)
        headers = column_headers(fields)
        self.tree.setColumnCount(len(headers))
        self.tree.setHeaderLabels(headers)
        if self._pending_header_state is not None:
            self.tree.header().restoreState(
                QByteArray.fromBase64(self._pending_header_state.encode("ascii"))
            )
            self._pending_header_state = None
        state_col = next((i for i, f in enumerate(fields) if f.name == "state"), None)

        self.tree.clear()
        to_reselect: QTreeWidgetItem | None = None
        for g in groups:
            label, _is_orphan = group_header(g)
            group_item = QTreeWidgetItem([label])
            group_item.setFirstColumnSpanned(True)
            self.tree.addTopLevelItem(group_item)
            group_item.setExpanded(True)
            for c in g.containers:
                child = QTreeWidgetItem(container_cells(c, fields))
                child.setData(0, _NAME_ROLE, c.name)
                color = _STATE_COLORS.get(c.state)
                if color is not None and state_col is not None:
                    child.setForeground(state_col, color)
                group_item.addChild(child)
                if c.name == prev:
                    to_reselect = child
        if to_reselect is not None:
            self.tree.setCurrentItem(to_reselect)

        self.card_view.set_groups(groups, now=now, columns=active_columns)

    def menu_labels_for(self, container_name: str) -> list[str]:
        """Action labels for ``container_name`` (empty if unknown/orphan)."""
        return [label for label, _ in self._actions_for(container_name)]

    def _actions_for(self, container_name: str) -> list[tuple[str, str]]:
        from jailbee.dashboard import actions_for_container

        return actions_for_container(self._groups, container_name)

    def _on_context_menu(self, pos: object) -> None:
        name = self._selected_name()
        if name is None:
            return
        actions = self._actions_for(name)
        menu = QMenu(self)
        if not actions:
            # Mirrors the card view: a view-only row explains itself, an
            # unknown one (stale selection) opens nothing at all.
            note = view_only_note(self._groups, name)
            if note is None:
                return
            menu.addAction(note).setEnabled(False)
        for label, verb in actions:
            act = menu.addAction(label)
            act.triggered.connect(
                lambda _checked=False, v=verb, n=name: self.actionRequested.emit(v, n)
            )
        # pos comes through as QPoint at runtime; PySide6's stub overload set
        # for the signal's `object` parameter doesn't narrow to QPoint here.
        menu.exec(self.tree.viewport().mapToGlobal(pos))  # type: ignore[call-overload]

    def set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)

    def set_refresh_ok(self, *, at: datetime, interval: float, paused: bool = False) -> None:
        """Status bar for a successful refresh: last-refresh time, cadence,
        and a ``(no-git)`` marker when git probing is disabled. Cadence reads
        ``manual`` when auto-refresh is paused, ``every Xs`` otherwise."""
        note = "" if self._git_enabled else "  ·  (no-git)"
        cadence = "manual" if paused else f"every {interval:.0f}s"
        self.set_status(f"Last refresh {at:%H:%M:%S} · {cadence}{note}")

    def set_refresh_failed(self, msg: str) -> None:
        """Status bar for a failed gather. Non-modal — the loop keeps
        retrying, so a dialog per failure would spam the user."""
        self.set_status(f"Refresh failed: {msg} — retrying…")
