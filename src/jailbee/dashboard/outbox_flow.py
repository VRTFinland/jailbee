"""The Outbox manager's controller: load, marks, delete, publish, clear.

Shared by the dashboard box and the standalone `jailbee outbox` app. It holds an
`OutboxState` (from `jailbee.dashboard.outbox`) and turns intents into
`jailbee outbox …` children through an injected `OutboxRunner`: off-thread
quiet runs for local `ls`/`drop`, detached jobs for the plan, the publish and the
clear (they take the lock and may call `gh`), and a terminal hand-off where a
child must ask itself (a PR creation — `pr push` — and publishing over SSH).

Publishing covers PR creations too: a proposal that awaits its PR (or a
pinned source awaiting its push) is published by handing `jailbee pr push`
the terminal. With marks, one question covers manifests and PR creations;
the manifests are published first, then the PR creations one hand-off at a
time, and the first that fails or is cancelled stops the rest.

Safety rules kept here, not in the frontends:

- every mutating child carries `--revision` from the shown listing (or a fresh
  listing after an earlier child drop changed its revision); the CLI refuses
  a proposal that changed since it was shown;
- `--foreign` is passed exactly when the plan said `foreign: true` and the
  warning was on screen; `--force` is never passed;
- container-written plan text goes through `safe_text` before display.

Textual-free; must not import `jailbee.dashboard.tui`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from jailbee.dashboard import outbox as dob
from jailbee.dashboard.accounts import CliResult
from jailbee.dashboard.jobs import JobResult

Intent = Literal[
    "expand",
    "collapse",
    "toggle",
    "mark",
    "delete",
    "publish",
    "clear",
    "preview",
    "raw",
    "refresh",
    "scope",
    "answer",
    "cancel",
    "tab",
    "open",
]

_Preview = Literal["auto", "on", "off", "full"]
_PREVIEW_MODES: dict[str, _Preview] = {"auto": "auto", "on": "on", "off": "off", "full": "full"}

_ASK_AGAIN = "outbox refreshed; ask again"
_LOADING = "reading the outbox…"
LOADING = _LOADING  # the busy line of a listing in flight (not a running publish)
_BAD_PLAN = "unexpected plan output"
_FOREIGN_FALLBACK = "⚠ foreign target: check where this will be published"
# Phrases of the plan's foreign warnings (`pr_outbox.foreign_warning`,
# `outbox.publish.issue_plan_json`); without one, the fallback line is shown.
_FOREIGN_MARKERS = ("foreign", "not bound to container", "not a repository of container")
_STALE_HINT = "use `jailbee outbox apply --force` from a terminal"
_CHANGED = "changed; refreshed"
_PR_CHANGED = "Proposal changed; refresh the outbox before creating a PR"
_NO_ABILITIES = dob.Abilities(False, False, False, False, False, False)

# Stand-ins for the representative argv of an ability when nothing is listed:
# the remote-SSH policy judges the command shape, not the names.
_ANY_PROPOSAL = "pr/proposal.json"
_ANY_REVISION = "0"

_DRAFT_CHOICE = ("Publish as draft review — only you see it until you submit it on GitHub", "draft")
_MARK_PROPOSAL = "publishing works on whole proposals: mark the proposal row"

# A proposal a batch publishes: container, proposal id, revision, foreign.
_BatchTarget = tuple[str, str, str, bool]
# A PR creation a batch hands off: container and the proposal as the question showed it.
_Creation = tuple[str, dob.ProposalItem]


@dataclass(frozen=True)
class _Plan:
    """A publication plan as the question shows it."""

    lines: tuple[str, ...]
    foreign: bool
    draftable: bool


class OutboxRunner(Protocol):
    def permitted(self, argv: Sequence[str]) -> bool: ...

    def run_quiet(self, argv: Sequence[str]) -> CliResult: ...

    def run_quiet_later(
        self, key: str, label: str, argv: Sequence[str], on_done: Callable[[CliResult], None]
    ) -> None:
        """`run_quiet` off the UI thread; ``on_done`` runs on it. ValueError: ``key`` runs."""
        ...

    def start(
        self,
        key: str,
        label: str,
        argv: Sequence[str],
        on_done: Callable[[JobResult], None],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None: ...

    def hand_off(self, argv: Sequence[str]) -> int | None: ...

    def hand_off_diagnostic(self, argv: Sequence[str]) -> tuple[int | None, str | None]: ...

    def changed(self) -> None: ...


def _safe(value: object) -> str:
    from jailbee.outbox.inspect import safe_text

    return safe_text(value if isinstance(value, str) else str(value))


def _lines(*texts: str) -> tuple[str, ...]:
    return tuple(
        line.strip() for text in texts for line in _safe(text).splitlines() if line.strip()
    )


def _keys_of(containers: Iterable[dob.ContainerListing]) -> set[dob.RowKey]:
    keys: set[dob.RowKey] = set()
    for container in containers:
        for proposal in container.proposals:
            keys.add((container.name, proposal.id, None, None))
            for action in proposal.actions:
                keys.add((container.name, proposal.id, action.index, None))
                for comment in action.comments:
                    keys.add((container.name, proposal.id, action.index, comment.index))
    return keys


def _subtree(containers: Iterable[dob.ContainerListing], key: dob.RowKey) -> set[dob.RowKey]:
    """``key`` and every listed row under it, collapsed or not."""
    keys = {key}
    if key[3] is not None:
        return keys
    for container in containers:
        if container.name != key[0]:
            continue
        for proposal in container.proposals:
            if proposal.id != key[1]:
                continue
            for action in proposal.actions:
                if key[2] is not None and action.index != key[2]:
                    continue
                keys.add((container.name, proposal.id, action.index, None))
                for comment in action.comments:
                    keys.add((container.name, proposal.id, action.index, comment.index))
    return keys


def _ancestors(key: dob.RowKey) -> set[dob.RowKey]:
    """The proposal and action rows above ``key``, not ``key`` itself."""
    above: set[dob.RowKey] = set()
    if key[2] is not None:
        above.add((key[0], key[1], None, None))
    if key[3] is not None:
        above.add((key[0], key[1], key[2], None))
    return above


def _covered(keys: Iterable[dob.RowKey]) -> set[dob.RowKey]:
    """``keys`` without those whose ancestor is also chosen: the whole covers its parts."""
    chosen = set(keys)
    return {k for k in chosen if not _ancestors(k) & chosen}


def _find(
    containers: Sequence[dob.ContainerListing], name: str, pid: str
) -> dob.ProposalItem | None:
    for container in containers:
        if container.name == name:
            for proposal in container.proposals:
                if proposal.id == pid:
                    return proposal
    return None


def _action(proposal: dob.ProposalItem, index: int) -> dob.ActionItem | None:
    return next((a for a in proposal.actions if a.index == index), None)


def _fingerprint(
    proposal: dob.ProposalItem, key: dob.RowKey, *, parent_comments: bool = True
) -> tuple[object, ...] | None:
    """What identifies a marked child across a re-list: kind and text, not index alone."""
    if key[2] is None:
        return None
    action = _action(proposal, key[2])
    if action is None:
        return None
    if key[3] is None:
        return ("action", action.kind, action.title, action.body)
    comment = next((c for c in action.comments if c.index == key[3]), None)
    if comment is None:
        return None
    parent = _action_print(action) if parent_comments else (action.kind, action.title, action.body)
    return ("comment", parent, comment.label, comment.text)


def _action_print(action: dob.ActionItem) -> tuple[object, ...]:
    """An action with everything a drop of it deletes: its text and its comments."""
    return (
        action.kind,
        action.title,
        action.body,
        tuple((c.label, c.text) for c in action.comments),
    )


def _scope_was_shown(listed: dob.ProposalItem, fresh: dob.ProposalItem, index: int) -> bool:
    """Whether dropping action ``index`` of ``fresh`` deletes only what the question showed.

    Every action the fresh `delete_scope` pulls in (itself and its dependents,
    with their comments) must match one of the scope shown. The fresh scope may
    be smaller — a marked dependent dropped earlier — but never gain anything.
    """
    shown_action, fresh_action = _action(listed, index), _action(fresh, index)
    if shown_action is None or fresh_action is None:
        return False
    if not shown_action.delete_scope or not fresh_action.delete_scope:
        return False
    if _action_print(fresh_action) != _action_print(shown_action):
        return False
    shown = {
        _action_print(a) for i in shown_action.delete_scope if (a := _action(listed, i)) is not None
    }
    for i in fresh_action.delete_scope:
        dependent = _action(fresh, i)
        if dependent is None or _action_print(dependent) not in shown:
            return False
    return True


def _block_reason(state: dob.OutboxState, key: dob.RowKey) -> str | None:
    """None when ``key`` may be deleted, else why not."""
    found = dob.find_proposal(state, key)
    if found is None:
        return "not listed"
    _, proposal = found
    if key[2] is None:
        return None if proposal.deletable else proposal.delete_block or "cannot be deleted"
    action = _action(proposal, key[2])
    if action is None:
        return "not listed"
    if action.delete_scope is not None:
        return None
    return action.delete_block or "cannot be deleted"


def _unpublishable_reason(proposal: dob.ProposalItem) -> str | None:
    """None when ``proposal`` may be planned for publishing, else today's status line."""
    pid = _safe(proposal.id)
    if proposal.state in ("awaiting-pr", "awaiting-push"):
        # A creatable one is a PR creation, never planned; this one has no scope.
        return f"{pid} awaits its PR: no PR to create from here"
    if not proposal.publishable:
        if proposal.error:
            return f"{pid}: {_safe(proposal.error)}"
        return f"{pid} is {_safe(proposal.state)}: nothing to publish"
    return None


def _creation_lines(name: str, proposal: dob.ProposalItem) -> tuple[str, ...]:
    """What the batch question says a PR creation does."""
    scope = proposal.create_scope or {}
    argv = dob.create_pr_argv(name, proposal.create_scope) or []
    if scope.get("kind") == "submodule":
        what = f"the branch of submodule {_safe(scope.get('path', ''))} in {_safe(name)}"
    elif scope.get("kind") == "source":
        what = f"the pinned source of {_safe(scope.get('manifest', ''))}"
    else:
        what = f"the branch of {_safe(name)}"
    return (
        f"pushes {what}, opens/updates its PR: jailbee {_safe(' '.join(argv))}",
        "runs in the terminal; it may ask its own questions",
    )


def _plan_lines(result: JobResult, pid: str, revision: str) -> _Plan | tuple[str, ...]:
    """The plan a dry run printed, or the status lines saying why there is none."""
    try:
        plan = json.loads(result.stdout)
    except ValueError:
        plan = None
    if not isinstance(plan, dict):
        return (_safe(result.failure_line()) or "could not read the publication plan",)
    if plan.get("schema") != 1 or plan.get("proposal") != pid:
        return (_BAD_PLAN,)
    refusals = [r for r in plan.get("refusals") or () if isinstance(r, dict)]
    if refusals:
        refused = [line for r in refusals for line in _lines(_safe(r.get("reason", "")))]
        if any(r.get("stale") is True for r in refusals):
            refused.append(_STALE_HINT)
        return tuple(refused or ["publication refused"])
    if not _well_formed(plan, revision):
        return (_BAD_PLAN,)
    lines = []
    for action in plan["actions"]:
        line = (
            f"action {_safe(action.get('index'))} {_safe(action.get('kind', ''))} → "
            f"{_safe(action.get('target', ''))}: {_safe(action.get('will', ''))}"
        )
        if action.get("will_draft") is not None:
            line += f" / draft: {_safe(action['will_draft'])}"
        lines.append(line)
    lines.extend(line for note in plan.get("notes") or () for line in _lines(_safe(note)))
    lines.append(
        f"{_safe(plan.get('login', ''))} publishes {_safe(plan.get('total', 0))} action(s)"
    )
    warnings = [line for w in plan["warnings"] for line in _lines(_safe(w))]
    lines.extend(f"⚠ {line}" for line in warnings)
    if plan["foreign"] and not any(
        marker in line.casefold() for line in warnings for marker in _FOREIGN_MARKERS
    ):
        lines.append(_FOREIGN_FALLBACK)
    return _Plan(tuple(lines), plan["foreign"] is True, plan.get("draftable") is True)


def _well_formed(plan: dict[str, object], revision: str) -> bool:
    """A publishable plan for the revision asked about, with every field the question uses."""
    actions, warnings, notes = plan.get("actions"), plan.get("warnings"), plan.get("notes", [])
    total = plan.get("total")
    return (
        plan.get("revision") == revision
        and isinstance(actions, list)
        and bool(actions)
        and all(isinstance(a, dict) for a in actions)
        and isinstance(warnings, list)
        and all(isinstance(w, str) for w in warnings)
        and isinstance(notes, list)
        and isinstance(plan.get("login"), str)
        and isinstance(total, int)
        and not isinstance(total, bool)
        and isinstance(plan.get("foreign"), bool)
        and isinstance(plan.get("draftable"), bool)
    )


class OutboxController:
    """Turns intents into state changes and `jailbee outbox` children.

    Every public method — and every job completion — replaces ``state`` and
    calls ``on_change(state)`` exactly once.
    """

    def __init__(
        self,
        runner: OutboxRunner,
        *,
        title: str,
        container: str | None,
        prefix: str = "",
        standalone: bool,
        on_change: Callable[[dob.OutboxState], None],
        sent_loader: Callable[[], tuple[dob.SentItem, ...]] | None = None,
        sent_only: bool = False,
    ) -> None:
        self.runner = runner
        self.container = container
        self.standalone = standalone
        self.on_change = on_change
        # Reads the Sent records on demand (never from the gatherer); ``sent_only``
        # is the repo timeline: the Sent view alone, no proposals and no runner use.
        self.sent_loader = sent_loader
        self.sent_only = sent_only
        self.state = dob.OutboxState(
            title,
            (),
            prefix=prefix,
            scope_name=container,
            tab="sent" if sent_only else "outbox",
            sent_repo=sent_only,
        )
        # Frontends may retain a busy mutation across close/reopen; plans are disposable.
        self.mutation_running = False
        # Whether a box shows this controller now: a PR creation's terminal hand-off,
        # due when its off-thread re-check returns, must not seize a terminal the
        # user has since moved on from. The standalone app is always shown.
        self.shown: Callable[[], bool] = lambda: True
        self._publish_token: object | None = None
        # The batch being planned; dropped by a reload or a cancel, so late plans do nothing.
        self._batch_token: object | None = None
        # The proposals the batch question showed: exactly these are published.
        self._pending_publish: tuple[_BatchTarget, ...] | None = None
        # The PR creations it showed, handed off after the publishes.
        self._pending_creations: tuple[_Creation, ...] = ()
        self._pending_skipped: tuple[str, ...] = ()
        # The keys asked about and the listing the question showed them from.
        self._pending_delete: (
            tuple[frozenset[dob.RowKey], tuple[dob.ContainerListing, ...]] | None
        ) = None
        # Marks carried over from an earlier box of this container, with the revision
        # each proposal had; checked against the first listing, then forgotten.
        self._restored_revisions: dict[str, str] | None = None
        self._delete_queue: list[
            tuple[frozenset[dob.RowKey], tuple[dob.ContainerListing, ...]]
        ] = []
        self._deleting: set[tuple[str, str]] = set()
        self._delete_stale: set[tuple[str, str]] = set()
        self._delete_running = False
        self._delete_total = 0
        self._delete_attempted = 0
        self._delete_deleted = 0
        self._delete_failed = 0
        self._delete_status: list[str] = []

    def revisions(self) -> dict[str, str]:
        """Proposal id -> revision of the listing shown now (for `restore_marks`)."""
        if self._restored_revisions is not None:
            return self._restored_revisions  # closed again before its first listing arrived
        return {p.id: p.revision for c in self.state.containers for p in c.proposals}

    def restore_marks(self, marks: frozenset[dob.RowKey], revisions: dict[str, str]) -> None:
        """Re-mark ``marks`` once the first listing arrives, minus changed or vanished proposals."""
        self._restored_revisions = revisions
        self.state = replace(self.state, marks=marks)

    def refuse_while_busy(self) -> None:
        """Say that a quit waits for the running mutation (the frontend refused it)."""
        self._status(f"busy: {self.state.busy} — wait for it to finish")

    # -- plumbing -----------------------------------------------------------

    def _set(self, state: dob.OutboxState) -> None:
        if state.busy is None:
            self.mutation_running = False
        self.state = state
        self.on_change(state)

    def _status(self, *lines: str) -> None:
        self._set(replace(self.state, status=tuple(lines)))

    def _ls_argv(self) -> list[str]:
        return dob.outbox_ls_argv(self.container, all_repos=self.state.all_repos)

    def _fetch(self) -> tuple[dob.ContainerListing, ...] | str:
        """The current listing, or the reason it could not be read."""
        return self._parse(self.runner.run_quiet(self._ls_argv()))

    @staticmethod
    def _parse(result: CliResult) -> tuple[dob.ContainerListing, ...] | str:
        # `outbox ls` exits 2 for an unavailable container but still prints the listing.
        try:
            return dob.parse_outbox_listing(result.stdout)
        except dob.OutboxLoadError as exc:
            return f"could not list the outbox: {(not result.ok and result.message) or exc}"

    def _abilities(self, containers: Sequence[dob.ContainerListing]) -> dob.Abilities:
        # Scope: only the app switches it, and only where an all-repos listing may run.
        scope = self.standalone and self.runner.permitted(dob.outbox_ls_argv(None, all_repos=True))
        live = next((c for c in containers if c.error is None), None)
        if live is None:
            return replace(_NO_ABILITIES, scope=scope)
        first = live.proposals[0] if live.proposals else None
        pid = first.id if first else _ANY_PROPOSAL
        rev = first.revision if first else _ANY_REVISION
        ok = self.runner.permitted
        publish = ok(dob.outbox_apply_argv(live.name, pid, rev, yes=True))
        create = dob.create_pr_argv(live.name, {"kind": "repo"})
        return dob.Abilities(
            delete=ok(dob.outbox_drop_argv(live.name, pid, rev)),
            publish=publish,
            publish_hand_off=not publish
            and ok(dob.outbox_apply_argv(live.name, pid, rev, yes=False)),
            create_pr=create is not None and ok(create),
            clear=ok(dob.clear_approved_argv(live.name, "0")),
            scope=scope,
        )

    def _listed(
        self,
        containers: tuple[dob.ContainerListing, ...],
        *,
        cursor: dob.RowKey | None,
        status: tuple[str, ...],
        refresh_metadata: bool = True,
    ) -> dob.OutboxState:
        keys = _keys_of(containers)
        candidates: list[dob.RowKey | None] = [cursor, self.state.cursor]
        for key in (cursor, self.state.cursor):
            if key is not None:
                candidates += [(key[0], key[1], key[2], None), (key[0], key[1], None, None)]
        first = next(((c.name, p.id, None, None) for c in containers for p in c.proposals), None)
        chosen = next((k for k in candidates if k is not None and k in keys), first)
        marks = {k for k in self.state.marks if k in keys}
        if self._restored_revisions is not None:
            now = {p.id: p.revision for c in containers for p in c.proposals}
            marks = {k for k in marks if now.get(k[1]) == self._restored_revisions.get(k[1])}
            self._restored_revisions = None
        # A marked proposal or action also covers the rows listed under it since.
        for key in [k for k in marks if k[3] is None]:
            marks |= _subtree(containers, key)
        # Local row removals reuse metadata; only a real listing re-reads it.
        if refresh_metadata:
            self._delete_stale.clear()
        sent_state = self._fresh_sent() if refresh_metadata else self.state
        # Nothing pending but records sent: open on the Sent tab, unless the user chose.
        empty = not any(c.proposals for c in containers)
        tab = "sent" if sent_state.sent and empty and not sent_state.tab_touched else sent_state.tab
        return replace(
            sent_state,
            tab=tab,
            containers=containers,
            expanded=frozenset(k for k in self.state.expanded if k in keys),
            marks=frozenset(marks),
            cursor=chosen,
            status=status,
            abilities=self._abilities(containers) if refresh_metadata else self.state.abilities,
        )

    def _fresh_sent(self) -> dob.OutboxState:
        """``state`` with the Sent records re-read (unchanged without a loader or on a failure)."""
        if self.sent_loader is None or self.sent_only:
            return self.state
        try:
            items = self.sent_loader()
        except Exception:  # the listing still shows; `r` on the Sent tab says why
            return self.state
        keys = {dob.sent_key(i) for i in items}
        cursor = self.state.sent_cursor
        if cursor not in keys:
            cursor = dob.sent_key(items[0]) if items else None
        return replace(self.state, sent=items, sent_cursor=cursor)

    def _reloaded(
        self, *, cursor: dob.RowKey | None = None, status: tuple[str, ...] = ()
    ) -> tuple[dob.OutboxState, bool]:
        fetched = self._fetch()
        if isinstance(fetched, str):
            return replace(self.state, status=(*status, fetched)), False
        return self._listed(fetched, cursor=cursor, status=status), True

    def _after_mutation(self, status: tuple[str, ...], *, prioritize: bool = False) -> None:
        self.mutation_running = True
        self.state = replace(self.state, busy=self.state.busy or "refreshing the outbox…")

        def done(result: CliResult) -> None:
            self.state = replace(self.state, busy=None)
            fetched = self._parse(result)
            if isinstance(fetched, str):
                lines = (fetched, *status) if prioritize and status else (*status, fetched)
                self._set(replace(self.state, status=lines))
            else:
                self._set(self._listed(fetched, cursor=None, status=status))
            self.runner.changed()

        self._quiet_later(self._ls_argv(), done)

    def _quiet_later(self, argv: list[str], done: Callable[[CliResult], None]) -> None:
        """Run one step off-thread and deliver failures through the same completion path."""
        try:
            self.runner.run_quiet_later(
                f"outbox-step:{id(self)}", self.state.busy or _LOADING, argv, done
            )
        except (OSError, ValueError) as exc:
            done(CliResult(False, str(exc)))
        self.on_change(self.state)

    def _start(
        self,
        busy: str,
        key: str,
        label: str,
        argv: list[str],
        done: Callable[[JobResult], None],
        on_progress: Callable[[int, int], None] | None = None,
        *,
        chained: bool = False,
    ) -> str | None:
        """Start a job; the error text if it did not start, else None.

        A ``chained`` start (a batch step) reports a failure only through the
        return value: the chain goes on with its next target and ends with the
        one ``on_change`` of its normal completion.
        """
        self.state = replace(self.state, busy=busy, question=None, status=())
        try:
            self.runner.start(key, label, argv, done, on_progress=on_progress)
        except (OSError, ValueError) as exc:
            if chained:
                self.state = replace(self.state, busy=None, status=())
            else:
                self._set(replace(self.state, busy=None, status=(str(exc),)))
            return str(exc)
        self.on_change(self.state)
        return None

    # -- public API ---------------------------------------------------------

    def load(self, *, cursor: dob.RowKey | None = None, status: tuple[str, ...] = ()) -> bool:
        """Re-list the outbox; False after a load error (``state.status`` says why).

        An open question was asked about the listing being replaced, so it is
        withdrawn (with its carry and any pending delete): the user asks again.
        The flows' own re-lists between steps do not come through here.
        """
        if self.state.question is not None or self._batch_token is not None:
            self.state = replace(self.state, question=None)
            self._pending_delete = None
            self._pending_publish = None
            self._pending_creations = ()
            self._pending_skipped = ()
            self._batch_token = None
            status = (_ASK_AGAIN, *status)
        state, ok = self._reloaded(cursor=cursor, status=status)
        self._set(state)
        return ok

    def load_sent(self) -> bool:
        """(Re-)read the Sent records into the state; False when the read failed."""
        if self.sent_loader is None:
            return False
        try:
            items = self.sent_loader()
        except Exception as exc:  # a DB problem is a status line, never a crash
            self._set(replace(self.state, status=(f"could not read the Sent log: {exc}",)))
            return False
        keys = {dob.sent_key(i) for i in items}
        cursor = self.state.sent_cursor
        if cursor not in keys:
            cursor = dob.sent_key(items[0]) if items else None
        self._set(replace(self.state, sent=items, sent_cursor=cursor, status=()))
        return True

    def load_later(self, *, cursor: dob.RowKey | None = None) -> None:
        """`load`, with the listing read off the UI thread; the state is busy meanwhile.

        Opening and refreshing read every store in the container through a
        `jailbee` child, which takes a second or two: the box must stay usable.
        While a listing or mutation is on its way this does nothing: that
        child's busy line stays until its completion reloads the outbox.
        """
        if self.sent_only:
            self.load_sent()
            return
        if self.state.busy is not None:
            self.on_change(self.state)  # already on its way; mutations reload when done
            return
        status: tuple[str, ...] = ()
        if self.state.question is not None:
            self._pending_delete = None
            self._pending_publish = None
            self._pending_creations = ()
            self._pending_skipped = ()
            status = (_ASK_AGAIN,)
        self.state = replace(self.state, busy=_LOADING, question=None, status=())
        argv = self._ls_argv()

        def done(result: CliResult) -> None:
            self.state = replace(self.state, busy=None)
            if argv != self._ls_argv():
                # The scope was switched while this was on its way: list that one.
                self.load_later(cursor=cursor)
                return
            fetched = self._parse(result)
            if isinstance(fetched, str):
                self._set(replace(self.state, status=(*status, fetched)))
            else:
                self._set(self._listed(fetched, cursor=cursor, status=status))

        where = self.container or "every container"
        try:
            self.runner.run_quiet_later(
                f"outbox-ls:{id(self)}", f"listing the outbox of {where}…", argv, done
            )
        except ValueError as exc:
            self._set(replace(self.state, busy=None, status=(*status, str(exc))))
            return
        self.on_change(self.state)

    def _sent_intent(self, name: Intent, key: dob.RowKey | None, value: str | None) -> None:
        """Keys in the Sent view: it only scrolls, previews, refreshes and switches tabs."""
        if key is not None and name not in ("answer", "cancel"):
            self.state = replace(self.state, sent_cursor=key)
        if name == "preview":
            toggled: _Preview = "on" if self.state.preview == "off" else "off"
            self._set(replace(self.state, preview=_PREVIEW_MODES.get(value or "", toggled)))
        elif name == "refresh":
            self.load_sent()
        elif name == "tab" and not self.sent_only:
            self._set(replace(self.state, tab="outbox", tab_touched=True, status=()))
        else:
            self._set(self.state)

    def intent(self, name: Intent, key: dob.RowKey | None, value: str | None = None) -> None:
        if self.state.tab == "sent":
            self._sent_intent(name, key, value)
            return
        if name == "tab":
            if self.state.sent:
                self._set(replace(self.state, tab="sent", tab_touched=True, status=()))
            else:
                self._status("no Sent records")
            return
        if key is not None and name not in ("answer", "cancel"):
            self.state = replace(self.state, cursor=key)
        key = key if key is not None else self.state.cursor
        if self._delete_running and name in ("publish", "clear"):
            selected = _covered(self.state.marks) or ({key} if key is not None else set())
            conflicts = [k for k in selected if (k[0], k[1]) in self._deleting]
            if name == "publish" and conflicts:
                self._status(
                    *(
                        f"{_safe(k[1])}: already in the delete queue"
                        for k in sorted(conflicts, key=str)
                    )
                )
            else:
                self._status(f"{name} must wait for the delete queue to finish")
            return
        if self.state.busy and (
            (name in ("delete", "publish", "clear") and not self._delete_running)
            or (name == "scope" and self.state.busy != _LOADING)
        ):
            self._status(f"busy: {self.state.busy}")
            return
        handler = {
            "expand": self._expand,
            "collapse": self._collapse,
            "toggle": self._toggle,
            "mark": self._mark,
            "delete": self._delete,
            "publish": self._publish,
            "clear": self._clear,
        }.get(name)
        if handler is not None:
            handler(key)
        elif name == "preview":
            toggled: _Preview = "on" if self.state.preview == "off" else "off"
            self._set(replace(self.state, preview=_PREVIEW_MODES.get(value or "", toggled)))
        elif name == "raw":
            self._set(replace(self.state, raw=not self.state.raw))
        elif name == "refresh":
            self.load_later(cursor=key)
        elif name == "scope":
            if self.state.abilities.scope:
                self.state = replace(self.state, all_repos=not self.state.all_repos)
            self.load_later(cursor=key)
        elif name == "answer":
            self._answer(value)
        else:
            self._cancel()

    # -- tree ---------------------------------------------------------------

    def _expand(self, key: dob.RowKey | None) -> None:
        if key is None or key[3] is not None:
            self._set(self.state)
            return
        self._set(replace(self.state, expanded=self.state.expanded | {key}))

    def _collapse(self, key: dob.RowKey | None) -> None:
        if key is None:
            self._set(self.state)
            return
        if key not in self.state.expanded and key[2] is not None:
            # A collapsed child: collapse its parent and move there.
            key = (
                (key[0], key[1], key[2], None)
                if key[3] is not None
                else (key[0], key[1], None, None)
            )
        self._set(replace(self.state, expanded=self.state.expanded - {key}, cursor=key))

    def _toggle(self, key: dob.RowKey | None) -> None:
        if key is not None and key in self.state.expanded:
            self._collapse(key)
        else:
            self._expand(key)

    def _mark(self, key: dob.RowKey | None) -> None:
        if key is None:
            self._set(self.state)
            return
        # Marks serve publish too: deletability is checked when `d` is pressed.
        if key not in _keys_of(self.state.containers):
            self._status(f"{_safe(key[1])}: not listed")
            return
        if key in self.state.marks:
            marks = self.state.marks - _subtree(self.state.containers, key) - _ancestors(key)
        else:
            marks = self.state.marks | _subtree(self.state.containers, key)
        self._set(replace(self.state, marks=marks, status=()))

    # -- delete -------------------------------------------------------------

    def _delete(self, key: dob.RowKey | None) -> None:
        if not self.state.abilities.delete:
            self._status("deleting is not available here")
            return
        keys = _covered(self.state.marks) or ({key} if key is not None else set())
        if not keys:
            self._status("Nothing to delete.")
            return
        conflicts = [k for k in keys if (k[0], k[1]) in self._deleting]
        if conflicts:
            self._status(
                *(f"{_safe(k[1])}: already in the delete queue" for k in sorted(conflicts, key=str))
            )
            return
        if any((k[0], k[1]) in self._delete_stale for k in keys):
            self._status(
                *(
                    f"{_safe(k[1])}: refresh required after deletion"
                    for k in sorted(keys, key=str)
                    if (k[0], k[1]) in self._delete_stale
                )
            )
            return
        refused = [(k, r) for k in sorted(keys, key=str) if (r := _block_reason(self.state, k))]
        if refused:
            self._status(*(f"{_safe(k[1])}: {_safe(r)}" for k, r in refused))
            return
        self._pending_delete = (frozenset(keys), self.state.containers)
        self._set(
            replace(
                self.state,
                question=dob.Question(
                    "delete",
                    "Delete from the outbox?",
                    dob.delete_scope_lines(self.state, keys),
                    (("No", "no"), ("Yes, delete", "yes")),
                    default="no",
                ),
                status=(),
            )
        )

    def _delete_busy(self) -> str:
        progress = min(self._delete_attempted + 1, self._delete_total)
        hint = "" if self.standalone else " · Esc leaves; deletion continues"
        return f"Deleting {progress}/{self._delete_total}…{hint}"

    def _run_delete(self) -> None:
        pending, self._pending_delete = self._pending_delete, None
        if pending is None:
            self._set(replace(self.state, question=None))
            return
        keys, shown = pending
        self._delete_queue.append((keys, shown))
        self._deleting.update((k[0], k[1]) for k in keys)
        self._delete_total += len(keys)
        # Consume only the confirmed marks; marks made while deleting belong to the user.
        consumed = set().union(*(_subtree(shown, k) for k in keys)) if keys else set()
        self.state = replace(
            self.state, question=None, marks=self.state.marks - consumed, status=()
        )
        if self._delete_running:
            self._set(replace(self.state, busy=self._delete_busy()))
            return
        self._delete_running = True
        self.mutation_running = True
        self._next_delete()

    def _next_delete(self) -> None:
        if not self._delete_queue:
            # Refresh requests during deletion coalesce into this one final listing.
            self.state = replace(self.state, busy=self._delete_busy())

            def listed(result: CliResult) -> None:
                try:
                    fetched = self._parse(result)
                    if not isinstance(fetched, str):
                        self.state = self._listed(fetched, cursor=None, status=())
                    else:
                        self._delete_status.append(fetched)
                except Exception as exc:
                    self._delete_status.append(f"could not refresh the outbox: {_safe(exc)}")
                    self._delete_failed += max(
                        0, self._delete_total - self._delete_deleted - self._delete_failed
                    )
                    self._delete_queue.clear()
                # A confirmation may have queued more work while the final read ran.
                if self._delete_queue:
                    self._next_delete()
                    return
                if self._delete_status:
                    details = "; ".join(self._delete_status)
                    summary = (
                        f"{self._delete_failed} not deleted ({details}); "
                        f"deleted {self._delete_deleted}"
                        if self._delete_failed
                        else f"{details}; deleted {self._delete_deleted}"
                    )
                else:
                    summary = f"Deleted {self._delete_deleted} from the outbox."
                status = (summary,)
                self._delete_running = False
                self._deleting.clear()
                self._delete_total = self._delete_attempted = 0
                self._delete_deleted = self._delete_failed = 0
                self._delete_status = []
                self._set(replace(self.state, busy=None, status=status))
                self.runner.changed()

            self._quiet_later(self._ls_argv(), listed)
            return
        keys, shown = self._delete_queue.pop(0)
        steps = self._delete_steps(keys, shown)

        def advance(result: CliResult | None = None) -> None:
            try:
                argv = next(steps) if result is None else steps.send(result)
            except StopIteration as finished:
                self._delete_status.extend(finished.value)
                self._next_delete()
                return
            except Exception as exc:
                self._delete_failed += max(
                    0, self._delete_total - self._delete_deleted - self._delete_failed
                )
                self._delete_status.append(f"delete failed: {_safe(exc)}")
                self._deleting.clear()
                self._delete_queue.clear()
                self._next_delete()
                return
            self.state = replace(self.state, busy=self._delete_busy())
            self._quiet_later(argv, advance)

        advance()

    def _deleted_row(self, name: str, listed: dob.ProposalItem, key: dob.RowKey | None) -> None:
        containers = []
        for container in self.state.containers:
            if container.name != name:
                containers.append(container)
                continue
            proposals = []
            for proposal in container.proposals:
                if proposal.id != listed.id:
                    proposals.append(proposal)
                elif key is not None:
                    actions = proposal.actions
                    if key[3] is None:
                        action = _action(listed, key[2] or 0)
                        scope = action.delete_scope if action is not None else ()
                        actions = tuple(a for a in actions if a.index not in (scope or (key[2],)))
                    else:
                        actions = tuple(
                            replace(a, comments=tuple(c for c in a.comments if c.index != key[3]))
                            if a.index == key[2]
                            else a
                            for a in actions
                        )
                    proposals.append(replace(proposal, actions=actions))
            containers.append(replace(container, proposals=tuple(proposals)))
        self.state = self._listed(
            tuple(containers), cursor=None, status=self.state.status, refresh_metadata=False
        )

    def _delete_steps(
        self, keys: frozenset[dob.RowKey], shown: tuple[dob.ContainerListing, ...]
    ) -> Generator[list[str], CliResult, tuple[str, ...]]:
        """Sequence checks and drops; runner completions resume this on the UI thread."""
        status: list[str] = []
        groups: dict[tuple[str, str], list[dob.RowKey]] = {}
        for key in sorted(keys, key=str):
            groups.setdefault((key[0], key[1]), []).append(key)
        for (name, pid), chosen in groups.items():
            # Checked against the listing the question showed, never a later one.
            listed = _find(shown, name, pid)
            if listed is None:
                status.append(f"{_safe(pid)}: {_CHANGED}")
                self._delete_attempted += len(chosen)
                self._delete_failed += len(chosen)
                self._deleting.discard((name, pid))
                continue
            before = self._delete_attempted
            done, failure = yield from self._delete_proposal(name, listed, chosen)
            if done and all(k[2] is not None for k in chosen):
                # Child drops change the revision and shift indices. Refresh this
                # proposal before releasing it for another confirmed delete.
                fetched = self._parse((yield self._ls_argv()))
                if isinstance(fetched, str):
                    self._delete_stale.add((name, pid))
                    self.state = replace(
                        self.state,
                        marks=frozenset(k for k in self.state.marks if k[:2] != (name, pid)),
                    )
                    status.append(fetched)
                else:
                    fresh = _find(fetched, name, pid)
                    previous = _find(self.state.containers, name, pid)
                    candidates = (
                        _keys_of((dob.ContainerListing(name, None, (), (fresh,)),))
                        if fresh is not None
                        else set()
                    )

                    def matching(
                        key: dob.RowKey,
                        fresh: dob.ProposalItem | None = fresh,
                        previous: dob.ProposalItem | None = previous,
                        candidates: set[dob.RowKey] = candidates,
                    ) -> list[dob.RowKey]:
                        if fresh is None or previous is None:
                            return []
                        return [
                            candidate
                            for candidate in candidates
                            if candidate[2] is not None
                            and _fingerprint(fresh, candidate) == _fingerprint(previous, key)
                        ]

                    cursor = self.state.cursor
                    if cursor is not None and cursor[:2] == (name, pid) and cursor[2] is not None:
                        matches = matching(cursor)
                        if len(matches) == 1:
                            cursor = matches[0]
                        else:
                            old_cursor = cursor
                            peers = [
                                k
                                for k in candidates
                                if k[2] is not None and (k[3] is None) == (old_cursor[3] is None)
                            ]
                            cursor = (
                                min(
                                    peers,
                                    key=lambda k: (
                                        abs((k[2] or 0) - (old_cursor[2] or 0)),
                                        abs((k[3] or 0) - (old_cursor[3] or 0)),
                                        k[2] or 0,
                                        k[3] or 0,
                                    ),
                                )
                                if peers
                                else (name, pid, None, None)
                            )
                    marks = set(self.state.marks)
                    for marked in self.state.marks:
                        if marked[:2] != (name, pid) or marked[2] is None:
                            continue
                        marks.discard(marked)
                        if fresh is None or previous is None:
                            continue
                        matches = matching(marked)
                        if len(matches) == 1:
                            marks.add(matches[0])
                    self.state = replace(self.state, marks=frozenset(marks))
                    containers = tuple(
                        replace(
                            c,
                            proposals=tuple(
                                replacement
                                for p in c.proposals
                                if (replacement := fresh if p.id == pid else p) is not None
                            ),
                        )
                        if c.name == name
                        else c
                        for c in self.state.containers
                    )
                    self.state = self._listed(
                        containers, cursor=cursor, status=self.state.status, refresh_metadata=False
                    )
            self._deleting.discard((name, pid))
            # Safety refusals skip this proposal's remaining children too: progress
            # counts handled selections, not only successful CLI calls.
            self._delete_attempted += len(chosen) - (self._delete_attempted - before)
            if failure is not None:
                self._delete_failed += len(chosen) - done
                status.append(f"{_safe(pid)}: {_safe(failure)}")
        return tuple(status)

    def _delete_proposal(
        self, name: str, listed: dob.ProposalItem, chosen: list[dob.RowKey]
    ) -> Generator[list[str], CliResult, tuple[int, str | None]]:
        """Drops for one proposal, highest index first; (drops done, failure or None)."""
        whole = (name, listed.id, None, None) in chosen
        children = [
            k
            for k in chosen
            if k[2] is not None
            and not (k[3] is not None and (name, listed.id, k[2], None) in chosen)
        ]
        children.sort(key=lambda k: (k[2] or 0, -1 if k[3] is None else k[3]), reverse=True)
        expected = {k: _fingerprint(listed, k, parent_comments=False) for k in children}
        steps: list[dob.RowKey | None] = [None] if whole else list(children)
        done = 0
        for step, key in enumerate(steps):
            fresh = listed
            if step:
                # Earlier child drops changed this proposal's revision and may shift
                # indices. Re-read only these steps, preserving the shown scope.
                fetched = self._parse((yield self._ls_argv()))
                if isinstance(fetched, str):
                    return done, fetched
                current = _find(fetched, name, listed.id)
                if current is None:
                    return done, _CHANGED
                fresh = current
                if any(
                    _fingerprint(fresh, k, parent_comments=False) != expected[k]
                    for k in steps[step:]
                    if k is not None
                ):
                    return done, _CHANGED
            if key is None:
                # Archive consent is what the question showed, not a later listing's.
                argv = dob.outbox_drop_argv(
                    name, fresh.id, fresh.revision, archive_journal=listed.delete_archive
                )
            elif key[3] is None:
                action = _action(fresh, key[2] or 0)
                scope = action.delete_scope if action is not None else None
                if not _scope_was_shown(listed, fresh, key[2] or 0):
                    return done, _CHANGED
                argv = dob.outbox_drop_argv(
                    name,
                    fresh.id,
                    fresh.revision,
                    action=key[2],
                    with_dependents=scope is not None and len(scope) > 1,
                )
            else:
                argv = dob.outbox_drop_argv(
                    name, fresh.id, fresh.revision, action=key[2], comment=key[3]
                )
            result = yield argv
            self._delete_attempted += 1
            if not result.ok:
                return done, result.message or "delete failed"
            done += 1
            self._delete_deleted += 1
            self._deleted_row(name, listed, key)
        return done, None

    # -- publish ------------------------------------------------------------

    def _publish(self, key: dob.RowKey | None) -> None:
        if self.state.marks:
            self._publish_marked()
        else:
            self._publish_one(key)

    def _publish_one(self, key: dob.RowKey | None) -> None:
        found = dob.find_proposal(self.state, key)
        if found is None:
            self._status("Nothing to publish here.")
            return
        container, proposal = found
        pid = _safe(proposal.id)
        if proposal.creatable:
            self._create_pr(container.name, proposal, key)
            return
        reason = _unpublishable_reason(proposal)
        if reason is not None:
            self._status(reason)
            return
        name, revision = container.name, proposal.revision
        if self.state.abilities.publish:
            self._start(
                "planning…",
                f"outbox-plan:{name}:{proposal.id}",
                f"planning {pid}…",
                dob.outbox_plan_argv(name, proposal.id, revision),
                lambda result: self._planned(name, proposal.id, revision, result),
            )
        elif self.state.abilities.publish_hand_off:
            # Over SSH `-y` is refused: the interactive apply asks under its own plan.
            rc = self.runner.hand_off(dob.outbox_apply_argv(name, proposal.id, revision, yes=False))
            self._after_mutation(() if rc is not None else ("outbox apply did not run",))
        else:
            self._status("publishing is not available here")

    def _planned(self, name: str, pid: str, revision: str, result: JobResult) -> None:
        self.state = replace(self.state, busy=None)
        plan = _plan_lines(result, pid, revision)
        if not isinstance(plan, _Plan):
            self._status(*plan)
            return
        choices: list[tuple[str, str]] = [("Publish", "publish")]
        if plan.draftable:
            choices.append(_DRAFT_CHOICE)
        choices.append(("Cancel", "cancel"))
        self._set(
            replace(
                self.state,
                question=dob.Question(
                    "publish",
                    f"Publish {_safe(pid)}?",
                    plan.lines,
                    tuple(choices),
                    default="cancel",
                    carry=(name, pid, revision, "foreign" if plan.foreign else ""),
                ),
                status=(),
            )
        )

    def _run_publish(self, question: dob.Question, value: str) -> None:
        name, pid, revision, foreign = question.carry
        self.mutation_running = True
        argv = dob.outbox_apply_argv(
            name, pid, revision, yes=True, draft=value == "draft", foreign=foreign == "foreign"
        )

        token = object()
        self._publish_token = token
        current_busy = "publishing…"

        def progress(n: int, total: int) -> None:
            nonlocal current_busy
            if self._publish_token is not token or self.state.busy != current_busy:
                return
            current_busy = f"publishing {_safe(pid)} {n}/{total}…"
            self._set(replace(self.state, busy=current_busy))

        def done(result: JobResult) -> None:
            if self._publish_token is token:
                self._publish_token = None
            self.state = replace(self.state, busy=None)
            self._after_mutation(_lines(result.stdout, result.stderr) or ("published",))

        self._start(
            current_busy,
            f"outbox-apply:{name}:{pid}",
            f"publishing {_safe(pid)}…",
            argv,
            done,
            on_progress=progress,
        )

    # -- publish the marked proposals ---------------------------------------

    def _publish_marked(self) -> None:
        """Plan every marked proposal one at a time, then ask once about them all."""
        targets = [
            (container, proposal)
            for container in self.state.containers
            for proposal in container.proposals
            if (container.name, proposal.id, None, None) in self.state.marks
        ]
        if not targets:
            self._status(_MARK_PROPOSAL)
            return
        abilities = self.state.abilities
        if not (abilities.publish or abilities.publish_hand_off or abilities.create_pr):
            self._status("publishing is not available here")
            return
        skipped: list[str] = []
        chosen: list[_BatchTarget] = []
        creations: list[_Creation] = []
        for container, proposal in targets:
            pid = _safe(proposal.id)
            if proposal.creatable:
                if abilities.create_pr:
                    creations.append((container.name, proposal))
                else:
                    skipped.append(f"⚠ {pid}: creating a PR is not available here")
                continue
            reason = _unpublishable_reason(proposal)
            if reason is not None:
                skipped.append(f"⚠ {reason}")
            elif not (abilities.publish or abilities.publish_hand_off):
                skipped.append(f"⚠ {pid}: publishing is not available here")
            else:
                chosen.append((container.name, proposal.id, proposal.revision, False))
        if not chosen and not creations:
            self._status(*(line.removeprefix("⚠ ") for line in skipped))
            return
        if abilities.publish or not chosen:
            self._plan_batch(tuple(chosen), skipped, tuple(creations))
        else:
            self._hand_off_batch(tuple(chosen), skipped, tuple(creations))

    def _hand_off_batch(
        self,
        targets: tuple[_BatchTarget, ...],
        skipped: list[str],
        creations: tuple[_Creation, ...],
    ) -> None:
        # Over SSH `-y` is refused: each interactive apply asks under its own plan.
        # The PR creations have no plan of their own, so one question covers them.
        if creations:
            self._ask_ssh_creations(targets, skipped, creations)
            return
        self._run_hand_off_batch(targets, [line.removeprefix("⚠ ") for line in skipped], ())

    def _ask_ssh_creations(
        self,
        targets: tuple[_BatchTarget, ...],
        skipped: list[str],
        creations: tuple[_Creation, ...],
    ) -> None:
        many = len(self.state.containers) > 1
        lines: list[str] = []
        for name, proposal in creations:
            pid = _safe(proposal.id)
            lines.append(f"{_safe(name)}: {pid}" if many else pid)
            lines.extend(_creation_lines(name, proposal))
        if targets:
            lines.append(
                "then, one by one, outbox apply for: "
                + ", ".join(_safe(pid) for _, pid, _, _ in targets)
                + " (each asks under its own plan; PR creations run only if all succeed)"
            )
            # Creations run after the hand-offs, so a refusal there must stop them.
            lines.insert(0, "manifests are handed to the terminal first")
        lines.extend(skipped)
        self._pending_skipped = tuple(skipped)
        self._pending_publish = targets
        self._pending_creations = creations
        self._set(
            replace(
                self.state,
                question=dob.Question(
                    "publish-batch-ssh",
                    f"Publish {len(targets) + len(creations)} proposals?",
                    tuple(lines),
                    (("Publish", "publish"), ("Cancel", "cancel")),
                    default="cancel",
                ),
                status=(),
            )
        )

    def _run_ssh_batch(self) -> None:
        skipped, self._pending_skipped = self._pending_skipped, ()
        targets, self._pending_publish = self._pending_publish or (), None
        creations, self._pending_creations = self._pending_creations, ()
        self.state = replace(self.state, question=None)
        self._run_hand_off_batch(targets, [line.removeprefix("⚠ ") for line in skipped], creations)

    def _run_hand_off_batch(
        self,
        targets: tuple[_BatchTarget, ...],
        status: list[str],
        creations: tuple[_Creation, ...],
    ) -> None:
        failed = False
        for name, pid, revision, _ in targets:
            rc = self.runner.hand_off(dob.outbox_apply_argv(name, pid, revision, yes=False))
            if rc is None:
                status.append(f"{_safe(pid)}: outbox apply did not run")
                failed = True
            elif rc != 0:
                status.append(f"{_safe(pid)}: outbox apply exited {rc}")
                failed = True
        marks = self._unmarked(targets)

        def finish(created: tuple[_Creation, ...], lines: tuple[str, ...]) -> None:
            self.state = replace(self.state, marks=marks - self._creation_keys(created))
            self._after_mutation((*status, *lines))

        if failed and creations:
            status.append("PR creations not run: " + ", ".join(_safe(p.id) for _, p in creations))
            finish((), ())
        else:
            self._run_creations(creations, finish)

    def _unmarked(self, targets: Iterable[_BatchTarget]) -> frozenset[dob.RowKey]:
        marks = set(self.state.marks)
        for name, pid, _, _ in targets:
            marks -= _subtree(self.state.containers, (name, pid, None, None))
        return frozenset(marks)

    def _creation_keys(self, creations: Iterable[_Creation]) -> frozenset[dob.RowKey]:
        keys: set[dob.RowKey] = set()
        for name, proposal in creations:
            keys |= _subtree(self.state.containers, (name, proposal.id, None, None))
        return frozenset(keys)

    def _run_creations(
        self,
        creations: tuple[_Creation, ...],
        done: Callable[[tuple[_Creation, ...], tuple[str, ...]], None],
    ) -> None:
        """Verify off-thread, then hand off each PR creation; the first failure stops the rest."""
        finished: list[_Creation] = []
        status: list[str] = []

        def advance(i: int) -> None:
            if i == len(creations):
                done(tuple(finished), tuple(status))
                return
            name, shown = creations[i]
            pid = _safe(shown.id)

            def checked(handed: bool, ok: bool, lines: tuple[str, ...]) -> None:
                if handed and ok:
                    finished.append((name, shown))
                    status.extend(f"{pid}: {line}" for line in lines or (self._finished(shown),))
                    advance(i + 1)
                    return
                status.extend(f"{pid}: {line}" for line in lines)
                rest = [_safe(p.id) for _, p in creations[i + 1 :]]
                if rest:
                    status.append(f"stopped; not run: {', '.join(rest)}")
                done(tuple(finished), tuple(status))

            self._hand_off_creation(name, shown, None, checked)

        advance(0)

    @staticmethod
    def _finished(proposal: dob.ProposalItem) -> str:
        return f"{'source' if proposal.state == 'awaiting-push' else 'pr'} push finished"

    def _plan_batch(
        self,
        targets: tuple[_BatchTarget, ...],
        excluded: list[str],
        creations: tuple[_Creation, ...] = (),
    ) -> None:
        if not targets:
            self._ask_batch([], excluded, creations)
            return
        token = object()
        self._batch_token = token
        planned: list[tuple[_BatchTarget, _Plan]] = []
        total = len(targets)

        def plan(i: int) -> None:
            name, pid, revision, _ = targets[i]

            def done(result: JobResult) -> None:
                self.state = replace(self.state, busy=None)
                if self._batch_token is not token:
                    self._set(self.state)  # withdrawn by a reload or a cancel
                    return
                parsed = _plan_lines(result, pid, revision)
                if isinstance(parsed, _Plan):
                    planned.append(((name, pid, revision, parsed.foreign), parsed))
                else:
                    excluded.extend(f"⚠ {_safe(pid)}: {line}" for line in parsed)
                if i + 1 < total:
                    plan(i + 1)
                else:
                    self._batch_token = None
                    self._ask_batch(planned, excluded, creations)

            error = self._start(
                f"planning {i + 1}/{total}…",
                f"outbox-plan:{name}:{pid}",
                f"planning {_safe(pid)}…",
                dob.outbox_plan_argv(name, pid, revision),
                done,
                chained=True,
            )
            if error is not None:  # the job did not start: this target is excluded
                excluded.append(f"⚠ {_safe(pid)}: {error}")
                if i + 1 < total:
                    plan(i + 1)
                else:
                    self._batch_token = None
                    self._ask_batch(planned, excluded, creations)

        plan(0)

    def _ask_batch(
        self,
        planned: list[tuple[_BatchTarget, _Plan]],
        excluded: list[str],
        creations: tuple[_Creation, ...] = (),
    ) -> None:
        if not planned and not creations:
            self._status(*(line.removeprefix("⚠ ") for line in excluded))
            return
        many = len(self.state.containers) > 1
        lines: list[str] = []
        for (name, pid, _, _), plan in planned:
            lines.append(f"{_safe(name)}: {_safe(pid)}" if many else _safe(pid))
            lines.extend(plan.lines)
        for name, proposal in creations:
            pid = _safe(proposal.id)
            lines.append(f"{_safe(name)}: {pid}" if many else pid)
            lines.extend(_creation_lines(name, proposal))
        lines.extend(excluded)
        choices: list[tuple[str, str]] = [("Publish", "publish")]
        if planned and all(plan.draftable for _, plan in planned):
            label, value = _DRAFT_CHOICE
            # A draft review is a manifest's: the PR creations publish as they always do.
            choices.append((f"{label} (PR creations as usual)" if creations else label, value))
        choices.append(("Cancel", "cancel"))
        self._pending_publish = tuple(target for target, _ in planned)
        self._pending_creations = creations
        count = len(planned) + len(creations)
        self._set(
            replace(
                self.state,
                question=dob.Question(
                    "publish-batch",
                    f"Publish {count} proposal{'s' if count > 1 else ''}?",
                    tuple(lines),
                    tuple(choices),
                    default="cancel",
                ),
                status=(),
            )
        )

    def _run_publish_batch(self, value: str) -> None:
        targets, self._pending_publish = self._pending_publish or (), None
        creations, self._pending_creations = self._pending_creations, ()
        if not targets and not creations:
            self._set(replace(self.state, question=None, status=("Nothing published.",)))
            return
        results: list[str] = []
        self.mutation_running = True

        def finish() -> None:
            # The non-interactive publishes are done: now each PR creation in turn.
            marks = self._unmarked(targets)

            def created_done(created: tuple[_Creation, ...], lines: tuple[str, ...]) -> None:
                self.state = replace(self.state, marks=marks - self._creation_keys(created))
                self._after_mutation((*results, *lines))

            self._run_creations(creations, created_done)

        if not targets:
            self.state = replace(self.state, question=None)
            finish()
            return
        token = object()
        self._publish_token = token
        total = len(targets)

        def apply(k: int) -> None:
            name, pid, revision, foreign = targets[k]
            argv = dob.outbox_apply_argv(
                name, pid, revision, yes=True, draft=value == "draft", foreign=foreign
            )
            current_busy = f"publishing {k + 1}/{total} {_safe(pid)}…"

            def progress(n: int, of: int) -> None:
                nonlocal current_busy
                if self._publish_token is not token or self.state.busy != current_busy:
                    return
                current_busy = f"publishing {k + 1}/{total} {_safe(pid)} {n}/{of}…"
                self._set(replace(self.state, busy=current_busy))

            def advance() -> None:
                if k + 1 < total:
                    apply(k + 1)
                    return
                if self._publish_token is token:
                    self._publish_token = None
                finish()

            def done(result: JobResult) -> None:
                self.state = replace(self.state, busy=None)
                summary = _lines(result.stdout, result.stderr) or ("published",)
                results.extend(f"{_safe(pid)}: {line}" for line in summary)
                advance()

            error = self._start(
                current_busy,
                f"outbox-apply:{name}:{pid}",
                f"publishing {_safe(pid)}…",
                argv,
                done,
                on_progress=progress,
                chained=True,
            )
            if error is not None:  # the job did not start: it gets its result line
                results.append(f"{_safe(pid)}: {error}")
                advance()

        apply(0)

    # -- create PR (published with `p`) ------------------------------------

    def _create_pr(self, name: str, listed: dob.ProposalItem, key: dob.RowKey | None) -> None:
        """Publish one PR creation: hand `pr push` the terminal, no question of ours."""
        if not self.state.abilities.create_pr:
            self._status("creating a PR is not available here")
            return

        def done(handed: bool, ok: bool, lines: tuple[str, ...]) -> None:
            if handed:
                self._after_mutation(lines, prioritize=True)
            else:
                self._set(replace(self.state, busy=None, status=lines))

        self._hand_off_creation(name, listed, key, done)

    def _hand_off_creation(
        self,
        name: str,
        listed: dob.ProposalItem,
        cursor: dob.RowKey | None,
        done: Callable[[bool, bool, tuple[str, ...]], None],
    ) -> None:
        """Re-check off-thread, then hand off on the UI thread (where the terminal lives)."""
        self.mutation_running = True
        self.state = replace(self.state, busy="checking the proposal…", question=None, status=())

        def checked(result: CliResult) -> None:
            fetched = self._parse(result)
            if isinstance(fetched, str):
                done(False, False, (fetched, _PR_CHANGED))
                return
            self.state = self._listed(fetched, cursor=cursor, status=())
            fresh = _find(fetched, name, listed.id)
            same = fresh is not None and (fresh.revision, fresh.state, fresh.create_scope) == (
                listed.revision,
                listed.state,
                listed.create_scope,
            )
            argv = dob.create_pr_argv(name, listed.create_scope) if same else None
            if argv is None:
                done(False, False, (_PR_CHANGED,))
                return
            if not self.runner.permitted(argv):
                # Checked per container: abilities were judged on the first one listed.
                done(False, False, (f"{_safe(name)}: creating a PR is not available here",))
                return
            what = "source push" if listed.state == "awaiting-push" else "pr push"
            if not self.shown():
                done(False, False, (f"{what} not run: outbox box closed",))
                return
            rc, diagnostic = self.runner.hand_off_diagnostic(argv)
            lines = (
                ()
                if rc == 0
                else (
                    diagnostic or (f"{what} did not run" if rc is None else f"{what} exited {rc}"),
                )
            )
            done(True, rc == 0, lines)

        self._quiet_later(self._ls_argv(), checked)

    # -- clear --------------------------------------------------------------

    def _clear(self, key: dob.RowKey | None) -> None:
        live = [c.name for c in self.state.containers if c.error is None]
        name = key[0] if key is not None else (live[0] if len(live) == 1 else None)
        if name is None or name not in live:
            self._status("Choose an available container to clear.")
            return
        if not self.state.abilities.clear:
            self._status("clearing is not available here")
            return
        self._start(
            "reading the outbox…",
            f"outbox-clear-prepare:{name}",
            f"reading outbox {name}…",
            dob.clear_prepare_argv(name),
            lambda result: self._clear_prepared(name, result),
        )

    def _clear_prepared(self, name: str, result: JobResult) -> None:
        from jailbee.outbox.clear_snapshot import decode_snapshot
        from jailbee.outbox.delete import clear_plan_lines
        from jailbee.outbox.models import OutboxError

        self.state = replace(self.state, busy=None)
        if result.returncode:
            self._status(_safe(result.failure_line()) or "snapshot failed")
            return
        token = result.stdout.strip()
        try:
            plans = decode_snapshot(token)
            if tuple(p.name for p in plans) != (name,):
                raise OutboxError("snapshot target mismatch")
        except OutboxError as exc:
            self._status(f"{_safe(name)}: {_safe(str(exc))}")
            return
        lines = tuple(_safe(line) for line in clear_plan_lines(plans))
        count = sum(len(p.delete) for p in plans)
        if not count:
            self._status(*lines)
            return
        self._set(
            replace(
                self.state,
                question=dob.Question(
                    "clear",
                    f"Clear the outbox of {_safe(name)}?",
                    lines,
                    (("No", "no"), (f"Yes, clear {count}", "yes")),
                    default="no",
                    carry=(name, token),
                ),
                status=(),
            )
        )

    def _run_clear(self, question: dob.Question) -> None:
        name, token = question.carry
        self.mutation_running = True

        def done(result: JobResult) -> None:
            self.state = replace(self.state, busy=None)
            last = result.failure_line() or next(
                (line.strip() for line in reversed(result.stdout.splitlines()) if line.strip()), ""
            )
            self._after_mutation((_safe(last),) if last else ())

        self._start(
            "clearing…",
            f"outbox-clear:{name}",
            f"clearing outbox {name}…",
            dob.clear_approved_argv(name, token),
            done,
        )

    # -- questions ----------------------------------------------------------

    def _answer(self, value: str | None) -> None:
        question = self.state.question
        if question is None:
            self._set(self.state)
            return
        if question.purpose == "delete" and value == "yes":
            self._run_delete()
        elif question.purpose == "publish" and value in ("publish", "draft"):
            self._run_publish(question, value)
        elif question.purpose == "publish-batch" and value in ("publish", "draft"):
            self._run_publish_batch(value)
        elif question.purpose == "publish-batch-ssh" and value == "publish":
            self._run_ssh_batch()
        elif question.purpose == "clear" and value == "yes":
            self._run_clear(question)
        else:
            self._cancel()

    def _cancel(self) -> None:
        question = self.state.question
        self._pending_delete = None
        self._pending_publish = None
        self._pending_creations = ()
        self._pending_skipped = ()
        self._batch_token = None
        message = {
            "publish": "Nothing published.",
            "publish-batch": "Nothing published.",
            "publish-batch-ssh": "Nothing published.",
            "delete": "Nothing deleted.",
            "clear": "Nothing cleared.",
        }.get(question.purpose if question else "")
        self._set(replace(self.state, question=None, status=(message,) if message else ()))
