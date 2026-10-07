"""Native Qt rendering of the dashboard's filtered container action leaves."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.dashboard.menus import MenuGroup, group_menu_actions

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from PySide6.QtWidgets import QMenu


def populate_action_menu(
    menu: QMenu, actions: Sequence[tuple[str, str]], emit: Callable[[str], None]
) -> None:
    """Add direct actions and native Launch/PR/Git submenus; only leaves dispatch."""
    for item in group_menu_actions(actions):
        if isinstance(item, MenuGroup):
            destination = menu.addMenu(item.label)
            leaves = item.actions
        else:
            destination = menu
            leaves = (item,)
        for label, verb in leaves:
            action = destination.addAction(label)
            action.triggered.connect(lambda _checked=False, v=verb: emit(v))
