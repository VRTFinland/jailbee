"""CLI tests for the `--tags`/`--follow-tags`/`--no-tags` trio.

Kept out of the (already 9000+ line) `tests/test_cli.py`: `_resolve_tag_policy`
plus its four-command wiring is a self-contained unit, and giving it its own
file keeps the wiring tests next to the resolver they depend on.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from jailbee.cli import _resolve_tag_policy, app
from jailbee.lifecycle import ResolvedContainer

# --- _resolve_tag_policy -----------------------------------------------


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({}, "reachable"),
        ({"all_flag": True}, "all"),
        ({"follow_flag": True}, "reachable"),
        ({"no_flag": True}, "none"),
    ],
)
def test_resolve_tag_policy_flag_beats_config(flags, expected):
    kwargs = {"all_flag": False, "follow_flag": False, "no_flag": False} | flags
    assert _resolve_tag_policy("reachable", **kwargs) == expected


def test_resolve_tag_policy_falls_back_to_config():
    assert _resolve_tag_policy("none", all_flag=False, follow_flag=False, no_flag=False) == "none"


def test_tag_flags_are_mutually_exclusive():
    with pytest.raises(typer.Exit):
        _resolve_tag_policy("none", all_flag=True, follow_flag=False, no_flag=True)


# --- End-to-end CLI wiring, one test per command ------------------------


def _wire_single(mocker, tmp_path):
    """A resolved single container, with the tag keys at their defaults."""
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "myrepo"
    cfg_mock.pull.tags = "reachable"
    cfg_mock.push.tags = "none"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat-a"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-a")
    mocker.patch("jailbee.cli._print_fetch_summary")
    mocker.patch("jailbee.cli._print_placement_report")
    return cfg_mock


def test_git_fetch_passes_the_tag_policy(mocker, tmp_path):
    _wire_single(mocker, tmp_path)
    sync_refs = mocker.patch("jailbee.sync.sync_refs_from_container")

    result = CliRunner().invoke(app, ["git", "fetch", "feat-a", "--tags"])

    assert result.exit_code == 0, result.output
    assert sync_refs.call_args.kwargs["tags"] == "all"


def test_git_fetch_defaults_to_the_config_key(mocker, tmp_path):
    cfg = _wire_single(mocker, tmp_path)
    cfg.pull.tags = "none"
    sync_refs = mocker.patch("jailbee.sync.sync_refs_from_container")

    result = CliRunner().invoke(app, ["git", "fetch", "feat-a"])

    assert result.exit_code == 0, result.output
    assert sync_refs.call_args.kwargs["tags"] == "none"


def test_git_checkout_passes_the_tag_policy(mocker, tmp_path):
    _wire_single(mocker, tmp_path)
    mocker.patch("jailbee.cli._should_show_plan", return_value=False)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(mocker.MagicMock(), ResolvedContainer("myrepo-feat-a", False)),
    )
    checkout = mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout", "feat-a", "--no-tags"])

    assert result.exit_code == 0, result.output
    assert checkout.call_args.kwargs["tags"] == "none"


def test_git_pull_passes_the_tag_policy(mocker, tmp_path):
    _wire_single(mocker, tmp_path)
    mocker.patch("jailbee.cli._should_show_plan", return_value=False)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(mocker.MagicMock(), ResolvedContainer("myrepo-feat-a", False)),
    )
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull", "feat-a", "--follow-tags"])

    assert result.exit_code == 0, result.output
    assert do_pull.call_args.kwargs["tags"] == "reachable"


def _push_result(**overrides):
    """A real `PushResult` — `_print_push_summary` does real comparisons
    (`old_oid == new_oid`, `local_only_commits > 0`) on whatever it's handed,
    which raises `TypeError` against an unconfigured `MagicMock`."""
    from jailbee.sync import PushResult

    fields = {
        "source": "feat-a",
        "source_ref": "refs/heads/feat-a",
        "container_ref": "refs/jailbee/host/feat-a",
        "old_oid": None,
        "new_oid": "abc1234",
    } | overrides
    return PushResult(**fields)


# --from names the source explicitly on every push test below so
# push.default_source (a Mock attribute on cfg, matching none of the
# 'default-branch'/'current'/'base' literals) never has to resolve — these
# tests are about the tag flag reaching the transport, not source resolution.


def test_git_push_passes_the_tag_policy(mocker, tmp_path):
    _wire_single(mocker, tmp_path)
    push = mocker.patch("jailbee.sync.push_to_container", return_value=_push_result())

    result = CliRunner().invoke(
        app, ["git", "push", "feat-a", "--plain", "--tags", "--from", "feat-a"]
    )

    assert result.exit_code == 0, result.output
    assert push.call_args.kwargs["tags"] == "all"


def test_git_push_defaults_to_the_config_key(mocker, tmp_path):
    cfg = _wire_single(mocker, tmp_path)
    cfg.push.tags = "reachable"
    push = mocker.patch("jailbee.sync.push_to_container", return_value=_push_result())

    result = CliRunner().invoke(app, ["git", "push", "feat-a", "--plain", "--from", "feat-a"])

    assert result.exit_code == 0, result.output
    assert push.call_args.kwargs["tags"] == "reachable"


def test_git_push_merge_passes_the_tag_policy(mocker, tmp_path):
    """`--merge`'s `tags` must reach `sync.push_and_merge`, not just `--plain`'s.

    `push_and_merge`/`push_and_rebase`/`push_and_reset` gained `tags` alongside
    `push_to_container` — this closes the gap where only the `--plain` action
    forwarded the flag.
    """
    from jailbee.sync import MergeInContainerResult

    _wire_single(mocker, tmp_path)
    merge = mocker.patch(
        "jailbee.sync.push_and_merge",
        return_value=MergeInContainerResult(
            push=_push_result(),
            container_branch="feat-a",
            fast_forward_only=True,
            head_oid="deadbeef1234",
        ),
    )

    result = CliRunner().invoke(
        app, ["git", "push", "feat-a", "--merge", "--tags", "--from", "feat-a"]
    )

    assert result.exit_code == 0, result.output
    assert merge.call_args.kwargs["tags"] == "all"


def test_git_push_rebase_passes_the_tag_policy(mocker, tmp_path):
    from jailbee.sync import RebaseInContainerResult

    _wire_single(mocker, tmp_path)
    rebase = mocker.patch(
        "jailbee.sync.push_and_rebase",
        return_value=RebaseInContainerResult(
            push=_push_result(), container_branch="feat-a", head_oid="deadbeef1234"
        ),
    )

    result = CliRunner().invoke(
        app, ["git", "push", "feat-a", "--rebase", "--follow-tags", "--from", "feat-a"]
    )

    assert result.exit_code == 0, result.output
    assert rebase.call_args.kwargs["tags"] == "reachable"


def test_git_push_force_passes_the_tag_policy(mocker, tmp_path):
    from jailbee.sync import ResetInContainerResult

    _wire_single(mocker, tmp_path)
    reset = mocker.patch(
        "jailbee.sync.push_and_reset",
        return_value=ResetInContainerResult(
            push=_push_result(),
            container_branch="feat-a",
            head_oid="deadbeef1234",
            discarded_commits=0,
            old_branch_oid=None,
        ),
    )

    result = CliRunner().invoke(
        app, ["git", "push", "feat-a", "--force", "--no-tags", "--from", "feat-a"]
    )

    assert result.exit_code == 0, result.output
    assert reset.call_args.kwargs["tags"] == "none"
