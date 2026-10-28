from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from unittest.mock import MagicMock

import pytest

from jailbee import lifecycle
from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, labels
from jailbee.orchestration.actions import ActionContext, Node, Outcome, plan_spawn
from jailbee.orchestration.grant import ChildRights, Grant, RepoCap
from jailbee.orchestration.protocol import SpawnManifest
from tests.conftest import make_config

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions, "live_create_jobs", lambda prefix: set())


def _ctx(
    tmp_path: Path,
    grant: dict[str, object] | None = None,
    extra: list[Node] | None = None,
    exists: bool = False,
) -> ActionContext:
    cfg = make_config(tmp_path)
    cfg = cfg.model_copy(
        update={"optional_mounts": {"data": MagicMock()}, "container_prefix": "proj"}
    )
    sender = Node(
        full=f"{cfg.container_prefix}-orch",
        short="orch",
        running=True,
        config={labels.GRANT: Grant.model_validate(grant or {}).to_label()},
    )
    incus = MagicMock()
    incus.exists.return_value = exists
    return ActionContext(
        cfg=cfg,
        gcfg=GlobalConfig(),
        incus=incus,
        root=tmp_path,
        sender=sender,
        nodes={n.full: n for n in [sender, *(extra or [])]},
        now=datetime(2026, 10, 25, tzinfo=UTC),
    )


def _m(branch: str = "fix/1", **rights: object) -> SpawnManifest:
    return SpawnManifest(
        version=1,
        id="a1",
        type="spawn",
        kind="fork",
        branch=branch,
        prompt="do it",
        rights=ChildRights.model_validate(rights),
    )


def test_spawn_records_on_done_destroy_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    plan = plan_spawn(ctx, _m(), queued_names=())
    assert isinstance(plan, actions.SpawnPlan)
    from jailbee.create_flow import CreateStarted

    create = MagicMock(return_value=CreateStarted(plan.child_full, 777, tmp_path / "log"))
    monkeypatch.setattr(actions, "start_create", create)
    out = actions.start_child(ctx, plan, fork=None)
    assert create.call_args.args[3].on_done == "destroy"
    assert out.detail.endswith("on done: destroy")


def test_spawn_without_destroy_operation_defaults_to_keep(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"operations": ["spawn", "message", "import"]})
    plan = plan_spawn(ctx, _m(), queued_names=())
    assert isinstance(plan, actions.SpawnPlan)
    assert (
        actions.child_options(ctx.cfg, plan, orchestrator_full=ctx.sender.full, fork=None).on_done
        == "keep"
    )


def test_spawn_explicit_destroy_without_destroy_operation_refused(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"operations": ["spawn", "message"]})
    out = plan_spawn(ctx, _m().model_copy(update={"on_done": "destroy"}), queued_names=())
    assert isinstance(out, Outcome) and out.state == "refused"
    assert "destroy` operation" in out.detail


def test_child_instance_config_writes_on_done(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    plan = plan_spawn(ctx, _m(), queued_names=())
    assert isinstance(plan, actions.SpawnPlan)
    opts = actions.child_options(ctx.cfg, plan, orchestrator_full=ctx.sender.full, fork=None)
    assert lifecycle._child_instance_config(opts)[labels.ON_DONE] == "destroy"


def test_plan_spawn_prepends_preamble(tmp_path: Path) -> None:
    plan = plan_spawn(_ctx(tmp_path), _m(), queued_names=())
    assert not isinstance(plan, Outcome)
    assert plan.prompt == (
        "You are a child container of orchestrator `orch`. "
        "Use the `jailbee-orchestration-child` skill.\n\ndo it"
    )
    assert plan.kind == "fork"
    assert plan.base is None


def test_child_full_from_branch(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    plan = plan_spawn(ctx, _m("fix/1"), queued_names=())
    assert not isinstance(plan, Outcome)
    assert plan.child_full == lifecycle.derive_container_name(ctx.cfg, "fix/1")
    assert plan.branch == "fix/1"


def test_mount_not_grantable_refused(tmp_path: Path) -> None:
    out = plan_spawn(_ctx(tmp_path), _m(mounts=["secrets"]), queued_names=())
    assert isinstance(out, Outcome)
    assert out.state == "refused"
    assert "mount 'secrets' is not grantable" in out.detail


def test_loose_not_grantable_refused(tmp_path: Path) -> None:
    out = plan_spawn(_ctx(tmp_path), _m(loose=True), queued_names=())
    assert isinstance(out, Outcome)
    assert "loose network mode is not grantable" in out.detail


def test_egress_not_grantable_refused(tmp_path: Path) -> None:
    out = plan_spawn(_ctx(tmp_path), _m(egress=["example.com"]), queued_names=())
    assert isinstance(out, Outcome)
    assert "egress 'example.com' is not covered by the grant" in out.detail


def test_multiple_problems_joined(tmp_path: Path) -> None:
    out = plan_spawn(_ctx(tmp_path), _m(loose=True, mounts=["secrets"]), queued_names=())
    assert isinstance(out, Outcome)
    assert "; " in out.detail


def test_grantable_rights_accepted(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"grantable": {"mounts": ["data"], "loose": True}})
    plan = plan_spawn(ctx, _m(mounts=["data"], loose=True), queued_names=())
    assert not isinstance(plan, Outcome)
    assert plan.rights.mounts == ("data",)


def test_rights_capped_by_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions.profiles, "repo_cap", lambda cfg: RepoCap(mounts=()))
    ctx = _ctx(tmp_path, {"grantable": {"mounts": ["data"]}})
    out = plan_spawn(ctx, _m(mounts=["data"]), queued_names=())
    assert isinstance(out, Outcome)
    assert out.state == "refused"
    assert "mount 'data'" in out.detail


def test_loose_capped_by_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions.profiles, "repo_cap", lambda cfg: RepoCap(loose=False))
    ctx = _ctx(tmp_path, {"grantable": {"loose": True}})
    out = plan_spawn(ctx, _m(loose=True), queued_names=())
    assert isinstance(out, Outcome)
    assert "loose" in out.detail


def test_existing_container_refused(tmp_path: Path) -> None:
    out = plan_spawn(_ctx(tmp_path, exists=True), _m("fix/1"), queued_names=())
    assert isinstance(out, Outcome)
    assert out.state == "refused"
    assert out.detail.startswith("container `")
    assert out.detail.endswith("` already exists")


def test_existing_node_refused(tmp_path: Path) -> None:
    probe = _ctx(tmp_path)
    full = lifecycle.derive_container_name(probe.cfg, "fix/1")
    node = Node(full=full, short=lifecycle.short_name(probe.cfg, full), running=False, config={})
    out = plan_spawn(_ctx(tmp_path, extra=[node]), _m("fix/1"), queued_names=())
    assert isinstance(out, Outcome)
    assert "already exists" in out.detail


def test_already_queued_refused(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    full = lifecycle.derive_container_name(ctx.cfg, "fix/1")
    out = plan_spawn(ctx, _m("fix/1"), queued_names={full})
    assert isinstance(out, Outcome)
    short = lifecycle.short_name(ctx.cfg, full)
    assert out.detail == f"a request for `{short}` is already queued"


def test_invalid_branch_refused(tmp_path: Path) -> None:
    out = plan_spawn(_ctx(tmp_path), _m("___"), queued_names=())
    assert isinstance(out, Outcome)
    assert out.state == "refused"
    assert "invalid container name" in out.detail


def _plan(tmp_path: Path, kind: Literal["fork", "new"] = "fork", **rights: object):
    ctx = _ctx(tmp_path)
    r = ChildRights.model_validate(rights)
    plan = actions.SpawnPlan("proj-fix-1", kind, "fix/1", "main" if kind == "new" else None, "p", r)
    return ctx, plan


def _verdict(mounts: tuple[str, ...] = (), widening: tuple[str, ...] = ()):
    from jailbee.branch_config import EscalationVerdict

    return EscalationVerdict(
        widening_steps=widening, attached_mounts=mounts, baseline_source="", untrusted=True
    )


@pytest.mark.parametrize(
    ("rights", "verdict", "expected"),
    [
        ({}, _verdict(), True),
        ({"mounts": ["data"]}, _verdict(mounts=("data",)), True),
        ({}, _verdict(mounts=("data",)), False),
        ({"loose": False}, _verdict(widening=("npm",)), False),
        ({"loose": True}, _verdict(widening=("npm",)), True),
    ],
)
def test_escalation_policy(rights: dict[str, object], verdict: object, expected: bool) -> None:
    policy = actions.escalation_policy(ChildRights.model_validate(rights))
    assert policy(verdict) is expected  # type: ignore[arg-type]  # test verdict


def test_child_options_fork_is_untrusted_and_pinned(tmp_path: Path) -> None:
    from jailbee.forking import ForkSource

    ctx, plan = _plan(tmp_path, mounts=["data"])
    fork = ForkSource("proj-src", "src-br", "deadbeef", "dev", False, {"user.jailbee.pr": "4"})
    o = actions.child_options(ctx.cfg, plan, orchestrator_full="proj-orch", fork=fork)
    assert o.inherited_labels == {"user.jailbee.pr": "4"}
    assert o.clone_commit == "deadbeef"
    assert o.fork_of == "proj-src"
    assert o.base_branch_label == "dev"
    assert o.untrusted_head is True
    assert o.base is None
    assert o.spawned_by == "proj-orch"
    assert o.rights == plan.rights
    assert o.name == "proj-fix-1" and o.container_branch == "fix/1"
    assert o.network == "strict"
    assert o.memory is None and o.clone is True and o.assume_yes is False
    assert o.initial_prompt == "p"


def test_child_options_new_loose_uses_base(tmp_path: Path) -> None:
    ctx, plan = _plan(tmp_path, "new", loose=True)
    o = actions.child_options(ctx.cfg, plan, orchestrator_full="proj-orch", fork=None)
    assert o.network == "loose"
    assert o.base == "main"
    assert o.clone_commit is None and o.fork_of is None
    assert o.untrusted_head is True


def test_start_child_needs_operator_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.create_flow import NeedsOperator

    def boom(*a: object, **k: object) -> None:
        raise NeedsOperator("x", reason="autofetch needs a key touch")

    monkeypatch.setattr(actions, "start_create", boom)
    ctx, plan = _plan(tmp_path)
    assert actions.start_child(ctx, plan, fork=None) == Outcome(
        "refused", "autofetch needs a key touch"
    )


def test_start_child_create_error_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.create_flow import CreateError

    def boom(*a: object, **k: object) -> None:
        raise CreateError("name taken")

    monkeypatch.setattr(actions, "start_create", boom)
    ctx, plan = _plan(tmp_path)
    assert actions.start_child(ctx, plan, fork=None) == Outcome("refused", "name taken")


def test_start_child_passes_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.create_flow import CreateStarted

    seen: dict[str, object] = {}

    def fake(cfg: object, incus: object, gcfg: object, opts: object, **kw: object) -> CreateStarted:
        seen.update(gcfg=gcfg, opts=opts, **kw)
        return CreateStarted("proj-fix-1", 777, tmp_path / "log")

    monkeypatch.setattr(actions, "start_create", fake)
    ctx, plan = _plan(tmp_path)
    out = actions.start_child(ctx, plan, fork=None)
    assert out.state == "started"
    assert out.detail == "creation of child `fix-1` started; on done: destroy"
    inter = seen["interaction"]
    assert inter.accept_escalation is not None  # type: ignore[attr-defined]
    assert inter.confirm is None  # type: ignore[attr-defined]
    assert inter.autofetch is False  # type: ignore[attr-defined]
    assert seen["gcfg"] is ctx.gcfg
    assert seen["config_path"] is None


def test_child_options_never_inherit_loose_repo_default(tmp_path: Path) -> None:
    """Spec §5.3: a child gets exactly its rights; a loose repo default is not one."""
    ctx, plan = _plan(tmp_path, loose=False)
    cfg = ctx.cfg.model_copy(
        update={"defaults": ctx.cfg.defaults.model_copy(update={"network": "loose"})}
    )
    o = actions.child_options(cfg, plan, orchestrator_full="proj-orch", fork=None)
    assert o.network == "strict"


def test_plan_spawn_refuses_a_child_being_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    full = lifecycle.derive_container_name(ctx.cfg, "fix/1")
    monkeypatch.setattr(actions, "live_create_jobs", lambda prefix: {full})
    out = plan_spawn(ctx, _m("fix/1"), queued_names=())
    assert isinstance(out, Outcome)
    assert out.detail == "container `fix-1` is already being created"


def _runner_m(runner: object) -> SpawnManifest:
    m = _m()
    return m.model_copy(update={"runner": runner})


def test_spawn_with_runner_sets_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.orchestration.runner import Runner

    monkeypatch.setattr(actions, "runner_problems", lambda *a, **k: [])
    ctx = _ctx(tmp_path)
    runner = Runner(agent="claude-jb", profile="grok", model="opus")
    plan = plan_spawn(ctx, _runner_m(runner), queued_names=())
    assert not isinstance(plan, Outcome)
    assert plan.runner == runner
    o = actions.child_options(ctx.cfg, plan, orchestrator_full="proj-orch", fork=None)
    assert o.runner == runner
    assert lifecycle._child_instance_config(o)[labels.RUNNER] == runner.to_label()


def test_spawn_without_runner_has_no_label(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    plan = plan_spawn(ctx, _m(), queued_names=())
    assert not isinstance(plan, Outcome)
    assert plan.runner is None
    o = actions.child_options(ctx.cfg, plan, orchestrator_full="proj-orch", fork=None)
    assert o.runner is None
    assert labels.RUNNER not in lifecycle._child_instance_config(o)


def test_spawn_refused_for_bad_runner(tmp_path: Path) -> None:
    from jailbee.orchestration.runner import Runner

    out = plan_spawn(_ctx(tmp_path), _runner_m(Runner(agent="claude-jb")), queued_names=())
    assert isinstance(out, Outcome) and out.state == "refused"
    assert "agent `claude` is not enabled in this repo" in out.detail


def test_spawn_refusal_joins_rights_and_runner_problems(tmp_path: Path) -> None:
    from jailbee.orchestration.runner import Runner

    m = _m(loose=True).model_copy(update={"runner": Runner(agent="claude-jb")})
    out = plan_spawn(_ctx(tmp_path), m, queued_names=())
    assert isinstance(out, Outcome) and out.state == "refused"
    assert "loose network mode is not grantable; agent `claude` is not enabled" in out.detail


@pytest.mark.parametrize(
    "explicit,container,expected",
    [
        (None, None, "haiku"),
        ("opus", None, "opus"),
        (None, "sonnet", "sonnet"),
    ],
)
def test_spawn_resolves_default_runner(tmp_path, monkeypatch, explicit, container, expected):
    from jailbee.config.models_orchestration import OrchestrationView
    from jailbee.orchestration.runner_model import Runner

    grant = {"default_runner": {"agent": "claude", "model": container}} if container else None
    ctx = _ctx(tmp_path, grant)
    ctx.cfg._orchestration_view = OrchestrationView(
        default_runner=Runner(agent="claude", model="haiku"), default_runner_source="repo"
    )
    monkeypatch.setattr(actions, "runner_problems", lambda *args: [])
    m = _runner_m(Runner(agent="claude", model=explicit)) if explicit else _m()
    plan = plan_spawn(ctx, m, queued_names=())
    assert isinstance(plan, actions.SpawnPlan)
    opts = actions.child_options(ctx.cfg, plan, orchestrator_full=ctx.sender.full, fork=None)
    assert opts.runner == Runner(agent="claude", model=expected)


def test_spawn_refuses_unserved_default(tmp_path, monkeypatch):
    from jailbee.config.models_orchestration import OrchestrationView
    from jailbee.orchestration.runner_model import Runner

    ctx = _ctx(tmp_path)
    ctx.cfg._orchestration_view = OrchestrationView(
        default_runner=Runner(agent="claude-jb", profile="grok"), default_runner_source="repo"
    )
    monkeypatch.setattr(actions, "runner_problems", lambda *args: ["profile not served"])
    assert plan_spawn(ctx, _m(), queued_names=()) == Outcome("refused", "profile not served")
