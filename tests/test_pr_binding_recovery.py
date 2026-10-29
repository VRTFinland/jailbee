"""Stateful regressions for binding identity and failed inheritance."""

import json

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.incus import IncusError
from jailbee.pr import PrInfo
from jailbee.pr_flow import ContainerLabelState, PrRecord
from tests.test_cli_pr import _pr_created, _publish_result, _setup


def _persist_labels(incus, labels):
    incus.config_get.side_effect = lambda full, key: labels.get(key)
    incus.config_set.side_effect = lambda full, key, value: labels.__setitem__(key, value)
    incus.config_unset_checked.side_effect = lambda full, key: labels.pop(key, None)


def _publication(mocker):
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container", return_value=_publish_result()
    )
    update = mocker.patch("jailbee.pr.view_existing_pr", return_value=_pr_created(already=True))
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    updates = mocker.patch("jailbee.pr_flow.apply_pr_updates")
    mocker.patch("jailbee.pr_links.link_pr_family")
    return publish, update, create, updates


@pytest.mark.parametrize("stacked", [False, True])
def test_retarget_round_trip_preserves_foreign_protections(mocker, tmp_path, stacked):
    prefix = "user.jailbee.stacked_pr" if stacked else "user.jailbee.pr"
    labels = {
        "user.jailbee.base_branch": "main",
        "user.jailbee.branch": "feat/foo",
        prefix: "10",
        prefix + "_branch": "mine",
        prefix + "_author": "1",
    }
    _, incus = _setup(mocker, tmp_path, labels=labels)
    _persist_labels(incus, labels)
    mocker.patch("jailbee.pr.resolve_pr", return_value=PrInfo(20, "foreign", "abc", "OPEN", "main"))
    if stacked:
        from jailbee.pr_flow import PrScope, bind_pr_by_number

        state = ContainerLabelState(incus, "sampleapp-feat-foo", prefix=prefix)
        bind_pr_by_number(
            PrScope(tmp_path, "origin", "", None),
            state,
            number=20,
            record=state.read(),
            yes=True,
            record_context="test",
            intent="bind",
        )
    else:
        result = CliRunner().invoke(app, ["pr", "bind", "feat-foo", "20", "--yes"])
        assert result.exit_code == 0, result.output
    assert ContainerLabelState(incus, "sampleapp-feat-foo", prefix=prefix).read() == PrRecord(
        20, "foreign", False, True
    )
    publish, update, create, updates = _publication(mocker)
    force = mocker.patch("jailbee.pr_flow.confirm_foreign_force_push")
    result = CliRunner().invoke(app, ["pr", "push", "feat-foo", "--force", "--yes", "--no-ai"])
    assert result.exit_code == 0, result.output
    assert force.call_args.args[2] == "20"
    assert updates.call_args.kwargs["foreign_head"] is True
    assert publish.call_args.kwargs["publish_name"] == "foreign"
    update.assert_called_once()
    create.assert_not_called()


def test_failed_authorship_revocation_does_not_retarget(mocker, tmp_path):
    labels = {
        "user.jailbee.pr": "10",
        "user.jailbee.pr_branch": "mine",
        "user.jailbee.pr_author": "1",
    }
    _, incus = _setup(mocker, tmp_path, labels=labels)
    _persist_labels(incus, labels)
    incus.config_unset_checked.side_effect = IncusError("cannot revoke")
    mocker.patch("jailbee.pr.resolve_pr", return_value=PrInfo(20, "foreign", "abc", "OPEN", "main"))
    result = CliRunner().invoke(app, ["pr", "bind", "feat-foo", "20", "--yes"])
    assert result.exit_code == 1
    assert ContainerLabelState(incus, "sampleapp-feat-foo").read() == PrRecord(
        10, "mine", True, False
    )


@pytest.mark.parametrize("stacked", [False, True])
@pytest.mark.parametrize("failed_unset", range(5))
def test_failed_unbind_retry_restores_ordinary_publication(mocker, tmp_path, stacked, failed_unset):
    prefix = "user.jailbee.stacked_pr" if stacked else "user.jailbee.pr"
    labels = {
        "user.jailbee.base_branch": "main",
        "user.jailbee.branch": "feat/foo",
        prefix: "10",
        prefix + "_branch": "mine",
        prefix + "_author": "1",
        prefix + "_adopted": "1",
        prefix + "_base": "parent",
    }
    _, incus = _setup(mocker, tmp_path, labels=labels)
    _persist_labels(incus, labels)
    count = 0

    def unset(full, key):
        nonlocal count
        count += 1
        if count == failed_unset + 1:
            raise IncusError("unset failed")
        labels.pop(key, None)

    incus.config_unset_checked.side_effect = unset
    args = ["pr", "unbind", "feat-foo", "--yes", *(["--stacked"] if stacked else [])]
    assert CliRunner().invoke(app, args).exit_code == 1
    retry = CliRunner().invoke(app, args)
    assert retry.exit_code == 0, retry.output
    assert not any(key.startswith(prefix) for key in labels)
    _, update, create, _ = _publication(mocker)
    find = mocker.patch("jailbee.pr.find_pr_for_branch", return_value=None)
    result = CliRunner().invoke(app, ["pr", "push", "feat-foo", "--yes", "--no-ai"])
    assert result.exit_code == 0, result.output
    find.assert_called_once()
    create.assert_called_once()
    update.assert_not_called()


@pytest.mark.parametrize("suffix", ["_author", "_adopted", "_base"])
def test_unbind_cleans_residual_only_metadata(mocker, tmp_path, suffix):
    labels = {"user.jailbee.pr" + suffix: "1"}
    _, incus = _setup(mocker, tmp_path, labels=labels)
    _persist_labels(incus, labels)
    result = CliRunner().invoke(app, ["pr", "unbind", "feat-foo", "--yes"])
    assert result.exit_code == 0, result.output
    assert labels == {}
    assert "#None" not in result.output


def test_submodule_open_uses_gitdir_only_host_root(mocker, tmp_path):
    _setup(mocker, tmp_path, labels={"user.jailbee.sub_pr": json.dumps({"lib/a": {"pr": 3}})})
    gitdir = tmp_path / ".git/modules/lib/a"
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=True)
    mocker.patch("jailbee.submodules.host_subrepo_root", return_value=gitdir)
    opened = mocker.patch("jailbee.pr.open_pr_in_browser")
    result = CliRunner().invoke(app, ["submodule", "pr", "open", "feat-foo", "lib/a"])
    assert result.exit_code == 0, result.output
    opened.assert_called_once_with(gitdir, 3)


@pytest.mark.parametrize("submodule", [False, True])
def test_bind_pins_actual_gh_argv_to_configured_remote(mocker, tmp_path, submodule):
    from subprocess import CompletedProcess

    cfg, incus = _setup(mocker, tmp_path)
    cfg.upstream_remote = "publish"
    root = tmp_path / ".git/modules/lib/a" if submodule else tmp_path
    labels = {}
    _persist_labels(incus, labels)
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=True)
    mocker.patch("jailbee.submodules.host_subrepo_root", return_value=root)
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="publish")
    mocker.patch(
        "jailbee.git.get_remote_url",
        side_effect=lambda cwd, remote: (
            "https://github.com/active/repo.git"
            if cwd == root and remote == "publish"
            else "https://github.com/wrong/default.git"
        ),
    )
    payload = json.dumps(
        {
            "number": 20,
            "headRefName": "right-head",
            "headRefOid": "abc",
            "state": "OPEN",
            "baseRefName": "main",
            "author": {"login": "other"},
            "isCrossRepository": False,
            "headRepositoryOwner": {"login": "active"},
        }
    )
    run = mocker.patch(
        "jailbee.pr.subprocess.run",
        side_effect=lambda argv, **kwargs: CompletedProcess(
            argv, 0, "https://github.com/active/repo.git" if argv[0] == "git" else payload, ""
        ),
    )
    args = (
        ["submodule", "pr", "bind", "feat-foo", "lib/a"]
        if submodule
        else ["pr", "bind", "feat-foo"]
    )
    result = CliRunner().invoke(app, [*args, "20", "--yes"])
    assert result.exit_code == 0, result.output
    argv = run.call_args.args[0]
    assert argv[-2:] == ["--repo", "active/repo"]
    assert run.call_args.kwargs["cwd"] == root


@pytest.mark.parametrize("stacked", [False, True])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("flag", ["_author", "_adopted"])
def test_numbered_missing_head_is_resolved_before_publication(
    mocker, tmp_path, stacked, fails, flag
):
    from jailbee.pr import PrError

    prefix = "user.jailbee.stacked_pr" if stacked else "user.jailbee.pr"
    labels = {
        "user.jailbee.base_branch": "main",
        "user.jailbee.branch": "feat/foo",
        prefix: "20",
        prefix + flag: "1",
    }
    _, incus = _setup(mocker, tmp_path, labels=labels)
    _persist_labels(incus, labels)
    resolved = mocker.patch(
        "jailbee.pr.resolve_pr", return_value=PrInfo(20, "real-head", "abc", "OPEN", "main")
    )
    if fails:
        resolved.side_effect = PrError("lookup failed")
    publish, update, create, _ = _publication(mocker)
    result = CliRunner().invoke(app, ["pr", "push", "feat-foo", "--yes", "--no-ai"])
    if fails:
        assert result.exit_code == 1, result.output
        publish.assert_not_called()
    else:
        assert result.exit_code == 0, result.output
        assert publish.call_args.kwargs["publish_name"] == "real-head"
        update.assert_called_once()
        assert (
            ContainerLabelState(incus, "sampleapp-feat-foo", prefix=prefix).read().head
            == "real-head"
        )
    create.assert_not_called()


@pytest.mark.parametrize("submodule", [False, True])
@pytest.mark.parametrize("verb", ["push", "bind", "unbind"])
def test_pending_inheritance_blocks_binding_and_publication(mocker, tmp_path, submodule, verb):
    labels = {
        "user.jailbee.pr_inheritance_pending": "1",
        "user.jailbee.base_branch": "main",
        "user.jailbee.branch": "feat/foo",
    }
    _, incus = _setup(mocker, tmp_path, labels=labels)
    _persist_labels(incus, labels)
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=True)
    mocker.patch(
        "jailbee.submodules.host_subrepo_root", return_value=tmp_path / ".git/modules/lib/a"
    )
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    publish, _, create, _ = _publication(mocker)
    subpublish = mocker.patch("jailbee.submodule_pr.transport_submodule_to_host")
    args = ["submodule", "pr", verb, "feat-foo", "lib/a"] if submodule else ["pr", verb, "feat-foo"]
    if verb == "bind":
        args += ["20"]
    result = CliRunner().invoke(app, [*args, "--yes"])
    assert result.exit_code == 1, result.output
    assert "inheritance" in result.output and "recreate" in result.output
    publish.assert_not_called()
    subpublish.assert_not_called()
    create.assert_not_called()
    assert labels["user.jailbee.pr_inheritance_pending"] == "1"


@pytest.mark.parametrize("failure", ["initial", "number", "branch", "adopted", "clear", None])
@pytest.mark.parametrize("stacked", [False, True])
@pytest.mark.parametrize("golden_copy", [False, True])
def test_lifecycle_inheritance_failure_blocks_next_publication(
    make_cfg, mocker, tmp_path, failure, stacked, golden_copy
):
    from jailbee.lifecycle import NewContainerOptions, new_container

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    prefix = "user.jailbee.stacked_pr" if stacked else "user.jailbee.pr"
    inherited = {prefix: "20", prefix + "_branch": "inherited-head", prefix + "_adopted": "1"}
    labels = {}
    _, incus = _setup(mocker, tmp_path, labels=labels)
    from jailbee.golden import golden_instance_name

    incus.exists.side_effect = lambda name: (
        golden_copy and name == golden_instance_name(cfg.golden.alias)
    )
    mocker.patch("jailbee.golden.golden_instance_pool", return_value="cow")
    _persist_labels(incus, labels)

    def init(image, name, **kwargs):
        if failure == "initial" and kwargs.get("config", {}).get(
            "user.jailbee.pr_inheritance_pending"
        ):
            raise IncusError("initial config failed")
        labels.update(kwargs.get("config", {}))

    def set_label(full, key, value):
        failed_key = {
            "number": prefix,
            "branch": prefix + "_branch",
            "adopted": prefix + "_adopted",
        }.get(failure)
        if key == failed_key:
            raise IncusError("inherited write failed")
        labels[key] = value

    def unset(full, key):
        if failure == "clear" and key == "user.jailbee.pr_inheritance_pending":
            raise IncusError("marker clear failed")
        labels.pop(key, None)

    incus.init.side_effect = init
    incus.copy.side_effect = init
    incus.config_set.side_effect = set_label
    incus.config_unset_checked.side_effect = unset
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=False)
    mocker.patch("jailbee.submodules.transport_submodules_to_container")
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="abc1234")
    mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=False,
        inherited_labels=inherited,
    )
    if failure:
        with pytest.raises((IncusError, RuntimeError)):
            new_container(cfg, incus, opts)
    else:
        new_container(cfg, incus, opts)
    if failure == "initial":
        assert labels == {}
        incus.start.assert_not_called()
        return
    publish, update, create, _ = _publication(mocker)
    result = CliRunner().invoke(app, ["pr", "push", "feat-foo", "--yes", "--no-ai"])
    if failure:
        assert labels.get("user.jailbee.pr_inheritance_pending")
        assert result.exit_code == 1, result.output
        assert "recreate" in result.output
        publish.assert_not_called()
        create.assert_not_called()
    else:
        assert "user.jailbee.pr_inheritance_pending" not in labels
        assert result.exit_code == 0, result.output
        assert publish.call_args.kwargs["publish_name"] == "inherited-head"
        update.assert_called_once()
        create.assert_not_called()


def test_submodule_open_missing_host_repo_does_not_launch(mocker, tmp_path):
    _setup(mocker, tmp_path, labels={"user.jailbee.sub_pr": json.dumps({"lib/a": {"pr": 3}})})
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=False)
    opened = mocker.patch("jailbee.pr.open_pr_in_browser")
    result = CliRunner().invoke(app, ["submodule", "pr", "open", "feat-foo", "lib/a"])
    assert result.exit_code == 1
    assert "not available on the host" in result.output
    opened.assert_not_called()
