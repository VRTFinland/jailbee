"""Tests for `jailbee branch` and its hidden `jailbee submodule checkout` alias."""

from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


def _cfg(mocker, tmp_path):
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    return cfg_mock


def test_branch_is_a_real_top_level_command():
    """Enumerate through click, not through Typer's registration lists.

    `app.registered_commands[*].name` is None and `app.registered_groups[*].name`
    a DefaultPlaceholder, so asserting on those passes against an empty set.
    """
    import typer.main

    names = set(typer.main.get_command(app).commands)
    assert "branch" in names


def test_branch_is_a_visible_top_level_command():
    import typer.main

    cmd = typer.main.get_command(app).commands["branch"]
    assert cmd.hidden is False


def test_submodule_checkout_alias_is_hidden():
    import typer.main

    sub = typer.main.get_command(app).commands["submodule"]
    assert sub.commands["checkout"].hidden is True


def test_branch_bare_aligns_host_to_current_branch(mocker, tmp_path):
    cfg_mock = _cfg(mocker, tmp_path)
    host = mocker.patch(
        "jailbee.sync.checkout_submodules_on_host", return_value=("feat/foo", [("lib", "feat/foo")])
    )
    resolve = mocker.patch("jailbee.cli._resolve_existing")

    result = runner.invoke(app, ["branch"])

    assert result.exit_code == 0, result.output
    host.assert_called_once_with(cfg_mock, branch=None, switch_superproject=False)
    resolve.assert_not_called()


def test_branch_positional_switches_the_superproject(mocker, tmp_path):
    cfg_mock = _cfg(mocker, tmp_path)
    host = mocker.patch("jailbee.sync.checkout_submodules_on_host", return_value=("master", []))

    result = runner.invoke(app, ["branch", "master"])

    assert result.exit_code == 0, result.output
    host.assert_called_once_with(cfg_mock, branch="master", switch_superproject=True)


def test_branch_submodules_only_leaves_the_superproject(mocker, tmp_path):
    cfg_mock = _cfg(mocker, tmp_path)
    host = mocker.patch("jailbee.sync.checkout_submodules_on_host", return_value=("master", []))

    result = runner.invoke(app, ["branch", "master", "--submodules-only"])

    assert result.exit_code == 0, result.output
    host.assert_called_once_with(cfg_mock, branch="master", switch_superproject=False)


def test_branch_container_form_never_touches_the_host(mocker, tmp_path):
    cfg_mock = _cfg(mocker, tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat-foo"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    host = mocker.patch("jailbee.sync.checkout_submodules_on_host")
    inside = mocker.patch(
        "jailbee.sync.checkout_submodules_in_container", return_value=("feat/foo", [])
    )

    result = runner.invoke(app, ["branch", "--container", "feat-foo"])

    assert result.exit_code == 0, result.output
    host.assert_not_called()
    inside.assert_called_once_with(cfg_mock, incus, "feat-foo", branch=None)


def test_branch_container_form_with_branch_override(mocker, tmp_path):
    cfg_mock = _cfg(mocker, tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat-foo"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    inside = mocker.patch(
        "jailbee.sync.checkout_submodules_in_container", return_value=("master", [])
    )

    result = runner.invoke(app, ["branch", "master", "--container", "feat-foo"])

    assert result.exit_code == 0, result.output
    inside.assert_called_once_with(cfg_mock, incus, "feat-foo", branch="master")


def test_alias_reaches_the_same_helper(mocker, tmp_path):
    cfg_mock = _cfg(mocker, tmp_path)
    host = mocker.patch("jailbee.sync.checkout_submodules_on_host", return_value=("master", []))

    result = runner.invoke(app, ["submodule", "checkout", "-b", "master"])

    assert result.exit_code == 0, result.output
    host.assert_called_once_with(cfg_mock, branch="master", switch_superproject=True)


def test_alias_points_at_the_new_command(mocker, tmp_path):
    _cfg(mocker, tmp_path)
    mocker.patch("jailbee.sync.checkout_submodules_on_host", return_value=("master", []))

    result = runner.invoke(app, ["submodule", "checkout", "-b", "master"])

    combined = (result.output or "") + (result.stderr or "")
    assert "jailbee branch" in combined


def test_alias_is_hidden_from_submodule_help():
    result = runner.invoke(app, ["submodule", "--help"])
    assert result.exit_code == 0
    assert "checkout" not in result.output


def test_submodules_only_with_container_is_a_usage_error(mocker, tmp_path):
    _cfg(mocker, tmp_path)
    inside = mocker.patch("jailbee.sync.checkout_submodules_in_container")
    resolve = mocker.patch("jailbee.cli._resolve_existing")

    result = runner.invoke(
        app, ["branch", "master", "--container", "feat-foo", "--submodules-only"]
    )

    assert result.exit_code == 2, result.output
    inside.assert_not_called()
    resolve.assert_not_called()  # rejected before anything is resolved
    assert "--submodules-only" in (result.output or "") + (result.stderr or "")


def test_alias_rejects_the_same_combination(mocker, tmp_path):
    _cfg(mocker, tmp_path)
    inside = mocker.patch("jailbee.sync.checkout_submodules_in_container")
    resolve = mocker.patch("jailbee.cli._resolve_existing")

    result = runner.invoke(app, ["submodule", "checkout", "feat-foo", "--submodules-only"])

    assert result.exit_code == 2, result.output
    inside.assert_not_called()
    resolve.assert_not_called()  # rejected before anything is resolved


def test_alias_container_form_with_branch_override(mocker, tmp_path):
    """The alias's positional is the CONTAINER and -b is the branch — the
    inverse of `jailbee branch`'s shape. This is the combination most likely
    to be wired backwards."""
    cfg_mock = _cfg(mocker, tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat-foo"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    host = mocker.patch("jailbee.sync.checkout_submodules_on_host")
    inside = mocker.patch(
        "jailbee.sync.checkout_submodules_in_container", return_value=("master", [])
    )

    result = runner.invoke(app, ["submodule", "checkout", "feat-foo", "-b", "master"])

    assert result.exit_code == 0, result.output
    host.assert_not_called()
    inside.assert_called_once_with(cfg_mock, incus, "feat-foo", branch="master")
