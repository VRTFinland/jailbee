from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)


def _probe_rig(mocker, *, issue=(), pr=(), files=None):
    incus = MagicMock()
    incus.exec_capped.return_value = json.dumps({"issue": list(issue), "pr": list(pr)})
    read = mocker.patch("jailbee.outbox_io.read_text_outbox", side_effect=files or [{}, {}])
    return incus, read


def test_probe_counts_manifests_not_actions_and_summarizes(mocker):
    from jailbee.orchestration.child_outbox import probe

    incus, read = _probe_rig(
        mocker,
        issue=("001-a.json", "broken.json"),
        pr=("002-y.json",),
        files=[
            {
                "001-a.json": json.dumps({"actions": [{"type": "create", "title": "Title"}]}),
                "broken.json": "not json",
                "x.progress.json": "{}",
                "body.md": "body",
            },
            {"002-y.json": json.dumps({"actions": [{"type": "review", "comments": [{}, {}, {}]}]})},
        ],
    )
    pending = probe(incus, "proj-c1", uid=1000)
    assert pending.count == 3
    assert pending.issue == ("001-a.json", "broken.json")
    assert pending.pr == ("002-y.json",)
    assert pending.summaries == (
        'issue/001-a.json: create "Title"',
        "issue/broken.json",
        "pr/002-y.json: review (3 comments)",
    )
    assert read.call_count == 2
    for call in read.call_args_list:
        assert call.kwargs["select"][0][0] == "*.json"
        assert call.kwargs["select"][0][1] <= 128
        assert call.kwargs["uid"] == 1000
        assert call.kwargs["max_file_bytes"] <= 65536
    assert {c.args[2] for c in read.call_args_list} == {
        "/home/dev/.jailbee/issue-outbox",
        "/home/dev/.jailbee/pr-outbox",
    }
    assert incus.exec_capped.call_args.kwargs["timeout"] == 60
    assert incus.exec_capped.call_args.kwargs["max_bytes"] <= 524288


def test_probe_deeply_nested_invalid_manifest_is_name_only(mocker):
    from jailbee.orchestration.child_outbox import probe

    incus, _ = _probe_rig(
        mocker, issue=("deep.json",), files=[{"deep.json": "[" * 30000 + "]" * 30000}, {}]
    )
    assert probe(incus, "proj-c1", uid=1000).summaries == ("issue/deep.json",)


def test_probe_snapshot_omission_cannot_hide_oversized_or_unreadable_manifest(mocker):
    from jailbee.orchestration.child_outbox import describe, probe

    incus, _ = _probe_rig(mocker, issue=("oversized.json", "unreadable.json"))
    pending = probe(incus, "proj-c1", uid=None)
    assert pending.count == 2
    assert "oversized.json" in describe("c1", pending)
    assert "unreadable.json" in describe("c1", pending)


@pytest.mark.parametrize(
    "raw", ["", "{}", '{"error":"permission denied"}', '{"issue":[],"pr":null}']
)
def test_probe_unknown_names_fail_closed(mocker, raw):
    from jailbee.orchestration.child_outbox import ChildOutboxError, probe

    incus, _ = _probe_rig(mocker)
    incus.exec_capped.return_value = raw
    with pytest.raises(ChildOutboxError):
        probe(incus, "proj-c1", uid=1000)


def test_probe_read_failure_is_not_empty(mocker):
    from jailbee.orchestration.child_outbox import ChildOutboxError, probe
    from jailbee.outbox_io import OutboxReadError

    incus, read = _probe_rig(mocker)
    read.side_effect = OutboxReadError("permission denied")
    with pytest.raises(ChildOutboxError):
        probe(incus, "proj-c1", uid=1000)


def test_probe_name_command_failure_is_not_empty(mocker):
    from jailbee.incus import IncusError
    from jailbee.orchestration.child_outbox import ChildOutboxError, probe

    incus, _ = _probe_rig(mocker)
    incus.exec_capped.side_effect = IncusError("timeout")
    with pytest.raises(ChildOutboxError):
        probe(incus, "proj-c1", uid=1000)


def test_probe_changed_snapshot_fails_closed(mocker):
    from jailbee.orchestration.child_outbox import ChildOutboxError, probe

    incus, _ = _probe_rig(mocker)
    incus.exec_capped.side_effect = ['{"issue":[],"pr":[]}', '{"issue":["late.json"],"pr":[]}']
    with pytest.raises(ChildOutboxError):
        probe(incus, "proj-c1", uid=1000)


def _run_name_script(mocker, issue: Path, pr: Path):
    from jailbee.orchestration.child_outbox import _NAMES_SCRIPT

    mocker.patch("sys.argv", ["probe", str(issue), str(pr), "128"])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(compile(_NAMES_SCRIPT, "<child outbox probe>", "exec"), {})
    return json.loads(output.getvalue())


def test_name_script_missing_real_directories_are_empty(tmp_path, mocker):
    assert _run_name_script(mocker, tmp_path / "issue", tmp_path / "pr") == {"issue": [], "pr": []}


def test_name_script_filters_top_level_regular_manifests(tmp_path, mocker):
    issue, pr = tmp_path / "issue", tmp_path / "pr"
    issue.mkdir()
    (issue / "a.json").write_text("x" * 65537)
    (issue / "x.progress.json").touch()
    (issue / "body.md").touch()
    (issue / ".temp.json").touch()
    (issue / "link.json").symlink_to(issue / "a.json")
    (issue / "dir.json").mkdir()
    (issue / "dir.json" / "nested.json").touch()
    assert _run_name_script(mocker, issue, pr) == {"issue": ["a.json"], "pr": []}


@pytest.mark.parametrize("uncertainty", ["symlink", "ancestor_symlink", "permission", "cap"])
def test_name_script_uncertainty_is_reported(tmp_path, mocker, uncertainty):
    issue, pr = tmp_path / "issue", tmp_path / "pr"
    issue.mkdir()
    if uncertainty == "symlink":
        pr.symlink_to(issue, target_is_directory=True)
    elif uncertainty == "ancestor_symlink":
        link = tmp_path / "link"
        link.symlink_to(issue, target_is_directory=True)
        pr = link / "missing"
    elif uncertainty == "permission":
        mocker.patch("os.access", return_value=False)
    else:
        for i in range(129):
            (issue / f"{i:03}.json").touch()
    assert "error" in _run_name_script(mocker, issue, pr)


def test_describe_includes_recovery_instruction():
    from jailbee.orchestration.child_outbox import PendingGithub, describe

    assert describe("c1", PendingGithub(("a.json",), (), ())) == (
        "child `c1` has 1 staged GitHub action(s) in its outbox (issue: a.json); "
        "they are never published from a child — ask it to send the content, "
        "then destroy with force: true"
    )
