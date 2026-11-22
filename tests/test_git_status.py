"""Tests for git_status.py."""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from jailbee.git_status import (
    GitStatus,
    OtherBranch,
    SubmoduleChange,
    _parse_submodules,
    _shortstat_ints,
    merge_label,
    parse_other_branches,
    parse_shortstat,
    probe_container_git,
)
from jailbee.host_target import SyncTarget, TargetSnapshot
from jailbee.incus import IncusError


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "clean"),
        ("\n", "clean"),
        ("   \n  \n", "clean"),
        (" 1 file changed, 12 insertions(+), 3 deletions(-)\n", "+12 -3"),
        (" 1 file changed, 12 insertions(+)\n", "+12 -0"),
        (" 1 file changed, 3 deletions(-)\n", "+0 -3"),
        # Two concatenated lines (staged + unstaged) — sums.
        (
            " 1 file changed, 5 insertions(+), 2 deletions(-)\n"
            " 2 files changed, 7 insertions(+), 1 deletion(-)\n",
            "+12 -3",
        ),
        # Mixed: one clean side, one dirty.
        (" 2 files changed, 7 insertions(+), 1 deletion(-)\n", "+7 -1"),
        # Singular: "1 deletion(-)" (no s).
        (" 1 file changed, 1 insertion(+), 1 deletion(-)\n", "+1 -1"),
        # Malformed input → "?".
        ("nonsense output", "?"),
    ],
)
def test_parse_shortstat(raw: str, expected: str) -> None:
    assert parse_shortstat(raw) == expected


def test_gitstatus_has_conflict_field() -> None:
    s = GitStatus(wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok")
    assert s.conflict == "ok"


def test_probe_returns_parsed_status_when_snippet_emits_four_fields(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = (
        " 1 file changed, 5 insertions(+), 2 deletions(-)\n"
        " 2 files changed, 7 insertions(+), 1 deletion(-)\n"
        "\x00"
        " 4 files changed, 200 insertions(+), 18 deletions(-)\n"
        "\x00"
        "3\n"
        "\x00"
        "conflict\n"
        "\x00"
    )
    result = probe_container_git(
        incus,
        full_name="SampleApp-feat-foo",
        repo_dir="/home/dev/SampleApp",
        base_branch="dev",
        default_branch="main",
    )
    assert result.wt == "+12 -3"
    assert result.ahead_diff == "+200 -18"
    assert result.ahead_count == "3"
    assert result.conflict == "conflict"


def test_probe_returns_clean_when_snippet_emits_empty_fields(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00\x00\x00ok\x00"
    result = probe_container_git(
        incus,
        full_name="c",
        repo_dir="/repo",
        base_branch=None,
        default_branch="main",
    )
    # Empty ahead_count is treated as "?", not "0", since the snippet
    # explicitly writes "0" when base resolves and "?" when it doesn't.
    assert result.wt == "clean"
    assert result.ahead_diff == "clean"
    assert result.ahead_count == "?"
    assert result.conflict == "ok"


def test_clean_submodule_keeps_wt_clean(mocker):
    # git status --porcelain stays empty when submodules are clean ->
    # WT must read clean (default git behavior, no --ignore-submodules needed).
    incus = mocker.MagicMock()
    # The probe snippet produces three NUL-separated fields.  A repo with
    # only clean submodules emits an empty WT field (porcelain output is
    # ""), the ahead/behind shortstat is also empty (on-branch, clean), and
    # rev-list count is "0".  This mirrors test_probe_returns_clean_when_
    # snippet_emits_empty_fields but pins the submodule-specific scenario.
    incus.exec.return_value = "\x00\x000\n\x00"
    result = probe_container_git(
        incus,
        full_name="SampleApp-feat-submod",
        repo_dir="/home/dev/SampleApp",
        base_branch="main",
        default_branch="main",
    )
    assert result.wt == "clean"


def test_probe_returns_all_question_marks_on_incus_error(mocker):
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("boom")
    result = probe_container_git(
        incus,
        full_name="c",
        repo_dir="/repo",
        base_branch="main",
        default_branch="main",
    )
    assert result == GitStatus(wt="?", ahead_diff="?", ahead_count="?", conflict="?")


def test_probe_returns_all_question_marks_on_timeout(mocker):
    """A busy container (e.g. mid background-create) makes the probe
    time out; `incus.exec` raises `IncusError` (the wrapper normalizes
    `subprocess.TimeoutExpired`), and the probe degrades to all-`?`
    rather than crashing the listing."""
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("`incus exec c` timed out after 3s")
    result = probe_container_git(
        incus,
        full_name="c",
        repo_dir="/repo",
        base_branch="main",
        default_branch="main",
    )
    assert result == GitStatus(wt="?", ahead_diff="?", ahead_count="?", conflict="?")


def _probe_warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "jailbee.git_status"]


def test_probe_logs_the_exec_error_that_made_it_unknown(mocker, caplog):
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("`incus exec c` timed out after 3s")
    with caplog.at_level("WARNING", logger="jailbee.git_status"):
        probe_container_git(incus, "c", "/repo", "main", "main")
    (message,) = _probe_warnings(caplog)
    assert "c" in message
    assert "timed out after 3s" in message


def test_probe_logs_short_output(mocker, caplog):
    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00"
    with caplog.at_level("WARNING", logger="jailbee.git_status"):
        probe_container_git(incus, "c", "/repo", "main", "main")
    (message,) = _probe_warnings(caplog)
    assert "short output" in message


def test_probe_logs_a_missing_repo_reported_by_the_snippet(mocker, caplog):
    incus = mocker.MagicMock()
    incus.exec.return_value = "?\x00?\x00?\x00?\x00"
    with caplog.at_level("WARNING", logger="jailbee.git_status"):
        probe_container_git(incus, "c", "/repo", "main", "main")
    (message,) = _probe_warnings(caplog)
    assert "repository" in message


def test_probe_logs_nothing_for_a_clean_result(mocker, caplog):
    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00\x000\n\x00ok\x00"
    with caplog.at_level("WARNING", logger="jailbee.git_status"):
        probe_container_git(incus, "c", "/repo", "main", "main")
    assert _probe_warnings(caplog) == []


def test_probe_passes_env_vars_into_snippet(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00\x00\x00ok\x00"
    probe_container_git(
        incus,
        full_name="c",
        repo_dir="/repo",
        base_branch="feature/x",
        default_branch="develop",
        timeout_s=5,
    )
    args, kwargs = incus.exec.call_args
    assert args[0] == "c"
    assert args[1][0] == "bash"
    assert kwargs.get("env") == {
        "REPO_DIR": "/repo",
        "BASE_BRANCH": "feature/x",
        "DEFAULT_BRANCH": "develop",
        "HOST_HEAD": "",
        "TARGET_SHA": "",
        "HOST_TARGET_SHA": "",
        "HOST_SOURCE": "/mnt/host-source",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    assert kwargs.get("timeout") == 5


def test_probe_forwards_uid_to_incus_exec(mocker):
    """Without uid, git refuses with 'dubious ownership' (root vs dev-owned repo)."""
    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00\x00\x00ok\x00"
    probe_container_git(
        incus,
        full_name="c",
        repo_dir="/repo",
        base_branch="main",
        default_branch="main",
        uid=53023,
    )
    assert incus.exec.call_args.kwargs.get("uid") == 53023


def test_probe_many_parallel_forwards_uid(mocker):
    from jailbee.git_status import probe_many_parallel

    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00\x00\x00ok\x00"
    probe_many_parallel(
        incus,
        targets=[("c1", "/r1", "main")],
        default_branch="main",
        uid=53023,
    )
    assert incus.exec.call_args.kwargs.get("uid") == 53023


def test_probe_passes_empty_base_branch_when_none(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00\x00\x00?\x00"
    probe_container_git(
        incus,
        full_name="c",
        repo_dir="/repo",
        base_branch=None,
        default_branch="main",
    )
    _args, kwargs = incus.exec.call_args
    assert kwargs.get("env", {}).get("BASE_BRANCH") == ""


def test_probe_many_parallel_returns_one_entry_per_target(mocker):
    from jailbee.git_status import probe_many_parallel

    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00\x00\x00ok\x00"

    results = probe_many_parallel(
        incus,
        targets=[("c1", "/r1", "main"), ("c2", "/r2", "main"), ("c3", "/r3", None)],
        default_branch="main",
    )
    assert set(results) == {"c1", "c2", "c3"}
    for r in results.values():
        assert r.wt == "clean"


def test_probe_many_parallel_failed_target_does_not_break_others(mocker):
    from jailbee.git_status import probe_many_parallel

    incus = mocker.MagicMock()

    def side_effect(name, *_args, **_kwargs):
        if name == "c-bad":
            raise IncusError("boom")
        return "\x00\x00\x00ok\x00"

    incus.exec.side_effect = side_effect

    results = probe_many_parallel(
        incus,
        targets=[("c1", "/r1", "main"), ("c-bad", "/r2", "main"), ("c3", "/r3", "main")],
        default_branch="main",
    )
    assert results["c-bad"].wt == "?"
    assert results["c1"].wt == "clean"
    assert results["c3"].wt == "clean"


def test_probe_many_parallel_with_empty_target_list_returns_empty_dict(mocker):
    from jailbee.git_status import probe_many_parallel

    incus = mocker.MagicMock()
    results = probe_many_parallel(
        incus,
        targets=[],
        default_branch="main",
    )
    assert results == {}
    incus.exec.assert_not_called()


def test_probe_snippet_never_uses_pinned_base_ref():
    from jailbee.git_status import _PROBE_SNIPPET

    assert "refs/jailbee/base/" not in _PROBE_SNIPPET
    assert "refs/remotes/origin/" not in _PROBE_SNIPPET
    assert 'git cat-file -e "${TARGET_SHA}^{commit}"' in _PROBE_SNIPPET


def test_probe_snippet_never_uses_default_branch_as_fallback():
    from jailbee.git_status import _PROBE_SNIPPET

    assert "DEFAULT_BRANCH" not in _PROBE_SNIPPET


def test_probe_returns_unknown_when_base_set_but_unresolved(mocker):
    """End-to-end parse: when the snippet cannot resolve a requested base
    branch it emits all-`?` (BASE stayed empty), and the parser preserves that
    rather than inventing a comparison."""
    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00?\x00?\x00?\x00"
    status = probe_container_git(
        incus,
        full_name="c",
        repo_dir="/repo",
        base_branch="release/0.98.0",
        default_branch="main",
    )
    assert status.wt == "clean"
    assert status.ahead_diff == "?"
    assert status.ahead_count == "?"
    assert status.conflict == "?"


def test_probe_snippet_sums_submodule_wt():
    from jailbee.git_status import _PROBE_SNIPPET

    # Working-tree submodule content is summed recursively.
    assert "submodule foreach --recursive --quiet" in _PROBE_SNIPPET
    assert "'git diff-index --shortstat HEAD || :'" in _PROBE_SNIPPET
    # Superproject WT ignores dirty submodule content (avoids double count)
    # while still flagging pointer/commit changes.
    assert "--ignore-submodules=dirty" in _PROBE_SNIPPET


def test_probe_sums_submodule_wt_into_wt_field(mocker):
    """Superproject + submodule WT shortstat lines sum into a single wt value."""
    incus = mocker.MagicMock()
    # WT field: superproject staged+unstaged, then a submodule foreach line.
    incus.exec.return_value = (
        " 1 file changed, 2 insertions(+), 1 deletion(-)\n"  # superproject WT
        " 1 file changed, 5 insertions(+)\n"  # submodule WT
        "\x00"
        "\x00"
        "0\n"
        "\x00"
        "ok\x00"
    )
    result = probe_container_git(
        incus,
        full_name="SampleApp-feat-submod",
        repo_dir="/home/dev/SampleApp",
        base_branch="main",
        default_branch="main",
    )
    assert result.wt == "+7 -1"


def test_probe_sums_submodule_only_wt(mocker):
    """Superproject clean, one dirty submodule -> wt reflects the submodule alone."""
    incus = mocker.MagicMock()
    incus.exec.return_value = (
        " 1 file changed, 4 insertions(+), 2 deletions(-)\n"  # submodule WT only
        "\x00"
        "\x00"
        "0\n"
        "\x00"
        "ok\x00"
    )
    result = probe_container_git(
        incus,
        full_name="SampleApp-feat-submod",
        repo_dir="/home/dev/SampleApp",
        base_branch="main",
        default_branch="main",
    )
    assert result.wt == "+4 -2"


def test_probe_snippet_sums_submodule_committed():
    from jailbee.git_status import _PROBE_SNIPPET

    # Superproject committed diff drops the gitlink pointer (replaced by real delta).
    assert "--shortstat --ignore-submodules=all" in _PROBE_SNIPPET
    assert '"${BASE}" HEAD' in _PROBE_SNIPPET
    # Gitlink SHA pairs are extracted from raw diff and diffed inside the submodule.
    assert 'git diff --raw --abbrev=40 "${BASE}" HEAD' in _PROBE_SNIPPET
    assert "160000" in _PROBE_SNIPPET


def test_probe_sums_submodule_committed_into_ahead_field(mocker):
    """Superproject + submodule committed shortstat lines sum into ahead_diff."""
    incus = mocker.MagicMock()
    incus.exec.return_value = (
        "\x00"
        " 1 file changed, 2 insertions(+)\n"  # superproject committed
        " 1 file changed, 3 insertions(+), 4 deletions(-)\n"  # submodule delta
        "\x00"
        "2\n"
        "\x00"
        "ok\x00"
    )
    result = probe_container_git(
        incus,
        full_name="SampleApp-feat-submod",
        repo_dir="/home/dev/SampleApp",
        base_branch="main",
        default_branch="main",
    )
    assert result.ahead_diff == "+5 -4"


def test_probe_committed_question_mark_survives_submodule_lines(mocker):
    """A '?' superproject committed field degrades AHEAD to '?' even with
    submodule shortstat lines appended after it."""
    incus = mocker.MagicMock()
    incus.exec.return_value = (
        "\x00"
        "?\n 1 file changed, 3 insertions(+)\n"  # superproject diff failed; sub delta appended
        "\x00"
        "2\n"
        "\x00"
        "ok\x00"
    )
    result = probe_container_git(
        incus,
        full_name="SampleApp-feat-submod",
        repo_dir="/home/dev/SampleApp",
        base_branch="main",
        default_branch="main",
    )
    assert result.ahead_diff == "?"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", (0, 0)),
        (" 1 file changed, 12 insertions(+), 3 deletions(-)", (12, 3)),
        (" 1 file changed, 5 insertions(+)", (5, 0)),
        (" 1 file changed, 3 deletions(-)", (0, 3)),
        ("nonsense", (0, 0)),
    ],
)
def test_shortstat_ints(raw, expected):
    assert _shortstat_ints(raw) == expected


def test_parse_submodules_merges_committed_and_wt():
    committed = (
        "deps/libfoo\tmodified\t2\t 1 file changed, 42 insertions(+), 7 deletions(-)\n"
        "vendor/bar\tnew\t5\t\n"
    )
    wt = (
        "deps/libfoo\t 1 file changed, 3 insertions(+)\n"
        "vendor/bar\t\n"
        "clean/sub\t\n"  # clean submodule — must be dropped
    )
    result = _parse_submodules(committed, wt)
    assert result == (
        SubmoduleChange("deps/libfoo", 42, 7, 2, 3, 0, "modified"),
        SubmoduleChange("vendor/bar", 0, 0, 5, 0, 0, "new"),
    )


def test_parse_submodules_removed_submodule_is_kept():
    committed = "vendor/gone\tremoved\t0\t\n"
    result = _parse_submodules(committed, "")
    assert result == (SubmoduleChange("vendor/gone", 0, 0, 0, 0, 0, "removed"),)


def test_parse_submodules_empty():
    assert _parse_submodules("", "") == ()


def test_probe_parses_submodule_fields(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = (
        " 1 file changed, 5 insertions(+)\n\x00"  # wt aggregate
        " 4 files changed, 200 insertions(+), 18 deletions(-)\n\x00"  # ahead aggregate
        "3\n\x00"  # count
        "ok\x00"  # conflict
        # field 5: committed struct
        "deps/libfoo\tmodified\t2\t 1 file changed, 42 insertions(+), 7 deletions(-)\n\x00"
        "deps/libfoo\t 1 file changed, 3 insertions(+)\n\x00"  # field 6
    )
    result = probe_container_git(
        incus, full_name="c", repo_dir="/repo", base_branch="main", default_branch="main"
    )
    assert result.wt == "+5 -0"
    assert result.ahead_diff == "+200 -18"
    assert result.submodules == (SubmoduleChange("deps/libfoo", 42, 7, 2, 3, 0, "modified"),)


def test_probe_four_field_output_yields_no_submodules(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00\x00\x00ok\x00"
    result = probe_container_git(
        incus, full_name="c", repo_dir="/repo", base_branch=None, default_branch="main"
    )
    assert result.submodules == ()


def test_probe_parses_head_sha_and_remote_contained(mocker):
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload("", "", "0", "ok", "", "", "abc123", "1", "", "0")

    st = probe_container_git(incus, "p-feat-x", "/repo", "main", "main")

    assert st.head_sha == "abc123"
    assert st.remote_contained is True


def test_probe_remote_contained_false_and_unknown(mocker):
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload("", "", "0", "ok", "", "", "abc123", "0", "", "0")
    assert probe_container_git(incus, "c", "/repo", "main", "main").remote_contained is False

    incus.exec.return_value = _payload("", "", "0", "ok", "", "", "abc123", "", "", "0")
    assert probe_container_git(incus, "c", "/repo", "main", "main").remote_contained is None


def test_probe_parses_the_local_diff_when_the_container_resolved_it(mocker):
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload(
        "",
        "",
        "0",
        "ok",
        "",
        "",
        "abc123",
        "1",
        " 2 files changed, 12 insertions(+), 3 deletions(-)",
        "3",
    )

    st = probe_container_git(incus, "c", "/repo", "main", "main", host_head="deadbeef")

    assert st.local_diff == "+12 -3"
    assert st.local_count == "3"


def test_probe_local_diff_empty_field_means_clean_not_unknown(mocker):
    """The snippet emits `?` when it could not compute; an empty field means
    it computed a clean diff. Conflating the two would send the host looking
    for objects it does not need."""
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload("", "", "0", "ok", "", "", "abc123", "1", "", "0")

    st = probe_container_git(incus, "c", "/repo", "main", "main", host_head="deadbeef")

    assert st.local_diff == "clean"
    assert st.local_count == "0"


def test_probe_local_diff_question_mark_stays_unknown(mocker):
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload("", "", "0", "ok", "", "", "abc123", "1", "?", "?")

    st = probe_container_git(incus, "c", "/repo", "main", "main")

    assert st.local_diff == "?"
    assert st.local_count == "?"


def test_probe_six_field_payload_still_degrades_the_new_fields(mocker):
    """An older container image, or any short read, must not break parsing."""
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload("", "", "0", "ok", "", "")

    st = probe_container_git(incus, "c", "/repo", "main", "main")

    assert st.head_sha == ""
    assert st.remote_contained is None
    assert st.local_diff == "?"
    assert st.local_count == "?"
    assert st.conflict == "ok"  # the original six still parsed


def test_probe_passes_host_head_into_the_exec_env(mocker):
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload("", "", "0", "ok", "", "", "abc", "1", "", "0")

    probe_container_git(incus, "c", "/repo", "main", "main", host_head="deadbeef")

    assert incus.exec.call_args.kwargs["env"]["HOST_HEAD"] == "deadbeef"


def test_probe_passes_empty_host_head_when_none(mocker):
    """The snippet tests `[ -n "$HOST_HEAD" ]`, so None must reach it as ""."""
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload("", "", "0", "ok", "", "", "abc", "1", "?", "?")

    probe_container_git(incus, "c", "/repo", "main", "main")

    assert incus.exec.call_args.kwargs["env"]["HOST_HEAD"] == ""


def test_probe_many_parallel_forwards_host_head_to_every_target(mocker):
    from jailbee.git_status import probe_many_parallel

    probe = mocker.patch("jailbee.git_status.probe_container_git")
    probe.return_value = mocker.Mock()
    incus = mocker.Mock()

    probe_many_parallel(
        incus,
        [("a", "/repo", "main"), ("b", "/repo", "main")],
        "main",
        host_head="deadbeef",
    )

    assert [c.kwargs["host_head"] for c in probe.call_args_list] == ["deadbeef", "deadbeef"]


def test_probe_does_not_take_the_git_index_lock(mocker):
    """The probe must run with GIT_OPTIONAL_LOCKS=0.

    The probe reads state, but `git diff` / `git diff --cached` /
    `git submodule foreach 'git diff'` refresh the index and write it back,
    which takes `.git/index.lock`. That makes a nominally read-only listing
    race any concurrent write in the same container: `jailbee git push`'s
    `git merge` then dies with "Unable to create '.git/index.lock': File
    exists". GIT_OPTIONAL_LOCKS=0 tells git to skip the lock and simply not
    write the refreshed cache back.
    """
    incus = mocker.MagicMock()
    incus.exec.return_value = "\x00\x00\x00ok\x00"
    probe_container_git(
        incus,
        full_name="c",
        repo_dir="/repo",
        base_branch="main",
        default_branch="main",
    )
    env = incus.exec.call_args.kwargs.get("env") or {}
    assert env.get("GIT_OPTIONAL_LOCKS") == "0"


def test_probe_never_runs_a_worktree_git_diff():
    """Worktree-side `git diff` takes `.git/index.lock` despite GIT_OPTIONAL_LOCKS=0.

    Porcelain `git diff` (worktree against the index or a commit) rewrites
    the index whenever it meets stat-dirty entries, and that refresh ignores
    GIT_OPTIONAL_LOCKS (git 2.53). The probe runs under a 3 s `incus exec`
    timeout, so a kill landing inside that window leaves a stale empty lock
    and every later `git merge` in the container fails. Worktree diffs must
    go through `git diff-index`, which never writes the index; `git diff`
    stays only for `--cached` and commit-to-commit comparisons.
    """
    from jailbee.git_status import _PROBE_SNIPPET

    joined = re.sub(r"\\\n\s*", " ", _PROBE_SNIPPET)
    calls = re.findall(r"(?<!`)\bgit diff (?P<args>[^)|;'\n]*)", joined)
    assert calls
    for args in calls:
        words = args.split()
        revs = [w for w in words if not w.startswith(("-", "2>"))]
        assert "--cached" in words or len(revs) >= 2 or any(".." in r for r in revs), args


def test_probe_reports_in_progress_merge_and_unmerged_count(mocker):
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    # 12 fields: wt, ahead, count, conflict, sub_committed, sub_wt,
    # head_sha, remote_contained, local_diff, local_count, in_progress, unmerged
    incus.exec.return_value = _payload(
        "", "", "0", "ok", "", "", "abc123", "0", "?", "?", "merge", "3"
    )
    status = probe_container_git(incus, "c", "/repo", "main", "main")
    assert status.in_progress == "merge"
    assert status.unmerged == 3
    # The prediction field is untouched by the new ones.
    assert status.conflict == "ok"


def test_probe_in_progress_is_unknown_on_a_ten_field_payload(mocker):
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload("", "", "0", "ok", "", "", "abc123", "0", "?", "?")
    status = probe_container_git(incus, "c", "/repo", "main", "main")
    assert status.in_progress == "?"
    assert status.unmerged is None


def test_probe_in_progress_unrecognised_value_degrades_to_unknown(mocker):
    """A 12-field payload can still carry a value outside the known set

    (e.g. a future git op, or the shell computing something unexpected);
    the parser must reject it rather than pass it through unvalidated.
    """
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload(
        "", "", "0", "ok", "", "", "abc123", "0", "?", "?", "bisect", "0"
    )
    status = probe_container_git(incus, "c", "/repo", "main", "main")
    assert status.in_progress == "?"


def test_probe_unmerged_non_numeric_or_sentinel_is_none(mocker):
    from jailbee.git_status import probe_container_git

    incus = mocker.Mock()
    incus.exec.return_value = _payload(
        "", "", "0", "ok", "", "", "abc123", "0", "?", "?", "merge", "abc"
    )
    assert probe_container_git(incus, "c", "/repo", "main", "main").unmerged is None

    incus.exec.return_value = _payload(
        "", "", "0", "ok", "", "", "abc123", "0", "?", "?", "merge", "?"
    )
    assert probe_container_git(incus, "c", "/repo", "main", "main").unmerged is None


def test_probe_snippet_resolves_git_dir_instead_of_testing_dot_git():
    from jailbee.git_status import _PROBE_SNIPPET

    # `.git` is a file in a linked worktree or a submodule, so the
    # in-progress detection must go through `git rev-parse --git-dir`.
    assert "rev-parse --git-dir" in _PROBE_SNIPPET
    assert "$GIT_DIR/rebase-merge" in _PROBE_SNIPPET
    assert "git ls-files --unmerged" in _PROBE_SNIPPET


def test_probe_snippet_checks_rebase_before_merge():
    from jailbee.git_status import _PROBE_SNIPPET

    # A conflicted `git rebase --merge` writes MERGE_HEAD too, so testing
    # MERGE_HEAD first would report "merging" for a rebase.
    assert _PROBE_SNIPPET.index("rebase-merge") < _PROBE_SNIPPET.index("MERGE_HEAD")


@pytest.mark.parametrize(
    "status,expected",
    [
        (None, ("—", "none")),
        (GitStatus("clean", "clean", "0", "ok"), ("ok", "ok")),
        (GitStatus("clean", "clean", "0", "conflict"), ("conflict", "predicted")),
        (GitStatus("clean", "clean", "0", "?"), ("?", "unknown")),
        # An unresolved merge in the tree outranks the prediction, even when
        # the prediction against base is clean — this is the reported bug.
        (
            GitStatus("clean", "clean", "0", "ok", in_progress="merge", unmerged=2),
            ("conflict!", "active"),
        ),
        # Merge started, conflicts already resolved, commit still pending.
        (
            GitStatus("clean", "clean", "0", "ok", in_progress="merge", unmerged=0),
            ("merging", "active"),
        ),
        (
            GitStatus("clean", "clean", "0", "ok", in_progress="rebase", unmerged=0),
            ("rebasing", "active"),
        ),
        (
            GitStatus("clean", "clean", "0", "ok", in_progress="cherry-pick", unmerged=0),
            ("cherry-picking", "active"),
        ),
        (
            GitStatus("clean", "clean", "0", "ok", in_progress="revert", unmerged=0),
            ("reverting", "active"),
        ),
        (
            GitStatus("clean", "clean", "0", "conflict", in_progress="", unmerged=0),
            ("conflict", "predicted"),
        ),
    ],
)
def test_merge_label(status, expected):
    assert merge_label(status) == expected


def _payload(*fields: str) -> str:
    """NUL-terminated probe output, exactly as the snippet prints it."""
    return "".join(f + "\0" for f in fields)


_TWELVE = ("", "", "0", "ok", "", "", "abc1234", "1", "?", "?", "", "0")


def test_probe_leaves_both_outbox_counts_to_the_host_store(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE, "3")

    status = probe_container_git(incus, "c", "/home/dev/repo", "main", "main")

    assert status.pending_pr_actions is None
    assert status.pending_issue_actions is None
    assert status.behind_count == "?"  # no target: the thirteenth field is not trusted
    assert not any("OUTBOX" in key for key in incus.exec.call_args.kwargs["env"])


def test_live_target_probe_uses_direct_tree_and_symmetric_commit_comparison(mocker):
    from jailbee.git_status import _PROBE_SNIPPET

    target = TargetSnapshot("main", "abc123", "local", "refs/remotes/origin/main", "local-ahead")
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(
        "clean",
        "",
        "2",
        "ok",
        "",
        "",
        "headsha",
        "0",
        "?",
        "?",
        "",
        "0",
        "1",
    )

    status = probe_container_git(incus, "c", "/repo", "main", "main", target=target)

    assert 'git diff --shortstat --ignore-submodules=all "${BASE}" HEAD' in _PROBE_SNIPPET
    assert 'git rev-list --left-right --count "${BASE}...HEAD"' in _PROBE_SNIPPET
    assert 'git merge-tree --write-tree "${BASE}" HEAD' in _PROBE_SNIPPET
    committed_section = _PROBE_SNIPPET.split('if [ -n "$BASE" ]; then', 1)[1]
    target_diff = committed_section.split("else", 1)[0]
    assert 'git diff --shortstat --ignore-submodules=all "${BASE}" HEAD' in target_diff
    assert incus.exec.call_args.kwargs["env"]["TARGET_SHA"] == "abc123"
    assert status.target_diff == "clean"
    assert status.ahead_count == "2"
    assert status.behind_count == "1"
    assert status.base_sha == "abc123"
    assert status.base_source == "local"
    assert status.tracking_relation == "local-ahead"


def test_live_target_is_forwarded_by_name_to_matching_parallel_probes(mocker):
    from jailbee.git_status import probe_many_parallel

    probe = mocker.patch("jailbee.git_status.probe_container_git")
    probe.return_value = mocker.Mock()
    target = TargetSnapshot("main", "abc123", "local", "refs/remotes/origin/main", "equal")

    probe_many_parallel(
        mocker.Mock(),
        [("a", "/repo", "main"), ("b", "/repo", "dev")],
        "main",
        target_by_name={"a": target},
    )

    assert [call.kwargs.get("target") for call in probe.call_args_list] == [target, None]


def test_parallel_probe_never_forwards_current_repo_head_to_foreign_repo(mocker):
    from jailbee.git_status import probe_many_parallel

    probe = mocker.patch("jailbee.git_status.probe_container_git")
    probe_many_parallel(
        mocker.Mock(),
        [("own", "/repo", "main"), ("foreign", "/repo", "main")],
        "main",
        host_head="own-head",
        host_head_by_name={"own": "own-head", "foreign": None},
    )
    assert [call.kwargs["host_head"] for call in probe.call_args_list] == ["own-head", None]


def test_unavailable_live_target_keeps_worktree_and_live_operation(mocker):
    unavailable = TargetSnapshot(
        "main", None, "unavailable", "refs/remotes/origin/main", "unavailable"
    )
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(
        " 1 file changed, 4 insertions(+)\n",
        "?",
        "?",
        "?",
        "",
        "",
        "headsha",
        "0",
        "?",
        "?",
        "merge",
        "2",
        "",
        "unavailable",
        "unavailable",
        "refs/remotes/origin/main",
    )

    status = probe_container_git(
        mocker.Mock(exec=incus.exec), "c", "/repo", "main", "main", target=unavailable
    )

    assert status.wt == "+4 -0"
    assert status.target_diff == status.ahead_count == status.behind_count == "?"
    assert status.in_progress == "merge"
    assert status.unmerged == 2


def test_live_target_does_not_fall_back_to_pinned_refs_when_sha_is_unavailable(mocker):
    from jailbee.git_status import _PROBE_SNIPPET

    target = TargetSnapshot("main", None, "unavailable", "refs/remotes/origin/main", "unavailable")
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(
        "", "?", "?", "?", "", "", "head", "0", "?", "?", "", "0", "?"
    )

    status = probe_container_git(incus, "c", "/repo", "main", "main", target=target)

    assert incus.exec.call_args.kwargs["env"]["TARGET_SHA"] == ""
    assert status.target_diff == status.ahead_count == status.behind_count == "?"
    assert 'git cat-file -e "${TARGET_SHA}^{commit}"' in _PROBE_SNIPPET
    assert "refs/jailbee/base/" not in _PROBE_SNIPPET


def test_missing_submodule_object_makes_live_target_diff_unknown(mocker):
    target = TargetSnapshot("main", "abc123", "local", "", "unavailable")
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(
        "",
        "?\n?",
        "2",
        "ok",
        "deps/lib\tmodified\t1\t0\t?\n",
        "",
        "head",
        "0",
        "?",
        "?",
        "",
        "0",
        "1",
    )

    status = probe_container_git(incus, "c", "/repo", "main", "main", target=target)

    assert status.target_diff == "?"
    assert status.ahead_count == "2"


def test_submodule_commit_counts_preserve_both_orientations():
    from jailbee.git_status import _parse_submodules

    changes = _parse_submodules("deps/lib\tmodified\t2\t1\t 1 file changed, 3 insertions(+)\n", "")

    assert changes[0].ahead_commits == 2
    assert changes[0].behind_commits == 1


@pytest.mark.parametrize(
    "line,target_ins,target_del,ahead_commits,behind_commits",
    [
        ("deps/lib\tmodified\t?\t?\t?", None, None, None, None),
        (
            "deps/lib\tmodified\t?\t?\t 1 file changed, 3 insertions(+)\n",
            3,
            0,
            None,
            None,
        ),
        ("deps/lib\tmodified\t2\t1\t?", None, None, 2, 1),
    ],
)
def test_submodule_probe_failures_preserve_unknown_fields_and_keep_the_row(
    line, target_ins, target_del, ahead_commits, behind_commits
):
    from jailbee.git_status import _parse_submodules

    changes = _parse_submodules(f"{line}\n", "")

    assert len(changes) == 1
    assert changes[0].target_ins == target_ins
    assert changes[0].target_del == target_del
    assert changes[0].ahead_commits == ahead_commits
    assert changes[0].behind_commits == behind_commits


@pytest.mark.parametrize(
    "gitlink,target_diff",
    [
        # An added or removed submodule counts as the one gitlink line
        # `git diff --shortstat` itself reports — never "clean", and never the
        # "?" that would hide the superproject's own diff.
        ("added", "+1 -0"),
        ("deleted", "+0 -1"),
        ("raw-failed", "?"),
    ],
)
def test_probe_gitlink_without_resolvable_endpoints_never_reports_clean(
    mocker, tmp_path, gitlink, target_diff
):
    from jailbee.git_status import _PROBE_SNIPPET

    # A fake git supplies real shell output while no real repo, Incus, or git
    # objects are touched. Only gitlink endpoints / raw-diff reliability vary.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_bin = bin_dir / "git"
    git_bin.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"diff --raw"*)\n'
        '    [ "$GITLINK" = raw-failed ] && exit 1\n'
        '    if [ "$GITLINK" = added ]; then old=$(printf "%040d" 0); new=$(printf "%040d" 1);\n'
        "      om=000000; nm=160000; status=A;\n"
        '    else old=$(printf "%040d" 1); new=$(printf "%040d" 0);\n'
        "      om=160000; nm=000000; status=D; fi\n"
        '    printf ":%s %s %s %s %s\\tdeps/lib\\n" "$om" "$nm" "$old" "$new" "$status" ;;\n'
        '  *"rev-list --left-right"*) printf "1\\t2\\n" ;;\n'
        '  *"merge-tree"*) exit 0 ;;\n'
        '  *"rev-parse HEAD"*) printf "headsha\\n" ;;\n'
        '  *"branch -r"*) exit 0 ;;\n'
        '  *"rev-parse --git-dir"*) printf ".git\\n" ;;\n'
        '  *"ls-files --unmerged"*) exit 0 ;;\n'
        '  *"submodule foreach"*) exit 0 ;;\n'
        '  *"diff"*) exit 0 ;;\n'
        "esac\n"
    )
    git_bin.chmod(0o755)

    def exec_snippet(_name, _args, *, env, **_kwargs):
        completed = subprocess.run(
            ["bash", "-c", _PROBE_SNIPPET],
            env={
                **os.environ,
                **env,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "GITLINK": gitlink,
            },
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout

    target = TargetSnapshot("main", "abc123", "local", "refs/remotes/origin/main", "equal")
    incus = mocker.Mock()
    incus.exec.side_effect = exec_snippet
    status = probe_container_git(incus, "c", str(repo), "main", "main", target=target)

    assert status.target_diff == target_diff
    assert status.ahead_count == "2"
    assert status.behind_count == "1"
    assert status.wt == "clean"
    if gitlink != "raw-failed":
        assert status.submodules[0].status == ("new" if gitlink == "added" else "removed")
        assert status.submodules[0].target_ins is None
        assert status.submodules[0].target_del is None
        assert status.submodules[0].ahead_commits is None
        assert status.submodules[0].behind_commits is None


@pytest.mark.parametrize("failure", ["exec", "partial"])
def test_probe_failure_preserves_host_target_snapshot(mocker, failure):
    target = TargetSnapshot("main", "abc123", "local", "refs/remotes/origin/main", "tracking-ahead")
    incus = mocker.Mock()
    if failure == "exec":
        incus.exec.side_effect = IncusError("probe failed")
    else:
        incus.exec.return_value = "?\x00?"

    status = probe_container_git(incus, "c", "/repo", "main", "main", target=target)

    assert status.wt == status.target_diff == status.ahead_count == "?"
    assert status.base_sha == "abc123"
    assert status.base_source == "local"
    assert status.tracking_relation == "tracking-ahead"
    assert status.upstream_ref == "refs/remotes/origin/main"


def _real_git(cwd, *args):
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.parametrize("host_has_commit", [True, False])
def test_probe_borrows_missing_submodule_commit_from_host_source(mocker, tmp_path, host_has_commit):
    """The target moved a submodule to a commit the container's clone lacks.

    The host repo, bind-mounted read-only at HOST_SOURCE, has it. The probe
    reads the host submodule's objects as an alternate instead of reporting
    the whole DIFF as unknown. Without the host copy the field stays "?".
    """
    from jailbee.git_status import _PROBE_SNIPPET

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _real_git(upstream, "init", "-q")
    (upstream / "f").write_text("one\n")
    _real_git(upstream, "add", "f")
    _real_git(upstream, "commit", "-qm", "c1")

    host = tmp_path / "host"
    host.mkdir()
    _real_git(host, "init", "-q")
    _real_git(host, "submodule", "add", "-q", str(upstream), "lib")
    _real_git(host, "commit", "-qm", "add lib")

    ctr = tmp_path / "ctr"
    _real_git(tmp_path, "clone", "-q", str(host), str(ctr))
    _real_git(ctr, "submodule", "update", "-q", "--init")

    # The target branch moves `lib` to a commit only the host has fetched.
    (upstream / "f").write_text("one\ntwo\nthree\n")
    _real_git(upstream, "commit", "-qam", "c2")
    _real_git(host / "lib", "pull", "-q", "origin", "main")
    _real_git(host, "commit", "-qam", "bump lib")
    target_sha = _real_git(host, "rev-parse", "HEAD")
    # The container cannot reach the submodule upstream, only the superproject.
    _real_git(ctr, "fetch", "-q", "--no-recurse-submodules", str(host), "main")

    # The container's own work: one new line in the superproject.
    (ctr / "g").write_text("mine\n")
    _real_git(ctr, "add", "g")
    _real_git(ctr, "commit", "-qm", "work")

    source = host if host_has_commit else tmp_path / "absent"

    def exec_snippet(_name, _args, *, env, **_kwargs):
        return subprocess.run(
            ["bash", "-c", _PROBE_SNIPPET],
            env={**os.environ, **env, "HOST_SOURCE": str(source)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    target = TargetSnapshot("main", target_sha, "local", "refs/remotes/origin/main", "equal")
    incus = mocker.Mock()
    incus.exec.side_effect = exec_snippet
    status = probe_container_git(incus, "c", str(ctr), "main", "main", target=target)

    if host_has_commit:
        # +1 from `g`; the target→HEAD submodule diff drops "two" and "three".
        assert status.target_diff == "+1 -2"
        assert status.submodules[0].target_del == 2
        assert status.submodules[0].behind_commits == 1
    else:
        assert status.target_diff == "?"


def _probe_real(mocker, repo, *, target, sync=None):
    from jailbee.git_status import _PROBE_SNIPPET

    def exec_snippet(_name, _args, *, env, **_kwargs):
        return subprocess.run(
            ["bash", "-c", _PROBE_SNIPPET],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    incus = mocker.Mock()
    incus.exec.side_effect = exec_snippet
    return probe_container_git(incus, "c", str(repo), "main", "main", target=target, sync=sync)


def _commit(repo, name):
    (repo / name).write_text(f"{name}\n")
    _real_git(repo, "add", name)
    _real_git(repo, "commit", "-qm", name)
    return _real_git(repo, "rev-parse", "HEAD")


def test_pr_bound_probe_compares_with_the_pr_head_and_still_counts_the_host_branch(
    mocker, tmp_path
):
    """↑ against the PR head is "push needed"; the host row keeps its own ↑/↓."""
    from jailbee.host_target import SyncTarget

    repo = tmp_path / "ctr"
    repo.mkdir()
    _real_git(repo, "init", "-q")
    host_sha = _commit(repo, "base")
    pr_sha = _commit(repo, "pushed")  # the PR head the host last fetched
    _commit(repo, "local-1")
    _commit(repo, "local-2")  # two commits beyond the PR head

    host = TargetSnapshot("main", host_sha, "local", "refs/remotes/origin/main", "equal")
    sync = SyncTarget("pr", "PR #7", pr_sha, "refs/jailbee/pr/7/head")
    status = _probe_real(mocker, repo, target=host, sync=sync)

    assert (status.ahead_count, status.behind_count) == ("2", "0")
    assert status.target_diff == "+2 -0"
    assert (status.host_ahead_count, status.host_behind_count) == ("3", "0")
    assert status.sync_target == sync
    # The host snapshot fields keep describing the host branch.
    assert status.base_sha == host_sha


def test_plain_probe_host_counts_equal_the_sync_counts(mocker, tmp_path):
    repo = tmp_path / "ctr"
    repo.mkdir()
    _real_git(repo, "init", "-q")
    host_sha = _commit(repo, "base")
    _commit(repo, "local-1")

    host = TargetSnapshot("main", host_sha, "local", "refs/remotes/origin/main", "equal")
    status = _probe_real(mocker, repo, target=host)

    assert (status.ahead_count, status.behind_count) == ("1", "0")
    assert (status.host_ahead_count, status.host_behind_count) == ("1", "0")
    assert status.sync_target is not None
    assert (status.sync_target.kind, status.sync_target.sha) == ("host", host_sha)


def _git(cwd, *args):
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_probe(repo, target_sha, host_target_sha=""):
    import os
    import subprocess

    from jailbee.git_status import _PROBE_SNIPPET

    env = {
        **os.environ,
        "REPO_DIR": str(repo),
        "TARGET_SHA": target_sha,
        "HOST_TARGET_SHA": host_target_sha,
        "HOST_SOURCE": "",
        "HOST_HEAD": "",
    }
    out = subprocess.run(
        ["bash", "-c", _PROBE_SNIPPET], env=env, capture_output=True, text=True, check=True
    ).stdout
    return out.split("\0")


def test_probe_missing_target_commit_reports_behind_unknown(tmp_path):
    host, container = tmp_path / "host", tmp_path / "container"
    host.mkdir()
    _git(host, "init", "-q")
    (host / "a").write_text("1")
    _git(host, "add", "a")
    _git(host, "commit", "-qm", "one")
    _git(tmp_path, "clone", "-q", str(host), str(container))
    (host / "a").write_text("2")
    _git(host, "commit", "-qam", "two")
    new_sha = _git(host, "rev-parse", "HEAD")

    parts = _run_probe(container, new_sha)

    assert parts[1] == "?"  # DIFF
    assert parts[2] == "?"  # ahead
    assert parts[3] == "?"  # MERGE
    assert parts[12] == "missing"  # behind: pull needed, count unknown


def test_probe_shared_clone_sees_new_host_commits_and_counts_them(tmp_path):
    """Clone-mode containers are `git clone --shared`: a new host commit is readable."""
    host, container = tmp_path / "host", tmp_path / "container"
    host.mkdir()
    _git(host, "init", "-q")
    (host / "a").write_text("1")
    _git(host, "add", "a")
    _git(host, "commit", "-qm", "one")
    _git(tmp_path, "clone", "-q", "--shared", str(host), str(container))
    (host / "a").write_text("2")
    _git(host, "commit", "-qam", "two")

    parts = _run_probe(container, _git(host, "rev-parse", "HEAD"))

    assert parts[12] == "1"


def test_probe_missing_host_target_reports_host_behind_missing(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a").write_text("1")
    _git(repo, "add", "a")
    _git(repo, "commit", "-qm", "one")

    parts = _run_probe(repo, _git(repo, "rev-parse", "HEAD"), "f" * 40)

    assert parts[14] == "missing"


def test_probe_missing_token_is_one_constant():
    from jailbee.git_status import _PROBE_SNIPPET, PROBE_MISSING

    assert PROBE_MISSING == "missing"
    assert f'="{PROBE_MISSING}"' in _PROBE_SNIPPET
    assert "@MISSING@" not in _PROBE_SNIPPET


def test_probe_parses_missing_host_behind_marker(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE, "3", "1", "missing")
    target = TargetSnapshot("main", "a" * 40, "local", "", "unavailable")
    sync = SyncTarget("container", "other", "b" * 40, "refs/x")

    status = probe_container_git(incus, "c", "/r", "main", "main", target=target, sync=sync)

    assert status.host_behind_count == "•"


def test_probe_present_target_commit_still_counts(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a").write_text("1")
    _git(repo, "add", "a")
    _git(repo, "commit", "-qm", "one")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a").write_text("2")
    _git(repo, "commit", "-qam", "two")

    parts = _run_probe(repo, base)

    assert parts[12] == "0"
    assert parts[2] == "1"


def test_probe_without_target_keeps_behind_unknown_question_mark(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a").write_text("1")
    _git(repo, "add", "a")
    _git(repo, "commit", "-qm", "one")

    assert _run_probe(repo, "")[12] == "?"


def test_probe_parses_behind_unknown_marker(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE, "missing")
    target = TargetSnapshot(
        branch="main",
        sha="a" * 40,
        source="local",
        upstream_ref="",
        tracking_relation="unavailable",
    )

    sync = SyncTarget("host", "main", "a" * 40, "refs/heads/main")
    status = probe_container_git(incus, "c", "/r", "main", "main", target=target, sync=sync)

    assert status.behind_count == "•"


def _real_git_incus(repo):
    """An ``Incus`` stand-in whose ``exec`` runs the snippet in a local repo."""
    import subprocess

    class _LocalIncus:
        def exec(self, _name, cmd, *, env=None, **_kwargs):
            import os

            merged = {**os.environ, **(env or {}), "REPO_DIR": str(repo)}
            return subprocess.run(
                cmd, env=merged, capture_output=True, text=True, check=True
            ).stdout

    return _LocalIncus()


def test_probe_reports_unknown_host_counts_when_the_host_branch_object_is_missing(tmp_path):
    """A PR-bound container that lacks the host branch tip: PR-head counts are
    measured, host ↑ is "?" (the destroy guard then falls back to head_sha) and
    host ↓ is "•" (behind, count unknown)."""
    import subprocess

    from jailbee.host_target import SyncTarget, TargetSnapshot

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "one")
    pr_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    git("commit", "-q", "--allow-empty", "-m", "two")

    status = probe_container_git(
        _real_git_incus(tmp_path),
        full_name="c",
        repo_dir=str(tmp_path),
        base_branch="main",
        default_branch="main",
        target=TargetSnapshot("main", "f" * 40, "local", "", "unavailable"),
        sync=SyncTarget("pr", "PR #7", pr_sha, "refs/jailbee/pr/7/head"),
    )

    assert status.ahead_count == "1"
    assert (status.host_ahead_count, status.host_behind_count) == ("?", "•")


# ---- other local branches (probe field 16) ----


def _git_in(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_snippet_lists_other_branches_with_slashes_and_remote_flag(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_in(repo, "init", "-q", "-b", "main")
    _git_in(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git_in(repo, "checkout", "-q", "-b", "feat/one")
    _git_in(repo, "commit", "-q", "--allow-empty", "-m", "one")
    _git_in(repo, "checkout", "-q", "main")
    _git_in(repo, "checkout", "-q", "-b", "pushed")
    _git_in(repo, "commit", "-q", "--allow-empty", "-m", "two")
    _git_in(repo, "update-ref", "refs/remotes/origin/pushed", "HEAD")
    _git_in(repo, "checkout", "-q", "main")

    parts = _run_probe(repo, "")

    assert parts[6] != ""  # HEAD sha: main
    found = {b.name: b for b in parse_other_branches(parts[15]) or ()}
    assert set(found) == {"feat/one", "pushed"}
    assert found["pushed"].remote_contained is True
    assert found["feat/one"].remote_contained is False
    assert all(len(b.sha) == 40 for b in found.values())


def test_snippet_omits_the_checked_out_branch_and_tips_equal_to_head(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_in(repo, "init", "-q", "-b", "main")
    _git_in(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git_in(repo, "branch", "alias")  # same tip as HEAD

    parts = _run_probe(repo, "")

    assert parse_other_branches(parts[15]) == ()


def test_parse_other_branches_unknown_and_malformed():
    assert parse_other_branches("?") is None
    assert parse_other_branches("bad line\n") == ()
    assert parse_other_branches("a/b\tabc\t?\n") == (OtherBranch("a/b", "abc", None),)


def test_probe_carries_other_branches_and_old_output_leaves_them_unmeasured(mocker):
    incus = mocker.MagicMock()
    fields = [""] * 15 + ["x/y\tdeadbeef\t0\n"]
    incus.exec.return_value = "\x00".join(fields) + "\x00"
    status = probe_container_git(incus, "c", "/r", None, "main")
    assert status.other_branches == (OtherBranch("x/y", "deadbeef", False),)

    incus.exec.return_value = "\x00".join([""] * 14) + "\x00"
    assert probe_container_git(incus, "c", "/r", None, "main").other_branches is None


def test_snippet_prints_question_mark_when_the_branch_listing_fails(tmp_path):
    import os
    import subprocess

    from jailbee.git_status import _PROBE_SNIPPET

    repo = tmp_path / "r"
    repo.mkdir()
    _git_in(repo, "init", "-q", "-b", "main")
    _git_in(repo, "commit", "-q", "--allow-empty", "-m", "base")
    real = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    shim.write_text(f'#!/bin/sh\ncase "$1" in for-each-ref) exit 1;; esac\nexec {real} "$@"\n')
    shim.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "REPO_DIR": str(repo),
        "TARGET_SHA": "",
        "HOST_TARGET_SHA": "",
        "HOST_SOURCE": "",
        "HOST_HEAD": "",
    }
    out = subprocess.run(
        ["bash", "-c", _PROBE_SNIPPET], env=env, capture_output=True, text=True, check=True
    ).stdout.split("\0")

    assert out[15] == "?"
    assert parse_other_branches(out[15]) is None


def test_snippet_remote_containment_costs_one_git_call_for_many_branches(tmp_path):
    import os
    import subprocess

    from jailbee.git_status import _PROBE_SNIPPET

    repo = tmp_path / "r"
    repo.mkdir()
    _git_in(repo, "init", "-q", "-b", "main")
    _git_in(repo, "commit", "-q", "--allow-empty", "-m", "base")
    for i in range(6):
        _git_in(repo, "checkout", "-q", "-b", f"b{i}", "main")
        _git_in(repo, "commit", "-q", "--allow-empty", "-m", f"c{i}")
        if i % 2:
            _git_in(repo, "update-ref", f"refs/remotes/origin/b{i}", "HEAD")
    _git_in(repo, "checkout", "-q", "main")
    real = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    shim = bin_dir / "git"
    shim.write_text(f'#!/bin/sh\necho "$*" >> {log}\nexec {real} "$@"\n')
    shim.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "REPO_DIR": str(repo),
        "TARGET_SHA": "",
        "HOST_TARGET_SHA": "",
        "HOST_SOURCE": "",
        "HOST_HEAD": "",
    }
    out = subprocess.run(
        ["bash", "-c", _PROBE_SNIPPET], env=env, capture_output=True, text=True, check=True
    ).stdout.split("\0")

    found = {b.name: b.remote_contained for b in parse_other_branches(out[15]) or ()}
    assert found == {f"b{i}": bool(i % 2) for i in range(6)}
    calls = log.read_text().splitlines()
    per_branch = [c for c in calls if "--contains" in c and c != "branch -r --contains HEAD"]
    assert per_branch == []
