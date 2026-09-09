"""Unit tests for jailbee.pr_outbox."""

from __future__ import annotations

import base64
import io
import json
import tarfile

import pytest


def _manifest_text(**overrides) -> str:
    payload = {
        "version": 1,
        "repo": "acme/widgets",
        "pr": 1234,
        "head_sha": "abc1234",
        "actions": [{"type": "comment", "body": "looks good"}],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_minimal_comment_manifest():
    from jailbee.pr_outbox import CommentAction, parse_manifest

    m = parse_manifest("001-x.json", _manifest_text(), {})

    assert m.name == "001-x.json"
    assert m.repo == "acme/widgets"
    assert m.pr == 1234
    assert m.actions == (CommentAction(body="looks good"),)


def test_parse_review_resolves_body_file_and_span():
    from jailbee.pr_outbox import LineComment, parse_manifest

    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body_file": "001-summary.md",
                "comments": [
                    {"path": "src/a.py", "line": 88, "body": "rounds the wrong way"},
                    {
                        "path": "src/a.py",
                        "start_line": 120,
                        "line": 134,
                        "body_file": "001-c2.md",
                    },
                ],
            }
        ]
    )
    m = parse_manifest(
        "001-x.json", text, {"001-summary.md": "Two findings.", "001-c2.md": "Extract a helper."}
    )

    review = m.actions[0]
    assert review.body == "Two findings."
    assert review.event == "COMMENT"
    assert review.comments == (
        LineComment(path="src/a.py", line=88, body="rounds the wrong way"),
        LineComment(path="src/a.py", line=134, start_line=120, body="Extract a helper."),
    )


def test_parse_rejects_approve_event():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(
        actions=[{"type": "review", "event": "APPROVE", "body": "ship it", "comments": []}]
    )
    with pytest.raises(ManifestError, match="event 'APPROVE'"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_body_and_body_file_together():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "comment", "body": "a", "body_file": "b.md"}])
    with pytest.raises(ManifestError, match=r"001-x.json action 0"):
        parse_manifest("001-x.json", text, {"b.md": "b"})


def test_parse_rejects_body_file_escaping_the_outbox():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "comment", "body_file": "../secrets.md"}])
    with pytest.raises(ManifestError, match="outside the outbox"):
        parse_manifest("001-x.json", text, {"../secrets.md": "leak"})


def test_parse_null_pr_allows_only_a_description():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    ok = parse_manifest(
        "002-d.json",
        _manifest_text(pr=None, head_sha=None, actions=[{"type": "description", "body": "B"}]),
        {},
    )
    assert ok.pr is None

    with pytest.raises(ManifestError, match="pr: null"):
        parse_manifest(
            "002-d.json",
            _manifest_text(pr=None, head_sha=None, actions=[{"type": "comment", "body": "c"}]),
            {},
        )


def test_parse_rejects_unknown_version_by_name():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    with pytest.raises(ManifestError, match="version 2"):
        parse_manifest("001-x.json", _manifest_text(version=2), {})


def test_parse_rejects_start_line_not_before_line():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body": "x",
                "comments": [
                    {"path": "a.py", "start_line": 134, "line": 120, "body": "b"},
                ],
            }
        ]
    )
    with pytest.raises(ManifestError, match="start_line 134 must be before line 120"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_invalid_side():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body": "x",
                "comments": [
                    {"path": "a.py", "line": 10, "side": "UP", "body": "b"},
                ],
            }
        ]
    )
    with pytest.raises(ManifestError, match="'RIGHT' or 'LEFT'"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_absolute_comment_path():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body": "x",
                "comments": [{"path": "/etc/passwd", "line": 1, "body": "b"}],
            }
        ]
    )
    with pytest.raises(ManifestError, match="outside the repo"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_comment_path_with_dotdot_component():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body": "x",
                "comments": [{"path": "../../etc/passwd", "line": 1, "body": "b"}],
            }
        ]
    )
    with pytest.raises(ManifestError, match="outside the repo"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_second_review_action():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    review = {"type": "review", "body": "x", "comments": []}
    text = _manifest_text(actions=[review, review])
    with pytest.raises(ManifestError, match="only one review action"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_second_description_action():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    description = {"type": "description", "body": "x"}
    text = _manifest_text(actions=[description, description])
    with pytest.raises(ManifestError, match="only one description action"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_unknown_action_type():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "bogus", "body": "x"}])
    with pytest.raises(ManifestError, match=r"action 0.*unknown action type 'bogus'"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_too_many_comments():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    comments = [{"path": "a.py", "line": i + 1, "body": "b"} for i in range(101)]
    text = _manifest_text(actions=[{"type": "review", "body": "x", "comments": comments}])
    with pytest.raises(ManifestError, match="more than the cap of 100"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_too_many_actions():
    actions = [{"type": "comment", "body": "x"} for _ in range(51)]
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=actions)
    with pytest.raises(ManifestError, match="more than the cap of 50"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_body_over_64kb():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "comment", "body": "x" * (65 * 1024)}])
    with pytest.raises(ManifestError, match="larger than 64 KB"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_body_file_absent_from_bodies():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "comment", "body_file": "missing.md"}])
    with pytest.raises(ManifestError, match="no such file in the outbox"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_empty_actions_list():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[])
    with pytest.raises(ManifestError, match="no actions"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_non_integer_pr():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(pr="not-a-number")
    with pytest.raises(ManifestError, match="pr must be an integer"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_non_integer_comment_id():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "reply", "comment_id": "abc", "body": "x"}])
    with pytest.raises(ManifestError, match="comment_id must be an integer"):
        parse_manifest("001-x.json", text, {})


def _archive(files: dict[str, bytes], *, extra: list[tarfile.TarInfo] | None = None) -> str:
    """Build a base64 tar exactly as the container-side command would emit it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=f"./{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return base64.b64encode(buf.getvalue()).decode()


def test_read_outbox_returns_every_text_file(mocker):
    from jailbee.pr_outbox import read_outbox

    incus = mocker.MagicMock()
    incus.exec.return_value = _archive({"001-x.json": b'{"version": 1}', "001-x.md": b"prose"})

    outbox = read_outbox(incus, "acme-feat-foo", uid=1000)

    assert outbox.files == {"001-x.json": '{"version": 1}', "001-x.md": "prose"}
    assert outbox.manifest_names == ["001-x.json"]
    # One round-trip, no shell string interpolation of the path.
    incus.exec.assert_called_once()
    cmd = incus.exec.call_args.args[1]
    assert cmd[0] == "bash" and cmd[1] == "-c"
    assert "/home/dev/.jailbee/pr-outbox" in cmd


def test_read_outbox_is_empty_when_the_directory_is_missing(mocker):
    from jailbee.pr_outbox import read_outbox

    incus = mocker.MagicMock()
    incus.exec.return_value = ""  # `cd || exit 0` produced nothing

    assert read_outbox(incus, "c", uid=1000).files == {}


def test_read_outbox_skips_progress_files_in_manifest_names(mocker):
    from jailbee.pr_outbox import read_outbox

    incus = mocker.MagicMock()
    incus.exec.return_value = _archive(
        {"002-b.json": b"{}", "001-a.json": b"{}", "001-a.json.progress.json": b"{}"}
    )

    # Sorted, and the sidecar is not a manifest.
    assert read_outbox(incus, "c", uid=1000).manifest_names == ["001-a.json", "002-b.json"]


def test_read_outbox_drops_hostile_members(mocker):
    from jailbee.pr_outbox import read_outbox

    absolute = tarfile.TarInfo(name="/etc/passwd")
    escape = tarfile.TarInfo(name="../../.ssh/id_ed25519")
    link = tarfile.TarInfo(name="./link.md")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/shadow"
    nested = tarfile.TarInfo(name="./deep/deeper/x.json")

    incus = mocker.MagicMock()
    incus.exec.return_value = _archive(
        {"001-x.json": b"{}"}, extra=[absolute, escape, link, nested]
    )

    assert read_outbox(incus, "c", uid=1000).files == {"001-x.json": "{}"}


def test_read_outbox_drops_oversized_member(mocker):
    from jailbee.pr_outbox import MAX_MANIFEST_BYTES, read_outbox

    incus = mocker.MagicMock()
    incus.exec.return_value = _archive(
        {"001-x.json": b"{}", "huge.json": b"x" * (MAX_MANIFEST_BYTES + 1)}
    )

    assert read_outbox(incus, "c", uid=1000).files == {"001-x.json": "{}"}


def test_read_outbox_drops_undecodable_utf8_member(mocker):
    from jailbee.pr_outbox import read_outbox

    incus = mocker.MagicMock()
    incus.exec.return_value = _archive({"001-x.json": b"{}", "bad.json": b"\xff\xfe\xfd"})

    assert read_outbox(incus, "c", uid=1000).files == {"001-x.json": "{}"}


def test_read_outbox_raises_on_undecodable_output(mocker):
    from jailbee.pr_outbox import OutboxReadError, read_outbox

    incus = mocker.MagicMock()
    incus.exec.return_value = "not base64 at all !!!"

    with pytest.raises(OutboxReadError, match="unreadable"):
        read_outbox(incus, "c", uid=1000)


def test_read_outbox_wraps_incus_failure(mocker):
    from jailbee.incus import IncusError
    from jailbee.pr_outbox import OutboxReadError, read_outbox

    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("exit 1: Instance is not running")

    with pytest.raises(OutboxReadError, match="not running"):
        read_outbox(incus, "c", uid=1000)
