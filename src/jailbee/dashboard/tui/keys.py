"""The terminal dashboard's key map: one table for the key loop and the help text."""

from __future__ import annotations

from dataclasses import dataclass

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard.commands import (
    check_dashboard_command,
    dashboard_action_argv,
)
from jailbee.dashboard.menus import (
    _GUI_VERBS,
    APPS_RUN_PREFIX,
    ATTACH_VERBS,
    actions_for_container,
    view_only_note,
)
from jailbee.dashboard.model import RepoGroup
from jailbee.remote_ssh.router import RouteError


@dataclass(frozen=True)
class KeyBinding:
    """One dashboard key: how it is typed, what it does, how it is described.

    :data:`KEY_BINDINGS` is the single source for all three — :func:`parse_key`
    is built from ``keys``, the quick-action gate from ``verb``, the help
    overlay from ``hint``/``label``/``group``. Three hand-maintained lists
    would drift.

    ``hint`` is empty for a token whose sibling documents it (``down`` is
    covered by ``up``'s "↑/↓ (j/k)").
    """

    token: str
    keys: tuple[bytes, ...]
    hint: str
    label: str
    group: str
    verb: str | None = None


KEY_BINDINGS: tuple[KeyBinding, ...] = (
    KeyBinding("up", (b"\x1b[A", b"k"), "↑/↓ (j/k)", "move the highlight", "Navigate"),
    KeyBinding("down", (b"\x1b[B", b"j"), "", "", "Navigate"),
    KeyBinding("scroll-left", (b"\x1b[D",), "←/→", "scroll columns", "Navigate"),
    KeyBinding("scroll-right", (b"\x1b[C",), "", "", "Navigate"),
    KeyBinding(
        "enter", (b"\r", b"\n"), "Enter", "open a container or repo menu (fold there)", "Navigate"
    ),
    KeyBinding(
        "cancel", (b"\x1b",), "Esc", "close a menu, panel or help; cancel a question", "Navigate"
    ),
    KeyBinding(
        "space",
        (b" ",),
        "Space",
        "fold/unfold the selected repo (Settings: toggle)",
        "Navigate",
    ),
    KeyBinding("action:tmux", (b"t",), "t", "attach tmux", "Actions", verb="tmux"),
    KeyBinding("action:shell", (b"s",), "s", "open a shell", "Actions", verb="shell"),
    KeyBinding("action:ide", (b"i",), "i", "launch the IDE", "Actions", verb="ide"),
    KeyBinding("action:chrome", (b"c",), "c", "launch Chrome", "Actions", verb="chrome"),
    KeyBinding("action:pr", (b"p",), "p", "open the PR", "Actions", verb="pr --open"),
    KeyBinding("action:pr-update", (b"P",), "P", "create or update the PR", "Actions", verb="pr"),
    KeyBinding("action:push", (b"u",), "u", "update from base", "Actions", verb="git push"),
    KeyBinding("action:diff", (b"d",), "d", "show the diff", "Actions", verb="git diff"),
    # Capital, so a stray `d` (diff) can never reach it. The confirmation is the
    # CLI's own `destroy` prompt, run in the terminal exactly as the menu entry.
    KeyBinding(
        "action:destroy",
        (b"D",),
        "D",
        "destroy the container (asks to confirm)",
        "Actions",
        verb="destroy",
    ),
    # Repo-scoped, not container-scoped: no `verb`, so it never reaches
    # `quick_verb`/`actions_for_container` (those gate on a container's state).
    # `run`'s dispatch handles it directly, with its own guard.
    KeyBinding("new", (b"n",), "n", "create a container in this repo", "Actions"),
    # Repo-scoped like `new`: no `verb`, so neither reaches `quick_verb` — the
    # config being edited belongs to the repo, not to the highlighted container.
    KeyBinding(
        "config-edit",
        (b"e",),
        "e / E",
        "edit this repo's config (E: the global one)",
        "Actions",
    ),
    KeyBinding("config-edit-global", (b"E",), "", "", "Actions"),
    # Host-wide, not row-scoped: the selected row only picks which repo the
    # `jailbee account …` children are run in.
    KeyBinding(
        "accounts",
        (b"A",),
        "A",
        "credential groups and stored logins",
        "Actions",
    ),
    KeyBinding("optimize", (b"o",), "o", "optimize column widths once", "View"),
    KeyBinding("refresh", (b"r",), "r", "force a full refresh", "View"),
    KeyBinding("details", (b"v",), "v", "show/hide the details panel", "View"),
    KeyBinding(
        "settings",
        (b"\x1bOQ", b"\x1b[12~", b"S"),
        "F2 / S",
        "columns and repo folding",
        "View",
    ),
    KeyBinding("tab", (b"\t",), "", "", "View"),
    KeyBinding("help", (b"h", b"?"), "h / ?", "this help", "View"),
    KeyBinding("command", (b"!",), "!", "run a jailbee command", "Actions"),
    KeyBinding("quit", (b"q",), "q", "quit (closes an overlay first)", "View"),
    # b"" is a zero-length read: stdin hit EOF, so there is nothing left to quit to.
    KeyBinding("interrupt", (b"\x03", b""), "Ctrl-C", "quit immediately", "View"),
)

_KEY_TOKENS: dict[bytes, str] = {k: b.token for b in KEY_BINDINGS for k in b.keys}

_GATE_NOTE = (
    "Action keys only fire when that action is offered for the highlighted "
    "container: a stopped container has no tmux or shell, the IDE and Chrome "
    "need the repo's own jetbrains/chrome config, the PR key needs a known PR, "
    "the workflow keys need a running clone-mode container (and the diff key "
    "needs something to show), and orphan rows are view-only."
)


def binding_for_token(token: str) -> KeyBinding | None:
    """The binding a :func:`parse_key` token came from (None if unmapped)."""
    return next((b for b in KEY_BINDINGS if b.token == token), None)


def quick_verb(
    groups: list[RepoGroup],
    name: str | None,
    token: str,
    *,
    remote: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
) -> str | None:
    """The verb a quick-action key should dispatch for ``name``, else None.

    None covers both "not an action key" and "that action isn't offered here".
    The gate is :func:`actions_for_container`, so ``menu_actions`` stays the
    only place that decides what a container allows — a quick key can never
    reach an action its own menu would not show.
    """
    binding = binding_for_token(token)
    if binding is None or binding.verb is None:
        return None
    offered = {
        verb
        for _label, verb in actions_for_container(
            groups, name, remote=remote, ssh_policy=ssh_policy, over_ssh=over_ssh
        )
    }
    return binding.verb if binding.verb in offered else None


def quick_reject_note(
    groups: list[RepoGroup],
    name: str | None,
    token: str,
    *,
    remote: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
) -> str:
    """Why a quick-action key did nothing, as one user-facing sentence.

    A key that silently declines is indistinguishable from a broken one, and
    the reason matters: a view-only row explains itself differently from a
    stopped container or a repo with the IDE turned off — and from a remote
    session, which never launches GUI apps (see :attr:`MenuContext.remote`).
    """
    if name is None:
        return "No container is selected"
    note = view_only_note(groups, name)
    if note is not None:
        return note
    binding = binding_for_token(token)
    gui = ssh_policy is not None and ssh_policy.gui
    if remote and binding is not None and binding.verb in _GUI_VERBS and not gui:
        return "GUI apps are not available over remote SSH"
    if over_ssh and binding is not None and binding.verb is not None:
        eligible = {
            verb
            for _label, verb in actions_for_container(
                groups, name, remote=remote, ssh_policy=ssh_policy
            )
        }
        if binding.verb in eligible:
            try:
                check_dashboard_command(
                    dashboard_action_argv(
                        binding.verb,
                        name,
                        force=binding.verb in ATTACH_VERBS
                        or binding.verb.startswith(APPS_RUN_PREFIX),
                    ),
                    ssh_policy,
                    over_ssh=True,
                )
            except RouteError as exc:
                return str(exc)
    what = f"'{binding.hint}' ({binding.label})" if binding is not None else f"'{token}'"
    return f"{what} is not available for '{name}'"


def parse_key(data: bytes) -> str:
    """Map a raw stdin read to a dashboard key token ('' if unmapped).

    A pure lookup into :data:`KEY_BINDINGS`, so a key cannot be readable
    without also being documented in the help overlay.

    Note the three ways out: ``cancel`` (bare Esc — arrows arrive as
    ``\\x1b[…``) and ``quit`` (``q``) close an open overlay first, while
    ``interrupt`` (Ctrl-C, EOF) always ends the dashboard. Folding them into
    one token would leave Ctrl-C unable to do anything but shut the menu.
    """
    return _KEY_TOKENS.get(data, "")
