"""Repository PR command group contracts."""

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.pr import PrInfo
from jailbee.pr_flow import PrRecord, ReviewTarget, offer_outbox_binding
from tests.test_cli_pr import _pr_created, _publish_result, _setup


@pytest.fixture(autouse=True)
def isolated(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.pr_submodule_flow.publish_submodule_prs_first", return_value=[])


def test_bare_pr_shows_help():
    runner = CliRunner()
    result = runner.invoke(app, ["pr"])
    assert result.exit_code == runner.invoke(app, ["submodule"]).exit_code
    for command in ("push", "bind", "unbind", "open"):
        assert command in result.output


def test_old_form_hints_push():
    result = CliRunner().invoke(app, ["pr", "feat-x"])
    assert result.exit_code == 2
    assert "jailbee pr push feat-x" in result.output


def test_submodule_old_form_hints_push():
    result = CliRunner().invoke(app, ["submodule", "pr", "feat-x"])
    assert result.exit_code == 2
    assert "jailbee submodule pr push feat-x" in result.output


def test_submodule_bind_missing_host_subrepo_exits_1(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=False)
    result = CliRunner().invoke(app, ["submodule", "pr", "bind", "feat-foo", "lib/a", "3", "--yes"])
    assert result.exit_code == 1
    assert "Submodule 'lib/a' is not checked out on the host" in result.output


def test_submodule_open_recorded(mocker, tmp_path):
    import json

    _setup(mocker, tmp_path, labels={"user.jailbee.sub_pr": json.dumps({"lib/a": {"pr": 3}})})
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=True)
    mocker.patch("jailbee.submodules.host_subrepo_root", return_value=tmp_path / "lib/a")
    opened = mocker.patch("jailbee.pr.open_pr_in_browser")
    result = CliRunner().invoke(app, ["submodule", "pr", "open", "feat-foo", "lib/a"])
    assert result.exit_code == 0, result.output
    opened.assert_called_once_with(tmp_path / "lib/a", 3)


def test_submodule_bind_records_entry(mocker, tmp_path):
    import json

    _, incus = _setup(mocker, tmp_path)
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=True)
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    mocker.patch("jailbee.pr.resolve_pr", return_value=PrInfo(3, "topic", "abc", "OPEN", "main"))
    result = CliRunner().invoke(app, ["submodule", "pr", "bind", "feat-foo", "lib/a", "3", "--yes"])
    assert result.exit_code == 0, result.output
    assert json.loads(incus.config_set.call_args.args[2]) == {
        "lib/a": {"pr": 3, "branch": "topic", "adopted": True, "author": False}
    }


def test_submodule_bind_path_candidates_include_all_initialized_github_repos(mocker, tmp_path):
    from jailbee.pr import PrError

    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.submodules.host_submodule_paths", return_value=["lib/a", "lib/b", "lib/c"]
    )
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=True)
    mocker.patch("jailbee.submodules.host_subrepo_root", side_effect=lambda root, path: root / path)
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    mocker.patch(
        "jailbee.pr.assert_github_remote",
        side_effect=lambda root, remote: (
            (_ for _ in ()).throw(PrError("not github")) if root.name == "c" else None
        ),
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = CliRunner().invoke(app, ["submodule", "pr", "bind", "feat-foo"])
    assert result.exit_code == 2
    assert "lib/a" in result.output and "lib/b" in result.output
    assert "lib/c" not in result.output


def test_submodule_unbind_removes_entry(mocker, tmp_path):
    import json

    _, incus = _setup(
        mocker, tmp_path, labels={"user.jailbee.sub_pr": json.dumps({"lib/a": {"pr": 3}})}
    )
    result = CliRunner().invoke(app, ["submodule", "pr", "unbind", "feat-foo", "lib/a", "--yes"])
    assert result.exit_code == 0, result.output
    incus.config_unset_checked.assert_called_once_with("sampleapp-feat-foo", "user.jailbee.sub_pr")


def test_submodule_bind_fails_when_record_write_fails(mocker, tmp_path):
    from jailbee.incus import IncusError

    _, incus = _setup(mocker, tmp_path)
    incus.config_set.side_effect = IncusError("write failed")
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=True)
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    mocker.patch("jailbee.pr.resolve_pr", return_value=PrInfo(3, "topic", "abc", "OPEN", "main"))
    result = CliRunner().invoke(app, ["submodule", "pr", "bind", "feat-foo", "lib/a", "3", "--yes"])
    assert result.exit_code == 1
    assert "write failed" in result.output


@pytest.mark.parametrize(
    "command,mode", [(c, m) for c in ("bind", "unbind") for m in ("stopped", "mount")]
)
def test_submodule_binding_is_label_only(mocker, tmp_path, command, mode):
    import json

    labels = {"user.jailbee.mode": "mount" if mode == "mount" else "clone"}
    if command == "unbind":
        labels["user.jailbee.sub_pr"] = json.dumps({"lib/a": {"pr": 3, "branch": "topic"}})
    _, incus = _setup(mocker, tmp_path, labels=labels)
    incus.list_containers.return_value = [
        {"name": "sampleapp-feat-foo", "status": "Stopped" if mode == "stopped" else "Running"}
    ]
    preflight = mocker.patch(
        "jailbee.sync.assert_container_publishable", side_effect=AssertionError("bridge required")
    )
    transport = mocker.patch(
        "jailbee.submodule_pr.transport_submodule_to_host",
        side_effect=AssertionError("transport required"),
    )
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=True)
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    mocker.patch("jailbee.pr.resolve_pr", return_value=PrInfo(3, "topic", "abc", "OPEN", "main"))
    args = (
        ["submodule", "pr", command, "feat-foo", "lib/a"]
        + (["3"] if command == "bind" else [])
        + ["--yes"]
    )
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    preflight.assert_not_called()
    transport.assert_not_called()
    incus.exec.assert_not_called()


def test_submodule_unbind_missing_path_requires_destructive_selection(mocker, tmp_path):
    import json

    _, incus = _setup(
        mocker, tmp_path, labels={"user.jailbee.sub_pr": json.dumps({"lib/a": {"pr": 3}})}
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = CliRunner().invoke(app, ["submodule", "pr", "unbind", "feat-foo", "--yes"])
    assert result.exit_code == 2
    assert "lib/a" in result.output
    incus.config_unset_checked.assert_not_called()


def test_submodule_unbind_no_recorded_paths_is_noop(mocker, tmp_path):
    _, incus = _setup(mocker, tmp_path, labels={})
    result = CliRunner().invoke(app, ["submodule", "pr", "unbind", "feat-foo", "--yes"])
    assert result.exit_code == 0, result.output
    assert "no submodule PR recorded" in result.output
    incus.config_unset_checked.assert_not_called()
    incus.config_set.assert_not_called()


def test_old_option_hints_moved():
    result = CliRunner().invoke(app, ["pr", "--draft", "feat-x"])
    assert result.exit_code == 2
    assert "options moved to `jailbee pr push`" in result.output
    assert "`--open` → `jailbee pr open`" in result.output


def test_git_pr_alias_removed():
    assert CliRunner().invoke(app, ["git", "pr", "x"]).exit_code == 2


@pytest.mark.parametrize("stopped", [False, True])
def test_pr_bind_records_adopted(mocker, tmp_path, stopped):
    _, incus = _setup(mocker, tmp_path)
    publish = mocker.patch("jailbee.sync.publish_branch_from_container")
    if stopped:
        mocker.patch("jailbee.sync.assert_container_publishable", side_effect=AssertionError)
    mocker.patch("jailbee.pr.resolve_pr", return_value=PrInfo(12, "topic", "abc", "OPEN", "main"))
    result = CliRunner().invoke(app, ["pr", "bind", "feat-foo", "12", "--yes"])
    assert result.exit_code == 0, result.output
    for key, value in (("pr_branch", "topic"), ("pr_adopted", "1"), ("pr", "12")):
        assert (
            mocker.call("sampleapp-feat-foo", f"user.jailbee.{key}", value)
            in incus.config_set.call_args_list
        )
    publish.assert_not_called()


@pytest.mark.parametrize("failed_write", [1, 2, 3])
def test_pr_bind_fails_if_label_write_fails(mocker, tmp_path, failed_write):
    from jailbee.incus import IncusError

    _, incus = _setup(mocker, tmp_path)
    incus.config_set.side_effect = [None] * (failed_write - 1) + [IncusError("write failed")]
    mocker.patch("jailbee.pr.resolve_pr", return_value=PrInfo(12, "topic", "abc", "OPEN", "main"))
    result = CliRunner().invoke(app, ["pr", "bind", "feat-foo", "12", "--yes"])
    assert result.exit_code == 1, result.output
    assert "write failed" in result.output
    assert incus.config_set.call_count == failed_write


def test_pr_bind_missing_number_noninteractive_exits_2(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = CliRunner().invoke(app, ["pr", "bind", "feat-foo"])
    assert result.exit_code == 2
    assert "PR number" in result.output


def test_pr_bind_already_bound_noop(mocker, tmp_path):
    _, incus = _setup(
        mocker, tmp_path, labels={"user.jailbee.pr": "12", "user.jailbee.pr_branch": "topic"}
    )
    resolve = mocker.patch("jailbee.pr.resolve_pr")
    result = CliRunner().invoke(app, ["pr", "bind", "feat-foo", "12"])
    assert result.exit_code == 0, result.output
    assert "Container 'feat-foo' is already bound to PR #12." in result.output
    resolve.assert_not_called()
    incus.config_set.assert_not_called()


@pytest.mark.parametrize("command", ["bind", "unbind", "open"])
def test_malformed_pr_label_exits_1(mocker, tmp_path, command):
    _setup(mocker, tmp_path, labels={"user.jailbee.pr": "broken"})
    argv = ["pr", command, "feat-foo"]
    if command == "bind":
        argv += ["12"]
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 1
    assert "broken" in result.output


def test_open_missing_stacked_does_not_fall_back(mocker, tmp_path):
    _setup(mocker, tmp_path, labels={"user.jailbee.pr": "12"})
    opened = mocker.patch("jailbee.pr.open_pr_in_browser")
    result = CliRunner().invoke(app, ["pr", "open", "feat-foo", "--stacked"])
    assert result.exit_code == 1
    opened.assert_not_called()


def test_pr_open_is_host_command():
    from jailbee.remote_ssh.router import is_host_command

    assert is_host_command("pr open")
    for command in ("push", "bind", "unbind"):
        assert not is_host_command(f"pr {command}")


def test_pr_unbind_clears(mocker, tmp_path):
    _, incus = _setup(mocker, tmp_path, labels={"user.jailbee.pr": "12"})
    result = CliRunner().invoke(app, ["pr", "unbind", "feat-foo", "--yes"])
    assert result.exit_code == 0, result.output
    assert (
        mocker.call("sampleapp-feat-foo", "user.jailbee.pr")
        in incus.config_unset_checked.call_args_list
    )
    assert "Unbound" in result.output


@pytest.mark.parametrize("stacked,number", [(False, 12), (True, 13)])
def test_pr_open_primary_and_stacked(mocker, tmp_path, stacked, number):
    _setup(mocker, tmp_path, labels={"user.jailbee.pr": "12", "user.jailbee.stacked_pr": "13"})
    opened = mocker.patch("jailbee.pr.open_pr_in_browser")
    result = CliRunner().invoke(
        app, ["pr", "open", "feat-foo"] + (["--stacked"] if stacked else [])
    )
    assert result.exit_code == 0, result.output
    opened.assert_called_once_with(tmp_path, number)


@pytest.mark.parametrize(
    "case",
    ["authored", "adopted", "name_adopted", "explicit_pr", "stacked", "as", "review_stacked"],
)
def test_push_skips_outbox_reference_scan_for_existing_or_explicit_target(mocker, tmp_path, case):
    labels = {"user.jailbee.base_branch": "main", "user.jailbee.branch": "feat/foo"}
    if case in ("authored", "adopted"):
        labels.update({"user.jailbee.pr": "12", "user.jailbee.pr_branch": "topic"})
        labels[f"user.jailbee.pr_{'author' if case == 'authored' else 'adopted'}"] = "1"
    elif case == "review_stacked":
        labels["user.jailbee.pr"] = "12"
    _setup(mocker, tmp_path, labels=labels)
    refs = mocker.patch("jailbee.pr_outbox.outbox_pr_references")
    offer = mocker.patch("jailbee.pr_flow.offer_outbox_binding")
    if case == "name_adopted":
        mocker.patch("jailbee.pr_flow.adopt_existing_pr_for_branch", return_value=(12, "topic"))
    if case == "explicit_pr":
        mocker.patch(
            "jailbee.pr_flow.bind_pr_by_number",
            return_value=PrRecord(number=12, head="topic", author=False, adopted=True),
        )
    if case in ("review_stacked", "stacked"):
        mocker.patch(
            "jailbee.pr_flow.resolve_review_target",
            return_value=ReviewTarget(
                stacked=True, parent_number=12, parent_head="topic", parent_head_ref="refs/x"
            ),
        )
    mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
    mocker.patch("jailbee.pr_flow.create_or_view_pr", return_value=_pr_created(already=True))
    mocker.patch("jailbee.pr_flow.apply_pr_updates")
    mocker.patch("jailbee.pr_links.link_pr_family")
    args = {"explicit_pr": ["--pr", "12"], "stacked": ["--stacked"], "as": ["--as", "other"]}
    result = CliRunner().invoke(app, ["pr", "push", "feat-foo", "--no-ai", *args.get(case, [])])
    assert result.exit_code == 0, result.output
    refs.assert_not_called()
    offer.assert_not_called()


def test_push_skips_scan_when_scope_slug_missing(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.pr_outbox.scope_slug", return_value=None)
    refs = mocker.patch("jailbee.pr_outbox.outbox_pr_references")
    mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
    mocker.patch("jailbee.pr_flow.create_or_view_pr", return_value=_pr_created(already=True))
    mocker.patch("jailbee.pr_flow.apply_pr_updates")
    mocker.patch("jailbee.pr_links.link_pr_family")
    result = CliRunner().invoke(app, ["pr", "push", "feat-foo", "--no-ai"])
    assert result.exit_code == 0, result.output
    refs.assert_not_called()


@pytest.mark.parametrize("yes,interactive", [(True, True), (False, False)])
def test_push_real_offer_gate_never_implicitly_binds(mocker, tmp_path, yes, interactive):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.pr_flow.offer_outbox_binding", side_effect=offer_outbox_binding)
    mocker.patch("jailbee.prompting.is_interactive", return_value=interactive)
    picker = mocker.patch("jailbee.prompting.choose")
    refs = mocker.patch("jailbee.pr_outbox.outbox_pr_references", return_value={5: ("a.json",)})
    binding = mocker.patch("jailbee.pr_flow.bind_pr_by_number")
    mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
    mocker.patch("jailbee.pr_flow.create_or_view_pr", return_value=_pr_created(already=True))
    mocker.patch("jailbee.pr_flow.apply_pr_updates")
    mocker.patch("jailbee.pr_links.link_pr_family")
    result = CliRunner().invoke(
        app, ["pr", "push", "feat-foo", "--no-ai", *(["--yes"] if yes else [])]
    )
    assert result.exit_code == 0, result.output
    assert "jailbee pr bind" in result.output
    refs.assert_called_once()
    binding.assert_not_called()
    picker.assert_not_called()


@pytest.mark.parametrize("no_outbox", [False, True])
def test_pr_push_offers_outbox_reference(mocker, tmp_path, no_outbox):
    _setup(mocker, tmp_path)
    refs = mocker.patch("jailbee.pr_outbox.outbox_pr_references", return_value={5: ("a.json",)})
    mocker.patch("jailbee.pr_flow.offer_outbox_binding", return_value=5)
    bound = mocker.patch(
        "jailbee.pr_flow.bind_pr_by_number",
        return_value=PrRecord(number=5, head="topic", author=False, adopted=True),
    )
    mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
    created = mocker.patch(
        "jailbee.pr_flow.create_or_view_pr", return_value=_pr_created(already=True)
    )
    mocker.patch("jailbee.pr_flow.apply_pr_updates")
    mocker.patch("jailbee.pr_links.link_pr_family")
    result = CliRunner().invoke(
        app, ["pr", "push", "feat-foo", "--no-ai"] + (["--no-outbox"] if no_outbox else [])
    )
    assert result.exit_code == 0, result.output
    if no_outbox:
        refs.assert_not_called()
        bound.assert_not_called()
    else:
        assert bound.call_args.kwargs["number"] == 5
        assert bound.call_args.kwargs["confirmed"] is True
        assert created.call_args.kwargs["is_update"] is True
