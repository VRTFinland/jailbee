"""Version reporting at the package/source boundary."""

import json
import subprocess

import pytest

from jailbee import version_info


def test_stamped_copy_reports_build_identity_without_git(tmp_path, mocker):
    package = tmp_path / "jailbee"
    package.mkdir()
    (package / "_build_info.json").write_text(
        json.dumps({"commit": "abc1234" + "0" * 33, "dirty": True})
    )
    run = mocker.patch("subprocess.run", side_effect=AssertionError("no subprocess"))
    assert version_info.display_version("1.8.9", package_dir=package) == "1.8.9 (abc1234-dirty)"
    run.assert_not_called()


@pytest.mark.parametrize("branch", ["main", "(detached)"])
@pytest.mark.parametrize("changes", ["", "1 .M N... 100644 100644 100644 a b src/file.py\n", "? new.py\n"])
def test_editable_reports_git_status_identity(tmp_path, mocker, branch, changes):
    package = tmp_path / "src" / "jailbee"
    package.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            [], 0, f"# branch.oid {'a' * 40}\n# branch.head {branch}\n{changes}", ""
        ),
    )
    suffix = "-dirty" if changes else ""
    assert version_info.display_version("1.8.9", package_dir=package) == f"1.8.9 (aaaaaaa{suffix})"
    run.assert_called_once()
    assert run.call_args.args[0] == [
        "git", "--no-optional-locks", "status", "--porcelain=v2", "--branch", "--untracked-files=normal"
    ]
    assert run.call_args.kwargs["cwd"] == tmp_path
    assert run.call_args.kwargs["timeout"] <= 1


def test_editable_git_failure_prints_plain_version(tmp_path, mocker):
    package = tmp_path / "src" / "jailbee"
    package.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    mocker.patch("subprocess.run", side_effect=FileNotFoundError())
    assert version_info.display_version("1.8.9", package_dir=package) == "1.8.9"


@pytest.mark.parametrize("metadata", [None, "bad json", "[]", '{"commit": "oops", "dirty": false}'])
def test_plain_install_never_uses_callers_checkout(tmp_path, monkeypatch, mocker, metadata):
    package = tmp_path / "site-packages" / "jailbee"
    package.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    if metadata is not None:
        (package / "_build_info.json").write_text(metadata)
    monkeypatch.chdir(tmp_path)
    mocker.patch("subprocess.run", side_effect=AssertionError("not a source checkout"))
    assert version_info.display_version("1.8.9", package_dir=package) == "1.8.9"


@pytest.mark.parametrize("container", [False, True])
@pytest.mark.parametrize("args", [["--version"], ["version"]])
def test_cli_prints_provenance(container, args, mocker):
    from typer.testing import CliRunner
    from jailbee import __version__, cli, container_cli

    mocker.patch("jailbee.version_info.identity", return_value={"commit": "c" * 40, "dirty": False})
    app = container_cli.container_app if container else cli.app
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"{__version__} (ccccccc)"


def test_packed_ref_checkout_uses_git_not_loose_ref_assumptions(tmp_path, mocker):
    package = tmp_path / "src" / "jailbee"
    package.mkdir(parents=True)
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "packed-refs").write_text(f"{'b' * 40} refs/heads/main\n")
    mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, f"# branch.oid {'b' * 40}\n# branch.head main\n", ""))
    assert version_info.display_version("1.8.9", package_dir=package) == "1.8.9 (bbbbbbb)"
