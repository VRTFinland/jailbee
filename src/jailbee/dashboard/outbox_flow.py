"""The Outbox manager's controller: load, marks, delete, publish, create PR, clear.

Shared by the dashboard box and the standalone `jailbee outbox` app. It holds an
`OutboxState` (from `jailbee.dashboard.outbox`) and turns intents into
`jailbee outbox …` children through an injected `OutboxRunner`: quiet runs for
the short local `ls`/`drop`, detached jobs for the plan, the publish and the
clear (they take the lock and may call `gh`), and a terminal hand-off where a
child must ask itself (Create PR, publishing over SSH).

Safety rules kept here, not in the frontends:

- every mutating child carries `--revision` from a listing read just before it,
  and a proposal that changed since it was shown is left alone ("changed;
  refreshed");
- `--foreign` is passed exactly when the plan said `foreign: true` and the
  warning was on screen; `--force` is never passed;
- container-written plan text goes through `safe_text` before display.

Textual-free; must not import `jailbee.dashboard.tui`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
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
    "create-pr",
    "clear",
    "preview",
    "raw",
    "refresh",
    "scope",
    "answer",
    "cancel",
]

_Preview = Literal["auto", "on", "off"]
_PREVIEW_MODES: dict[str, _Preview] = {"auto": "auto", "on": "on", "off": "off"}

_STALE_HINT ="use `jailbee outbox apply --force` from a terminal"
_CHANGED = "changed; refreshed"
_PR_CHANGED = "Proposal changed; refresh the outbox before creating a PR"
_NO_ABILITIES = dob.Abilities(False, False, False, False, False, False)

# Stand-ins for the representative argv of an ability when nothing is listed:
# the remote-SSH policy judges the command shape, not the names.
_ANY_PROPOSAL = "pr/proposal.json"
_ANY_REVISION = "0"


class OutboxRunner(Protocol):
    def permitted(self, argv: Sequence[str]) -> bool: ...

    def run_quiet(self, argv: Sequence[str]) -> CliResult: ...

    def start(
        self, key: str, label: str, argv: Sequence[str], on_done: Callable[[JobResult], None]
    ) -> None: ...

    def hand_off(self, argv: Sequence[str]) -> int | None: ...

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


def _fingerprint(proposal: dob.ProposalItem, key: dob.RowKey) -> tuple[object, ...] | None:
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
    return ("comment", action.kind, comment.label, comment.text)


def _block_reason(state: dob.OutboxState, key: dob.RowKey) -> str | None:
    """None when ``key`` may be marked for deletion, else why not."""
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
    ) -> None:
        self.runner = runner
        self.container = container
        self.standalone = standalone
        self.on_change = on_change
        self.state = dob.OutboxState(title, (), prefix=prefix, scope_name=container)
        self._pending_delete: frozenset[dob.RowKey] = frozenset()

    # -- plumbing -----------------------------------------------------------

    def _set(self, state: dob.OutboxState) -> None:
        self.state = state
        self.on_change(state)

    def _status(self, *lines: str) -> None:
        self._set(replace(self.state, status=tuple(lines)))

    def _fetch(self) -> tuple[dob.ContainerListing, ...] | str:
        """The current listing, or the reason it could not be read."""
        result = self.runner.run_quiet(
            dob.outbox_ls_argv(self.container, all_repos=self.state.all_repos)
        )
        # `outbox ls` exits 2 for an unavailable container but still prints the listing.
        try:
            return dob.parse_outbox_listing(result.stdout)
        except dob.OutboxLoadError as exc:
            return f"could not list the outbox: {(not result.ok and result.message) or exc}"

    def _abilities(self, containers: Sequence[dob.ContainerListing]) -> dob.Abilities:
        live = next((c for c in containers if c.error is None), None)
        if live is None:
            return replace(_NO_ABILITIES, scope=self.standalone)
        first = live.proposals[0] if live.proposals else None
        pid = first.id if first else _ANY_PROPOSAL
        rev = first.revision if first else _ANY_REVISION
        ok = self.runner.permitted
        publish = ok(dob.outbox_apply_argv(live.name, pid, rev, yes=True))
        create = dob.create_pr_argv(live.name, {"kind": "repo"})
        return dob.Abilities(
            delete=ok(dob.outbox_drop_argv(live.name, pid, rev)),
            publish=publish,
            publish_hand_off=not publish and ok(dob.outbox_apply_argv(live.name, pid, rev, yes=False)),
            create_pr=create is not None and ok(create),
            clear=ok(dob.clear_approved_argv(live.name, "0")),
            scope=self.standalone,
        )

    def _listed(
        self,
        containers: tuple[dob.ContainerListing, ...],
        *,
        cursor: dob.RowKey | None,
        status: tuple[str, ...],
    ) -> dob.OutboxState:
        keys = _keys_of(containers)
        candidates: list[dob.RowKey | None] = [cursor, self.state.cursor]
        for key in (cursor, self.state.cursor):
            if key is not None:
                candidates += [(key[0], key[1], key[2], None), (key[0], key[1], None, None)]
        first = next(
            ((c.name, p.id, None, None) for c in containers for p in c.proposals), None
        )
        chosen = next((k for k in candidates if k is not None and k in keys), first)
        return replace(
            self.state,
            containers=containers,
            expanded=frozenset(k for k in self.state.expanded if k in keys),
            marks=frozenset(k for k in self.state.marks if k in keys),
            cursor=chosen,
            status=status,
            abilities=self._abilities(containers),
        )

    def _reloaded(
        self, *, cursor: dob.RowKey | None = None, status: tuple[str, ...] = ()
    ) -> tuple[dob.OutboxState, bool]:
        fetched = self._fetch()
        if isinstance(fetched, str):
            return replace(self.state, status=(*status, fetched)), False
        return self._listed(fetched, cursor=cursor, status=status), True

    def _after_mutation(self, status: tuple[str, ...]) -> None:
        state, _ = self._reloaded(status=status)
        self._set(state)
        self.runner.changed()

    def _start(
        self, busy: str, key: str, label: str, argv: list[str], done: Callable[[JobResult], None]
    ) -> None:
        self._set(replace(self.state, busy=busy, question=None, status=()))
        try:
            self.runner.start(key, label, argv, done)
        except (OSError, ValueError) as exc:
            self._set(replace(self.state, busy=None, status=(str(exc),)))

    # -- public API ---------------------------------------------------------

    def load(self, *, cursor: dob.RowKey | None = None, status: tuple[str, ...] = ()) -> bool:
        """Re-list the outbox; False after a load error (``state.status`` says why)."""
        state, ok = self._reloaded(cursor=cursor, status=status)
        self._set(state)
        return ok

    def intent(self, name: Intent, key: dob.RowKey | None, value: str | None = None) -> None:
        if key is not None and name not in ("answer", "cancel"):
            self.state = replace(self.state, cursor=key)
        key = key if key is not None else self.state.cursor
        if name in ("delete", "publish", "create-pr", "clear") and self.state.busy:
            self._status(f"busy: {self.state.busy}")
            return
        handler = {
            "expand": self._expand,
            "collapse": self._collapse,
            "toggle": self._toggle,
            "mark": self._mark,
            "delete": self._delete,
            "publish": self._publish,
            "create-pr": self._create_pr,
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
            self.state = replace(self.state, question=None)
            self.load(cursor=key)
        elif name == "scope":
            if self.state.abilities.scope:
                self.state = replace(self.state, all_repos=not self.state.all_repos, question=None)
            self.load(cursor=key)
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
            key = (key[0], key[1], key[2], None) if key[3] is not None else (key[0], key[1], None, None)
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
        reason = _block_reason(self.state, key)
        if reason is not None:
            self._status(f"{_safe(key[1])}: {_safe(reason)}")
            return
        self._set(replace(self.state, marks=self.state.marks ^ {key}, status=()))

    # -- delete -------------------------------------------------------------

    def _delete(self, key: dob.RowKey | None) -> None:
        if not self.state.abilities.delete:
            self._status("deleting is not available here")
            return
        keys = set(self.state.marks) or ({key} if key is not None else set())
        if not keys:
            self._status("Nothing to delete.")
            return
        refused = [(k, r) for k in sorted(keys, key=str) if (r := _block_reason(self.state, k))]
        if refused:
            self._status(*(f"{_safe(k[1])}: {_safe(r)}" for k, r in refused))
            return
        self._pending_delete = frozenset(keys)
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

    def _run_delete(self) -> None:
        keys, self._pending_delete = self._pending_delete, frozenset()
        shown = self.state
        status: list[str] = []
        deleted = 0
        groups: dict[tuple[str, str], list[dob.RowKey]] = {}
        for container in shown.containers:
            for proposal in container.proposals:
                chosen = [k for k in keys if k[:2] == (container.name, proposal.id)]
                if chosen:
                    groups[(container.name, proposal.id)] = chosen
        for (name, pid), chosen in groups.items():
            listed = _find(shown.containers, name, pid)
            if listed is None:
                continue
            done, failure = self._delete_proposal(name, listed, chosen)
            deleted += done
            if failure is not None:
                status.append(f"{_safe(pid)}: {_safe(failure)}")
        if deleted and not status:
            status.append(f"Deleted {deleted} from the outbox.")
        self.state = replace(self.state, marks=frozenset(), question=None)
        self._after_mutation(tuple(status))

    def _delete_proposal(
        self, name: str, listed: dob.ProposalItem, chosen: list[dob.RowKey]
    ) -> tuple[int, str | None]:
        """Drops for one proposal, highest index first; (drops done, failure or None)."""
        whole = (name, listed.id, None, None) in chosen
        children = [
            k
            for k in chosen
            if k[2] is not None and not (k[3] is not None and (name, listed.id, k[2], None) in chosen)
        ]
        children.sort(key=lambda k: (k[2] or 0, -1 if k[3] is None else k[3]), reverse=True)
        expected = {k: _fingerprint(listed, k) for k in children}
        steps: list[dob.RowKey | None] = [None] if whole else list(children)
        done = 0
        for step, key in enumerate(steps):
            fetched = self._fetch()
            if isinstance(fetched, str):
                return done, fetched
            fresh = _find(fetched, name, listed.id)
            if fresh is None:
                return done, _CHANGED
            if step == 0:
                # Nothing is done to a proposal that changed since it was shown.
                if fresh.revision != listed.revision:
                    return done, _CHANGED
            elif any(_fingerprint(fresh, k) != expected[k] for k in steps[step:] if k is not None):
                return done, _CHANGED
            if key is None:
                argv = dob.outbox_drop_argv(
                    name, fresh.id, fresh.revision, archive_journal=fresh.delete_archive
                )
            elif key[3] is None:
                action = _action(fresh, key[2] or 0)
                scope = action.delete_scope if action is not None else None
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
            result = self.runner.run_quiet(argv)
            if not result.ok:
                return done, result.message or "delete failed"
            done += 1
        return done, None

    # -- publish ------------------------------------------------------------

    def _publish(self, key: dob.RowKey | None) -> None:
        found = dob.find_proposal(self.state, key)
        if found is None:
            self._status("Nothing to publish here.")
            return
        container, proposal = found
        pid = _safe(proposal.id)
        if proposal.state == "awaiting-pr":
            self._status(f"{pid} awaits its PR: press c to create it")
            return
        if not proposal.publishable:
            self._status(f"{pid}: {_safe(proposal.error)}" if proposal.error else f"{pid} is {_safe(proposal.state)}: nothing to publish")
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
        try:
            plan = json.loads(result.stdout)
        except ValueError:
            plan = None
        if not isinstance(plan, dict):
            self._status(_safe(result.failure_line()) or "could not read the publication plan")
            return
        refusals = [r for r in plan.get("refusals") or () if isinstance(r, dict)]
        if refusals:
            lines = [line for r in refusals for line in _lines(_safe(r.get("reason", "")))]
            if any(r.get("stale") is True for r in refusals):
                lines.append(_STALE_HINT)
            self._status(*(lines or ["publication refused"]))
            return
        lines = []
        for action in plan.get("actions") or ():
            if not isinstance(action, dict):
                continue
            line = (
                f"action {_safe(action.get('index'))} {_safe(action.get('kind', ''))} → "
                f"{_safe(action.get('target', ''))}: {_safe(action.get('will', ''))}"
            )
            if action.get("will_draft") is not None:
                line += f" / draft: {_safe(action['will_draft'])}"
            lines.append(line)
        lines.extend(line for note in plan.get("notes") or () for line in _lines(_safe(note)))
        lines.append(f"{_safe(plan.get('login', ''))} publishes {_safe(plan.get('total', 0))} action(s)")
        lines.extend(f"⚠ {line}" for w in plan.get("warnings") or () for line in _lines(_safe(w)))
        choices: list[tuple[str, str]] = [("Publish", "publish")]
        if plan.get("draftable") is True:
            choices.append(
                ("Publish as draft review — only you see it until you submit it on GitHub", "draft")
            )
        choices.append(("Cancel", "cancel"))
        foreign = "foreign" if plan.get("foreign") is True else ""
        self._set(
            replace(
                self.state,
                question=dob.Question(
                    "publish",
                    f"Publish {_safe(pid)}?",
                    tuple(lines),
                    tuple(choices),
                    default="cancel",
                    carry=(name, pid, revision, foreign),
                ),
                status=(),
            )
        )

    def _run_publish(self, question: dob.Question, value: str) -> None:
        name, pid, revision, foreign = question.carry
        argv = dob.outbox_apply_argv(
            name, pid, revision, yes=True, draft=value == "draft", foreign=foreign == "foreign"
        )

        def done(result: JobResult) -> None:
            self.state = replace(self.state, busy=None)
            self._after_mutation(_lines(result.stdout, result.stderr) or ("published",))

        self._start("publishing…", f"outbox-apply:{name}:{pid}", f"publishing {_safe(pid)}…", argv, done)

    # -- create PR ----------------------------------------------------------

    def _create_pr(self, key: dob.RowKey | None) -> None:
        found = dob.find_proposal(self.state, key)
        if found is None:
            self._status("Nothing to create a PR for here.")
            return
        container, listed = found
        if not self.state.abilities.create_pr:
            self._status("creating a PR is not available here")
            return
        if not listed.creatable:
            self._status(f"{_safe(listed.id)}: no PR to create")
            return
        state, ok = self._reloaded(cursor=key)
        fresh = _find(state.containers, container.name, listed.id) if ok else None
        same = fresh is not None and (fresh.revision, fresh.state, fresh.create_scope) == (
            listed.revision,
            listed.state,
            listed.create_scope,
        )
        argv = dob.create_pr_argv(container.name, listed.create_scope) if same else None
        if argv is None:
            self._set(replace(state, status=(*state.status, _PR_CHANGED)))
            return
        self.state = state
        self.runner.hand_off(argv)
        self._after_mutation(())

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

        def done(result: JobResult) -> None:
            self.state = replace(self.state, busy=None)
            last = result.failure_line() or next(
                (line.strip() for line in reversed(result.stdout.splitlines()) if line.strip()), ""
            )
            self._after_mutation((_safe(last),) if last else ())

        self._start("clearing…", f"outbox-clear:{name}", f"clearing outbox {name}…", dob.clear_approved_argv(name, token), done)

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
        elif question.purpose == "clear" and value == "yes":
            self._run_clear(question)
        else:
            self._cancel()

    def _cancel(self) -> None:
        question = self.state.question
        self._pending_delete = frozenset()
        message = {
            "publish": "Nothing published.",
            "delete": "Nothing deleted.",
            "clear": "Nothing cleared.",
        }.get(question.purpose if question else "")
        self._set(replace(self.state, question=None, status=(message,) if message else ()))
