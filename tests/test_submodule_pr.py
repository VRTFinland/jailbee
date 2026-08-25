"""Tests for `jailbee submodule pr`'s detection, resolution and publishing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jailbee import submodule_pr
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
