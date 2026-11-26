"""A done report belongs to the controller, after linked worktrees are removed."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee.inbox import send, socket_cli
from jailbee.inbox.container_cmd import inbox_app


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    info = tmp_path / "container.json"
    info.write_text(json.dumps({"repo_dir": str(repo)}))
    monkeypatch.setattr(send, "CONTAINER_INFO_FILE", info)
    monkeypatch.chdir(repo)
    return repo


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("metadata", ["present", "missing", "malformed"])
def test_done_from_linked_worktree_is_refused_before_sending(
    checkout: Path, tmp_path: Path, monkeypatch, mocker, relative: bool, metadata: str
) -> None:
    gitdir = checkout / ".git" / "worktrees" / "helper"
    gitdir.mkdir(parents=True)
    worktree = tmp_path / "helper"
    worktree.mkdir()
    target = "../repo/.git/worktrees/helper" if relative else str(gitdir)
    (worktree / ".git").write_text(f"gitdir: {target}\n")
    nested = worktree / "src" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    if metadata == "missing":
        send.CONTAINER_INFO_FILE.unlink()
    elif metadata == "malformed":
        send.CONTAINER_INFO_FILE.write_text("not JSON")
    transmit = mocker.patch.object(socket_cli, "call_send", return_value={"state": "done"})

    result = CliRunner().invoke(
        inbox_app, ["send", "message", "--kind", "done", "--text", "partial result"]
    )

    assert result.exit_code == 1, result.output
    assert "done is the top-level agent's report" in result.stderr
    assert "return your result to it" in result.stderr
    assert result.stdout == ""
    transmit.assert_not_called()


@pytest.mark.parametrize("elsewhere", [False, True])
def test_done_from_main_checkout_with_registered_worktree_is_refused(
    checkout: Path, tmp_path: Path, monkeypatch, mocker, elsewhere: bool
) -> None:
    (checkout / ".git" / "worktrees" / "helper").mkdir(parents=True)
    if elsewhere:
        monkeypatch.chdir(tmp_path)
    transmit = mocker.patch.object(socket_cli, "call_send", return_value={"state": "done"})

    result = CliRunner().invoke(
        inbox_app, ["send", "message", "--kind", "done", "--text", "finished"]
    )

    assert result.exit_code == 1, result.output
    assert "git worktree remove" in result.stderr
    assert result.stdout == ""
    transmit.assert_not_called()


def test_done_from_linked_submodule_worktree_is_refused(
    checkout: Path, tmp_path: Path, monkeypatch, mocker
) -> None:
    gitdir = checkout / ".git" / "modules" / "library" / "worktrees" / "helper"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    worktree = tmp_path / "helper"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    monkeypatch.chdir(worktree)
    transmit = mocker.patch.object(socket_cli, "call_send", return_value={"state": "done"})

    result = CliRunner().invoke(
        inbox_app, ["send", "message", "--kind", "done", "--text", "partial result"]
    )

    assert result.exit_code == 1, result.output
    assert "return your result to it" in result.stderr
    transmit.assert_not_called()


@pytest.mark.parametrize("layout", ["main", "empty-registry", "submodule"])
def test_done_without_linked_worktrees_is_sent(
    checkout: Path, monkeypatch, mocker, layout: str
) -> None:
    if layout == "empty-registry":
        (checkout / ".git" / "worktrees").mkdir()
    elif layout == "submodule":
        gitdir = checkout / ".git" / "modules" / "library"
        gitdir.mkdir(parents=True)
        submodule = checkout / "library"
        submodule.mkdir()
        (submodule / ".git").write_text("gitdir: ../.git/modules/library\n")
        monkeypatch.chdir(submodule)
    transmit = mocker.patch.object(socket_cli, "call_send", return_value={"state": "done"})

    result = CliRunner().invoke(
        inbox_app, ["send", "message", "--kind", "done", "--text", "finished"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("message-")
    assert transmit.call_args.args[1]["manifest"]["kind"] == "done"


@pytest.mark.parametrize("kind", ["question", "note", "blocked"])
def test_other_messages_from_linked_worktree_are_sent(
    checkout: Path, tmp_path: Path, monkeypatch, mocker, kind: str
) -> None:
    gitdir = checkout / ".git" / "worktrees" / "helper"
    gitdir.mkdir(parents=True)
    worktree = tmp_path / "helper"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    monkeypatch.chdir(worktree)
    transmit = mocker.patch.object(socket_cli, "call_send", return_value={"state": "done"})

    result = CliRunner().invoke(
        inbox_app, ["send", "message", "--kind", kind, "--text", "controller message"]
    )

    assert result.exit_code == 0, result.output
    assert transmit.call_args.args[1]["manifest"]["kind"] == kind
