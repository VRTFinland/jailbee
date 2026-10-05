"""Tests for git_status.py."""

from __future__ import annotations

import os
import subprocess

import pytest

from jailbee.git_status import (
    GitStatus,
    SubmoduleChange,
    _parse_submodules,
    _shortstat_ints,
    merge_label,
    parse_shortstat,
    probe_container_git,
)
from jailbee.host_target import TargetSnapshot
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
        "OUTBOX_DIR": "/home/dev/.jailbee/pr-outbox",
        "ISSUE_OUTBOX_DIR": "/home/dev/.jailbee/issue-outbox",
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
    assert "'git diff --shortstat HEAD || :'" in _PROBE_SNIPPET
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


def test_probe_parses_the_pending_action_count(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE, "3")

    status = probe_container_git(incus, "c", "/home/dev/repo", "main", "main")

    assert status.pending_pr_actions == 3


def test_probe_passes_the_outbox_dir_in_the_environment(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE, "0")

    probe_container_git(incus, "c", "/home/dev/repo", "main", "main")

    env = incus.exec.call_args.kwargs["env"]
    # $HOME is not dependable under `incus exec --user`, so the path is passed in.
    assert env["OUTBOX_DIR"].endswith("/.jailbee/pr-outbox")


def test_twelve_field_payload_still_parses_with_an_unknown_count(mocker):
    """Regression pin: the tiered parser must keep older output working."""
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE)

    status = probe_container_git(incus, "c", "/home/dev/repo", "main", "main")

    assert status.pending_pr_actions is None
    assert status.head_sha == "abc1234"  # everything else unchanged


def test_non_numeric_pending_count_is_unknown(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE, "?")

    status = probe_container_git(incus, "c", "/home/dev/repo", "main", "main")

    assert status.pending_pr_actions is None


def test_probe_parses_the_pending_issue_action_count(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE, "3", "5")

    status = probe_container_git(incus, "c", "/home/dev/repo", "main", "main")

    assert status.pending_pr_actions == 3
    assert status.pending_issue_actions == 5


def test_probe_passes_the_issue_outbox_dir_in_the_environment(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE, "0", "0")

    probe_container_git(incus, "c", "/home/dev/repo", "main", "main")

    env = incus.exec.call_args.kwargs["env"]
    # $HOME is not dependable under `incus exec --user`, so the path is passed in.
    assert env["ISSUE_OUTBOX_DIR"].endswith("/.jailbee/issue-outbox")


def test_thirteen_field_payload_still_parses_with_an_unknown_issue_count(mocker):
    """Regression pin: the tiered parser must keep 13-field output working."""
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE, "3")

    status = probe_container_git(incus, "c", "/home/dev/repo", "main", "main")

    assert status.pending_pr_actions == 3
    assert status.pending_issue_actions is None
    assert status.head_sha == "abc1234"  # everything else unchanged


def test_non_numeric_pending_issue_count_is_unknown(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = _payload(*_TWELVE, "3", "?")

    status = probe_container_git(incus, "c", "/home/dev/repo", "main", "main")

    assert status.pending_issue_actions is None


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
        "0",
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
        "0",
        "0",
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
        "", "?", "?", "?", "", "", "head", "0", "?", "?", "", "0", "0", "?"
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


@pytest.mark.parametrize("store", ["pr", "issue"])
@pytest.mark.parametrize(
    "state,expected",
    [
        ("missing", 0),
        ("missing-parents", 0),
        ("empty", 0),
        ("entries", 2),
        ("symlink", None),
        ("dangling-symlink", None),
        ("file", None),
        ("unreadable", None),
        ("unsearchable", None),
        ("missing-inaccessible-parent", None),
    ],
)
def test_probe_counts_only_root_manifests_and_preserves_unknown_stores(
    mocker, tmp_path, store, state, expected
):
    # Removing the regular-file/progress guards or coercing access failures
    # to zero must fail these tests, including when pytest runs as root.
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    stores = {name: tmp_path / name for name in ("pr", "issue")}
    other = stores["issue" if store == "pr" else "pr"]
    other.mkdir()
    for index in range(3):
        (other / f"{index}.json").write_text("{}")
    outbox = stores[store]
    denied = ""
    denied_flag = ""
    if state == "missing-parents":
        outbox = tmp_path / "absent" / "nested" / store
        stores[store] = outbox
    elif state == "missing-inaccessible-parent":
        parent = tmp_path / "blocked"
        parent.mkdir()
        outbox = parent / store
        stores[store] = outbox
        denied = str(parent)
        denied_flag = "-x"
    elif state in ("symlink", "dangling-symlink"):
        outbox.symlink_to(other if state == "symlink" else tmp_path / "absent")
    elif state == "file":
        outbox.write_text("{}")
    elif state != "missing":
        outbox.mkdir()
        if state == "entries":
            (outbox / "proposal with\nnewline.json").write_text('{"actions": [1, 2, 3]}')
            (outbox / "invalid.json").write_text("not parsed by the cheap counter")
            (outbox / "proposal.progress.json").write_text("{}")
            (outbox / "directory.json").mkdir()
            (outbox / "directory.json" / "nested.json").write_text("{}")
            (outbox / "link.json").symlink_to(outbox / "invalid.json")
            (outbox / "broken.json").symlink_to(outbox / "missing.json")
            os.mkfifo(outbox / "pipe.json")
            (outbox / "publication.log").write_text("log")
        elif state in ("unreadable", "unsearchable"):
            (outbox / "proposal.json").write_text("{}")
            denied = str(outbox)
            denied_flag = "-r" if state == "unreadable" else "-x"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_bin = bin_dir / "git"
    git_bin.write_text(
        '#!/bin/sh\ncase "$*" in\n'
        '  "rev-parse HEAD") printf "headsha\\n" ;;\n'
        '  "rev-parse --git-dir") printf ".git\\n" ;;\n'
        '  *"--verify"*) exit 1 ;;\n'
        "esac\n"
    )
    git_bin.chmod(0o755)

    def exec_snippet(_name, args, *, env, **_kwargs):
        # Inject access-test results, not chmod-only assertions: root bypasses
        # permission bits. All other filesystem tests remain real Bash tests.
        access_checks = r"""
[() {
    if builtin [ "$2" = "$DENIED_DIR" ] && builtin [ "$1" = "$DENIED_FLAG" ]; then
        return 1
    fi
    builtin [ "$@"
}
"""
        completed = subprocess.run(
            [args[0], args[1], access_checks + args[2]],
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                **env,
                "OUTBOX_DIR": str(stores["pr"]),
                "ISSUE_OUTBOX_DIR": str(stores["issue"]),
                "DENIED_DIR": denied,
                "DENIED_FLAG": denied_flag,
            },
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        fields = completed.stdout.split("\0")
        assert fields[6:12] == ["headsha", "0", "?", "?", "", "0"]
        assert fields[12 if store == "pr" else 13] == ("?" if expected is None else str(expected))
        assert fields[13 if store == "pr" else 12] == "3"
        assert fields[14] == "?"
        return completed.stdout

    incus = mocker.Mock()
    incus.exec.side_effect = exec_snippet
    status = probe_container_git(incus, "c", str(repo), "main", "main")
    assert getattr(status, f"pending_{store}_actions") == expected
    assert getattr(status, f"pending_{'issue' if store == 'pr' else 'pr'}_actions") == 3
    if state in ("missing", "missing-parents", "missing-inaccessible-parent"):
        assert not outbox.exists()
