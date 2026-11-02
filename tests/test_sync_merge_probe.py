import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from jailbee.incus import ExecResult, Incus, IncusError
from jailbee.sync import MergeProbe, SyncError, abort_merge_in_container, probe_merge_in_container


def probe(*results: ExecResult) -> tuple[MagicMock, MergeProbe]:
    incus = MagicMock()
    incus.exec_timeout = None
    incus.exec_bytes.side_effect = results
    result = probe_merge_in_container(incus, "box", "/repo", ref="refs/jailbee/x", uid=1000)
    return incus, result


def test_merge_probe_contained() -> None:
    incus, result = probe(ExecResult(0, b"", b""))
    assert result == MergeProbe("contained", ())
    incus.exec_bytes.assert_called_once_with(
        "box",
        ["git", "-C", "/repo", "merge-base", "--is-ancestor", "refs/jailbee/x", "HEAD"],
        uid=1000,
        timeout=None,
    )


def test_merge_probe_fast_forward() -> None:
    incus, result = probe(ExecResult(1, b"", b""), ExecResult(0, b"", b""))
    assert result == MergeProbe("fast_forward", ())
    assert incus.exec_bytes.call_args_list[1] == call(
        "box",
        ["git", "-C", "/repo", "merge-base", "--is-ancestor", "HEAD", "refs/jailbee/x"],
        uid=1000,
        timeout=None,
    )


def test_merge_probe_clean_merge() -> None:
    incus, result = probe(ExecResult(1, b"", b""), ExecResult(1, b"", b""), ExecResult(0, b"", b""))
    assert result == MergeProbe("merge", ())
    incus.exec_bytes.assert_called_with(
        "box",
        [
            "git",
            "-C",
            "/repo",
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            "HEAD",
            "refs/jailbee/x",
        ],
        uid=1000,
        timeout=None,
    )


def test_merge_probe_conflicts_listed() -> None:
    _, result = probe(
        ExecResult(1, b"", b""),
        ExecResult(1, b"", b""),
        ExecResult(1, b"0" * 40 + b"\na.py\nb.py\na.py\n", b""),
    )
    assert result.conflicts == ("a.py", "b.py")
    assert result.relation == "merge"


def test_merge_probe_conflict_without_paths() -> None:
    _, result = probe(
        ExecResult(1, b"", b""), ExecResult(1, b"", b""),
        ExecResult(1, b"0" * 40 + b"\n", b""),
    )
    assert result.conflicted
    assert result.conflicts == ()


def test_merge_tree_probe_error_raises() -> None:
    with pytest.raises(SyncError, match="merge check failed in box: broken"):
        probe(ExecResult(1, b"", b""), ExecResult(1, b"", b""), ExecResult(128, b"", b"broken"))


@pytest.mark.parametrize(
    "results",
    [
        (ExecResult(128, b"", b"broken"),),
        (ExecResult(1, b"", b""), ExecResult(128, b"", b"broken")),
    ],
)
def test_merge_probe_ancestor_error_raises(results: tuple[ExecResult, ...]) -> None:
    with pytest.raises(SyncError, match="cannot compare HEAD with refs/jailbee/x in box: broken"):
        probe(*results)


@pytest.mark.parametrize("timeout", [7, 37])
def test_probe_passes_configured_timeout_to_subprocess(timeout: int) -> None:
    incus = Incus().with_exec_timeout(timeout)
    results = [subprocess.CompletedProcess([], rc, b"", b"") for rc in (1, 1, 0)]
    with patch("jailbee.incus.subprocess.run", side_effect=results) as run:
        result = probe_merge_in_container(incus, "box", "/repo", ref="refs/jailbee/x", uid=1000)
    assert result == MergeProbe("merge", ())
    assert run.call_count == 3
    assert all(item.kwargs["timeout"] == timeout for item in run.call_args_list)


def test_abort_merge_swallows_incus_error() -> None:
    incus = MagicMock()
    incus.exec.side_effect = IncusError("failed")
    assert abort_merge_in_container(incus, "box", "/repo", uid=1000) is False
    incus.exec.assert_called_once_with("box", ["git", "-C", "/repo", "merge", "--abort"], uid=1000)
