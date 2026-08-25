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


def test_adopt_returns_none_without_a_branch(tmp_path, mocker):
    state = mocker.MagicMock()
    assert (
        pr_flow.adopt_existing_pr_for_branch(_super_scope(tmp_path), state, branch=None, yes=True)
        is None
    )


def test_adopt_returns_none_when_no_pr_exists(tmp_path, mocker):
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=None)
    assert (
        pr_flow.adopt_existing_pr_for_branch(
            _super_scope(tmp_path), mocker.MagicMock(), branch="feat/foo", yes=True
        )
        is None
    )


@pytest.mark.parametrize("state_value", ["CLOSED", "MERGED"])
def test_adopt_skips_a_closed_or_merged_pr(tmp_path, mocker, state_value):
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=_pr_info(state=state_value))
    assert (
        pr_flow.adopt_existing_pr_for_branch(
            _super_scope(tmp_path), mocker.MagicMock(), branch="feat/foo", yes=True
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
            _super_scope(tmp_path), mocker.MagicMock(), branch="feat/foo", yes=True
        )
        is None
    )


def test_adopt_records_the_pr_and_returns_it(tmp_path, mocker):
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=_pr_info())
    state = mocker.MagicMock()

    result = pr_flow.adopt_existing_pr_for_branch(
        _super_scope(tmp_path), state, branch="feat/foo", yes=True
    )

    assert result == (7, "feat/foo")
    state.record.assert_called_once_with(head="feat/foo", author=False, adopted=True, number=7)


def test_adopt_exits_1_without_a_tty(tmp_path, mocker):
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    with pytest.raises(typer.Exit) as excinfo:
        pr_flow.adopt_existing_pr_for_branch(
            _super_scope(tmp_path), mocker.MagicMock(), branch="feat/foo", yes=False
        )
    assert excinfo.value.exit_code == 1


def test_adopt_aborts_on_decline(tmp_path, mocker):
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=False)
    with pytest.raises(typer.Abort):
        pr_flow.adopt_existing_pr_for_branch(
            _super_scope(tmp_path), mocker.MagicMock(), branch="feat/foo", yes=False
        )
