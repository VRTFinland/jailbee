"""Main window for the Qt dashboard.

A repo-grouped QTreeWidget over the shared dashboard data layer. Actions come
from ``jailbee.dashboard.menus.actions_for_container`` so the GUI and TUI stay in sync. The
window is passive: it renders snapshots pushed via ``set_groups`` and emits
``actionRequested(verb, container_name)`` — the app layer performs the launch.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QEvent, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
)

from jailbee.dashboard.columns import (
    all_column_names,
    default_columns,
    dynamic_column_names,
    normalize_columns,
    reorder_visible,
    visible_fields,
)
from jailbee.dashboard.menus import MenuGroup, group_menu_actions, view_only_note
from jailbee.dashboard.model import RepoTarget
from jailbee.dashboard.sorting import DEFAULT_SORT, SortSpec, click_sort, sort_groups
from jailbee.dashboard.visibility import visible_repo_groups
from jailbee.qtui.action_menu import populate_action_menu
from jailbee.qtui.cards import CardView
from jailbee.qtui.model import (
    STATE_COLORS,
    cell_tooltip,
    column_headers,
    container_cells,
    field_tooltip,
    group_header,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jailbee.dashboard.model import RepoGroup
    from jailbee.dashboard.columns import FieldSpecCI

# Custom role storing the full container name on a tree item.
_NAME_ROLE = int(Qt.ItemDataRole.UserRole)

# Group rows carry their repo prefix; container rows carry their name. Two
# roles rather than one, so `_selected_name` cannot mistake a group header for
# a container (the tree has no other way to tell the two apart).
_PREFIX_ROLE = int(Qt.ItemDataRole.UserRole) + 1

# Built from the framework-free STATE_COLORS (shared with the card view).
_STATE_COLORS = {state: QColor(hex_) for state, hex_ in STATE_COLORS.items()}

# Layout name -> QStackedWidget index.
_LAYOUT_INDEX = {"table": 0, "cards": 1}


def _filtered_columns(names: Sequence[str]) -> tuple[str, ...]:
    """``names`` reduced to real columns in their stored order, ``name`` first.

    Falls back to :func:`default_columns` when no real column survives — a
    stale or hand-edited set must not leave the window with only what the
    normaliser forces in.
    """
    known = frozenset(all_column_names())
    return normalize_columns(names) if any(n in known for n in names) else default_columns()


class MainWindow(QMainWindow):
    """Live container view; emits action requests for the app to execute."""

    actionRequested = Signal(str, str)  # noqa: N815 - Qt signal naming convention (camelCase); payload: (verb, container_name)
    refreshRequested = Signal()  # noqa: N815 - Qt signal naming convention (camelCase); "Refresh now" was triggered
    activeChanged = Signal(bool)  # noqa: N815 - Qt signal naming convention (camelCase); False while minimised
    layoutChanged = Signal(str)  # noqa: N815 - Qt signal naming convention (camelCase); payload: "table" | "cards"
    cardStyleChanged = Signal(str)  # noqa: N815 - Qt signal naming; payload: "compact" | "grid"
    columnsChanged = Signal()  # noqa: N815 - Qt signal naming convention (camelCase); the enabled column set changed
    newContainerRequested = Signal(str)  # noqa: N815 - Qt signal naming convention (camelCase); payload: repo prefix, "" when nothing is selected
    newPrContainerRequested = Signal(str)  # noqa: N815 - payload: repo prefix
    configEditRequested = Signal(str, bool)  # noqa: N815 - Qt signal naming convention (camelCase); payload: (repo prefix, edit the global layer)
    repoVisibilityChanged = Signal()  # noqa: N815 - repository visibility preference changed
    sortChanged = Signal()  # noqa: N815 - Qt signal naming convention (camelCase); the row sort changed

    def __init__(
        self,
        *,
        layout: str = "cards",
        card_style: str = "compact",
        header_state: str | None = None,
        enabled_columns: Sequence[str] | None = None,
        show_empty_repos: bool = True,
        hidden_repos: frozenset[str] = frozenset(),
        sort: SortSpec = DEFAULT_SORT,
    ) -> None:
        super().__init__()
        self._sort = sort
        self._fields: list[FieldSpecCI] = []
        self._groups: list[RepoGroup] = []
        self._all_groups: list[RepoGroup] = []
        self._show_empty_repos = show_empty_repos
        self._hidden_repos = hidden_repos
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
        header = self.tree.header()
        # Sorting stays in the core (`sort_groups`): the tree is rebuilt on every
        # refresh and `setSortingEnabled` would sort the group rows too. The header
        # is made clickable by hand and shows the core's sort as its indicator.
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(False)  # NAME is the row's identity
        header.sectionClicked.connect(self._on_header_clicked)
        header.sectionMoved.connect(self._on_section_moved)

        self.card_view = CardView()
        self.card_view.actionRequested.connect(self.actionRequested)  # re-emit
        self.card_view.newContainerRequested.connect(self.newContainerRequested)
        self.card_view.newPrContainerRequested.connect(self.newPrContainerRequested)
        self.card_view.set_card_style(self._card_style)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.tree)  # index 0 = table
        self.stack.addWidget(self.card_view)  # index 1 = cards
        self.empty_state_label = QLabel()
        self.empty_state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.empty_state_label)
        self.stack.setCurrentIndex(_LAYOUT_INDEX[self._layout])
        self.setCentralWidget(self.stack)

        self.statusBar()
        self._build_view_menu(self._layout)
        self._build_columns_menu()
        self._build_repositories_menu()
        self._build_card_style_menu(self._card_style)
        self._build_refresh_menu()
        self._build_container_menu()
        self._build_config_menu()

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
        self._order_columns_menu()

    def _order_columns_menu(self) -> None:
        """Enabled columns in their order, then the rest alphabetically — the TUI's Fields order."""
        enabled = [n for n in self._enabled_columns if n in self._column_actions]
        rest = sorted(n for n in self._column_actions if n not in self._enabled_columns)
        for action in self._column_actions.values():
            self.columns_menu.removeAction(action)
        for name in (*enabled, *rest):
            self.columns_menu.addAction(self._column_actions[name])

    def _build_repositories_menu(self) -> None:
        self.repositories_menu = self.view_menu.addMenu("&Repositories")
        self._show_empty_action = self.repositories_menu.addAction("Show empty repos")
        self._show_empty_action.setCheckable(True)
        self._show_empty_action.setChecked(self._show_empty_repos)
        self._show_empty_action.triggered.connect(self._toggle_show_empty)
        self._repo_actions: dict[str, QAction] = {}

    def _update_repo_menu(self) -> None:
        prefixes = list(dict.fromkeys([*(g.prefix for g in self._all_groups), *self._hidden_repos]))
        if prefixes == list(self._repo_actions):
            return
        for action in self._repo_actions.values():
            self.repositories_menu.removeAction(action)
            action.deleteLater()
        self._repo_actions.clear()
        for prefix in prefixes:
            action = self.repositories_menu.addAction(prefix)
            action.setCheckable(True)
            action.setChecked(prefix not in self._hidden_repos)
            action.triggered.connect(lambda checked=False, p=prefix: self._toggle_repo(p, checked))
            self._repo_actions[prefix] = action

    def _render_visible_groups(self, *, now: datetime, columns: Sequence[str] | None) -> None:
        self._groups = visible_repo_groups(
            self._all_groups,
            show_empty_repos=self._show_empty_repos,
            hidden_repos=self._hidden_repos,
        )
        enabled = columns if columns is not None else self._enabled_columns
        self._groups = sort_groups(self._groups, self._sort, enabled, now=now)
        if not self._groups:
            self.empty_state_label.setText(
                "No repositories are visible. Change visibility in View > Repositories."
                if self._all_groups
                else "No repositories found."
            )
            self.stack.setCurrentWidget(self.empty_state_label)
        else:
            self.stack.setCurrentIndex(_LAYOUT_INDEX[self._layout])
        self._render_groups(self._groups, now=now, columns=columns)

    def _toggle_show_empty(self, checked: bool) -> None:
        self._show_empty_repos = checked
        self._visibility_changed()

    def _toggle_repo(self, prefix: str, checked: bool) -> None:
        self._hidden_repos = (
            self._hidden_repos - {prefix} if checked else self._hidden_repos | {prefix}
        )
        self._visibility_changed()

    def _visibility_changed(self) -> None:
        self.repoVisibilityChanged.emit()
        self._show_empty_action.setChecked(self._show_empty_repos)
        self._update_repo_menu()
        if hasattr(self, "_last_now"):
            self._render_visible_groups(now=self._last_now, columns=self._last_columns)

    def show_empty_repos(self) -> bool:
        return self._show_empty_repos

    def hidden_repos(self) -> frozenset[str]:
        return self._hidden_repos

    def _toggle_column(self, name: str, checked: bool) -> None:
        """Flip one column, refusing to leave the table with none.

        A dashboard rendering zero columns reads as broken rather than as
        configured, so the last one is pinned and its action snaps back.
        """
        if not checked and len(self._enabled_columns) == 1:
            self._column_actions[name].setChecked(True)
            return
        if name == "name":
            self._column_actions[name].setChecked(True)
            return
        if checked:
            self._enabled_columns = normalize_columns((*self._enabled_columns, name))
        else:
            self._enabled_columns = tuple(n for n in self._enabled_columns if n != name)
        self._order_columns_menu()
        self.columnsChanged.emit()

    def _on_header_clicked(self, index: int) -> None:
        if not 0 <= index < len(self._fields):
            return
        now = getattr(self, "_last_now", None) or datetime.now().astimezone()
        self._sort = click_sort(self._sort, self._fields[index].name, now=now)
        self.sortChanged.emit()
        if hasattr(self, "_last_now"):
            self._render_visible_groups(now=self._last_now, columns=self._last_columns)

    def _on_section_moved(self, _logical: int, _old_visual: int, _new_visual: int) -> None:
        header = self.tree.header()
        visible = [
            self._fields[header.logicalIndex(v)].name
            for v in range(header.count())
            if 0 <= header.logicalIndex(v) < len(self._fields)
        ]
        self._enabled_columns = normalize_columns(reorder_visible(self._enabled_columns, visible))
        self._order_columns_menu()
        self.columnsChanged.emit()

    def sort_spec(self) -> SortSpec:
        return self._sort

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

    def _build_refresh_menu(self) -> None:
        """Build the Refresh menu: a manual "Refresh now" (F5).

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

    def _build_container_menu(self) -> None:
        """The one repo-scoped menu: creating a container, not acting on one.

        Exposed as ``self.container_menu`` for the same reason
        ``_build_refresh_menu`` exposes its own — a submenu fetched through
        ``menuBar().actions()`` can look deleted once the QAction wrapper it
        came from is collected.
        """
        menu = self.menuBar().addMenu("&Container")
        self.container_menu = menu
        self.new_container_action = menu.addAction("&New…")
        self.new_container_action.setShortcut(QKeySequence("Ctrl+N"))
        self.new_container_action.triggered.connect(
            lambda: self.newContainerRequested.emit(self._selected_prefix() or "")
        )
        self.new_pr_container_action = menu.addAction("New from &PR…")
        self.new_pr_container_action.triggered.connect(
            lambda: self.newPrContainerRequested.emit(self._selected_prefix() or "")
        )

    def _build_config_menu(self) -> None:
        """The second repo-scoped menu: editing config, not acting on a container.

        Exposed as ``self.config_menu`` for the same reason the others are — a
        submenu fetched through ``menuBar().actions()`` can look deleted once
        the QAction wrapper it came from is collected.
        """
        menu = self.menuBar().addMenu("Confi&g")
        self.config_menu = menu
        self.edit_repo_config_action = menu.addAction("Edit &repo config…")
        self.edit_repo_config_action.triggered.connect(
            lambda: self.configEditRequested.emit(self._selected_prefix() or "", False)
        )
        self.edit_global_config_action = menu.addAction("Edit &global config…")
        self.edit_global_config_action.triggered.connect(
            lambda: self.configEditRequested.emit(self._selected_prefix() or "", True)
        )

    def _selected_name(self) -> str | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        name = item.data(0, _NAME_ROLE)
        return str(name) if name else None

    def _selected_prefix(self) -> str | None:
        """The repo prefix behind whatever is selected right now.

        The table and the card view keep independent, private selections —
        ``CardView`` never writes into the tree's ``_PREFIX_ROLE`` data, so a
        card click is invisible to a tree walk. Reading only the tree left
        Ctrl+N dead in the default cards layout: a user could click a card,
        press Ctrl+N, and get an unsatisfiable "select a repo" prompt with no
        way to satisfy it in that view. So this consults whichever view is
        actually on screen (``self._layout``) rather than always the tree:
        the card's own group for cards mode, the current row (its own prefix
        for a group header, its parent's for a container row) for table mode.

        When that view-specific lookup finds nothing selected at all (not a
        stale selection — something *was* highlighted but no longer resolves,
        which stays None), a single-repo user is still rescued: if exactly
        one configured group exists, its prefix is returned so that user can
        never hit an unsatisfiable prompt just because nothing happens to be
        highlighted yet. With two or more configured groups and no selection,
        this returns None, same as before.
        """
        if self._layout == "cards":
            name = self.card_view.selected_name()
            if name is None:
                return self._sole_configured_prefix()
            for g in self._groups:
                if any(c.name == name for c in g.containers):
                    return g.prefix
            return None  # stale: the selected card's container is gone
        item = self.tree.currentItem()
        if item is None:
            return self._sole_configured_prefix()
        while item is not None:
            prefix = item.data(0, _PREFIX_ROLE)
            if prefix:
                return str(prefix)
            item = item.parent()
        return None

    def _sole_configured_prefix(self) -> str | None:
        """The one actionable group's prefix, if there's exactly one.

        The single-repo fallback used by ``_selected_prefix`` when nothing is
        selected in the active view. "Actionable" is having a repo root, not
        having a config file: a repo whose config is synthesized is one a
        container can be created in (see :meth:`RepoTarget.of`).
        """
        configured = [g for g in self._groups if RepoTarget.of(g) is not None]
        return configured[0].prefix if len(configured) == 1 else None

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
        self._all_groups = groups
        self._last_now = now
        self._last_columns = columns
        self._update_repo_menu()
        self._render_visible_groups(now=now, columns=columns)

    def _render_groups(
        self,
        groups: list[RepoGroup],
        *,
        now: datetime,
        columns: Sequence[str] | None,
    ) -> None:
        self._groups = groups
        prev = self._selected_name()
        active_columns = columns if columns is not None else self._enabled_columns

        all_containers = [c for g in groups for c in g.containers]
        fields = visible_fields(now, all_containers, enabled=active_columns)
        self._fields = fields
        headers = column_headers(fields)
        self.tree.setColumnCount(len(headers))
        self.tree.setHeaderLabels(headers)
        for index, field in enumerate(fields):
            self.tree.headerItem().setToolTip(index, field_tooltip(field))
        if self._pending_header_state is not None:
            self.tree.header().restoreState(
                QByteArray.fromBase64(self._pending_header_state.encode("ascii"))
            )
            self._pending_header_state = None
        header = self.tree.header()
        # One column order only: the logical one built from `enabled_columns`.
        # A drag (or an old saved state) leaves a visual permutation behind.
        blocked = header.blockSignals(True)
        try:
            for logical in range(header.count()):
                visual = header.visualIndex(logical)
                if visual != logical:
                    header.moveSection(visual, logical)
        finally:
            header.blockSignals(blocked)
        sort_col = next((i for i, f in enumerate(fields) if f.name == self._sort.field), -1)
        header.setSortIndicator(
            sort_col,
            Qt.SortOrder.DescendingOrder if self._sort.desc else Qt.SortOrder.AscendingOrder,
        )
        state_col = next((i for i, f in enumerate(fields) if f.name == "state"), None)

        self.tree.clear()
        to_reselect: QTreeWidgetItem | None = None
        for g in groups:
            label, _is_orphan = group_header(g)
            if not g.containers:
                label = f"{label}  (0 containers)"
            group_item = QTreeWidgetItem([label])
            group_item.setFirstColumnSpanned(True)
            group_item.setData(0, _PREFIX_ROLE, g.prefix)
            self.tree.addTopLevelItem(group_item)
            group_item.setExpanded(True)
            for c in g.containers:
                child = QTreeWidgetItem(container_cells(c, fields))
                child.setData(0, _NAME_ROLE, c.name)
                for index, field in enumerate(fields):
                    child.setToolTip(
                        index,
                        "<qt>" + escape(cell_tooltip(c, field)).replace("\n", "<br>") + "</qt>",
                    )
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
        return [
            item.label if isinstance(item, MenuGroup) else item[0]
            for item in group_menu_actions(self._actions_for(container_name))
        ]

    def _actions_for(self, container_name: str) -> list[tuple[str, str]]:
        from jailbee.dashboard.menus import actions_for_container

        return actions_for_container(self._groups, container_name)

    def _on_context_menu(self, pos: object) -> None:
        name = self._selected_name()
        if name is None:
            # A group header: the only thing it can offer is creating a
            # container in that repo.
            prefix = self._selected_prefix()
            if prefix is None:
                return
            group = next((g for g in self._groups if g.prefix == prefix), None)
            if group is None or RepoTarget.of(group) is None:
                return
            group_menu = QMenu(self)
            act = group_menu.addAction("New container…")
            act.triggered.connect(
                lambda _checked=False, p=prefix: self.newContainerRequested.emit(p)
            )
            pr_act = group_menu.addAction("New from PR…")
            pr_act.triggered.connect(
                lambda _checked=False, p=prefix: self.newPrContainerRequested.emit(p)
            )
            # Same stub gap as below: pos is a QPoint at runtime, but PySide6's
            # overload set for the signal's `object` parameter doesn't narrow to it.
            group_menu.exec(self.tree.viewport().mapToGlobal(pos))  # type: ignore[call-overload]
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
        populate_action_menu(menu, actions, lambda verb: self.actionRequested.emit(verb, name))
        # pos comes through as QPoint at runtime; PySide6's stub overload set
        # for the signal's `object` parameter doesn't narrow to QPoint here.
        menu.exec(self.tree.viewport().mapToGlobal(pos))  # type: ignore[call-overload]

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        """Tell the controller when the window is minimised or restored, so
        the shared state service stops gathering for a window nobody sees."""
        if event.type() == QEvent.Type.WindowStateChange:
            self.activeChanged.emit(not self.isMinimized())
        super().changeEvent(event)

    def set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)

    def set_refresh_ok(self, *, at: datetime, git_enabled: bool) -> None:
        """Status bar for a fresh snapshot: its time, and ``(no-git)`` when the
        state service is not probing git."""
        note = "" if git_enabled else "  ·  (no-git)"
        self.set_status(f"Last refresh {at:%H:%M:%S}{note}")

    def set_refresh_failed(self, msg: str) -> None:
        """Status bar for a state-client problem. ``msg`` is already a complete
        sentence ("refresh failed: …", "state service disconnected — …"), so it
        is shown as is. Non-modal — a dialog per failure would spam the user."""
        self.set_status(msg)
