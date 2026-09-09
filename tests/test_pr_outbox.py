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
    # A hard link pointing at the legitimate member: extractfile() follows
    # hard links unconditionally and would hand back real content, so this
    # is the case `member.isfile()` actually exists to stop (a symlink is
    # already caught earlier by extractfile() returning None).
    hardlink = tarfile.TarInfo(name="./hardlink.json")
    hardlink.type = tarfile.LNKTYPE
    hardlink.linkname = "./001-x.json"

    incus = mocker.MagicMock()
    incus.exec.return_value = _archive(
        {"001-x.json": b"{}"}, extra=[absolute, escape, link, nested, hardlink]
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


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git@github.com:acme/widgets.git", "acme/widgets"),
        ("https://github.com/acme/widgets.git", "acme/widgets"),
        ("https://github.com/acme/widgets", "acme/widgets"),
        ("ssh://git@github.com/acme/widgets.git", "acme/widgets"),
        ("https://gitlab.com/acme/widgets.git", None),
        ("", None),
    ],
)
def test_github_slug(url, slug):
    from jailbee.pr_outbox import github_slug

    assert github_slug(url) == slug


def _pr_info(number=1234, head_sha="abc1234", head_ref="feat/foo"):
    from jailbee.pr import PrInfo

    return PrInfo(
        number=number, head_ref=head_ref, head_sha=head_sha, state="OPEN", base_ref="main"
    )


def _target_setup(mocker, tmp_path, *, labels=None, pr=None):
    """Host-side mocks shared by the gate tests."""
    mocker.patch("jailbee.git.get_remote_url", return_value="git@github.com:acme/widgets.git")
    mocker.patch("jailbee.pr.resolve_pr", return_value=pr or _pr_info())
    incus = mocker.MagicMock()
    label_map = labels if labels is not None else {"user.jailbee.pr": "1234"}
    incus.config_get.side_effect = lambda name, key: label_map.get(key)
    return incus


def test_resolve_target_accepts_the_containers_own_pr(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path)
    cfg = make_cfg(tmp_path)
    manifest = parse_manifest("001-x.json", _manifest_text(), {})

    target = resolve_target(cfg, incus, "acme-feat-foo", manifest, force=False)

    assert target.pr is not None and target.pr.number == 1234
    assert target.stale is False


def test_resolve_target_refuses_a_foreign_repo(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import GateError, parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path)
    manifest = parse_manifest("001-x.json", _manifest_text(repo="evil/other"), {})

    with pytest.raises(GateError, match="evil/other"):
        resolve_target(make_cfg(tmp_path), incus, "c", manifest, force=False)


def test_resolve_target_refuses_a_pr_the_container_does_not_own(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import GateError, parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path, labels={"user.jailbee.pr": "1234"})
    manifest = parse_manifest("001-x.json", _manifest_text(pr=999), {})

    with pytest.raises(GateError, match="#999"):
        resolve_target(make_cfg(tmp_path), incus, "c", manifest, force=False)


def test_resolve_target_falls_back_to_the_branchs_pr_without_a_label(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path, labels={"user.jailbee.branch": "feat/foo"})
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=_pr_info())
    manifest = parse_manifest("001-x.json", _manifest_text(), {})

    assert resolve_target(make_cfg(tmp_path), incus, "c", manifest, force=False).pr.number == 1234


def test_stale_head_blocks_a_review_but_not_a_reply(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import GateError, parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path, pr=_pr_info(head_sha="def5678"))
    cfg = make_cfg(tmp_path)

    review = parse_manifest(
        "001-r.json",
        _manifest_text(actions=[{"type": "review", "body": "s", "comments": []}]),
        {},
    )
    with pytest.raises(GateError, match="head moved abc1234 → def5678"):
        resolve_target(cfg, incus, "c", review, force=False)

    # --force lets it through, still marked stale so the plan can say so.
    forced = resolve_target(cfg, incus, "c", review, force=True)
    assert forced.stale is True

    reply = parse_manifest(
        "001-p.json", _manifest_text(actions=[{"type": "reply", "comment_id": 9, "body": "ok"}]), {}
    )
    assert resolve_target(cfg, incus, "c", reply, force=False).stale is True  # informational only


def test_null_pr_manifest_resolves_without_a_pr(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path)
    manifest = parse_manifest(
        "002-d.json",
        _manifest_text(pr=None, head_sha=None, actions=[{"type": "description", "body": "B"}]),
        {},
    )

    assert resolve_target(make_cfg(tmp_path), incus, "c", manifest, force=False).pr is None


def test_plan_lines_show_anchors_truncated_bodies_and_a_description_diff():
    from jailbee.pr_outbox import Target, parse_manifest, plan_lines

    long_comment_body = (
        "This rounds half-down where the spec says half-up, which shifts every total."
    )
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[
                {
                    "type": "review",
                    "body": "Two findings.",
                    "comments": [
                        {
                            "path": "src/a.py",
                            "line": 88,
                            "body": long_comment_body,
                        },
                        {
                            "path": "src/a.py",
                            "start_line": 120,
                            "line": 134,
                            "body": "Extract a helper.",
                        },
                    ],
                },
                {"type": "comment", "body": "Two blocking findings.", "reply_to": 4455},
                {"type": "description", "body": "New body.\n", "title": "feat: round half-up"},
            ]
        ),
        {},
    )
    lines = plan_lines(
        Target(manifest=manifest, pr=_pr_info(), stale=False), current_body="Old body.\n"
    )
    joined = "\n".join(lines)

    assert "src/a.py:88" in joined
    assert "src/a.py:120-134" in joined
    assert "This rounds half-down" in joined
    assert "shifts every total" not in joined  # truncated to one line
    assert "reply to general comment #4455" in joined
    assert "-Old body." in joined and "+New body." in joined  # unified diff


# --------------------------------------------------------------------------
# apply_manifest / finalize / record_consumed / read_progress (Task 6)
# --------------------------------------------------------------------------


def _apply_mocks(mocker):
    return {
        "review": mocker.patch("jailbee.pr.submit_review", return_value="https://x/r"),
        "reply": mocker.patch("jailbee.pr.reply_to_review_comment", return_value="https://x/p"),
        "comment": mocker.patch("jailbee.pr.add_issue_comment", return_value="https://x/c"),
        "edit": mocker.patch("jailbee.pr.edit_pr"),
    }


def test_apply_runs_review_then_comments_then_description(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Progress, Target, apply_manifest, parse_manifest

    calls = _apply_mocks(mocker)
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[
                {"type": "description", "body": "New body.", "title": "t"},
                {"type": "reply", "comment_id": 9, "body": "ok"},
                {
                    "type": "review",
                    "body": "s",
                    "comments": [{"path": "a.py", "line": 1, "body": "b"}],
                },
            ]
        ),
        {},
    )
    incus = mocker.MagicMock()

    outcome = apply_manifest(
        make_cfg(tmp_path),
        incus,
        "c",
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
    )

    assert outcome.failure is None
    assert outcome.applied == (2, 1, 0)  # review, reply, description
    calls["review"].assert_called_once()
    assert calls["review"].call_args.kwargs["commit_id"] == "abc1234"
    calls["edit"].assert_called_once()


def test_apply_prefixes_a_general_reply_with_a_permalink(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Progress, Target, apply_manifest, parse_manifest

    calls = _apply_mocks(mocker)
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(actions=[{"type": "comment", "body": "Agreed.", "reply_to": 4455}]),
        {},
    )

    apply_manifest(
        make_cfg(tmp_path),
        mocker.MagicMock(),
        "c",
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
    )

    body = calls["comment"].call_args.args[2]
    assert "#issuecomment-4455" in body
    assert body.rstrip().endswith("Agreed.")


def test_apply_skips_indices_already_applied(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Progress, Target, apply_manifest, parse_manifest

    calls = _apply_mocks(mocker)
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[
                {"type": "comment", "body": "one"},
                {"type": "comment", "body": "two"},
            ]
        ),
        {},
    )

    outcome = apply_manifest(
        make_cfg(tmp_path),
        mocker.MagicMock(),
        "c",
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        Progress(applied=frozenset({0}), urls={}),
        uid=1000,
    )

    assert outcome.applied == (1,)
    assert calls["comment"].call_count == 1
    assert calls["comment"].call_args.args[2] == "two"


def test_apply_stops_at_the_first_failure_and_records_progress(mocker, make_cfg, tmp_path):
    from jailbee.pr import PrReviewError
    from jailbee.pr_outbox import Progress, Target, apply_manifest, parse_manifest

    calls = _apply_mocks(mocker)
    calls["comment"].side_effect = ["https://x/c", PrReviewError("HTTP 500")]
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[
                {"type": "comment", "body": "one"},
                {"type": "comment", "body": "two"},
                {"type": "comment", "body": "three"},
            ]
        ),
        {},
    )
    incus = mocker.MagicMock()

    outcome = apply_manifest(
        make_cfg(tmp_path),
        incus,
        "c",
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
    )

    assert outcome.applied == (0,)
    assert outcome.failure is not None and "HTTP 500" in outcome.failure
    assert calls["comment"].call_count == 2  # the third was never attempted


def test_apply_writes_the_progress_sidecar_after_each_success(mocker, make_cfg, tmp_path):
    """A crash between actions must not lose what already landed.

    apply_manifest is handed `incus`/`container`/`uid` for exactly this: it
    must persist the running total to the container after every successful
    action, not only once at the very end via `finalize`.
    """
    from jailbee.pr_outbox import Progress, Target, apply_manifest, parse_manifest

    _apply_mocks(mocker)
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[{"type": "comment", "body": "one"}, {"type": "comment", "body": "two"}]
        ),
        {},
    )
    incus = mocker.MagicMock()

    apply_manifest(
        make_cfg(tmp_path),
        incus,
        "c",
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
    )

    sidecar_writes = [
        c
        for c in incus.exec.call_args_list
        if c.args[1][0] == "bash" and any("001-x.json.progress.json" in str(a) for a in c.args[1])
    ]
    assert len(sidecar_writes) == 2, "one write after each of the two successful actions"
    # The second (final) write reflects both indices, not just the latest one.
    joined = " ".join(sidecar_writes[-1].args[1])
    assert '"applied": [0, 1]' in joined or '"applied":[0,1]' in joined


def test_apply_reports_a_recording_failure_after_a_successful_post_and_stops(
    mocker, make_cfg, tmp_path
):
    """Fix-round-1 regression: a container-write failure after a successful
    GitHub post must not silently repost on the next run.

    Action 0 lands on GitHub, but the sidecar write that was supposed to
    record it raises `IncusError` (container stopped, disk full, ...). That
    must not escape as a bare exception: index 0 stays in `applied`/`urls`
    (it really did land), `failure` says plainly that it landed and could
    not be recorded, and action 1 is never attempted — exactly like a
    `PrError` mid-run.
    """
    from jailbee.incus import IncusError
    from jailbee.pr_outbox import Progress, Target, apply_manifest, parse_manifest

    calls = _apply_mocks(mocker)
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[{"type": "comment", "body": "one"}, {"type": "comment", "body": "two"}]
        ),
        {},
    )
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("container is not running")

    outcome = apply_manifest(
        make_cfg(tmp_path),
        incus,
        "c",
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
    )

    assert outcome.applied == (0,), "the post landed even though it couldn't be recorded"
    assert outcome.urls == ("https://x/c",)
    assert outcome.failure is not None
    assert "action 0" in outcome.failure
    assert "published" in outcome.failure
    assert "re-run may repost" in outcome.failure
    assert calls["comment"].call_count == 1, "action 1 must never be attempted"


def test_finalize_does_not_append_a_stale_log_line_when_nothing_new_applied(mocker):
    """Minor fix-round-1: the log line is gated on *this call's* new indices,
    not the merged total — a re-finalize that applied nothing new must not
    write an `actions=0` line."""
    from jailbee.pr_outbox import ApplyOutcome, Outbox, Target, finalize, parse_manifest

    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[{"type": "comment", "body": "a"}, {"type": "comment", "body": "b"}]
        ),
        {},
    )
    outbox = Outbox(
        files={
            "001-x.json": "…",
            "001-x.json.progress.json": '{"applied": [0], "urls": {"0": "https://x/a"}}',
        }
    )
    incus = mocker.MagicMock()

    finalize(
        incus,
        "c",
        outbox,
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        ApplyOutcome(applied=(), urls=(), failure="HTTP 500"),
        uid=1000,
    )

    log_writes = [
        c
        for c in incus.exec.call_args_list
        if c.args[1][0] == "bash" and any(str(a).endswith("applied.log") for a in c.args[1])
    ]
    assert not log_writes, "a re-finalize that applied nothing new must not log a stale line"
    # The (idempotent) sidecar write is still fine to repeat.
    sidecar_writes = [
        c
        for c in incus.exec.call_args_list
        if c.args[1][0] == "bash" and any("progress.json" in str(a) for a in c.args[1])
    ]
    assert sidecar_writes


def test_finalize_raises_when_the_sidecar_write_fails(mocker):
    """Fix-round-1: a container-write failure inside finalize must not be a
    bare, context-free IncusError, and must stop before the log/rm steps."""
    from jailbee.incus import IncusError
    from jailbee.pr_outbox import (
        ApplyOutcome,
        FinalizeError,
        Outbox,
        Target,
        finalize,
        parse_manifest,
    )

    manifest = parse_manifest(
        "001-x.json", _manifest_text(actions=[{"type": "comment", "body": "a"}]), {}
    )
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("disk full")

    with pytest.raises(FinalizeError, match=r"001-x\.json"):
        finalize(
            incus,
            "c",
            Outbox(files={"001-x.json": "…"}),
            Target(manifest=manifest, pr=_pr_info(), stale=False),
            ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
            uid=1000,
        )

    # Only the sidecar-write attempt happened; log/rm were never tried.
    assert incus.exec.call_count == 1


def test_finalize_raises_when_deleting_a_fully_applied_manifest_fails(mocker):
    """Fix-round-1: `rm` failing on a fully-applied manifest must raise
    FinalizeError (with the sidecar and log already durably written), not
    vanish as a bare IncusError."""
    from jailbee.incus import IncusError
    from jailbee.pr_outbox import (
        ApplyOutcome,
        FinalizeError,
        Outbox,
        Target,
        finalize,
        parse_manifest,
    )

    manifest = parse_manifest(
        "001-x.json", _manifest_text(actions=[{"type": "comment", "body": "a"}]), {}
    )
    incus = mocker.MagicMock()

    def fake_exec(container, cmd, **kwargs):
        if cmd[0] == "rm":
            raise IncusError("permission denied")
        return ""

    incus.exec.side_effect = fake_exec

    with pytest.raises(FinalizeError, match="could not be deleted"):
        finalize(
            incus,
            "c",
            Outbox(files={"001-x.json": "…"}),
            Target(manifest=manifest, pr=_pr_info(), stale=False),
            ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
            uid=1000,
        )


def test_record_consumed_raises_when_the_sidecar_write_fails(mocker):
    """Fix-round-1: the same container-write protection applies to the
    single-index `record_consumed` path `jb pr` will use."""
    from jailbee.incus import IncusError
    from jailbee.pr_outbox import FinalizeError, record_consumed

    def fake_exec(container, cmd, **kwargs):
        if cmd[0] == "cat":
            raise IncusError("no such file")
        if cmd[0] == "bash":
            raise IncusError("disk full")
        return ""

    incus = mocker.MagicMock()
    incus.exec.side_effect = fake_exec

    with pytest.raises(FinalizeError, match=r"002-d\.json"):
        record_consumed(incus, "c", "002-d.json", 0, "https://x/pr", uid=1000)


def test_finalize_deletes_a_fully_applied_manifest_and_its_own_bodies(mocker):
    from jailbee.pr_outbox import ApplyOutcome, Outbox, Target, finalize, parse_manifest

    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(actions=[{"type": "comment", "body_file": "001-x.md"}]),
        {"001-x.md": "text"},
    )
    outbox = Outbox(files={"001-x.json": "…", "001-x.md": "text", "002-y.json": "…"})
    incus = mocker.MagicMock()

    finalize(
        incus,
        "c",
        outbox,
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
        uid=1000,
    )

    removed = [c for c in incus.exec.call_args_list if "rm" in c.args[1]]
    assert removed, "a fully applied manifest must be deleted"
    argv = " ".join(removed[0].args[1])
    assert "001-x.json" in argv and "001-x.md" in argv
    assert "002-y.json" not in argv


def test_finalize_keeps_a_shared_body_file_referenced_by_another_manifest(mocker):
    """A `body_file` still named by a pending manifest must survive cleanup."""
    from jailbee.pr_outbox import ApplyOutcome, Outbox, Target, finalize, parse_manifest

    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(actions=[{"type": "comment", "body_file": "shared.md"}]),
        {"shared.md": "text"},
    )
    other_manifest_text = _manifest_text(actions=[{"type": "comment", "body_file": "shared.md"}])
    outbox = Outbox(
        files={"001-x.json": "…", "shared.md": "text", "002-y.json": other_manifest_text}
    )
    incus = mocker.MagicMock()

    finalize(
        incus,
        "c",
        outbox,
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
        uid=1000,
    )

    removed = [c for c in incus.exec.call_args_list if "rm" in c.args[1]]
    assert removed, "a fully applied manifest must still be deleted"
    argv = " ".join(removed[0].args[1])
    assert "001-x.json" in argv
    assert "shared.md" not in argv, "shared.md is still referenced by 002-y.json"


def test_finalize_keeps_a_partly_applied_manifest_and_writes_progress(mocker):
    from jailbee.pr_outbox import ApplyOutcome, Outbox, Target, finalize, parse_manifest

    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[{"type": "comment", "body": "a"}, {"type": "comment", "body": "b"}]
        ),
        {},
    )
    incus = mocker.MagicMock()

    finalize(
        incus,
        "c",
        Outbox(files={"001-x.json": "…"}),
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        ApplyOutcome(applied=(0,), urls=("https://x/c",), failure="HTTP 500"),
        uid=1000,
    )

    written = " ".join(" ".join(c.args[1]) for c in incus.exec.call_args_list)
    assert "001-x.json.progress.json" in written
    assert '"applied": [0]' in written or '"applied":[0]' in written
    assert "rm" not in written


def test_finalize_merges_new_progress_with_what_a_previous_run_already_landed(mocker):
    """A second run's outcome must not clobber a first run's recorded progress.

    If this ran twice against the same outbox, index 0 must still be
    remembered as applied even though *this* call's outcome only carries the
    newly-applied index 1 — otherwise a retry would repost index 0.
    """
    from jailbee.pr_outbox import ApplyOutcome, Outbox, Target, finalize, parse_manifest

    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[{"type": "comment", "body": "a"}, {"type": "comment", "body": "b"}]
        ),
        {},
    )
    outbox = Outbox(
        files={
            "001-x.json": "…",
            "001-x.json.progress.json": '{"applied": [0], "urls": {"0": "https://x/a"}}',
        }
    )
    incus = mocker.MagicMock()

    finalize(
        incus,
        "c",
        outbox,
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        ApplyOutcome(applied=(1,), urls=("https://x/b",), failure=None),
        uid=1000,
    )

    removed = [c for c in incus.exec.call_args_list if "rm" in c.args[1]]
    assert removed, "both indices are now applied, so the manifest must be deleted"


def test_finalize_appends_one_applied_log_line(mocker):
    """The one extra test the brief describes in prose, not in code.

    `finalize` appends one line to `applied.log` containing the manifest
    name, `pr=1234`, the action count and every URL.
    """
    from jailbee.pr_outbox import ApplyOutcome, Outbox, Target, finalize, parse_manifest

    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[{"type": "comment", "body": "a"}, {"type": "comment", "body": "b"}]
        ),
        {},
    )
    incus = mocker.MagicMock()

    finalize(
        incus,
        "c",
        Outbox(files={"001-x.json": "…"}),
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        ApplyOutcome(applied=(0, 1), urls=("https://x/a", "https://x/b"), failure=None),
        uid=1000,
    )

    log_writes = [
        c
        for c in incus.exec.call_args_list
        if c.args[1][0] == "bash" and any(str(a).endswith("applied.log") for a in c.args[1])
    ]
    assert len(log_writes) == 1
    line = log_writes[0].args[1][4]  # ["bash", "-c", script, "bash", line, path]
    assert "001-x.json" in line
    assert "pr=1234" in line
    assert "actions=2" in line
    assert "https://x/a" in line and "https://x/b" in line


def test_finalize_presents_an_empty_receipt_url_as_a_placeholder_not_a_blank_link(mocker):
    """A 2xx response that lacks `html_url` makes pr.py return "".

    That empty string must never be written into applied.log as if it were a
    real link — a blank field there reads as "the link is missing", not "no
    link exists", and would look like a jailbee bug rather than a GitHub
    response quirk.
    """
    from jailbee.pr_outbox import ApplyOutcome, Outbox, Target, finalize, parse_manifest

    manifest = parse_manifest(
        "001-x.json", _manifest_text(actions=[{"type": "comment", "body": "a"}]), {}
    )
    incus = mocker.MagicMock()

    finalize(
        incus,
        "c",
        Outbox(files={"001-x.json": "…"}),
        Target(manifest=manifest, pr=_pr_info(), stale=False),
        ApplyOutcome(applied=(0,), urls=("",), failure=None),
        uid=1000,
    )

    log_writes = [
        c
        for c in incus.exec.call_args_list
        if c.args[1][0] == "bash" and any(str(a).endswith("applied.log") for a in c.args[1])
    ]
    line = log_writes[0].args[1][4]
    assert "urls=" in line
    assert not line.rstrip().endswith("urls=")  # not a bare, blank field
    assert "(no url)" in line


def test_read_progress_tolerates_a_missing_or_broken_sidecar():
    from jailbee.pr_outbox import Outbox, read_progress

    assert read_progress(Outbox(files={}), "001-x.json").applied == frozenset()
    broken = Outbox(files={"001-x.json.progress.json": "{ not json"})
    assert read_progress(broken, "001-x.json").applied == frozenset()
    good = Outbox(files={"001-x.json.progress.json": '{"applied": [0, 2], "urls": {}}'})
    assert read_progress(good, "001-x.json").applied == frozenset({0, 2})


def test_record_consumed_writes_progress_and_deletes_when_nothing_pending(mocker):
    """The single-index form `jb pr` (Task 11) uses.

    No `Outbox`/`Target` is in hand at that call site — only the manifest
    name and the one index `jb pr` itself just consumed — so this reads the
    manifest and its sidecar directly off the container.
    """
    from jailbee.incus import IncusError
    from jailbee.pr_outbox import record_consumed

    manifest_json = _manifest_text(
        pr=None, head_sha=None, actions=[{"type": "description", "body": "B"}]
    )

    def fake_exec(container, cmd, **kwargs):
        assert container == "c"
        if cmd[0] == "cat":
            path = cmd[1]
            if path.endswith(".progress.json"):
                raise IncusError("no such file")
            return manifest_json
        return ""

    incus = mocker.MagicMock()
    incus.exec.side_effect = fake_exec

    record_consumed(incus, "c", "002-d.json", 0, "https://x/pr", uid=1000)

    calls = incus.exec.call_args_list
    write_calls = [c for c in calls if c.args[1][0] == "bash"]
    assert any(any("002-d.json.progress.json" in str(a) for a in c.args[1]) for c in write_calls)
    log_call = next(c for c in write_calls if any("applied.log" in str(a) for a in c.args[1]))
    line = log_call.args[1][4]
    assert "002-d.json" in line and "actions=1" in line and "https://x/pr" in line

    rm_calls = [c for c in calls if c.args[1][0] == "rm"]
    assert rm_calls, "the manifest's only action is now applied; it must be deleted"
    assert any("002-d.json" in a for a in rm_calls[0].args[1])


def test_record_consumed_keeps_a_manifest_still_missing_other_actions(mocker):
    from jailbee.incus import IncusError
    from jailbee.pr_outbox import record_consumed

    manifest_json = _manifest_text(
        actions=[{"type": "description", "body": "B"}, {"type": "comment", "body": "c"}]
    )

    def fake_exec(container, cmd, **kwargs):
        if cmd[0] == "cat":
            if cmd[1].endswith(".progress.json"):
                raise IncusError("no such file")
            return manifest_json
        return ""

    incus = mocker.MagicMock()
    incus.exec.side_effect = fake_exec

    record_consumed(incus, "c", "001-x.json", 0, "https://x/pr", uid=1000)

    rm_calls = [c for c in incus.exec.call_args_list if c.args[1][0] == "rm"]
    assert not rm_calls, "one of two actions is applied; the manifest must stay"


def test_drop_manifest_deletes_it_with_its_sidecar_and_own_bodies(mocker):
    from jailbee.pr_outbox import Outbox, drop_manifest

    outbox = Outbox(
        files={
            "001-x.json": _manifest_text(actions=[{"type": "comment", "body_file": "001-x.md"}]),
            "001-x.json.progress.json": '{"applied": [], "urls": {}}',
            "001-x.md": "text",
            "002-y.json": _manifest_text(),
        }
    )
    incus = mocker.MagicMock()

    deleted = drop_manifest(incus, "c", outbox, "001-x.json", uid=1000)

    assert deleted == ["001-x.json", "001-x.json.progress.json", "001-x.md"]
    rm_calls = [c for c in incus.exec.call_args_list if c.args[1][0] == "rm"]
    assert len(rm_calls) == 1
    argv = " ".join(rm_calls[0].args[1])
    assert "001-x.json" in argv and "001-x.md" in argv
    assert "002-y.json" not in argv


def test_drop_manifest_keeps_a_body_file_another_manifest_still_uses(mocker):
    from jailbee.pr_outbox import Outbox, drop_manifest

    shared = _manifest_text(actions=[{"type": "comment", "body_file": "shared.md"}])
    outbox = Outbox(files={"001-x.json": shared, "002-y.json": shared, "shared.md": "text"})
    incus = mocker.MagicMock()

    deleted = drop_manifest(incus, "c", outbox, "001-x.json", uid=1000)

    assert deleted == ["001-x.json"]
    argv = " ".join(incus.exec.call_args_list[0].args[1])
    assert "shared.md" not in argv


def test_drop_manifest_raises_when_the_deletion_fails(mocker):
    import pytest

    from jailbee.incus import IncusError
    from jailbee.pr_outbox import FinalizeError, Outbox, drop_manifest

    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("instance is not running")

    with pytest.raises(FinalizeError, match=r"001-x\.json"):
        drop_manifest(incus, "c", Outbox(files={"001-x.json": "…"}), "001-x.json", uid=1000)


def test_action_summary_counts_actions_by_type():
    from jailbee.pr_outbox import action_summary, parse_manifest

    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[
                {"type": "review", "body": "summary", "comments": []},
                {"type": "reply", "comment_id": 7, "body": "a"},
                {"type": "reply", "comment_id": 8, "body": "b"},
                {"type": "description", "body": "new body"},
            ]
        ),
        {},
    )

    assert action_summary(manifest) == "review:1 reply:2 description:1"


def test_pending_indices_skips_what_already_landed():
    from jailbee.pr_outbox import Progress, parse_manifest, pending_indices

    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[
                {"type": "comment", "body": "a"},
                {"type": "comment", "body": "b"},
                {"type": "comment", "body": "c"},
            ]
        ),
        {},
    )

    assert pending_indices(manifest, Progress(applied=frozenset(), urls={})) == [0, 1, 2]
    assert pending_indices(manifest, Progress(applied=frozenset({1}), urls={})) == [0, 2]
    assert pending_indices(manifest, Progress(applied=frozenset({0, 1, 2}), urls={})) == []


def test_show_lines_render_every_body_in_full():
    """`show_lines` is the untruncated counterpart of `plan_lines`."""
    from jailbee.pr_outbox import parse_manifest, show_lines

    long_body = "x" * 400
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            actions=[
                {
                    "type": "review",
                    "body": long_body,
                    "comments": [
                        {
                            "path": "src/a.py",
                            "line": 134,
                            "start_line": 120,
                            "body": "line one\nline two",
                        }
                    ],
                },
                {"type": "comment", "body": "general", "reply_to": 4455},
                {"type": "description", "title": "T", "branch": "b", "body": "new body"},
            ]
        ),
        {},
    )

    lines = show_lines(manifest)

    assert lines[0] == "001-x.json  acme/widgets  PR #1234"
    assert long_body in lines, "no truncation, and no re-wrapping"
    assert "  src/a.py:120-134" in lines
    # A multi-line body arrives as its own lines, so a caller printing line by
    # line reproduces it exactly.
    assert ["line one", "line two"] == lines[lines.index("  src/a.py:120-134") + 1 :][:2]
    assert "action 1 · COMMENT (general), replying to general comment #4455" in lines
    assert "  title: T" in lines
    assert "  branch: b" in lines
    assert "new body" in lines


def test_show_lines_name_a_manifest_with_no_pr_yet():
    from jailbee.pr_outbox import parse_manifest, show_lines

    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(pr=None, actions=[{"type": "description", "body": "b"}]),
        {},
    )

    assert show_lines(manifest)[0].endswith("no PR yet")


# --------------------------------------------------------------------------
# `pending_pr_text` — the description `jailbee pr` uses instead of Claude
# --------------------------------------------------------------------------


def _description_manifest(**overrides) -> str:
    """A manifest `jailbee pr`'s create path accepts: this repo, no PR yet."""
    payload = {"pr": None, "head_sha": None, "actions": [{"type": "description", "body": "B"}]}
    payload.update(overrides)
    return _manifest_text(**payload)


def _host_repo(mocker, url="git@github.com:acme/widgets.git"):
    """Stub the host's own upstream remote — the repo-lock gate reads it."""
    return mocker.patch("jailbee.git.get_remote_url", return_value=url)


def test_pending_pr_text_returns_the_description_as_a_prtext(mocker, make_cfg, tmp_path):
    from jailbee.pr_ai import PrText
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    text = _description_manifest(
        actions=[{"type": "description", "body": "Body.", "title": "feat: x", "branch": "feat/x"}]
    )
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"002-d.json": text}))

    found = pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000)

    assert found is not None
    assert found.text == PrText(title="feat: x", body="Body.", branch="feat/x")
    assert found.manifest == "002-d.json"
    assert found.index == 0


def test_pending_pr_text_is_none_without_a_description(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-x.json": _manifest_text()}),
    )

    assert pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000) is None


def test_pending_pr_text_skips_an_already_consumed_description(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(
            files={
                "001-x.json": _description_manifest(),
                "001-x.json.progress.json": '{"applied": [0], "urls": {}}',
            }
        ),
    )

    assert pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000) is None


def test_pending_pr_text_asks_which_manifest_when_two_compete(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    a = _description_manifest(actions=[{"type": "description", "body": "A"}])
    b = _description_manifest(actions=[{"type": "description", "body": "B"}])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-a.json": a, "002-b.json": b}),
    )

    found = pending_pr_text(
        make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000, pick=lambda names: "002-b.json"
    )

    assert found is not None and found.manifest == "002-b.json"
    assert found.text.body == "B"


def test_pending_pr_text_declines_to_guess_without_a_picker(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    a = _description_manifest(actions=[{"type": "description", "body": "A"}])
    b = _description_manifest(actions=[{"type": "description", "body": "B"}])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-a.json": a, "002-b.json": b}),
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    # `pick=None` is the off-TTY case: warn, name both, and fall back.
    assert pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000) is None
    assert "001-a.json" in warn.call_args.args[0]
    assert "002-b.json" in warn.call_args.args[0]


def test_pending_pr_text_returns_none_when_the_picker_cancels(mocker, make_cfg, tmp_path):
    """A cancelled picker means "run Claude after all", not "guess"."""
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    a = _description_manifest(actions=[{"type": "description", "body": "A"}])
    b = _description_manifest(actions=[{"type": "description", "body": "B"}])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-a.json": a, "002-b.json": b}),
    )

    found = pending_pr_text(
        make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000, pick=lambda names: None
    )

    assert found is None


def test_pending_pr_text_survives_an_unreadable_outbox(mocker, make_cfg, tmp_path):
    """`jailbee pr` must never die because the outbox could not be read."""
    from jailbee.pr_outbox import OutboxReadError, pending_pr_text

    mocker.patch(
        "jailbee.pr_outbox.read_outbox", side_effect=OutboxReadError("instance is not running")
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    assert pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000) is None
    assert "not running" in warn.call_args.args[0]


def test_pending_pr_text_skips_a_malformed_manifest_and_uses_the_good_one(
    mocker, make_cfg, tmp_path
):
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    good = _description_manifest(actions=[{"type": "description", "body": "Good."}])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-bad.json": "{not json", "002-good.json": good}),
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    found = pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000)

    assert found is not None and found.manifest == "002-good.json"
    assert "001-bad.json" in warn.call_args.args[0]


def test_pending_pr_text_falls_back_to_the_container_branch_and_first_body_line(
    mocker, make_cfg, tmp_path
):
    """`title: null` / `branch: null` are filled in; `PrText` needs all three."""
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    text = _description_manifest(
        actions=[{"type": "description", "body": "# Add a thing\n\nMore."}]
    )
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))
    incus = mocker.MagicMock()
    incus.config_get.return_value = "feat/foo"

    found = pending_pr_text(make_cfg(tmp_path), incus, "c", uid=1000)

    assert found is not None
    assert found.text.title == "Add a thing"
    assert found.text.branch == "feat/foo"
    assert found.text.body == "# Add a thing\n\nMore."


# --- Gate 1: the repo lock ------------------------------------------------


def test_pending_pr_text_skips_a_manifest_for_another_repo(mocker, make_cfg, tmp_path):
    """The gate the design calls the costliest to skip: a container must not
    hand `jailbee pr` a description written for an unrelated repository."""
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker, "https://github.com/acme/widgets.git")
    text = _description_manifest(repo="evil/other")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))
    warn = mocker.patch("jailbee.pr_outbox.warn")

    assert pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000) is None
    assert "evil/other" in warn.call_args.args[0]


def test_pending_pr_text_refuses_when_the_host_has_no_github_remote(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker, "/srv/mirrors/widgets.git")
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-d.json": _description_manifest()}),
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    assert pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000) is None
    assert "GitHub remote" in warn.call_args.args[0]


def test_pending_pr_text_does_not_ask_git_for_an_empty_outbox(mocker, make_cfg, tmp_path):
    """The empty outbox is the common case; it must not cost a git round-trip."""
    from jailbee.pr_outbox import Outbox, pending_pr_text

    remote = _host_repo(mocker)
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={}))

    assert pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000) is None
    remote.assert_not_called()


# --- Gate 2: which PR the description belongs to --------------------------


def test_pending_pr_text_skips_a_numbered_manifest_on_the_create_path(mocker, make_cfg, tmp_path):
    """`pr: 1234` describes a PR that already exists.

    Publishing its body as a brand-new PR's — a `jailbee new --pr 1234`
    container run through `jailbee pr --stacked` — would put the text somewhere
    it was never meant to go and burn the action index, so `jailbee review
    apply` could never post it where it belongs.
    """
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    text = _description_manifest(pr=1234, head_sha="abc1234")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))

    assert pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000) is None


def test_pending_pr_text_accepts_a_numbered_manifest_for_the_named_pr(mocker, make_cfg, tmp_path):
    """`for_pr` is the update path's gate: that PR's own description is fair game."""
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    text = _description_manifest(pr=1234, head_sha="abc1234")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))

    found = pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000, for_pr=1234)

    assert found is not None and found.manifest == "001-d.json"


def test_pending_pr_text_skips_a_manifest_for_a_different_pr(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    text = _description_manifest(pr=999, head_sha="abc1234")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))

    assert (
        pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000, for_pr=1234) is None
    )


def test_pending_pr_text_accepts_a_null_pr_manifest_on_the_update_path(mocker, make_cfg, tmp_path):
    """The container may have written the description before the PR existed."""
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-d.json": _description_manifest()}),
    )

    found = pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), "c", uid=1000, for_pr=1234)

    assert found is not None


# --- The proposed branch name is untrusted input --------------------------


def test_pending_pr_text_keeps_a_valid_proposed_branch(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    check = mocker.patch("jailbee.git.check_ref_format", return_value=True)
    text = _description_manifest(actions=[{"type": "description", "body": "B", "branch": "feat/x"}])
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))
    incus = mocker.MagicMock()
    incus.config_get.return_value = "feat/foo"

    found = pending_pr_text(make_cfg(tmp_path), incus, "c", uid=1000)

    assert found is not None and found.text.branch == "feat/x"
    check.assert_called_once_with("feat/x")


def test_pending_pr_text_rejects_an_invalid_proposed_branch(mocker, make_cfg, tmp_path):
    """`--as` exits 2 and the AI proposal falls back; the untrusted source is
    checked too, or `-x` / `a b` reaches `git push` and `gh pr create --head`
    and fails *after* this run has already pushed."""
    from jailbee.pr_outbox import Outbox, pending_pr_text

    _host_repo(mocker)
    mocker.patch("jailbee.git.check_ref_format", return_value=False)
    text = _description_manifest(actions=[{"type": "description", "body": "B", "branch": "-x"}])
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))
    warn = mocker.patch("jailbee.pr_outbox.warn")
    incus = mocker.MagicMock()
    incus.config_get.return_value = "feat/foo"

    found = pending_pr_text(make_cfg(tmp_path), incus, "c", uid=1000)

    assert found is not None
    assert found.text.branch == "feat/foo"  # the container's own branch, not '-x'
    assert found.text.body == "B"  # the description itself is still used
    assert "-x" in warn.call_args.args[0]
