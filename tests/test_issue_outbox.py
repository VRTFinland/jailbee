from __future__ import annotations

from pathlib import Path

import pytest


def _write_gitdir(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


def test_resolve_repo_targets_uses_only_the_host_declared_repository_tree(
    tmp_path, mocker, make_cfg
):
    from jailbee.issue_outbox import resolve_repo_targets
    from jailbee.submodules import DeclaredSubmodule

    cfg = make_cfg(tmp_path)
    parser_root = tmp_path / "libs/parser"
    lexer_root = parser_root / "vendor/lexer"
    _write_gitdir(parser_root)
    _write_gitdir(lexer_root)
    _write_gitdir(tmp_path / "container-only")
    mocker.patch(
        "jailbee.issue_outbox.submodules.declared_submodule_remotes",
        return_value=(
            DeclaredSubmodule("libs/parser", "../parser-declaration.git"),
            DeclaredSubmodule(
                "libs/parser/vendor/lexer", "https://github.com/acme/lexer-declaration.git"
            ),
        ),
    )
    mocker.patch(
        "jailbee.issue_outbox.git.detect_upstream_remote",
        side_effect=lambda root: {
            parser_root: "canonical",
            lexer_root: "origin",
        }[root],
    )
    mocker.patch(
        "jailbee.issue_outbox.git.get_remote_url",
        side_effect=lambda root, remote: {
            (tmp_path, "origin"): "https://github.com/acme/app.git",
            (parser_root, "canonical"): "git@github.com:acme/parser.git",
            (lexer_root, "origin"): "ssh://git@github.com/acme/lexer.git",
        }[(root, remote)],
    )

    targets = resolve_repo_targets(cfg)

    assert set(targets) == {".", "libs/parser", "libs/parser/vendor/lexer"}
    assert targets["."] == targets["."].__class__(".", tmp_path, "acme/app")
    assert targets["libs/parser"].slug == "acme/parser"
    assert targets["libs/parser/vendor/lexer"].slug == "acme/lexer"
    assert "container-only" not in targets


def test_resolve_repo_targets_uses_relative_declaration_for_uninitialized_leaf(
    tmp_path, mocker, make_cfg
):
    from jailbee.issue_outbox import RepoTarget, resolve_repo_targets
    from jailbee.submodules import DeclaredSubmodule

    cfg = make_cfg(tmp_path)
    mocker.patch(
        "jailbee.issue_outbox.submodules.declared_submodule_remotes",
        return_value=(DeclaredSubmodule("libs/parser", "../parser.git"),),
    )
    mocker.patch(
        "jailbee.issue_outbox.git.get_remote_url",
        return_value="https://github.com/acme/app.git",
    )
    detect = mocker.patch("jailbee.issue_outbox.git.detect_upstream_remote")

    targets = resolve_repo_targets(cfg)

    assert targets["libs/parser"] == RepoTarget(
        "libs/parser", tmp_path / "libs/parser", "acme/parser"
    )
    detect.assert_not_called()


@pytest.mark.parametrize(
    ("root_url", "declared", "message"),
    [
        ("https://gitlab.com/acme/app.git", (), "superproject.*GitHub"),
        (
            "https://github.com/acme/app.git",
            (("libs/parser", "https://gitlab.com/acme/parser.git"),),
            "libs/parser.*GitHub",
        ),
        (
            "https://github.com/acme/app.git",
            (("libs/parser", "../../../../parser.git"),),
            "libs/parser.*GitHub",
        ),
    ],
)
def test_resolve_repo_targets_rejects_non_github_or_unresolvable_remotes(
    tmp_path, mocker, make_cfg, root_url, declared, message
):
    from jailbee.issue_outbox import resolve_repo_targets
    from jailbee.submodules import DeclaredSubmodule

    cfg = make_cfg(tmp_path)
    mocker.patch(
        "jailbee.issue_outbox.submodules.declared_submodule_remotes",
        return_value=tuple(DeclaredSubmodule(*entry) for entry in declared),
    )
    mocker.patch("jailbee.issue_outbox.git.get_remote_url", return_value=root_url)

    with pytest.raises(ValueError, match=message):
        resolve_repo_targets(cfg)


def test_resolve_repo_targets_does_not_fall_back_to_declaration_for_initialized_repo(
    tmp_path, mocker, make_cfg
):
    from jailbee.issue_outbox import resolve_repo_targets
    from jailbee.submodules import DeclaredSubmodule

    cfg = make_cfg(tmp_path)
    parser_root = tmp_path / "libs/parser"
    _write_gitdir(parser_root)
    mocker.patch(
        "jailbee.issue_outbox.submodules.declared_submodule_remotes",
        return_value=(DeclaredSubmodule("libs/parser", "https://github.com/acme/parser.git"),),
    )
    mocker.patch(
        "jailbee.issue_outbox.git.get_remote_url",
        side_effect=["https://github.com/acme/app.git", None],
    )
    mocker.patch("jailbee.issue_outbox.git.detect_upstream_remote", return_value="upstream")

    with pytest.raises(ValueError, match=r"libs/parser.*upstream remote"):
        resolve_repo_targets(cfg)
