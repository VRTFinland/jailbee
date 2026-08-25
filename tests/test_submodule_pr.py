"""Tests for `jailbee submodule pr`'s detection, resolution and publishing."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from jailbee import git, submodule_pr
from jailbee.incus import IncusError
from tests.conftest import make_cfg


def _line(path, commits="2", branch="feat/foo", dirty="", head="aaa", recorded="aaa", subject="s"):
    return "\t".join([path, commits, branch, dirty, head, recorded, subject])


def _incus_returning(*lines):
    incus = MagicMock()
    incus.exec.return_value = "\n".join(lines) + "\n"
    return incus


def _detect(incus, tmp_path):
    return submodule_pr.detect_candidates(
        make_cfg(tmp_path),
        incus,
        "sampleapp-feat-foo",
        repo_dir="/home/dev/repo",
        base_branch="main",
        short="feat-foo",
    )


def test_detect_parses_one_submodule(tmp_path):
    subs = _detect(_incus_returning(_line("lib/a")), tmp_path)
    assert subs == [
        submodule_pr.SubCandidate(
            path="lib/a",
            commits=2,
            branch="feat/foo",
            dirty=False,
            head_sha="aaa",
            recorded_sha="aaa",
            subject="s",
        )
    ]


def test_detect_reads_an_unknown_count_as_none(tmp_path):
    subs = _detect(_incus_returning(_line("lib/a", commits="?")), tmp_path)
    assert subs[0].commits is None


def test_detect_reads_a_detached_submodule_as_no_branch(tmp_path):
    subs = _detect(_incus_returning(_line("lib/a", branch="")), tmp_path)
    assert subs[0].branch is None


def test_detect_reads_the_dirty_flag(tmp_path):
    subs = _detect(_incus_returning(_line("lib/a", dirty="1")), tmp_path)
    assert subs[0].dirty is True


def test_detect_keeps_tabs_out_of_the_subject_split(tmp_path):
    subs = _detect(_incus_returning(_line("lib/a", subject="feat: a\tb")), tmp_path)
    assert subs[0].subject == "feat: a\tb"


def test_detect_passes_the_base_branch_in_the_environment(tmp_path):
    incus = _incus_returning(_line("lib/a"))
    _detect(incus, tmp_path)
    assert incus.exec.call_args.kwargs["env"]["JB_BASE"] == "main"
    assert incus.exec.call_args.kwargs["cwd"] == "/home/dev/repo"


def test_detect_ignores_blank_and_short_lines(tmp_path):
    incus = MagicMock()
    incus.exec.return_value = "\n\ngarbage\n" + _line("lib/a") + "\n"
    assert [s.path for s in _detect(incus, tmp_path)] == ["lib/a"]


def test_detect_raises_when_the_container_query_fails(tmp_path):
    incus = MagicMock()
    incus.exec.side_effect = IncusError("no such container")
    with pytest.raises(submodule_pr.SubmodulePrError):
        _detect(incus, tmp_path)


def test_gitlink_stale_when_the_superproject_records_another_commit():
    sub = submodule_pr.SubCandidate(
        path="lib/a",
        commits=2,
        branch="feat/foo",
        dirty=False,
        head_sha="aaa",
        recorded_sha="bbb",
        subject="s",
    )
    assert sub.gitlink_stale is True


def test_gitlink_stale_false_when_the_shas_match():
    sub = submodule_pr.SubCandidate(
        path="lib/a",
        commits=2,
        branch="feat/foo",
        dirty=False,
        head_sha="aaa",
        recorded_sha="aaa",
        subject="s",
    )
    assert sub.gitlink_stale is False


@pytest.mark.parametrize(
    ("head_sha", "recorded_sha"),
    [
        pytest.param("", "", id="both-empty"),
        pytest.param("", "aaa", id="head-empty"),
        pytest.param("aaa", "", id="recorded-empty"),
    ],
)
def test_gitlink_stale_false_when_a_sha_is_empty(head_sha, recorded_sha):
    sub = submodule_pr.SubCandidate(
        path="lib/a",
        commits=2,
        branch="feat/foo",
        dirty=False,
        head_sha=head_sha,
        recorded_sha=recorded_sha,
        subject="s",
    )
    assert sub.gitlink_stale is False


def _sub(path, commits=2):
    return submodule_pr.SubCandidate(
        path=path,
        commits=commits,
        branch="feat/foo",
        dirty=False,
        head_sha="aaa",
        recorded_sha="aaa",
        subject="s",
    )


def test_select_explicit_path_wins_even_with_no_commits():
    chosen = submodule_pr.select_target([_sub("lib/a", commits=0)], "lib/a")
    assert chosen.path == "lib/a"


def test_select_explicit_unknown_path_raises():
    with pytest.raises(submodule_pr.UnknownSubmodulePathError) as excinfo:
        submodule_pr.select_target([_sub("lib/a")], "lib/nope")
    assert "lib/a" in str(excinfo.value)


def test_select_auto_targets_the_single_candidate():
    subs = [_sub("lib/a"), _sub("lib/b", commits=0)]
    assert submodule_pr.select_target(subs, None).path == "lib/a"


def test_select_auto_ignores_an_unknown_count():
    subs = [_sub("lib/a", commits=None), _sub("lib/b")]
    assert submodule_pr.select_target(subs, None).path == "lib/b"


def test_select_raises_when_nothing_is_ahead():
    with pytest.raises(submodule_pr.NoSubmoduleCandidatesError):
        submodule_pr.select_target([_sub("lib/a", commits=0)], None)


def test_select_raises_with_the_candidates_when_ambiguous():
    with pytest.raises(submodule_pr.AmbiguousSubmoduleTargetError) as excinfo:
        submodule_pr.select_target([_sub("lib/a"), _sub("lib/b")], None)
    assert [c.path for c in excinfo.value.candidates] == ["lib/a", "lib/b"]


def test_resolve_remote_uses_the_submodules_own_remote(tmp_path, mocker):
    mocker.patch("jailbee.git.detect_upstream_remote", return_value="upstream")
    assert submodule_pr.resolve_remote(tmp_path, "lib/a") == "upstream"


def test_resolve_remote_falls_back_to_origin(tmp_path, mocker):
    mocker.patch("jailbee.git.detect_upstream_remote", return_value=None)
    assert submodule_pr.resolve_remote(tmp_path, "lib/a") == "origin"


def test_resolve_base_override_wins(tmp_path, mocker):
    declared = mocker.patch("jailbee.submodules.declared_branch_for_path")
    assert submodule_pr.resolve_base_branch(tmp_path, "lib/a", override="release") == "release"
    declared.assert_not_called()


def test_resolve_base_uses_the_gitmodules_declaration(tmp_path, mocker):
    mocker.patch("jailbee.submodules.declared_branch_for_path", return_value="develop")
    assert submodule_pr.resolve_base_branch(tmp_path, "lib/a", override=None) == "develop"


def test_resolve_base_reads_the_parent_level_for_a_nested_submodule(tmp_path, mocker):
    declared = mocker.patch("jailbee.submodules.declared_branch_for_path", return_value="develop")
    submodule_pr.resolve_base_branch(tmp_path, "lib/a/inner", override=None)
    assert declared.call_args.args[1] == str(tmp_path / "lib/a")
    assert declared.call_args.args[2] == "inner"


def test_resolve_base_uses_the_remote_head(tmp_path, mocker):
    mocker.patch("jailbee.submodules.declared_branch_for_path", return_value=None)
    mocker.patch("jailbee.git.detect_upstream_remote", return_value="upstream")
    mocker.patch("jailbee.git.run_capture", return_value=(True, "upstream/trunk\n"))
    assert submodule_pr.resolve_base_branch(tmp_path, "lib/a", override=None) == "trunk"


def test_resolve_base_falls_back_to_main(tmp_path, mocker):
    mocker.patch("jailbee.submodules.declared_branch_for_path", return_value=None)
    mocker.patch("jailbee.git.detect_upstream_remote", return_value="origin")
    mocker.patch("jailbee.git.run_capture", return_value=(False, ""))
    assert submodule_pr.resolve_base_branch(tmp_path, "lib/a", override=None) == "main"


def test_source_ref_for_a_branch():
    assert (
        submodule_pr.source_ref("feat-foo", "lib/a", "feat/foo")
        == "refs/jailbee-sub/feat-foo/lib/a/heads/feat/foo"
    )


def test_source_ref_for_a_detached_submodule():
    assert (
        submodule_pr.source_ref("feat-foo", "lib/a", None) == "refs/jailbee-sub/feat-foo/lib/a/HEAD"
    )


def _state(incus, subpath="lib/a"):
    return submodule_pr.SubmodulePrState(incus, "sampleapp-feat-foo", subpath)


def test_state_reads_an_empty_map_as_a_blank_record(mocker):
    from jailbee.pr_flow import PrRecord

    incus = MagicMock()
    incus.config_get.return_value = None
    assert _state(incus).read() == PrRecord(number=None, head=None, author=False, adopted=False)


def test_state_reads_a_recorded_entry(mocker):
    from jailbee.pr_flow import PrRecord

    incus = MagicMock()
    incus.config_get.return_value = (
        '{"lib/a": {"pr": 12, "branch": "user/x", "author": true, "adopted": false}}'
    )
    assert _state(incus).read() == PrRecord(number=12, head="user/x", author=True, adopted=False)


def test_state_ignores_another_paths_entry():
    incus = MagicMock()
    incus.config_get.return_value = '{"lib/b": {"pr": 12, "branch": "user/x"}}'
    assert _state(incus).read().number is None


def test_state_survives_malformed_json():
    incus = MagicMock()
    incus.config_get.return_value = "{not json"
    assert _state(incus).read().number is None


def test_state_record_writes_one_merged_map():
    incus = MagicMock()
    incus.config_get.return_value = '{"lib/b": {"pr": 9, "branch": "old"}}'

    _state(incus).record(head="user/x", author=True, adopted=False, number=12)

    key, value = incus.config_set.call_args.args[1:3]
    assert key == submodule_pr.STATE_KEY
    written = json.loads(value)
    assert written["lib/b"]["pr"] == 9
    assert written["lib/a"] == {
        "pr": 12,
        "branch": "user/x",
        "author": True,
        "adopted": False,
    }


def test_state_record_keeps_the_existing_number_when_none():
    incus = MagicMock()
    incus.config_get.return_value = '{"lib/a": {"pr": 12, "branch": "user/x"}}'

    _state(incus).record(head="user/x", author=False, adopted=True, number=None)

    written = json.loads(incus.config_set.call_args.args[2])
    assert written["lib/a"]["pr"] == 12
    assert written["lib/a"]["adopted"] is True


def test_state_record_survives_a_failed_write():
    incus = MagicMock()
    incus.config_get.return_value = None
    incus.config_set.side_effect = IncusError("boom")

    _state(incus).record(head="user/x", author=True, adopted=False, number=12)


def test_recorded_paths_lists_the_map_keys():
    incus = MagicMock()
    incus.config_get.return_value = '{"lib/b": {"pr": 9}, "lib/a": {"pr": 12}}'
    assert submodule_pr.recorded_paths(incus, "c1") == ["lib/a", "lib/b"]


def _publish(tmp_path, mocker, **kwargs):
    defaults = dict(
        subpath="lib/a",
        repo_dir="/home/dev/repo",
        branch="feat/foo",
        publish_name="user/x",
        remote="origin",
        force=False,
    )
    defaults.update(kwargs)
    return submodule_pr.publish_submodule_branch(
        make_cfg(tmp_path),
        MagicMock(),
        "sampleapp-feat-foo",
        "feat-foo",
        **defaults,
    )


def test_publish_transports_only_the_target_submodule(tmp_path, mocker):
    transport = mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.git.rev_parse", return_value="abc123")
    mocker.patch("jailbee.git.push_to_remote")

    _publish(tmp_path, mocker)

    assert transport.call_args.kwargs["only"] == "lib/a"


def test_publish_pushes_the_branch_ref_to_the_submodule_remote(tmp_path, mocker):
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.git.rev_parse", return_value="abc123")
    push = mocker.patch("jailbee.git.push_to_remote")

    result = _publish(tmp_path, mocker, remote="upstream")

    assert push.call_args.args[0] == tmp_path / "lib/a"
    assert push.call_args.args[1] == "upstream"
    assert push.call_args.args[2] == "refs/jailbee-sub/feat-foo/lib/a/heads/feat/foo"
    assert push.call_args.args[3] == "user/x"
    assert push.call_args.kwargs["force_with_lease"] is None
    assert result.forced is False


def test_publish_uses_the_head_ref_for_a_detached_submodule(tmp_path, mocker):
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.git.rev_parse", return_value="abc123")
    push = mocker.patch("jailbee.git.push_to_remote")

    _publish(tmp_path, mocker, branch=None)

    assert push.call_args.args[2] == "refs/jailbee-sub/feat-foo/lib/a/HEAD"


def test_publish_takes_a_lease_only_with_force(tmp_path, mocker):
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.git.rev_parse", return_value="abc123")
    mocker.patch("jailbee.git.remote_branch_sha", return_value="deadbee")
    push = mocker.patch("jailbee.git.push_to_remote")

    result = _publish(tmp_path, mocker, force=True)

    assert push.call_args.kwargs["force_with_lease"] == "deadbee"
    assert result.forced is True


def test_publish_raises_when_the_source_ref_is_missing(tmp_path, mocker):
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.git.rev_parse", return_value=None)
    push = mocker.patch("jailbee.git.push_to_remote")

    with pytest.raises(submodule_pr.SubmodulePrError) as excinfo:
        _publish(tmp_path, mocker)

    assert "refs/jailbee-sub/feat-foo/lib/a/heads/feat/foo" in str(excinfo.value)
    push.assert_not_called()


def test_publish_maps_a_push_failure_to_a_submodule_pr_error(tmp_path, mocker):
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.git.rev_parse", return_value="abc123")
    mocker.patch("jailbee.git.push_to_remote", side_effect=git.GitError("rejected"))
    mocker.patch("jailbee.retry.confirm_retry_quiet", return_value=False)

    with pytest.raises(submodule_pr.SubmodulePrError) as excinfo:
        _publish(tmp_path, mocker)

    assert "user/x" in str(excinfo.value)
