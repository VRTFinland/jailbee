"""Tests for git_status.py."""

from __future__ import annotations

import pytest

from jailbee.git_status import (
    GitStatus,
    SubmoduleChange,
    _parse_submodules,
    _shortstat_ints,
    parse_shortstat,
    probe_container_git,
)
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
    assert result == GitStatus(
        wt="+12 -3", ahead_diff="+200 -18", ahead_count="3", conflict="conflict"
    )


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
    assert result == GitStatus(wt="clean", ahead_diff="clean", ahead_count="?", conflict="ok")


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


def test_probe_snippet_prefers_gie_base_ref():
    from jailbee.git_status import _PROBE_SNIPPET

    assert "refs/jailbee/base/${BASE_BRANCH}" in _PROBE_SNIPPET
    # It must be checked before the origin/<base> fallback.
    gie_idx = _PROBE_SNIPPET.index("refs/jailbee/base/${BASE_BRANCH}")
    origin_idx = _PROBE_SNIPPET.index("refs/remotes/origin/${BASE_BRANCH}")
    assert gie_idx < origin_idx


def test_probe_snippet_guards_default_fallback_on_empty_base_branch():
    """The origin/<default_branch> fallback must be gated on an *empty*
    BASE_BRANCH. Otherwise a PR-review container whose base ref never made it
    into the clone silently diffs the head against the default branch and
    reports a huge, wrong AHEAD instead of an honest "?"."""
    from jailbee.git_status import _PROBE_SNIPPET

    default_ref = "refs/remotes/origin/${DEFAULT_BRANCH}"
    idx = _PROBE_SNIPPET.index(default_ref)
    # The `elif` clause that introduces the default-branch fallback must carry
    # a `[ -z "$BASE_BRANCH" ]` guard between it and the DEFAULT_BRANCH ref.
    guard_start = _PROBE_SNIPPET.rindex("elif", 0, idx)
    assert '[ -z "$BASE_BRANCH" ]' in _PROBE_SNIPPET[guard_start:idx]


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
    assert '"${BASE}...HEAD"' in _PROBE_SNIPPET
    # Gitlink SHA pairs are extracted from raw diff and diffed inside the submodule.
    assert 'git diff --raw --abbrev=40 "${BASE}...HEAD"' in _PROBE_SNIPPET
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


def _payload(*fields: str) -> str:
    """Ten NUL-terminated probe fields, in wire order."""
    return "".join(f"{f}\x00" for f in fields)


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
