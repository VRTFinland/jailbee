"""Off-loop inbox queries and immutable views of completed rounds and actions."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from jailbee.orchestration import wire
from jailbee.orchestration.outbox import grant_json_text
from jailbee.orchestration.store import StoreError

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.orchestration import broker_state
    from jailbee.orchestration.actions import ActionContext
    from jailbee.orchestration.store import BrokerStore

log = logging.getLogger(__name__)

SNAPSHOT_OPS = frozenset({"children", "status", "grant"})
STORE_OPS = frozenset({"list", "read", "ack"})
# Outbox ops are SQLite queries too: query workers, never the round loop.
QUERY_OPS = SNAPSHOT_OPS | STORE_OPS | wire.OUTBOX_OPS
# The roles whose outbox writes (stage, drop, drafts) the broker accepts.
WRITER_ROLES = frozenset({"plain", "orchestrator"})


@dataclass(frozen=True)
class ContainerSnapshot:
    """JSON strings sever every nested reference to mutable broker/context data."""

    role: str
    ready: bool
    children_json: str
    grant_json: str | None
    project_config: Config | None = None


class Queries:
    """Only the broker publishes; readers capture one whole immutable generation.

    The store holds a path, not a SQLite connection: its methods open and close
    a connection per call, retaining BEGIN IMMEDIATE for acknowledgement writes.
    """

    def __init__(self, store: BrokerStore) -> None:
        self._store = store
        self._snapshot: Mapping[str, ContainerSnapshot] = MappingProxyType({})

    def publish(self, snapshot: Mapping[str, ContainerSnapshot]) -> None:
        self._snapshot = MappingProxyType(dict(snapshot))

    def snapshot(self) -> Mapping[str, ContainerSnapshot]:
        """Return the immutable generation for executor-side recovery."""
        return self._snapshot

    def publish_container(self, uuid: str, view: ContainerSnapshot) -> None:
        """Replace one executor-built view without rebuilding unrelated containers."""
        self.publish({**self._snapshot, uuid: view})

    def answer(self, uuid: str, request: wire.Request) -> wire.Response:
        try:
            result = self._dispatch(uuid, request)
        except wire.WireError as exc:
            return wire.fail(exc.code, exc.detail)
        except StoreError as exc:
            return wire.fail(wire.STORE, str(exc))
        except Exception:
            log.exception("broker: query %r failed", request.op)
            return wire.fail(wire.REFUSED, "broker error")
        return wire.ok(result)

    def _dispatch(self, uuid: str, request: wire.Request) -> object:
        op, args = request.op, request.args
        if op in wire.OUTBOX_OPS:
            return self._outbox(uuid, op, args)
        if op in STORE_OPS:
            from jailbee.orchestration import socket_ops

            if not self._store.has_recipient_uuid(uuid):
                raise wire.WireError(
                    wire.STORE, "inbox store recipient is not ready; retry shortly"
                )
            known = self._snapshot.get(uuid)
            if known is not None and known.role == "plain":
                raise wire.WireError(wire.NOT_ORCHESTRATED, "this container is not an orchestrator")
            if op == "list":
                return socket_ops.handle_list(self._store, uuid, after=args.get("after"))
            if op == "read":
                return socket_ops.handle_read(
                    self._store,
                    uuid,
                    id=args.get("id"),
                    keep=args.get("keep", False),
                    now=time.time(),
                )
            return socket_ops.handle_ack(
                self._store,
                uuid,
                id=args.get("id"),
                now=time.time(),
                ok_statuses=args.get("ok_statuses", False),
            )

        # Never consult BrokerMemory, ActionContext, Incus or SQLite here.
        snapshot = self._snapshot
        view = snapshot.get(uuid)
        if view is None or not view.ready:
            raise wire.WireError(wire.STORE, "inbox store recipient is not ready; retry shortly")
        if op == "grant":
            if view.grant_json is None:
                raise wire.WireError(
                    wire.NOT_ORCHESTRATED, "this container holds no orchestration grant"
                )
            return json.loads(view.grant_json)
        if op not in ("children", "status"):
            raise wire.WireError(wire.BAD_REQUEST, f"unknown op '{op}'")
        child = args.get("child") if op == "status" else None
        if child is not None and not isinstance(child, str):
            raise wire.WireError(wire.BAD_REQUEST, "child must be a string or null")
        if view.role != "orchestrator":
            raise wire.WireError(wire.NOT_ORCHESTRATED, "this container is not an orchestrator")
        result: dict[str, object] = json.loads(view.children_json)
        if child is not None:
            children = result["children"]
            assert isinstance(children, list)
            picked = [c for c in children if c.get("name") == child]
            if not picked:
                raise wire.WireError(wire.NOT_FOUND, f"unknown child '{child}'")
            result["children"] = picked
        return result

    def _outbox(self, uuid: str, op: str, args: dict[str, object]) -> object:
        """Outbox ops for any container; a child may read but never stage. The
        role comes from the completed-round view, so the broker, not the
        container CLI, decides."""
        from jailbee.outbox import broker_ops
        from jailbee.outbox.store_db import OutboxStore

        view = self._snapshot.get(uuid)
        if view is None or not view.ready:
            raise wire.WireError(
                wire.STORE, "the broker does not know this container yet; retry shortly"
            )
        reading = op in (
            wire.OUTBOX_LIST,
            wire.OUTBOX_SHOW,
            wire.OUTBOX_SENT,
            *wire.OUTBOX_DRAFT_READ_OPS,
        )
        # Fail closed: only the roles allowed to stage are named; a child, an
        # unknown role or an invalid grant ("none") may read but never change.
        if not reading:
            refusal = _writer_refusal(view.role)
            if refusal is not None:
                raise refusal
        if op == wire.OUTBOX_SENT:
            from jailbee.outbox.publication_db import PublicationStore

            return broker_ops.handle_sent(PublicationStore(self._store), uuid, args)
        cfg = view.project_config
        reader = title_reader = None
        allowed_boards: set[str] = set()
        if cfg is not None:
            allowed_boards.update(board.casefold() for board in cfg.github.projects)
            if cfg.github.project:
                allowed_boards.add(cfg.github.project.casefold())
        if view.role == "orchestrator" and view.grant_json is not None:
            grant = json.loads(view.grant_json)
            allowed_boards.update(
                entry["board"].casefold() for entry in grant.get("publish", {}).get("projects", [])
            )
        if op in (wire.OUTBOX_PUT, wire.OUTBOX_DRAFT_APPEND):
            manifest = args.get("manifest")
            entries = (
                manifest.get("actions", []) if isinstance(manifest, dict) else []
            ) if op == wire.OUTBOX_PUT else args.get("actions", [args.get("action")])
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("type") in (
                        "project_add", "project_set", "project_clear", "project_remove"
                    ):
                        board = entry.get("project", cfg.github.project if cfg else None)
                        if isinstance(board, str) and board.casefold() not in allowed_boards:
                            raise wire.WireError(
                                wire.REFUSED,
                                f"project {board!r} is not allowed for staging; add it to "
                                "github.projects in the host-local repos/<container_prefix>.yaml "
                                "or the orchestrator's publish.projects grant",
                            )
        if cfg is not None:
            from jailbee import issue_outbox, issue_projects

            def reader(
                board: str, repo: str, number: int, field: str | None
            ) -> issue_projects.ProjectExpectation:
                targets = issue_outbox.resolve_repo_targets(cfg)
                target = issue_outbox.target_for(targets, repo, cfg.repo_root)
                if target is None:
                    raise ValueError(f"repo {repo!r} is not host-authorized")
                project = issue_projects.resolve_project(cfg.repo_root, board)
                item = issue_projects.get_target(cfg.repo_root, project, target.slug, number)
                value = None
                if field is not None:
                    fields = issue_projects.list_fields(cfg.repo_root, project)
                    resolved_field = issue_projects.find_field(fields, field)
                    value = issue_projects.get_field_value(cfg.repo_root, item, resolved_field)
                return issue_projects.ProjectExpectation(item.present, value, project.title)

            def title_reader(board: str) -> str:
                return issue_projects.resolve_project(cfg.repo_root, board).title

        return broker_ops.handle(
            OutboxStore(self._store),
            uuid,
            op,
            args,
            now=time.time(),
            project_reader=reader,
            project_title_reader=title_reader,
            default_project=getattr(cfg.github, "project", None) if cfg else None,
        )

    def put_refusal(self, uuid: str) -> wire.Response | None:
        """The answer an ``outbox.put`` from ``uuid`` gets for its role, if refused.

        For the listener, which reads only the head of a put line: a role the
        current view already refuses is answered without buffering or decoding
        the (up to `wire.OUTBOX_MAX_LINE_BYTES`) line. An unknown or not-ready
        container returns None and is decided by `answer` as usual.
        """
        view = self._snapshot.get(uuid)
        if view is None or not view.ready:
            return None
        refusal = _writer_refusal(view.role)
        return None if refusal is None else wire.fail(refusal.code, refusal.detail)


def _writer_refusal(role: str) -> wire.WireError | None:
    """The refusal of an outbox change by ``role``, or None when it may stage."""
    if role in WRITER_ROLES:
        return None
    if role == "child":
        from jailbee.container_outbox.role import CHILD_REFUSAL

        return wire.WireError(wire.REFUSED, CHILD_REFUSAL)
    return wire.WireError(
        wire.REFUSED,
        "the broker could not establish this container's role; "
        "outbox changes are refused until it can (retry after the next broker round)",
    )


def _context_snapshot(memory: broker_state.BrokerMemory, ctx: ActionContext) -> ContainerSnapshot:
    """Serialize on the executor so query readers never reach mutable contexts."""
    from jailbee.orchestration import socket_ops

    assert memory.store is not None
    previous = memory.queries.snapshot().get(ctx.sender.key) if memory.queries else None
    grant = ctx.grant()
    try:
        ready = memory.store.has_recipient_uuid(ctx.sender.key)
    except StoreError:
        log.exception("broker: recipient readiness for %s failed", ctx.sender.key)
        ready = previous.ready if previous is not None else False
    try:
        children = socket_ops.children_result(memory, memory.store, ctx.sender.key, ctx)
    except StoreError:
        log.exception("broker: child history for %s failed", ctx.sender.key)
        view = memory.children.get(ctx.sender.full)
        old = json.loads(previous.children_json) if previous is not None else {}
        children = {
            "observed_at": view.observed_at if view is not None else None,
            "children": list(view.children) if view is not None else [],
            "last_from_child": old.get("last_from_child", {}),
        }
    return ContainerSnapshot(
        role=ctx.role(),
        ready=ready,
        children_json=json.dumps(children),
        grant_json=grant_json_text(grant, *ctx.default_runner()) if grant is not None else None,
        project_config=ctx.cfg,
    )


def publish_context(memory: broker_state.BrokerMemory, ctx: ActionContext) -> None:
    """Commit one action's view without publishing other unfinished round state."""
    if memory.queries is not None and memory.store is not None:
        try:
            memory.queries.publish_container(ctx.sender.key, _context_snapshot(memory, ctx))
        except Exception:
            log.exception("broker: publishing query view for %s failed", ctx.sender.key)


def _fallback_role(memory: broker_state.BrokerMemory, uuid: str) -> str:
    """The role of a container without a context this round, failing closed.

    Its labels in the round's listing decide: a child (or one the broker last
    prepared as a child) stays ``child`` whatever happened to its context; an
    unlabelled container is ``plain``. Anything else — an orchestrator label
    without a valid context, or an instance absent from the listing — is
    ``none``, which may read its outbox but not change it.
    """
    listed = memory.listed_roles.get(uuid)
    if listed == "child" or memory.env_roles.get(uuid) == "child":
        return "child"
    return "plain" if listed == "plain" else "none"


def publish_queries(memory: broker_state.BrokerMemory) -> None:
    """Build on the broker thread, then atomically replace the completed-round view.

    Each component is isolated so failed store reads do not hide other updates.
    Neither contexts nor their mutable children/grant data cross into the listener.
    """
    if memory.queries is None or memory.store is None:
        return
    empty_children = json.dumps({"children": [], "observed_at": None, "last_from_child": {}})
    try:
        snapshot = {
            uuid: ContainerSnapshot(
                _fallback_role(memory, uuid),
                True,
                empty_children,
                None,
                memory.project_configs.get(uuid),
            )
            for uuid in memory.store.recipients().values()
        }
    except Exception:
        log.exception("broker: publishing query recipients failed")
        snapshot = {
            uuid: ContainerSnapshot(
                "none" if view.role == "none" else _fallback_role(memory, uuid),
                view.ready,
                empty_children,
                None,
            )
            for uuid, view in memory.queries.snapshot().items()
        }
    for uuid, ctx in memory.contexts.items():
        try:
            snapshot[uuid] = _context_snapshot(memory, ctx)
        except Exception:
            log.exception("broker: publishing query view for %s failed", uuid)
            # Fail closed: never retain a stale grant if serialization fails.
            snapshot[uuid] = ContainerSnapshot("none", False, empty_children, None)
    try:
        memory.queries.publish(snapshot)
    except Exception:
        log.exception("broker: publishing query snapshot failed")
