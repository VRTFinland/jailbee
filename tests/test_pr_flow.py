"""Tests for the shared PR flow extracted from `jailbee pr`."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from jailbee import pr_flow


def _super_scope(tmp_path: Path) -> pr_flow.PrScope:
    return pr_flow.PrScope(repo_root=tmp_path, remote="origin", prefix="", subpath=None)


def _sub_scope(tmp_path: Path) -> pr_flow.PrScope:
    return pr_flow.PrScope(
        repo_root=tmp_path / "libs" / "foo",
        remote="upstream",
        prefix="submodule 'libs/foo': ",
        subpath="libs/foo",
    )


def test_scope_for_repo_uses_superproject_settings(tmp_path):
    cfg = _cfg(tmp_path)

    assert pr_flow.PrScope.for_repo(cfg) == pr_flow.PrScope(
        repo_root=cfg.repo_root,
        remote=cfg.upstream_remote,
        prefix="",
        subpath=None,
    )


def test_scope_for_submodule_resolves_its_remote(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="upstream")

    assert pr_flow.PrScope.for_submodule(cfg, "libs/foo") == pr_flow.PrScope(
        repo_root=cfg.repo_root / "libs/foo",
        remote="upstream",
        prefix="submodule 'libs/foo': ",
        subpath="libs/foo",
    )


def test_candidate_scopes_returns_repo_then_sorted_submodules(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    mocker.patch("jailbee.submodules.host_submodule_paths", return_value=["z", "a"])
    mocker.patch(
        "jailbee.submodule_pr.resolve_remote",
        side_effect=lambda _repo_root, subpath: {"a": "a-upstream", "z": "z-origin"}[subpath],
    )

    scopes = pr_flow.candidate_scopes(cfg)

    assert [scope.subpath for scope in scopes] == [None, "a", "z"]
    assert [scope.remote for scope in scopes] == [cfg.upstream_remote, "a-upstream", "z-origin"]


def test_candidate_scopes_merges_only_initialized_recorded_paths_once(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    mocker.patch("jailbee.submodules.host_submodule_paths", return_value=["z", "a", "z"])
    mocker.patch(
        "jailbee.submodules.host_subrepo_exists",
        side_effect=lambda root, path: root == tmp_path and path in {"a", "b", "z"},
    )
    remote = mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")

    scopes = pr_flow.candidate_scopes(cfg, extra_paths=["z", "b", "missing", "b"])

    assert [scope.subpath for scope in scopes] == [None, "a", "b", "z"]
    assert [call.args[1] for call in remote.call_args_list] == ["a", "b", "z"]


def test_noun_names_the_pr_number(tmp_path):
    assert _super_scope(tmp_path).noun("12") == "PR #12"


def test_noun_falls_back_without_a_number(tmp_path):
    assert _super_scope(tmp_path).noun(None) == "the container's PR"


def test_noun_is_prefixed_for_a_submodule(tmp_path):
    assert _sub_scope(tmp_path).noun("12") == "submodule 'libs/foo': PR #12"


def test_command_is_jailbee_pr_for_the_superproject(tmp_path):
    assert _super_scope(tmp_path).command == "jailbee pr"


def test_command_is_jailbee_submodule_pr_for_a_submodule(tmp_path):
    assert _sub_scope(tmp_path).command == "jailbee submodule pr"


def test_reject_as_on_pr_update_exits_2(tmp_path):
    with pytest.raises(typer.Exit) as excinfo:
        pr_flow.reject_as_on_pr_update(_super_scope(tmp_path), "user/x", "12")
    assert excinfo.value.exit_code == 2


def test_foreign_force_push_is_silent_with_yes(tmp_path, mocker):
    confirm = mocker.patch("typer.confirm")
    pr_flow.confirm_foreign_force_push(_super_scope(tmp_path), "feat-foo", "12", "user/x", yes=True)
    confirm.assert_not_called()


def test_foreign_force_push_exits_1_without_a_tty(tmp_path, mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    with pytest.raises(typer.Exit) as excinfo:
        pr_flow.confirm_foreign_force_push(
            _super_scope(tmp_path), "feat-foo", "12", "user/x", yes=False
        )
    assert excinfo.value.exit_code == 1


def test_foreign_force_push_aborts_on_decline(tmp_path, mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=False)
    with pytest.raises(typer.Abort):
        pr_flow.confirm_foreign_force_push(
            _super_scope(tmp_path), "feat-foo", "12", "user/x", yes=False
        )


def test_confirm_branch_name_returns_proposal_when_equal_to_source(mocker):
    prompt = mocker.patch("typer.prompt")
    assert pr_flow.confirm_pr_branch_name("feat/foo", "feat/foo") == "feat/foo"
    prompt.assert_not_called()


def test_confirm_branch_name_returns_proposal_off_tty(mocker):
    mocker.patch("sys.stdin.isatty", return_value=False)
    prompt = mocker.patch("typer.prompt")
    assert pr_flow.confirm_pr_branch_name("user/ai", "feat/foo") == "user/ai"
    prompt.assert_not_called()


def test_confirm_branch_name_reprompts_until_valid(mocker):
    mocker.patch("sys.stdin.isatty", return_value=True)
    mocker.patch("typer.prompt", side_effect=["bad name", "user/ok"])
    mocker.patch("jailbee.git.check_ref_format", side_effect=[False, True])
    assert pr_flow.confirm_pr_branch_name("user/ai", "feat/foo") == "user/ok"


def _cfg(tmp_path):
    from tests.conftest import make_cfg

    return make_cfg(tmp_path)


def test_description_update_explicit_fields_win(tmp_path, mocker):
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    result = pr_flow.resolve_pr_description_update(
        _cfg(tmp_path),
        mocker.MagicMock(),
        "c1",
        _super_scope(tmp_path),
        branch="feat/foo",
        base="main",
        title="Set",
        body=None,
        description=False,
        ai_on=True,
    )
    assert result is not None and (result.title, result.body) == ("Set", None)
    gen.assert_not_called()


def test_description_update_skips_without_a_request(tmp_path, mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = pr_flow.resolve_pr_description_update(
        _cfg(tmp_path),
        mocker.MagicMock(),
        "c1",
        _super_scope(tmp_path),
        branch="feat/foo",
        base="main",
        title=None,
        body=None,
        description=False,
        ai_on=True,
    )
    assert result is None


def test_description_update_passes_the_scope_subpath_to_the_ai(tmp_path, mocker):
    from jailbee.pr_ai import PrText

    gen = mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="t", body="b", branch="feat/x"),
    )
    result = pr_flow.resolve_pr_description_update(
        _cfg(tmp_path),
        mocker.MagicMock(),
        "c1",
        _sub_scope(tmp_path),
        branch="feat/foo",
        base="main",
        title=None,
        body=None,
        description=True,
        ai_on=True,
    )
    assert result is not None and (result.title, result.body) == ("t", "b")
    assert gen.call_args.kwargs["subpath"] == "libs/foo"


def test_description_update_offer_is_suppressed_on_a_foreign_pr(tmp_path, mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=True)
    result = pr_flow.resolve_pr_description_update(
        _cfg(tmp_path),
        mocker.MagicMock(),
        "c1",
        _super_scope(tmp_path),
        branch="feat/foo",
        base="main",
        title=None,
        body=None,
        description=False,
        ai_on=True,
        foreign_head=True,
    )
    assert result is None
    confirm.assert_not_called()


def _pr_info(number=7, head="feat/foo", state="OPEN", cross=False, owner=None):
    from jailbee.pr import PrInfo

    return PrInfo(
        number=number,
        head_ref=head,
        head_sha="abc",
        state=state,
        base_ref="main",
        author_login="someone",
        is_cross_repository=cross,
        head_repo_owner=owner,
    )


def test_container_label_state_reads_the_labels(mocker):
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": "12",
        "user.jailbee.pr_branch": "user/x",
        "user.jailbee.pr_author": "1",
    }.get(key)

    record = pr_flow.ContainerLabelState(incus, "c1").read()

    assert record == pr_flow.PrRecord(number=12, head="user/x", author=True, adopted=False)


def test_container_label_state_raises_on_a_malformed_pr_label(mocker):
    """FIX 5 regression: a non-numeric `user.jailbee.pr` must fail closed, not
    silently read as `number=None` (== "no PR"), which would turn OFF every
    guard keyed on `pr_label` (--as rejection, the foreign-force
    confirmation, foreign_head) for a container that plainly has a PR."""
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: {"user.jailbee.pr": "not-a-number"}.get(key)

    with pytest.raises(pr_flow.MalformedPrLabelError):
        pr_flow.ContainerLabelState(incus, "c1").read()


def test_container_label_state_writes_pr_branch_first_and_number_last(mocker):
    incus = mocker.MagicMock()

    pr_flow.ContainerLabelState(incus, "c1").record(
        head="user/x", author=True, adopted=False, number=12
    )

    keys = [call.args[1] for call in incus.config_set.call_args_list]
    assert keys == ["user.jailbee.pr_branch", "user.jailbee.pr_author", "user.jailbee.pr"]


def test_container_label_state_omits_the_number_when_none(mocker):
    incus = mocker.MagicMock()

    pr_flow.ContainerLabelState(incus, "c1").record(
        head="user/x", author=False, adopted=True, number=None
    )

    keys = [call.args[1] for call in incus.config_set.call_args_list]
    assert keys == ["user.jailbee.pr_branch", "user.jailbee.pr_adopted"]


def test_container_label_state_survives_a_failed_write(mocker):
    from jailbee.incus import IncusError

    incus = mocker.MagicMock()
    incus.config_set.side_effect = IncusError("boom")

    # Best-effort, like the original: warns rather than raising.
    pr_flow.ContainerLabelState(incus, "c1").record(
        head="user/x", author=True, adopted=False, number=12
    )


def test_container_label_state_record_context_replaces_generic_warning(mocker):
    from jailbee.incus import IncusError

    warn = mocker.patch("jailbee.pr_flow.warn")
    incus = mocker.MagicMock()
    incus.config_set.side_effect = IncusError("boom")

    pr_flow.ContainerLabelState(incus, "c1").record(
        head="user/x",
        author=True,
        adopted=False,
        number=123,
        context="PR #123 created, but failed to record the PR label on 'feat-foo'",
    )

    warn.assert_called_once_with(
        "PR #123 created, but failed to record the PR label on 'feat-foo': boom"
    )


def test_adopt_returns_none_without_a_branch(tmp_path, mocker):
    state = mocker.MagicMock()
    assert (
        pr_flow.adopt_existing_pr_for_branch(
            _super_scope(tmp_path), state, branch=None, yes=True, record_context="on 'feat-foo'"
        )
        is None
    )


def test_adopt_returns_none_when_no_pr_exists(tmp_path, mocker):
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=None)
    assert (
        pr_flow.adopt_existing_pr_for_branch(
            _super_scope(tmp_path),
            mocker.MagicMock(),
            branch="feat/foo",
            yes=True,
            record_context="on 'feat-foo'",
        )
        is None
    )


@pytest.mark.parametrize("state_value", ["CLOSED", "MERGED"])
def test_adopt_skips_a_closed_or_merged_pr(tmp_path, mocker, state_value):
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=_pr_info(state=state_value))
    assert (
        pr_flow.adopt_existing_pr_for_branch(
            _super_scope(tmp_path),
            mocker.MagicMock(),
            branch="feat/foo",
            yes=True,
            record_context="on 'feat-foo'",
        )
        is None
    )


def test_adopt_skips_a_fork_head(tmp_path, mocker):
    mocker.patch(
        "jailbee.pr.find_pr_for_branch",
        return_value=_pr_info(cross=True, owner="someone-else"),
    )
    assert (
        pr_flow.adopt_existing_pr_for_branch(
            _super_scope(tmp_path),
            mocker.MagicMock(),
            branch="feat/foo",
            yes=True,
            record_context="on 'feat-foo'",
        )
        is None
    )


def test_adopt_records_the_pr_and_returns_it(tmp_path, mocker):
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=_pr_info())
    state = mocker.MagicMock()

    result = pr_flow.adopt_existing_pr_for_branch(
        _super_scope(tmp_path), state, branch="feat/foo", yes=True, record_context="on 'feat-foo'"
    )

    assert result == (7, "feat/foo")
    state.record.assert_called_once_with(
        head="feat/foo",
        author=False,
        adopted=True,
        number=7,
        context="Could not record PR #7 on 'feat-foo'",
    )


def test_adopt_exits_1_without_a_tty(tmp_path, mocker):
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=_pr_info())
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    with pytest.raises(typer.Exit) as excinfo:
        pr_flow.adopt_existing_pr_for_branch(
            _super_scope(tmp_path),
            mocker.MagicMock(),
            branch="feat/foo",
            yes=False,
            record_context="on 'feat-foo'",
        )
    assert excinfo.value.exit_code == 1


def test_adopt_aborts_on_decline(tmp_path, mocker):
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=_pr_info())
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=False)
    with pytest.raises(typer.Abort):
        pr_flow.adopt_existing_pr_for_branch(
            _super_scope(tmp_path),
            mocker.MagicMock(),
            branch="feat/foo",
            yes=False,
            record_context="on 'feat-foo'",
        )


def _text(branch="user/ai"):
    from jailbee.pr_ai import PrText

    return PrText(title="AI title", body="AI body", branch=branch)


def _plan(tmp_path, mocker, *, cfg=None, scope=None, **kwargs):
    defaults = dict(
        is_update=False,
        stored_head=None,
        source_branch="feat/foo",
        base="main",
        title=None,
        body=None,
        as_name=None,
        no_ai=False,
        status_label="Generating…",
    )
    defaults.update(kwargs)
    return pr_flow.resolve_pr_text_and_head(
        cfg if cfg is not None else _cfg(tmp_path),
        mocker.MagicMock(),
        "c1",
        scope if scope is not None else _super_scope(tmp_path),
        **defaults,
    )


def test_update_reuses_the_stored_head_and_never_generates(tmp_path, mocker):
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    plan = _plan(tmp_path, mocker, is_update=True, stored_head="user/x")
    assert plan == pr_flow.HeadPlan(publish_name="user/x", ai_text=None)
    gen.assert_not_called()


def test_as_name_wins_over_the_ai(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=_text())
    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    plan = _plan(tmp_path, mocker, cfg=cfg, as_name="user/mine")
    assert plan.publish_name == "user/mine"


def test_invalid_as_name_exits_2(tmp_path, mocker):
    mocker.patch("jailbee.git.check_ref_format", return_value=False)
    with pytest.raises(typer.Exit) as excinfo:
        _plan(tmp_path, mocker, as_name="bad name")
    assert excinfo.value.exit_code == 2


def test_no_ai_keeps_the_source_branch(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    plan = _plan(tmp_path, mocker, cfg=cfg, no_ai=True)
    assert plan == pr_flow.HeadPlan(publish_name="feat/foo", ai_text=None)
    gen.assert_not_called()


def test_ai_branch_off_keeps_the_source_branch_but_still_generates_text(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}}, pr={"ai_branch": False})
    mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=_text())
    plan = _plan(tmp_path, mocker, cfg=cfg)
    assert plan.publish_name == "feat/foo"
    assert plan.ai_text is not None


def test_ai_description_off_still_proposes_a_branch(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}}, pr={"ai_description": False})
    mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=_text())
    mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", side_effect=lambda p, s: p)
    plan = _plan(tmp_path, mocker, cfg=cfg)
    assert plan.publish_name == "user/ai"


def test_both_toggles_off_skips_generation_entirely(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        agents={"claude": {"enabled": True}},
        pr={"ai_branch": False, "ai_description": False},
    )
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    plan = _plan(tmp_path, mocker, cfg=cfg)
    assert plan == pr_flow.HeadPlan(publish_name="feat/foo", ai_text=None)
    gen.assert_not_called()


def test_generation_failure_falls_back_to_the_source_branch(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=None)
    plan = _plan(tmp_path, mocker, cfg=cfg)
    assert plan == pr_flow.HeadPlan(publish_name="feat/foo", ai_text=None)


def test_generation_failure_on_a_submodule_names_the_submodule_command(tmp_path, mocker):
    """The failure warning points the user at `{scope.command} --description`
    to fix it up by hand. For a submodule scope that must read `jailbee
    submodule pr --description`, not the superproject's `jailbee pr
    --description` — closes the gap left untested after the shared-flow
    extraction (`scope.command` is a `PrScope` property, exercised elsewhere
    only via the superproject scope's default)."""
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=None)
    warn = mocker.patch("jailbee.pr_flow.warn")

    plan = _plan(tmp_path, mocker, cfg=cfg, scope=_sub_scope(tmp_path))

    assert plan == pr_flow.HeadPlan(publish_name="feat/foo", ai_text=None)
    warn.assert_called_once()
    assert "jailbee submodule pr --description" in warn.call_args.args[0]


def test_generation_passes_the_scope_subpath(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=_text())
    mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", side_effect=lambda p, s: p)
    _plan(tmp_path, mocker, cfg=cfg, scope=_sub_scope(tmp_path))
    assert gen.call_args.kwargs["subpath"] == "libs/foo"


def test_no_source_branch_skips_generation(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    plan = _plan(tmp_path, mocker, cfg=cfg, source_branch=None)
    assert plan == pr_flow.HeadPlan(publish_name=None, ai_text=None)
    gen.assert_not_called()


def _outbox_source(manifest="002-d.json", index=0, branch="feat/x"):
    from jailbee.pr_ai import PrText
    from jailbee.pr_outbox import OutboxPrText

    return OutboxPrText(
        text=PrText(title="feat: x", body="Body.", branch=branch),
        manifest=manifest,
        index=index,
    )


def test_create_path_uses_the_outbox_and_never_runs_claude(tmp_path, mocker):
    from jailbee.pr_ai import PrText

    generate = mocker.patch("jailbee.pr_ai.generate_pr_text")
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())
    mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", side_effect=lambda p, s: p)

    plan = _plan(tmp_path, mocker, use_outbox=True)

    generate.assert_not_called()
    assert plan.ai_text == PrText(title="feat: x", body="Body.", branch="feat/x")
    assert plan.outbox_source is not None and plan.outbox_source.manifest == "002-d.json"


def test_create_outbox_lookup_forwards_the_scope_and_source_branch(tmp_path, mocker):
    scope = _sub_scope(tmp_path)
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)

    _plan(
        tmp_path,
        mocker,
        scope=scope,
        source_branch="sub-head",
        use_outbox=True,
    )

    assert pending.call_args.kwargs["scope"] == scope
    assert pending.call_args.kwargs["source_branch"] == "sub-head"


def test_create_path_ignores_the_outbox_when_not_asked(tmp_path, mocker):
    """`jailbee submodule pr` shares this function and must be unaffected."""
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text")
    mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=None)

    _plan(tmp_path, mocker)

    pending.assert_not_called()


def test_update_path_never_looks_at_the_outbox(tmp_path, mocker):
    """The update path is Task 11's; this one must not reach the outbox at all."""
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text")

    plan = _plan(tmp_path, mocker, use_outbox=True, is_update=True, stored_head="user/x")

    pending.assert_not_called()
    assert plan == pr_flow.HeadPlan(publish_name="user/x", ai_text=None)


def test_outbox_branch_goes_through_the_one_confirmation(tmp_path, mocker):
    """A manifest-proposed head is confirmed exactly like a Claude-proposed one."""
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())
    confirm = mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", return_value="feat/chosen")

    plan = _plan(tmp_path, mocker, use_outbox=True)

    confirm.assert_called_once_with("feat/x", "feat/foo")
    assert plan.publish_name == "feat/chosen"


def test_as_name_wins_over_the_outbox(tmp_path, mocker):
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())
    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    confirm = mocker.patch("jailbee.pr_flow.confirm_pr_branch_name")

    plan = _plan(tmp_path, mocker, use_outbox=True, as_name="user/mine")

    assert plan.publish_name == "user/mine"
    assert plan.outbox_source is not None
    confirm.assert_not_called()


def test_explicit_title_and_body_skip_the_outbox_lookup(tmp_path, mocker):
    """Nothing may be consumed when the manifest's text cannot be used."""
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text")

    plan = _plan(tmp_path, mocker, use_outbox=True, title="T", body="B")

    pending.assert_not_called()
    assert plan.outbox_source is None


def test_the_outbox_survives_no_ai(tmp_path, mocker):
    """`--no-ai` skips the Claude run; a manifest is not a Claude run."""
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    generate = mocker.patch("jailbee.pr_ai.generate_pr_text")
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())
    mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", side_effect=lambda p, s: p)

    plan = _plan(tmp_path, mocker, cfg=cfg, use_outbox=True, no_ai=True)

    generate.assert_not_called()
    assert plan.ai_text is not None and plan.ai_text.title == "feat: x"


def test_an_empty_outbox_falls_back_to_the_ai(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)
    generate = mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=_text())
    mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", side_effect=lambda p, s: p)

    plan = _plan(tmp_path, mocker, cfg=cfg, use_outbox=True)

    generate.assert_called_once()
    assert plan.publish_name == "user/ai"
    assert plan.outbox_source is None


def test_record_outbox_consumption_names_the_manifest(tmp_path, mocker):
    from tests.conftest import make_cfg

    record = mocker.patch("jailbee.pr_outbox.record_consumed")
    info = mocker.patch("jailbee.pr_flow.info")

    pr_flow.record_outbox_consumption(
        make_cfg(tmp_path), mocker.MagicMock(), "c1", _outbox_source(), "https://x/pull/1"
    )

    record.assert_called_once()
    assert record.call_args.args[2:5] == ("002-d.json", 0, "https://x/pull/1")
    assert "002-d.json" in info.call_args.args[0]


def test_record_outbox_consumption_is_a_no_op_without_a_source(tmp_path, mocker):
    from tests.conftest import make_cfg

    record = mocker.patch("jailbee.pr_outbox.record_consumed")
    info = mocker.patch("jailbee.pr_flow.info")

    pr_flow.record_outbox_consumption(
        make_cfg(tmp_path), mocker.MagicMock(), "c1", None, "https://x/pull/1"
    )

    record.assert_not_called()
    info.assert_not_called()


def test_record_outbox_consumption_warns_but_does_not_raise(tmp_path, mocker):
    """The PR already exists by then; a failed record must not lose the URL."""
    from jailbee.pr_outbox import FinalizeError
    from tests.conftest import make_cfg

    mocker.patch("jailbee.pr_outbox.record_consumed", side_effect=FinalizeError("disk full"))
    warn = mocker.patch("jailbee.pr_flow.warn")

    pr_flow.record_outbox_consumption(
        make_cfg(tmp_path), mocker.MagicMock(), "c1", _outbox_source(), "https://x/pull/1"
    )

    assert "disk full" in warn.call_args.args[0]


# ---- the update path's outbox description ---------------------------------


def _update_edit(tmp_path, mocker, *, scope=None, **kwargs):
    """`resolve_pr_description_update` with the update path's usual arguments."""
    call = {
        "branch": "feat/foo",
        "base": "main",
        "title": None,
        "body": None,
        "description": False,
        "ai_on": True,
    }
    call.update(kwargs)
    return pr_flow.resolve_pr_description_update(
        _cfg(tmp_path),
        mocker.MagicMock(),
        "c1",
        scope if scope is not None else _super_scope(tmp_path),
        **call,
    )


def test_update_path_prefers_the_outbox_over_regenerating(tmp_path, mocker):
    generate = mocker.patch("jailbee.pr_ai.generate_pr_text")
    confirm = mocker.patch("typer.confirm")
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())

    edit = _update_edit(tmp_path, mocker, use_outbox=True)

    assert edit is not None
    assert (edit.title, edit.body) == ("feat: x", "Body.")
    assert edit.source is not None and edit.source.manifest == "002-d.json"
    generate.assert_not_called()
    confirm.assert_not_called()  # the answer already exists; do not ask


def test_update_outbox_lookup_forwards_the_scope_and_source_branch(tmp_path, mocker):
    scope = _sub_scope(tmp_path)
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)

    _update_edit(
        tmp_path,
        mocker,
        scope=scope,
        branch="sub-head",
        use_outbox=True,
    )

    assert pending.call_args.kwargs["scope"] == scope
    assert pending.call_args.kwargs["source_branch"] == "sub-head"


def test_explicit_title_and_body_still_outrank_the_outbox(tmp_path, mocker):
    """Nothing may be consumed when the manifest's text cannot be used."""
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)

    edit = _update_edit(tmp_path, mocker, title="typed", use_outbox=True)

    assert edit is not None and (edit.title, edit.body) == ("typed", None)
    assert edit.source is None
    pending.assert_not_called()


def test_the_update_path_ignores_the_outbox_when_not_asked(tmp_path, mocker):
    """`jailbee submodule pr` shares this function and keeps the default."""
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)

    assert _update_edit(tmp_path, mocker) is None
    pending.assert_not_called()


def test_a_foreign_pr_takes_a_description_that_names_it_after_confirming(tmp_path, mocker):
    """A PR jailbee did not open is still often the user's own, adopted with
    `jailbee pr --pr N`. Dropping the description its agent wrote for that very
    number was the bug; the guard is a narrowed lookup plus one question."""
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=True)
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())

    edit = _update_edit(tmp_path, mocker, use_outbox=True, foreign_head=True)

    assert edit is not None and (edit.title, edit.body) == ("feat: x", "Body.")
    assert edit.source is not None and edit.source.manifest == "002-d.json"
    assert pending.call_args.kwargs["numbered_only"] is True
    assert confirm.call_count == 1


def test_a_declined_foreign_description_consumes_nothing(tmp_path, mocker):
    """Declining refuses *this text*, not the run: nothing is returned to
    `edit_pr`, so `record_outbox_consumption` is never reached and the manifest
    stays pending for `jailbee review apply`."""
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=False)
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())

    edit = _update_edit(tmp_path, mocker, use_outbox=True, foreign_head=True)

    assert edit is None


def test_a_declined_foreign_description_still_lets_description_regenerate(tmp_path, mocker):
    """`--description` is what the user typed; refusing the manifest's text must
    not refuse that too."""
    from jailbee.pr_ai import PrText

    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=False)
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())
    mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="regen", body="Fresh.", branch="b"),
    )

    edit = _update_edit(
        tmp_path,
        mocker,
        use_outbox=True,
        foreign_head=True,
        description=True,
    )

    assert edit is not None and (edit.title, edit.body) == ("regen", "Fresh.")
    assert edit.source is None  # nothing from the outbox was consumed


def test_a_foreign_description_is_never_applied_off_a_tty(tmp_path, mocker):
    """The question has no answer when there is nobody to ask, and an
    unattended run may not decide it on the user's behalf."""
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    confirm = mocker.patch("typer.confirm")
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())

    edit = _update_edit(tmp_path, mocker, use_outbox=True, foreign_head=True)

    assert edit is None
    confirm.assert_not_called()


def test_an_authored_pr_asks_nothing_before_using_the_outbox(tmp_path, mocker):
    """The confirmation belongs to the foreign head alone: jailbee's own PR
    carries the description its own container wrote, as it always did."""
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm")
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())

    edit = _update_edit(tmp_path, mocker, use_outbox=True, for_pr=77)

    assert edit is not None and edit.source is not None
    assert pending.call_args.kwargs["numbered_only"] is False
    confirm.assert_not_called()


def test_an_explicit_title_still_applies_to_a_foreign_pr(tmp_path, mocker):
    """The outbox gate above must not swallow what the user typed themselves."""
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)

    edit = _update_edit(tmp_path, mocker, title="typed", use_outbox=True, foreign_head=True)

    assert edit is not None and (edit.title, edit.body) == ("typed", None)
    pending.assert_not_called()


def test_a_pre_resolved_outbox_hint_is_not_looked_up_again(tmp_path, mocker):
    """The create path already asked; asking again would put the same choice to
    the user twice in one run, and the second answer would win after the push."""
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)

    edit = _update_edit(tmp_path, mocker, use_outbox=True, outbox_hint=_outbox_source())

    assert edit is not None and (edit.title, edit.body) == ("feat: x", "Body.")
    assert edit.source is not None and edit.source.manifest == "002-d.json"
    pending.assert_not_called()


def test_without_a_hint_the_update_path_still_looks_up_its_own(tmp_path, mocker):
    """The create path resolving nothing (no `pr: null` manifest) must not
    disable the update path's own, first, lookup for a `pr: <number>` one."""
    pending = mocker.patch(
        "jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source("003-for-77.json")
    )

    edit = _update_edit(tmp_path, mocker, use_outbox=True, for_pr=77, outbox_hint=None)

    assert edit is not None and edit.source is not None
    assert edit.source.manifest == "003-for-77.json"
    assert pending.call_args.kwargs["for_pr"] == 77


def test_the_picker_is_not_offered_when_prompting_is_disabled(tmp_path, mocker, monkeypatch):
    """`JAILBEE_NONINTERACTIVE` on a pty: a blocking `questionary.select` here
    would land after the branch was already pushed."""
    monkeypatch.setenv("JAILBEE_NONINTERACTIVE", "1")
    mocker.patch("sys.stdin.isatty", return_value=True)
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)

    _update_edit(tmp_path, mocker, use_outbox=True)

    assert pending.call_args.kwargs["pick"] is None


def test_an_empty_outbox_leaves_the_regeneration_offer_in_place(tmp_path, mocker):
    from jailbee.pr_ai import PrText

    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=True)
    mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="t", body="b", branch="feat/x"),
    )

    edit = _update_edit(tmp_path, mocker, use_outbox=True)

    assert edit is not None and (edit.title, edit.body) == ("t", "b")
    assert edit.source is None
    confirm.assert_called_once()


def _created(number=123, already=False):
    from jailbee.pr import PrCreated

    return PrCreated(
        number=number,
        url=f"https://github.com/acme/widgets/pull/{number}",
        already_existed=already,
    )


def test_create_text_prefers_explicit_fields(tmp_path):
    title, body = pr_flow.resolve_create_text(
        _super_scope(tmp_path),
        ai_on=True,
        ai_text=_text(),
        title="Mine",
        body="Body",
        fallback_ref="refs/jailbee/feat-foo/feat/foo",
        publish_name="feat/foo",
        origin_label="container 'feat-foo'",
    )
    assert (title, body) == ("Mine", "Body")


def test_create_text_uses_the_ai_when_on(tmp_path):
    title, body = pr_flow.resolve_create_text(
        _super_scope(tmp_path),
        ai_on=True,
        ai_text=_text(),
        title=None,
        body=None,
        fallback_ref="refs/jailbee/feat-foo/feat/foo",
        publish_name="feat/foo",
        origin_label="container 'feat-foo'",
    )
    assert (title, body) == ("AI title", "AI body")


def test_create_text_falls_back_to_the_commit_subject(tmp_path, mocker):
    mocker.patch("jailbee.git.commit_subject", return_value="feat: do thing")
    title, body = pr_flow.resolve_create_text(
        _super_scope(tmp_path),
        ai_on=False,
        ai_text=None,
        title=None,
        body=None,
        fallback_ref="refs/jailbee/feat-foo/feat/foo",
        publish_name="feat/foo",
        origin_label="container 'feat-foo'",
    )
    assert title == "feat: do thing"
    assert "container 'feat-foo'" in body


def test_create_text_falls_back_to_the_publish_name(tmp_path, mocker):
    mocker.patch("jailbee.git.commit_subject", return_value=None)
    title, _ = pr_flow.resolve_create_text(
        _super_scope(tmp_path),
        ai_on=False,
        ai_text=None,
        title=None,
        body=None,
        fallback_ref="refs/jailbee/feat-foo/feat/foo",
        publish_name="feat/foo",
        origin_label="container 'feat-foo'",
    )
    assert title == "feat/foo"


def test_create_or_view_records_authorship_on_create(tmp_path, mocker):
    mocker.patch("jailbee.pr.create_pr", return_value=_created())
    state = mocker.MagicMock()

    created = pr_flow.create_or_view_pr(
        _super_scope(tmp_path),
        state,
        is_update=False,
        head="feat/foo",
        base="main",
        title="t",
        body="b",
        draft=True,
        label="jailbee pr",
    )

    assert created.number == 123
    state.record.assert_called_once_with(head="feat/foo", author=True, adopted=False, number=123)


@pytest.mark.parametrize(
    ("is_update", "already_exists"), [(False, False), (False, True), (True, True)]
)
def test_outbox_create_and_existing_lookup_pin_scope_repo(
    tmp_path, mocker, is_update, already_exists
):
    from subprocess import CompletedProcess

    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/library")
    mocker.patch.dict("os.environ", {"GH_REPO": "unrelated/default"})
    commands = []

    def run(cmd, **kwargs):
        if cmd[0] == "git":
            return CompletedProcess(cmd, 0, "https://github.com/acme/library", "")
        commands.append(cmd)
        assert kwargs["cwd"] == tmp_path / "libs/foo"
        if cmd[:3] == ["gh", "pr", "create"]:
            return CompletedProcess(
                cmd,
                1 if already_exists else 0,
                "https://github.com/acme/library/pull/42",
                "already exists" if already_exists else "",
            )
        return CompletedProcess(
            cmd, 0, '{"number": 42, "url": "https://github.com/acme/library/pull/42"}', ""
        )

    mocker.patch("subprocess.run", side_effect=run)
    created = pr_flow.create_or_view_pr(
        _sub_scope(tmp_path),
        mocker.MagicMock(),
        is_update=is_update,
        head="library-head",
        base="main",
        title="Library title",
        body="Library body",
        draft=True,
        label="jailbee submodule pr",
        use_outbox=True,
    )

    assert created.number == 42
    assert len(commands) == (2 if already_exists and not is_update else 1)
    assert all("--repo" in cmd for cmd in commands)
    assert all(cmd[cmd.index("--repo") + 1] == "acme/library" for cmd in commands)


@pytest.mark.parametrize("hinted", [False, True])
def test_outbox_description_edit_pins_scope_repo(tmp_path, mocker, hinted):
    from subprocess import CompletedProcess

    source = _outbox_source()
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=source)
    mocker.patch("jailbee.pr_outbox.record_consumed")
    mocker.patch.dict("os.environ", {"GH_REPO": "unrelated/default"})
    run = mocker.patch("subprocess.run", return_value=CompletedProcess([], 0, "", ""))

    updated = _apply_updates(
        tmp_path, mocker, use_outbox=True, outbox_hint=source if hinted else None
    )

    assert updated.description_source == "002-d.json"
    run.assert_called_once()
    cmd = run.call_args.args[0]
    assert cmd[:4] == ["gh", "pr", "edit", "1234"]
    assert "--repo" in cmd
    assert cmd[cmd.index("--repo") + 1] == "acme/widgets"


def test_outbox_create_refuses_an_unresolvable_scope(tmp_path, mocker):
    from jailbee.pr import PrError

    mocker.patch("jailbee.git.get_remote_url", return_value=None)
    create = mocker.patch("jailbee.pr.create_pr")

    with pytest.raises(PrError, match="GitHub repository"):
        pr_flow.create_or_view_pr(
            _sub_scope(tmp_path),
            mocker.MagicMock(),
            is_update=False,
            head="library-head",
            base="main",
            title="t",
            body="b",
            draft=True,
            label="jailbee submodule pr",
            use_outbox=True,
        )
    create.assert_not_called()


def test_create_or_view_does_not_record_on_update(tmp_path, mocker):
    mocker.patch("jailbee.pr.view_existing_pr", return_value=_created(already=True))
    create = mocker.patch("jailbee.pr.create_pr")
    state = mocker.MagicMock()

    pr_flow.create_or_view_pr(
        _super_scope(tmp_path),
        state,
        is_update=True,
        head="feat/foo",
        base="main",
        title="t",
        body="b",
        draft=True,
        label="jailbee pr",
    )

    create.assert_not_called()
    state.record.assert_not_called()


def test_create_or_view_forwards_record_context_with_the_pr_number(tmp_path, mocker):
    mocker.patch("jailbee.pr.create_pr", return_value=_created(number=456))
    state = mocker.MagicMock()

    pr_flow.create_or_view_pr(
        _super_scope(tmp_path),
        state,
        is_update=False,
        head="feat/foo",
        base="main",
        title="t",
        body="b",
        draft=True,
        label="jailbee pr",
        record_context="failed to record the PR label on 'feat-foo'",
    )

    state.record.assert_called_once_with(
        head="feat/foo",
        author=True,
        adopted=False,
        number=456,
        context="PR #456 created, but failed to record the PR label on 'feat-foo'",
    )


def _apply_updates(tmp_path, mocker, **kwargs):
    """`apply_pr_updates` with the superproject update path's usual arguments."""
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/widgets")
    mocker.patch("jailbee.pr_flow.validate_outbox_source")
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [{"name": "c1", "created_at": "2026-09-30T12:00:00Z"}]
    call = {
        "number": 1234,
        "branch": "feat/foo",
        "base": "main",
        "title": None,
        "body": None,
        "description": False,
        "ready": None,
        "ai_on": True,
        "foreign_head": False,
        "url": "https://x/pull/1234",
    }
    call.update(kwargs)
    return pr_flow.apply_pr_updates(_cfg(tmp_path), incus, "c1", _super_scope(tmp_path), **call)


def test_apply_updates_edits_and_toggles(tmp_path, mocker):
    mocker.patch(
        "jailbee.pr_flow.resolve_pr_description_update",
        return_value=pr_flow.DescriptionUpdate(title="t", body="b"),
    )
    edit = mocker.patch("jailbee.pr.edit_pr")
    ready = mocker.patch("jailbee.pr.set_ready")

    result = _apply_updates(tmp_path, mocker, number=123, description=True, ready=True)

    edit.assert_called_once()
    ready.assert_called_once_with(tmp_path, 123, True)
    assert result == pr_flow.PrUpdate(
        title_changed=True, body_changed=True, state_note=" (marked ready)"
    )


@pytest.mark.parametrize("title", [None, "Typed title"])
def test_outbox_updates_refuse_mutations_without_a_repository(tmp_path, mocker, title):
    from subprocess import CompletedProcess

    mocker.patch("jailbee.git.get_remote_url", return_value=None)
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    run = mocker.patch("subprocess.run", return_value=CompletedProcess([], 0, "", ""))
    warn = mocker.patch("jailbee.pr_flow.warn")

    incus = mocker.MagicMock()
    incus.list_containers.return_value = [{"name": "c1", "created_at": "2026-09-30T12:00:00Z"}]
    updated = pr_flow.apply_pr_updates(
        _cfg(tmp_path),
        incus,
        "c1",
        _super_scope(tmp_path),
        number=42,
        branch="feat/foo",
        base="main",
        title=title,
        body=None,
        description=False,
        ready=True,
        ai_on=False,
        foreign_head=False,
        url="https://github.com/acme/widgets/pull/42",
        use_outbox=True,
    )

    run.assert_not_called()
    assert updated == pr_flow.PrUpdate(title_changed=False, body_changed=False, state_note="")
    assert "Cannot resolve the GitHub repository" in warn.call_args.args[0]


def test_apply_updates_warns_but_survives_an_edit_failure(tmp_path, mocker):
    from jailbee.pr import PrEditError

    mocker.patch(
        "jailbee.pr_flow.resolve_pr_description_update",
        return_value=pr_flow.DescriptionUpdate(title="t", body="b"),
    )
    mocker.patch("jailbee.pr.edit_pr", side_effect=PrEditError("boom"))

    result = _apply_updates(tmp_path, mocker, number=123, description=True)

    assert result == pr_flow.PrUpdate(title_changed=False, body_changed=False, state_note="")


def test_apply_pr_updates_records_the_consumed_description(tmp_path, mocker):
    mocker.patch(
        "jailbee.pr_flow.resolve_pr_description_update",
        return_value=pr_flow.DescriptionUpdate(title="t", body="b", source=_outbox_source()),
    )
    mocker.patch("jailbee.pr.edit_pr")
    record = mocker.patch("jailbee.pr_outbox.record_consumed")

    update = _apply_updates(tmp_path, mocker)

    record.assert_called_once()
    # `record_outbox_consumption` passes manifest/index/url positionally.
    assert record.call_args.args[2:5] == ("002-d.json", 0, "https://x/pull/1234")
    assert update.description_source == "002-d.json"


def test_a_failed_edit_leaves_the_manifest_pending(tmp_path, mocker):
    """Nothing recorded on failure, so a retry reuses the manifest."""
    from jailbee.pr import PrEditError

    mocker.patch(
        "jailbee.pr_flow.resolve_pr_description_update",
        return_value=pr_flow.DescriptionUpdate(title="t", body="b", source=_outbox_source()),
    )
    mocker.patch("jailbee.pr.edit_pr", side_effect=PrEditError("HTTP 403"))
    record = mocker.patch("jailbee.pr_outbox.record_consumed")

    update = _apply_updates(tmp_path, mocker)

    record.assert_not_called()
    assert update.description_source is None


def test_apply_pr_updates_asks_the_outbox_about_its_own_pr(tmp_path, mocker):
    """Without its own number the lookup would accept only `pr: null` manifests
    and never find the description written for the PR being updated."""
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)

    _apply_updates(tmp_path, mocker, use_outbox=True)

    assert pending.call_args.kwargs["for_pr"] == 1234


def test_apply_pr_updates_forwards_a_create_path_hint(tmp_path, mocker):
    """The already-existed path: the create-path lookup's result is reused
    rather than a second lookup being run against a wider candidate set."""
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)
    mocker.patch("jailbee.pr.edit_pr")
    record = mocker.patch("jailbee.pr_outbox.record_consumed")

    update = _apply_updates(
        tmp_path, mocker, use_outbox=True, outbox_hint=_outbox_source("001-hinted.json")
    )

    pending.assert_not_called()
    record.assert_called_once()
    assert update.description_source == "001-hinted.json"


def test_a_failed_consumption_record_still_names_the_source(tmp_path, mocker):
    """The edit landed; a container-side bookkeeping failure must not hide that."""
    from jailbee.pr_outbox import FinalizeError

    mocker.patch(
        "jailbee.pr_flow.resolve_pr_description_update",
        return_value=pr_flow.DescriptionUpdate(title="t", body="b", source=_outbox_source()),
    )
    mocker.patch("jailbee.pr.edit_pr")
    mocker.patch("jailbee.pr_outbox.record_consumed", side_effect=FinalizeError("disk full"))
    warn = mocker.patch("jailbee.pr_flow.warn")

    update = _apply_updates(tmp_path, mocker)

    assert update.description_source == "002-d.json"
    assert "disk full" in warn.call_args.args[0]


def test_the_update_path_does_not_announce_the_source_twice(tmp_path, mocker):
    """`render_pr_outcome` carries the manifest name on this path, so the create
    path's `info` line would be the same sentence a second time."""
    mocker.patch(
        "jailbee.pr_flow.resolve_pr_description_update",
        return_value=pr_flow.DescriptionUpdate(title="t", body="b", source=_outbox_source()),
    )
    mocker.patch("jailbee.pr.edit_pr")
    mocker.patch("jailbee.pr_outbox.record_consumed")
    info = mocker.patch("jailbee.pr_flow.info")

    _apply_updates(tmp_path, mocker)

    info.assert_not_called()


def test_render_outcome_create_draft(tmp_path, mocker):
    success = mocker.patch("jailbee.pr_flow.success")
    pr_flow.render_pr_outcome(
        _super_scope(tmp_path),
        url="https://github.com/acme/widgets/pull/123",
        number=123,
        is_update=False,
        publish_name="feat/foo",
        forced=False,
        ready=False,
        update=None,
    )
    success.assert_called_once_with(
        "Draft PR #123 created for 'feat/foo': https://github.com/acme/widgets/pull/123"
    )


def test_render_outcome_create_ready(tmp_path, mocker):
    success = mocker.patch("jailbee.pr_flow.success")
    pr_flow.render_pr_outcome(
        _super_scope(tmp_path),
        url="https://github.com/acme/widgets/pull/123",
        number=123,
        is_update=False,
        publish_name="feat/foo",
        forced=False,
        ready=True,
        update=None,
    )
    success.assert_called_once_with(
        "PR #123 created for 'feat/foo': https://github.com/acme/widgets/pull/123"
    )


def test_render_outcome_update_all_variants(tmp_path, mocker):
    success = mocker.patch("jailbee.pr_flow.success")
    scope = _super_scope(tmp_path)

    pr_flow.render_pr_outcome(
        scope,
        url="U",
        number=1,
        is_update=True,
        publish_name="feat/foo",
        forced=True,
        ready=None,
        update=pr_flow.PrUpdate(title_changed=True, body_changed=True, state_note=""),
    )
    success.assert_called_with(
        "PR #1 updated — head force-pushed (--force-with-lease), title and description refreshed. U"
    )

    pr_flow.render_pr_outcome(
        scope,
        url="U",
        number=1,
        is_update=True,
        publish_name="feat/foo",
        forced=False,
        ready=None,
        update=pr_flow.PrUpdate(title_changed=False, body_changed=True, state_note=""),
    )
    success.assert_called_with("PR #1 updated — head moved, description refreshed. U")

    pr_flow.render_pr_outcome(
        scope,
        url="U",
        number=1,
        is_update=True,
        publish_name="feat/foo",
        forced=False,
        ready=None,
        update=pr_flow.PrUpdate(title_changed=True, body_changed=False, state_note=""),
    )
    success.assert_called_with("PR #1 updated — head moved, title updated. U")

    pr_flow.render_pr_outcome(
        scope,
        url="U",
        number=1,
        is_update=True,
        publish_name="feat/foo",
        forced=False,
        ready=None,
        update=pr_flow.PrUpdate(
            title_changed=False, body_changed=False, state_note=" (marked draft)"
        ),
    )
    success.assert_called_with(
        "PR #1 updated — head moved; description unchanged. (marked draft) U"
    )


def test_render_outcome_names_the_description_source(tmp_path, mocker):
    """A user who sees no such line knows Claude wrote the description."""
    success = mocker.patch("jailbee.pr_flow.success")
    pr_flow.render_pr_outcome(
        _super_scope(tmp_path),
        url="U",
        number=1,
        is_update=True,
        publish_name="feat/foo",
        forced=False,
        ready=None,
        update=pr_flow.PrUpdate(
            title_changed=True,
            body_changed=True,
            state_note="",
            description_source="002-d.json",
        ),
    )
    success.assert_called_once_with(
        "PR #1 updated — head moved, title and description refreshed "
        "(description from 002-d.json). U"
    )


def test_render_outcome_update_defaults_a_missing_update_to_a_no_op(tmp_path, mocker):
    """FIX 4: `update=None` on the update path (e.g. a detached submodule with
    nothing to regenerate/toggle) used to hit an `assert update is not None`;
    render_pr_outcome now defaults it to a no-op PrUpdate instead of requiring
    every caller to construct one just to satisfy that precondition. Renders
    identically to an explicit no-op PrUpdate (see the last case above)."""
    success = mocker.patch("jailbee.pr_flow.success")
    pr_flow.render_pr_outcome(
        _super_scope(tmp_path),
        url="U",
        number=1,
        is_update=True,
        publish_name="feat/foo",
        forced=False,
        ready=None,
        update=None,
    )
    success.assert_called_once_with("PR #1 updated — head moved; description unchanged. U")


# ---- stacked PR labels ----------------------------------------------------


def test_container_label_state_reads_the_stacked_labels(mocker):
    """A stacked PR is jailbee's own, recorded beside the review container's
    parent PR rather than on top of it — so the same state class reads a
    different label prefix."""
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": "12",  # the parent PR, must not be read here
        "user.jailbee.stacked_pr": "40",
        "user.jailbee.stacked_pr_branch": "fix/x",
        "user.jailbee.stacked_pr_author": "1",
    }.get(key)

    record = pr_flow.ContainerLabelState(incus, "c1", prefix=pr_flow.STACKED_LABEL_PREFIX).read()

    assert record == pr_flow.PrRecord(number=40, head="fix/x", author=True, adopted=False)


def test_container_label_state_writes_the_stacked_labels(mocker):
    incus = mocker.MagicMock()

    pr_flow.ContainerLabelState(incus, "c1", prefix=pr_flow.STACKED_LABEL_PREFIX).record(
        head="fix/x", author=True, adopted=False, number=40
    )

    keys = [call.args[1] for call in incus.config_set.call_args_list]
    assert keys == [
        "user.jailbee.stacked_pr_branch",
        "user.jailbee.stacked_pr_author",
        "user.jailbee.stacked_pr",
    ]


def test_malformed_stacked_label_names_the_stacked_key(mocker):
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.stacked_pr": "not-a-number"
    }.get(key)

    with pytest.raises(pr_flow.MalformedPrLabelError, match=r"user\.jailbee\.stacked_pr="):
        pr_flow.ContainerLabelState(incus, "c1", prefix=pr_flow.STACKED_LABEL_PREFIX).read()


# ---- resolve_review_target ------------------------------------------------


def _record(number=7, head=None, author=False, adopted=False):
    return pr_flow.PrRecord(number=number, head=head, author=author, adopted=adopted)


def _resolve(tmp_path, record, state, **kwargs):
    return pr_flow.resolve_review_target(
        _super_scope(tmp_path),
        state,
        "review-7",
        record,
        **kwargs,
    )


def _fake_select(mocker, index):
    """Patch questionary.select to pick the `index`th choice, honouring the real
    `questionary.Choice` value semantics (`value=None` falls back to the title)."""
    captured: dict[str, list] = {}

    class _Question:
        def ask(self):
            return captured["choices"][index].value

    def fake(message, choices):
        captured["choices"] = choices
        return _Question()

    mocker.patch("questionary.select", side_effect=fake)
    return captured


def test_pick_review_action_returns_the_selected_action(mocker):
    _fake_select(mocker, 0)
    assert pr_flow._pick_review_action(7, "feat/x") == "adopt"
    _fake_select(mocker, 1)
    assert pr_flow._pick_review_action(7, "feat/x") == "stacked"


def test_pick_review_action_maps_the_cancel_entry_to_none(mocker):
    """`questionary.Choice` treats `value=None` as *unset* and falls back to the
    title, so a cancel entry needs an explicit sentinel — otherwise cancelling
    answers the string "cancel" and gets published as an action."""
    _fake_select(mocker, -1)
    assert pr_flow._pick_review_action(7, "feat/x") is None


def test_pick_outbox_manifest_returns_the_selected_name(mocker):
    _fake_select(mocker, 1)
    assert pr_flow._pick_outbox_manifest(["001-a.json", "002-b.json"]) == "002-b.json"


def test_pick_outbox_manifest_maps_the_cancel_entry_to_none(mocker):
    """The same `value=None` trap as `_pick_review_action`: without an explicit
    sentinel the cancel entry answers its own title and is used as a manifest
    name — which is exactly the "never guess between two descriptions" rule."""
    _fake_select(mocker, -1)
    assert pr_flow._pick_outbox_manifest(["001-a.json", "002-b.json"]) is None


def test_the_picker_is_offered_only_on_a_tty(tmp_path, mocker):
    """Off a TTY there is nobody to ask, and `pending_pr_text` must then refuse
    an ambiguity rather than block on a prompt after the branch was pushed."""
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)

    mocker.patch("sys.stdin.isatty", return_value=False)
    _plan(tmp_path, mocker, use_outbox=True)
    assert pending.call_args.kwargs["pick"] is None

    mocker.patch("sys.stdin.isatty", return_value=True)
    _plan(tmp_path, mocker, use_outbox=True)
    assert pending.call_args.kwargs["pick"] is pr_flow._pick_outbox_manifest


def test_the_create_picker_honours_jailbee_noninteractive(tmp_path, mocker, monkeypatch):
    """A pty is not enough: a scripted run sets `JAILBEE_NONINTERACTIVE`, and a
    `questionary.select` here would block after the branch was pushed."""
    monkeypatch.setenv("JAILBEE_NONINTERACTIVE", "1")
    mocker.patch("sys.stdin.isatty", return_value=True)
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)

    _plan(tmp_path, mocker, use_outbox=True)

    assert pending.call_args.kwargs["pick"] is None


def test_review_target_is_none_without_a_pr_label(tmp_path, mocker):
    record = _record(number=None)
    assert _resolve(tmp_path, record, mocker.MagicMock(), yes=False, stacked=False) is None


@pytest.mark.parametrize("labels", [{"author": True}, {"adopted": True}])
def test_review_target_is_none_once_the_decision_was_recorded(tmp_path, mocker, labels):
    record = _record(**labels)
    assert _resolve(tmp_path, record, mocker.MagicMock(), yes=True, stacked=False) is None


def test_stacked_needs_a_review_container(tmp_path, mocker):
    record = _record(number=None)
    with pytest.raises(typer.Exit) as excinfo:
        _resolve(tmp_path, record, mocker.MagicMock(), yes=False, stacked=True)
    assert excinfo.value.exit_code == 2


@pytest.mark.parametrize("labels", [{"author": True}, {"adopted": True}])
def test_stacked_refused_once_the_container_publishes_to_a_pr_head(tmp_path, mocker, labels):
    """The head of an existing PR is fixed; a stacked PR would need a different
    one, so the two are mutually exclusive on the same container."""
    record = _record(**labels)
    with pytest.raises(typer.Exit) as excinfo:
        _resolve(tmp_path, record, mocker.MagicMock(), yes=False, stacked=True)
    assert excinfo.value.exit_code == 2


def test_stacked_flag_returns_the_parent_as_the_base(tmp_path, mocker):
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7, head="feat/x"))
    state = mocker.MagicMock()

    target = _resolve(tmp_path, _record(), state, yes=False, stacked=True)

    assert target == pr_flow.ReviewTarget(
        stacked=True,
        parent_number=7,
        parent_head="feat/x",
        parent_head_ref="refs/jailbee/pr/7/head",
    )
    # Nothing is recorded yet: the stacked PR does not exist until it is created.
    state.record.assert_not_called()


def test_yes_still_adopts_the_parent_head(tmp_path, mocker):
    """`--yes` predates --stacked and means "adopt", so it must not silently
    start opening a second PR instead of updating the reviewed one."""
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7, head="feat/x"))
    state = mocker.MagicMock()

    target = _resolve(tmp_path, _record(), state, yes=True, stacked=False)

    assert target is not None and not target.stacked
    assert target.parent_head == "feat/x"
    state.record.assert_called_once()
    assert state.record.call_args.kwargs["adopted"] is True
    assert state.record.call_args.kwargs["author"] is False


def test_no_tty_without_a_flag_exits_and_names_stacked(tmp_path, mocker):
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7, head="feat/x"))
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    err = mocker.patch("jailbee.pr_flow.error")

    with pytest.raises(typer.Exit) as excinfo:
        _resolve(tmp_path, _record(), mocker.MagicMock(), yes=False, stacked=False)

    assert excinfo.value.exit_code == 1
    assert "--stacked" in err.call_args.args[0]


def test_menu_choice_stacked_opens_a_new_pr(tmp_path, mocker):
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7, head="feat/x"))
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.pr_flow._pick_review_action", return_value="stacked")
    state = mocker.MagicMock()

    target = _resolve(tmp_path, _record(), state, yes=False, stacked=False)

    assert target is not None and target.stacked
    state.record.assert_not_called()


def test_menu_choice_adopt_records_the_decision(tmp_path, mocker):
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7, head="feat/x"))
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.pr_flow._pick_review_action", return_value="adopt")
    state = mocker.MagicMock()

    target = _resolve(tmp_path, _record(), state, yes=False, stacked=False)

    assert target is not None and not target.stacked
    state.record.assert_called_once()


def test_menu_cancel_aborts(tmp_path, mocker):
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7, head="feat/x"))
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.pr_flow._pick_review_action", return_value=None)

    with pytest.raises(typer.Abort):
        _resolve(tmp_path, _record(), mocker.MagicMock(), yes=False, stacked=False)


def test_fork_parent_cannot_be_stacked_on(tmp_path, mocker):
    """A fork PR's head is not a branch in this origin, so it cannot be the
    base of a PR opened here — the stack has to live in the fork."""
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=_pr_info(number=7, head="feat/x", cross=True, owner="someone-else"),
    )
    err = mocker.patch("jailbee.pr_flow.error")

    with pytest.raises(typer.Exit) as excinfo:
        _resolve(tmp_path, _record(), mocker.MagicMock(), yes=False, stacked=True)

    assert excinfo.value.exit_code == 1
    assert "someone-else" in err.call_args.args[0]


def test_fork_parent_still_refuses_adoption(tmp_path, mocker):
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=_pr_info(number=7, head="feat/x", cross=True, owner="someone-else"),
    )

    with pytest.raises(typer.Exit) as excinfo:
        _resolve(tmp_path, _record(), mocker.MagicMock(), yes=True, stacked=False)

    assert excinfo.value.exit_code == 1


def test_unresolvable_parent_pr_exits_1(tmp_path, mocker):
    from jailbee.pr import PrError

    mocker.patch("jailbee.pr.resolve_pr", side_effect=PrError("gh exploded"))

    with pytest.raises(typer.Exit) as excinfo:
        _resolve(tmp_path, _record(), mocker.MagicMock(), yes=True, stacked=False)

    assert excinfo.value.exit_code == 1


def test_validate_missing_outbox_source_does_not_read(mocker, make_cfg, tmp_path):
    from jailbee.pr_flow import validate_outbox_source

    incus = mocker.MagicMock()
    reader = mocker.patch("jailbee.pr_outbox.read_outbox")
    validate_outbox_source(make_cfg(tmp_path), incus, "c", None)
    reader.assert_not_called()
    assert incus.mock_calls == []


@pytest.mark.parametrize("body_file", [None, " space pr=7 .md"])
def test_staged_description_extensions_follow_actual_source_validation(
    mocker, make_cfg, tmp_path, body_file
):
    import json

    from jailbee.outbox_io import ContainerIdentity
    from jailbee.pr_flow import PrScope, validate_outbox_source
    from jailbee.pr_outbox import Outbox, OutboxChanged, pending_pr_text

    cfg = make_cfg(tmp_path)
    action = {"type": "description", "body_file": body_file, "agent_metadata": {"body_file": 17}}
    if body_file is None:
        action["body"] = "Inline"
    payload = {
        "version": 1,
        "repo": "acme/widgets",
        "pr": None,
        "actions": [action],
        "agent_metadata": {"nested": {"body_file": "extension.md"}},
    }
    files = {
        "one.json": json.dumps(payload),
        "extension.md": "Extension",
        **({body_file: "Body"} if body_file else {}),
    }
    identity = ContainerIdentity("c", "created")
    outbox = Outbox(files, identity=identity)
    incus = mocker.Mock()
    incus.config_get.return_value = "feature"
    reader = mocker.patch("jailbee.pr_outbox.read_outbox", return_value=outbox)
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/widgets.git")
    source = pending_pr_text(
        cfg,
        incus,
        "c",
        scope=PrScope(cfg.repo_root, cfg.upstream_remote, "", None),
        source_branch="feature",
        uid=1000,
    )
    assert source is not None
    assert source.body_files == (((body_file, "Body"),) if body_file else ())
    validate_outbox_source(cfg, incus, "c", source)
    reader.return_value = Outbox(
        files | {"extension.md": "Changed ignored file"}, identity=identity
    )
    validate_outbox_source(cfg, incus, "c", source)
    if body_file:
        reader.return_value = Outbox(files | {body_file: "Changed body"}, identity=identity)
        with pytest.raises(OutboxChanged):
            validate_outbox_source(cfg, incus, "c", source)


def test_validate_synthetic_outbox_source_requires_revision(mocker, make_cfg, tmp_path):
    from jailbee.pr_flow import validate_outbox_source
    from jailbee.pr_outbox import OutboxChanged

    incus = mocker.MagicMock()
    with pytest.raises(OutboxChanged, match=r"revision evidence.*refresh"):
        validate_outbox_source(make_cfg(tmp_path), incus, "c", _outbox_source())
    assert incus.mock_calls == []


@pytest.mark.parametrize("outcome", ["edit", "cancel", "failure", "early_return", "changed"])
def test_standalone_updates_guard_and_revalidate_after_prompt(mocker, tmp_path, make_cfg, outcome):
    from contextlib import contextmanager

    from jailbee.outbox.io import PrManagement
    from jailbee.outbox.models import OutboxChanged
    from jailbee.pr import PrEditError

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [{"name": "c1", "created_at": "created"}]
    events = []
    manager = PrManagement(tmp_path / "locks")

    @contextmanager
    def lock(identity):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    mocker.patch.object(manager, "lock", side_effect=lock)
    source = _outbox_source()
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=source)
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)

    def confirm(*args, **kwargs):
        events.append("prompt")
        if outcome == "cancel":
            raise typer.Abort()
        return True

    mocker.patch("typer.confirm", side_effect=confirm)
    mocker.patch(
        "jailbee.git.get_remote_url",
        return_value=None if outcome == "early_return" else "https://github.com/acme/widgets",
    )

    def validate(*args):
        assert args[3] is source
        events.append("validate")
        if outcome == "changed":
            raise OutboxChanged("refresh required")

    mocker.patch.object(pr_flow, "validate_outbox_source", side_effect=validate)

    def edit(*args, **kwargs):
        events.append("edit")
        if outcome == "failure":
            raise PrEditError("denied")

    mocker.patch("jailbee.pr.edit_pr", side_effect=edit)
    mocker.patch(
        "jailbee.pr_outbox.record_consumed", side_effect=lambda *a, **k: events.append("consume")
    )

    def apply():
        return pr_flow.apply_pr_updates(
            cfg,
            incus,
            "c1",
            _super_scope(tmp_path),
            number=42,
            branch="feat/foo",
            base="main",
            title=None,
            body=None,
            description=False,
            ready=None,
            ai_on=False,
            foreign_head=True,
            url="https://x/pull/42",
            use_outbox=True,
            management=manager,
        )

    if outcome in {"cancel", "changed"}:
        with pytest.raises(typer.Abort if outcome == "cancel" else OutboxChanged):
            apply()
    else:
        apply()
    assert events[0] == "enter" and events[-1] == "exit"
    assert events.index("enter") < events.index("prompt")
    if outcome == "edit":
        assert events == ["enter", "prompt", "validate", "edit", "consume", "exit"]
    elif outcome == "failure":
        assert events == ["enter", "prompt", "validate", "edit", "exit"]
    else:
        assert "edit" not in events and "consume" not in events


def test_disabled_publication_guard_never_looks_up_identity(mocker, tmp_path, make_cfg):
    from jailbee.outbox.io import PrManagement

    incus = mocker.MagicMock()
    manager = PrManagement(tmp_path / "locks")
    with pr_flow.outbox_publication_guard(
        make_cfg(tmp_path), incus, "missing", enabled=False, management=manager
    ):
        pass
    assert incus.mock_calls == []
    assert not manager.root.exists()


def test_publication_guard_rejects_identity_replaced_while_waiting(mocker, tmp_path, make_cfg):
    from contextlib import contextmanager

    from jailbee.outbox.io import PrManagement
    from jailbee.outbox.models import OutboxChanged

    incus = mocker.MagicMock()
    incus.list_containers.return_value = [{"name": "c1", "created_at": "original"}]
    manager = PrManagement(tmp_path / "locks")
    events = []

    @contextmanager
    def lock(identity):
        assert identity.created_at == "original"
        events.append("enter")
        incus.list_containers.return_value = [{"name": "c1", "created_at": "replacement"}]
        try:
            yield
        finally:
            events.append("exit")

    mocker.patch.object(manager, "lock", side_effect=lock)
    with pytest.raises(OutboxChanged, match="refresh required"):
        with pr_flow.outbox_publication_guard(
            make_cfg(tmp_path), incus, "c1", enabled=True, management=manager
        ):
            events.append("select")
    assert events == ["enter", "exit"]


def test_detached_source_still_takes_the_outbox_branch(tmp_path, mocker):
    """A detached submodule has no source branch; the manifest's proposed head
    is then the only name there is, and dropping it made `jailbee pr` fail
    with "no head branch name was chosen"."""
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())
    confirm = mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", return_value="feat/x")

    plan = _plan(tmp_path, mocker, use_outbox=True, source_branch=None)

    confirm.assert_called_once_with("feat/x", None)
    assert plan.publish_name == "feat/x"


def test_confirm_branch_name_prompts_when_there_is_no_source_branch(mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    prompt = mocker.patch("typer.prompt", return_value="feat/typed")

    assert pr_flow.confirm_pr_branch_name("feat/x", None) == "feat/typed"
    prompt.assert_called_once()
