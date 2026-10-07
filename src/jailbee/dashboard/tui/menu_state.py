"""Open action menus as immutable state, and the moves between them."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import actions as dact
from jailbee.dashboard.commands import (
    permitted,
)
from jailbee.dashboard.menus import (
    _NETWORK_MODES,
    MenuGroup,
    MenuItem,
    _insert_after_job,
    _insert_before_network,
    _with_credential_group,
    actions_for_container,
    group_menu_actions,
)
from jailbee.dashboard.model import RepoGroup, RepoTarget, _find_group
from jailbee.dashboard.tui.keys import KEY_BINDINGS

if TYPE_CHECKING:
    pass


@dataclass
class MenuState:
    """An open action menu, rendered inline under the dashboard table.

    ``actions`` is captured when the menu opens rather than recomputed per
    frame: the dashboard keeps refreshing behind the menu, and a list that
    re-derived itself from live state would reorder rows under the cursor
    mid-keystroke. The staleness that buys is bounded — dispatching a verb
    the container has since outgrown just lets the real ``jailbee`` command
    report the problem, exactly as the previous questionary menu did.
    """

    container: str
    actions: list[tuple[str, str]]
    index: int = 0
    active_group: str | None = None
    parent_index: int = 0


@dataclass
class RepoMenuState:
    """Repo-scoped actions for a selected header, distinct from container verbs."""

    repo: str
    actions: list[MenuItem]
    index: int = 0
    active_group: str | None = None
    parent_index: int = 0


def open_menu(
    groups: list[RepoGroup],
    name: str | None,
    *,
    remote: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
) -> MenuState | None:
    """The menu for ``name``, or None when there is nothing to show.

    None covers every no-actions case — unknown container, nothing selected,
    or a view-only (orphan) group. Callers surface :func:`view_only_note`
    instead, because an empty menu frame is indistinguishable from a broken one.

    The terminal menu also offers ``Credential group…`` and the
    :mod:`jailbee.dashboard.actions` entries (autostart, snapshots, mounts),
    which the dashboard handles itself rather than dispatching. They are added
    here, not in :func:`menu_actions`, because the Qt dashboard shares that list.
    """
    actions = actions_for_container(
        groups, name, remote=remote, ssh_policy=ssh_policy, over_ssh=over_ssh
    )
    if name is None:
        return None
    group = _find_group(groups, name)
    container = (
        next((c for c in group.containers if c.name == name), None) if group is not None else None
    )
    if group is None or container is None:
        return None
    # An orphan group is view-only: no shared action and no terminal extra either.
    if not actions and RepoTarget.of(group) is None:
        return None
    extras = dact.container_extras(container, group.optional_mounts, ssh_policy, over_ssh=over_ssh)
    actions = _insert_after_job(actions, extras.after_job)
    if extras.before_network:
        actions = _insert_before_network(actions, extras.before_network)
    # Probed with placeholders: the policy judges the command, not its values.
    if permitted(["account", "group", "use", "x", "y"], ssh_policy, over_ssh=over_ssh):
        actions = _with_credential_group(actions)
    # The shared list can be empty (an SSH allowlist naming no lifecycle or
    # shell verb) while a terminal-only entry is still permitted; nothing at
    # all means no menu.
    if not actions:
        return None
    return MenuState(name, actions)


def open_repo_menu(
    groups: list[RepoGroup],
    prefix: str,
    folded: frozenset[str],
    *,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
) -> RepoMenuState | None:
    """Offer creation, the credential group, egress and repo-level CLI entries, folding for all.

    Everything but folding is offered for actionable repos only.

    The credential group and egress entries are hidden when the SSH policy
    refuses them, so a session never sees an entry that can only fail.
    """
    group = next((g for g in groups if g.prefix == prefix), None)
    if group is None:
        return None
    actions: list[MenuItem] = []
    if RepoTarget.of(group) is not None:
        actions.append(("New container…", "new"))
        actions.append(("New from PR…", "new-pr"))
        # Probed with a placeholder group: the policy judges the command, not its value.
        if permitted(["account", "group", "set", "x"], ssh_policy, over_ssh=over_ssh):
            actions.append(("Credential group…", "credential-group"))
        if permitted(["account", "ls"], ssh_policy, over_ssh=over_ssh):
            actions.append(("Accounts…", "accounts"))
        if permitted(["net", "egress", "ls", "--repo"], ssh_policy, over_ssh=over_ssh):
            actions.append(MenuGroup("Network →", (("Egress…", "net egress ls"),)))
        extras = dact.repo_extras(ssh_policy, over_ssh=over_ssh)
        if extras.apply is not None:
            actions.append(extras.apply)
        if extras.diagnostics:
            actions.append(MenuGroup(dact.DIAGNOSTICS_LABEL, extras.diagnostics))
        if extras.prune is not None:
            actions.append(extras.prune)
    actions.append(("Unfold" if prefix in folded else "Fold", "fold"))
    return RepoMenuState(prefix, actions)


def _menu_entries(menu: MenuState | RepoMenuState) -> Sequence[MenuItem]:
    """Visible entries at this level, derived only from captured leaves."""
    items = (
        menu.actions
        if isinstance(menu, RepoMenuState)
        else group_menu_actions(menu.actions, include_network=True, terminal_order=True)
    )
    if menu.active_group is None:
        return items
    return next(
        (
            item.actions
            for item in items
            if isinstance(item, MenuGroup) and item.label == menu.active_group
        ),
        (),
    )


def enter_menu(menu: MenuState | RepoMenuState) -> tuple[MenuState | RepoMenuState, str | None]:
    """Enter a selected group or return its selected executable verb."""
    entries = _menu_entries(menu)
    if not 0 <= menu.index < len(entries):
        return menu, None
    selected = entries[menu.index]
    if isinstance(selected, MenuGroup):
        return replace(menu, active_group=selected.label, parent_index=menu.index, index=0), None
    return menu, selected[1]


def back_menu(menu: MenuState | RepoMenuState) -> MenuState | RepoMenuState | None:
    """Go back to the highlighted parent group; close at the root."""
    if menu.active_group is None:
        return None
    return replace(menu, active_group=None, index=menu.parent_index)


def move_menu(menu: MenuState | RepoMenuState, delta: int) -> MenuState | RepoMenuState:
    """Move the cursor within the visible level, clamped at both ends."""
    last = max(0, len(_menu_entries(menu)) - 1)
    return replace(menu, index=max(0, min(last, menu.index + delta)))


# Each menu entry's own key, by leaf verb (labels carry counts) or group label.
# Scoped to the level that is open, so `l` is Lifecycle at the root and `git
# pull` inside Git. Where a dashboard quick key exists the letter matches it,
# and Destroy stays a capital as there. Every entry a menu can show has one,
# unique among the entries that can share its level, so no key moves when
# another entry comes or goes; the tests enumerate those combinations. Only
# app launches (`Launch →`, labels from the repo's config) take a free letter
# of their label (see `menu_hotkeys`).
_MENU_KEYS: dict[str, str] = {
    # container root (start and a lone stop never share it)
    "tmux": "t",
    "outbox browse": "o",
    "Launch →": "a",
    "start": "s",
    "job log": "b",
    "job log --follow": "b",
    "job clear": "x",
    dact.AUTOSTART_STATUS: "A",
    dact.AUTOSTART_CANCEL: "C",
    "Git →": "g",
    "PR →": "p",
    "Lifecycle →": "l",
    dact.SNAPSHOTS: "n",
    dact.MOUNT_ADD: "m",
    dact.MOUNT_REMOVE: "u",
    "credential-group": "c",
    "Network →": "w",
    # Git →
    "merge": "m",
    "git pull": "l",
    "git push": "u",
    "git push --pr": "r",
    "git retarget": "b",
    "git diff": "d",
    # PR →
    "pr --open": "p",
    "pr": "P",
    # Lifecycle → (each also alone at the root when the SSH policy hides the rest)
    "restart": "r",
    "stop": "s",
    "destroy": "D",
    # Network → (modes take their own initial, see `_preferred_menu_key`)
    "net egress ls": "e",
    # repo menu
    "new": "n",
    "new-pr": "p",
    "accounts": "a",
    dact.REPO_APPLY: "y",
    dact.DIAGNOSTICS_LABEL: "d",
    dact.REPO_PRUNE: "r",
    "fold": "f",
    # Diagnostics →
    dact.REPO_DOCTOR: "d",
    dact.REPO_DISK_USAGE: "u",
}

# Tokens the open menu already answers (`run`'s overlay branch); their keys
# can never be an entry's own.
_MENU_HANDLED_TOKENS = frozenset(
    {"up", "down", "enter", "cancel", "quit", "help", "settings", "interrupt"}
)
_MENU_RESERVED_KEYS = frozenset(
    key.decode()
    for b in KEY_BINDINGS
    if b.token in _MENU_HANDLED_TOKENS
    for key in b.keys
    if len(key) == 1 and key.isascii() and key.decode().isprintable()
)


def _preferred_menu_key(item: MenuItem) -> str | None:
    if isinstance(item, MenuGroup):
        return _MENU_KEYS.get(item.label)
    verb = item[1]
    if verb.startswith("net ") and verb.removeprefix("net ") in _NETWORK_MODES:
        return verb.removeprefix("net ")[0]
    return _MENU_KEYS.get(verb)


def menu_hotkeys(entries: Sequence[MenuItem]) -> list[str | None]:
    """Each entry's key at this level, parallel to ``entries``.

    Preferred keys (:data:`_MENU_KEYS`) are handed out first, in entry order,
    so a fixed key never moves because an entry above it appeared. The rest
    take the first free letter of their label, then a digit; None once those
    run out. Keys the open menu already handles are never assigned.
    """
    taken = set(_MENU_RESERVED_KEYS)
    keys: list[str | None] = [None] * len(entries)
    for i, item in enumerate(entries):
        key = _preferred_menu_key(item)
        if key is not None and key not in taken:
            keys[i] = key
            taken.add(key)
    for i, item in enumerate(entries):
        if keys[i] is not None:
            continue
        label = item.label if isinstance(item, MenuGroup) else item[0]
        candidates = [ch for ch in label.lower() if ch.isascii() and ch.isalpha()]
        key = next((ch for ch in (*candidates, *"123456789") if ch not in taken), None)
        keys[i] = key
        if key is not None:
            taken.add(key)
    return keys


def hotkey_menu(menu: MenuState | RepoMenuState, data: bytes) -> MenuState | RepoMenuState | None:
    """``menu`` with the cursor on the entry whose key ``data`` is, else None."""
    try:
        typed = data.decode()
    except UnicodeDecodeError:
        return None
    keys = menu_hotkeys(_menu_entries(menu))
    if typed not in keys:
        return None
    return replace(menu, index=keys.index(typed))


def menu_verb(menu: MenuState | RepoMenuState) -> str | None:
    """Selected leaf verb, or None for a group or an empty menu."""
    entries = _menu_entries(menu)
    if not 0 <= menu.index < len(entries):
        return None
    entry = entries[menu.index]
    return None if isinstance(entry, MenuGroup) else entry[1]
