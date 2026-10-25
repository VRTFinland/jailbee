from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from jailbee import lifecycle
from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, labels
from jailbee.orchestration.actions import ActionContext, Node, Outcome, plan_spawn
from jailbee.orchestration.grant import ChildRights, Grant, RepoCap
from jailbee.orchestration.protocol import SpawnManifest
from tests.conftest import make_config

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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
    out = plan_spawn(_ctx(tmp_path), _m("---"), queued_names=())
    assert isinstance(out, Outcome)
    assert out.state == "refused"
    assert "invalid container name" in out.detail
