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


def _probe_rig(mocker, *, issue=None, pr=(), files=None):
    """`issue` maps staged issue proposal names to manifests (the host store's rows)."""
    incus = MagicMock()
    incus.exec_capped.return_value = json.dumps({"pr": list(pr)})
    read = mocker.patch("jailbee.outbox_io.read_text_outbox", side_effect=files or [{}])
    staged = mocker.patch(
        "jailbee.orchestration.child_outbox._staged_issues", return_value=dict(issue or {})
    )
    return incus, read, staged


def test_probe_counts_manifests_not_actions_and_summarizes(mocker):
    from jailbee.orchestration.child_outbox import probe

    incus, read, staged = _probe_rig(
        mocker,
        issue={
            "001-a.json": json.dumps({"actions": [{"type": "create", "title": "Title"}]}),
            "broken.json": "not json",
        },
        pr=("002-y.json",),
        files=[
            {"002-y.json": json.dumps({"actions": [{"type": "review", "comments": [{}, {}, {}]}]})},
        ],
    )
    pending = probe(incus, "proj-c1", uid=1000, uuid="uuid-1")
    assert pending.count == 3
    assert pending.issue == ("001-a.json", "broken.json")
    assert pending.pr == ("002-y.json",)
    assert pending.summaries == (
        'issue/001-a.json: create "Title"',
        "issue/broken.json",
        "pr/002-y.json: review (3 comments)",
    )
    staged.assert_called_with("uuid-1")
    # Only the PR store is a container directory; issue proposals are never read from one.
    assert read.call_count == 1
    call = read.call_args
    assert call.kwargs["select"][0][0] == "*.json"
    assert call.kwargs["select"][0][1] <= 128
    assert call.kwargs["uid"] == 1000
    assert call.kwargs["max_file_bytes"] <= 65536
    assert call.args[2] == "/home/dev/.jailbee/pr-outbox"
    assert incus.exec_capped.call_args.kwargs["timeout"] == 60
    assert incus.exec_capped.call_args.kwargs["max_bytes"] <= 524288


def test_probe_deeply_nested_invalid_manifest_is_name_only(mocker):
    from jailbee.orchestration.child_outbox import probe

    incus, _, _ = _probe_rig(mocker, issue={"deep.json": "[" * 30000 + "]" * 30000})
    assert probe(incus, "proj-c1", uid=1000, uuid="u").summaries == ("issue/deep.json",)


def test_probe_snapshot_omission_cannot_hide_oversized_or_unreadable_manifest(mocker):
    from jailbee.orchestration.child_outbox import describe, probe

    incus, _, _ = _probe_rig(mocker, pr=("oversized.json", "unreadable.json"))
    pending = probe(incus, "proj-c1", uid=None)
    assert pending.count == 2
    assert "oversized.json" in describe("c1", pending)
    assert "unreadable.json" in describe("c1", pending)


def test_staged_issues_come_from_the_host_store_by_uuid(mocker):
    from types import SimpleNamespace

    from jailbee.orchestration.child_outbox import _staged_issues

    rows = [SimpleNamespace(name="001-a.json", manifest="{}")]
    read = mocker.patch("jailbee.outbox.store_db.read_proposals", return_value=rows)
    assert _staged_issues("uuid-1") == {"001-a.json": "{}"}
    assert read.call_args.args[1:] == ("uuid-1", "issue")
    assert _staged_issues(None) == {}


def test_unreadable_host_store_fails_closed(mocker):
    from jailbee.orchestration.child_outbox import ChildOutboxError, _staged_issues
    from jailbee.orchestration.store import StoreError

    mocker.patch("jailbee.outbox.store_db.read_proposals", side_effect=StoreError("corrupt"))
    with pytest.raises(ChildOutboxError):
        _staged_issues("uuid-1")


@pytest.mark.parametrize("raw", ["", "{}", '{"error":"permission denied"}', '{"pr":null}'])
def test_probe_unknown_names_fail_closed(mocker, raw):
    from jailbee.orchestration.child_outbox import ChildOutboxError, probe

    incus, _, _ = _probe_rig(mocker)
    incus.exec_capped.return_value = raw
    with pytest.raises(ChildOutboxError):
        probe(incus, "proj-c1", uid=1000)


def test_probe_read_failure_is_not_empty(mocker):
    from jailbee.orchestration.child_outbox import ChildOutboxError, probe
    from jailbee.outbox_io import OutboxReadError

    incus, read, _ = _probe_rig(mocker)
    read.side_effect = OutboxReadError("permission denied")
    with pytest.raises(ChildOutboxError):
        probe(incus, "proj-c1", uid=1000)


def test_probe_name_command_failure_is_not_empty(mocker):
    from jailbee.incus import IncusError
    from jailbee.orchestration.child_outbox import ChildOutboxError, probe

    incus, _, _ = _probe_rig(mocker)
    incus.exec_capped.side_effect = IncusError("timeout")
    with pytest.raises(ChildOutboxError):
        probe(incus, "proj-c1", uid=1000)


def test_probe_changed_snapshot_fails_closed(mocker):
    from jailbee.orchestration.child_outbox import ChildOutboxError, probe

    incus, _, _ = _probe_rig(mocker)
    incus.exec_capped.side_effect = ['{"pr":[]}', '{"pr":["late.json"]}']
    with pytest.raises(ChildOutboxError):
        probe(incus, "proj-c1", uid=1000)


def test_probe_changed_issue_proposals_fail_closed(mocker):
    from jailbee.orchestration.child_outbox import ChildOutboxError, probe

    incus, _, staged = _probe_rig(mocker)
    staged.side_effect = [{}, {"late.json": "{}"}]
    with pytest.raises(ChildOutboxError):
        probe(incus, "proj-c1", uid=1000, uuid="u")


def _run_name_script(mocker, pr: Path):
    from jailbee.orchestration.child_outbox import _NAMES_SCRIPT

    mocker.patch("sys.argv", ["probe", str(pr), "128"])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(compile(_NAMES_SCRIPT, "<child outbox probe>", "exec"), {})
    return json.loads(output.getvalue())


def test_name_script_missing_real_directory_is_empty(tmp_path, mocker):
    assert _run_name_script(mocker, tmp_path / "pr") == {"pr": []}


def test_name_script_filters_top_level_regular_manifests(tmp_path, mocker):
    pr = tmp_path / "pr"
    pr.mkdir()
    (pr / "a.json").write_text("x" * 65537)
    (pr / "x.progress.json").touch()
    (pr / "body.md").touch()
    (pr / ".temp.json").touch()
    (pr / "link.json").symlink_to(pr / "a.json")
    (pr / "dir.json").mkdir()
    (pr / "dir.json" / "nested.json").touch()
    assert _run_name_script(mocker, pr) == {"pr": ["a.json"]}


@pytest.mark.parametrize("uncertainty", ["symlink", "ancestor_symlink", "permission", "cap"])
def test_name_script_uncertainty_is_reported(tmp_path, mocker, uncertainty):
    pr = tmp_path / "pr"
    real = tmp_path / "real"
    real.mkdir()
    if uncertainty == "symlink":
        pr.symlink_to(real, target_is_directory=True)
    elif uncertainty == "ancestor_symlink":
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        pr = link / "missing"
    elif uncertainty == "permission":
        pr.mkdir()
        mocker.patch("os.access", return_value=False)
    else:
        pr.mkdir()
        for i in range(129):
            (pr / f"{i:03}.json").touch()
    assert "error" in _run_name_script(mocker, pr)


def test_describe_includes_recovery_instruction():
    from jailbee.orchestration.child_outbox import PendingGithub, describe

    assert describe("c1", PendingGithub(("a.json",), (), ())) == (
        "child `c1` has 1 staged GitHub action(s) in its outbox (issue: a.json); "
        "they are never published from a child — ask it to send the content, "
        "then destroy with force: true"
    )
