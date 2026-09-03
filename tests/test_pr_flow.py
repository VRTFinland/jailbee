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
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    with pytest.raises(typer.Exit) as excinfo:
        pr_flow.confirm_foreign_force_push(
            _super_scope(tmp_path), "feat-foo", "12", "user/x", yes=False
        )
    assert excinfo.value.exit_code == 1


def test_foreign_force_push_aborts_on_decline(tmp_path, mocker):
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
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
    assert result == ("Set", None)
    gen.assert_not_called()


def test_description_update_skips_without_a_request(tmp_path, mocker):
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
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
    assert result == ("t", "b")
    assert gen.call_args.kwargs["subpath"] == "libs/foo"


def test_description_update_offer_is_suppressed_on_a_foreign_pr(tmp_path, mocker):
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
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
        offer_regen=False,
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
    confirmation, offer_regen) for a container that plainly has a PR."""
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
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
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
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
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
    from tests.conftest import make_cfg, with_agent

    cfg = with_agent(make_cfg(tmp_path), "claude", enabled=True)
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
    from tests.conftest import make_cfg, with_agent

    cfg = with_agent(make_cfg(tmp_path), "claude", enabled=True)
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    plan = _plan(tmp_path, mocker, cfg=cfg, no_ai=True)
    assert plan == pr_flow.HeadPlan(publish_name="feat/foo", ai_text=None)
    gen.assert_not_called()


def test_ai_branch_off_keeps_the_source_branch_but_still_generates_text(tmp_path, mocker):
    from tests.conftest import make_cfg, with_agent

    cfg = with_agent(make_cfg(tmp_path), "claude", enabled=True, ai_pr_branch=False)
    mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=_text())
    plan = _plan(tmp_path, mocker, cfg=cfg)
    assert plan.publish_name == "feat/foo"
    assert plan.ai_text is not None


def test_ai_description_off_still_proposes_a_branch(tmp_path, mocker):
    from tests.conftest import make_cfg, with_agent

    cfg = with_agent(make_cfg(tmp_path), "claude", enabled=True, ai_pr_description=False)
    mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=_text())
    mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", side_effect=lambda p, s: p)
    plan = _plan(tmp_path, mocker, cfg=cfg)
    assert plan.publish_name == "user/ai"


def test_both_toggles_off_skips_generation_entirely(tmp_path, mocker):
    from tests.conftest import make_cfg, with_agent

    cfg = with_agent(
        make_cfg(tmp_path),
        "claude",
        enabled=True,
        ai_pr_branch=False,
        ai_pr_description=False,
    )
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    plan = _plan(tmp_path, mocker, cfg=cfg)
    assert plan == pr_flow.HeadPlan(publish_name="feat/foo", ai_text=None)
    gen.assert_not_called()


def test_generation_failure_falls_back_to_the_source_branch(tmp_path, mocker):
    from tests.conftest import make_cfg, with_agent

    cfg = with_agent(make_cfg(tmp_path), "claude", enabled=True)
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
    from tests.conftest import make_cfg, with_agent

    cfg = with_agent(make_cfg(tmp_path), "claude", enabled=True)
    mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=None)
    warn = mocker.patch("jailbee.pr_flow.warn")

    plan = _plan(tmp_path, mocker, cfg=cfg, scope=_sub_scope(tmp_path))

    assert plan == pr_flow.HeadPlan(publish_name="feat/foo", ai_text=None)
    warn.assert_called_once()
    assert "jailbee submodule pr --description" in warn.call_args.args[0]


def test_generation_passes_the_scope_subpath(tmp_path, mocker):
    from tests.conftest import make_cfg, with_agent

    cfg = with_agent(make_cfg(tmp_path), "claude", enabled=True)
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=_text())
    mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", side_effect=lambda p, s: p)
    _plan(tmp_path, mocker, cfg=cfg, scope=_sub_scope(tmp_path))
    assert gen.call_args.kwargs["subpath"] == "libs/foo"


def test_no_source_branch_skips_generation(tmp_path, mocker):
    from tests.conftest import make_cfg, with_agent

    cfg = with_agent(make_cfg(tmp_path), "claude", enabled=True)
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    plan = _plan(tmp_path, mocker, cfg=cfg, source_branch=None)
    assert plan == pr_flow.HeadPlan(publish_name=None, ai_text=None)
    gen.assert_not_called()


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


def test_apply_updates_edits_and_toggles(tmp_path, mocker):
    mocker.patch("jailbee.pr_flow.resolve_pr_description_update", return_value=("t", "b"))
    edit = mocker.patch("jailbee.pr.edit_pr")
    ready = mocker.patch("jailbee.pr.set_ready")

    result = pr_flow.apply_pr_updates(
        _cfg(tmp_path),
        mocker.MagicMock(),
        "c1",
        _super_scope(tmp_path),
        number=123,
        branch="feat/foo",
        base="main",
        title=None,
        body=None,
        description=True,
        ready=True,
        ai_on=True,
        offer_regen=True,
    )

    edit.assert_called_once()
    ready.assert_called_once_with(tmp_path, 123, True)
    assert result == pr_flow.PrUpdate(
        title_changed=True, body_changed=True, state_note=" (marked ready)"
    )


def test_apply_updates_warns_but_survives_an_edit_failure(tmp_path, mocker):
    from jailbee.pr import PrEditError

    mocker.patch("jailbee.pr_flow.resolve_pr_description_update", return_value=("t", "b"))
    mocker.patch("jailbee.pr.edit_pr", side_effect=PrEditError("boom"))

    result = pr_flow.apply_pr_updates(
        _cfg(tmp_path),
        mocker.MagicMock(),
        "c1",
        _super_scope(tmp_path),
        number=123,
        branch="feat/foo",
        base="main",
        title=None,
        body=None,
        description=True,
        ready=None,
        ai_on=True,
        offer_regen=True,
    )

    assert result == pr_flow.PrUpdate(title_changed=False, body_changed=False, state_note="")


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
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    err = mocker.patch("jailbee.pr_flow.error")

    with pytest.raises(typer.Exit) as excinfo:
        _resolve(tmp_path, _record(), mocker.MagicMock(), yes=False, stacked=False)

    assert excinfo.value.exit_code == 1
    assert "--stacked" in err.call_args.args[0]


def test_menu_choice_stacked_opens_a_new_pr(tmp_path, mocker):
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7, head="feat/x"))
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.pr_flow._pick_review_action", return_value="stacked")
    state = mocker.MagicMock()

    target = _resolve(tmp_path, _record(), state, yes=False, stacked=False)

    assert target is not None and target.stacked
    state.record.assert_not_called()


def test_menu_choice_adopt_records_the_decision(tmp_path, mocker):
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7, head="feat/x"))
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.pr_flow._pick_review_action", return_value="adopt")
    state = mocker.MagicMock()

    target = _resolve(tmp_path, _record(), state, yes=False, stacked=False)

    assert target is not None and not target.stacked
    state.record.assert_called_once()


def test_menu_cancel_aborts(tmp_path, mocker):
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7, head="feat/x"))
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
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
