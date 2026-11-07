"""Unit tests for jailbee.pr_outbox."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jailbee.pr_flow import PrScope


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


def _stored_outbox(mocker, files, *, container="c", seed=True):
    """Stage `files` (manifests, bodies, progress sidecars) in the host outbox store.

    Returns the mocked Incus (whose instance row carries the store's uuid) and the
    `Outbox` read back from the store.
    """
    from jailbee.pr_outbox import read_outbox
    from tests.pr_store_support import instance, seed_pr_store

    if seed:
        seed_pr_store(files, container=container)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [instance(container)]
    return incus, read_outbox(incus, container, uid=1000)


def _staged_digest(manifest, *, uuid="uuid-feat"):
    """Stage a stand-in proposal named like `manifest` and return its stored digest.

    `apply_manifest` publishes only a proposal still stored with the digest it
    was planned with; tests that build the parsed manifest by hand stage one.
    """
    from tests.pr_store_support import seed_pr_store

    return seed_pr_store({manifest.name: _manifest_text()}, uuid=uuid)[manifest.name]


def _record_applied_before_store(files, name, indices, *, container="c"):
    """Journal `indices` of `name` as applied while its proposal is not in the store yet.

    A journal that is complete with the proposal still stored only arises when
    cleanup did not finish; `settle` removes the proposal in the same
    transaction, so the proposal is staged after the journal (`_stored_outbox`).
    """
    from jailbee.outbox import publication_db
    from jailbee.pr_outbox import proposal_files_digest
    from tests.pr_store_support import UUID

    digest = proposal_files_digest(files, name)
    count = len(json.loads(files[name])["actions"])
    pubs = publication_db.open_publications()
    with pubs.claim(UUID, "pr", name) as claim:
        pubs.begin(claim, digest=digest, action_count=count, container_full=container, now=1.0)
        for index in indices:
            pubs.mark_prepared(claim, digest, index, repo="r", now=1.0)
            pubs.settle(claim, digest, index, outcome="applied", url=f"https://x/{index}", now=1.0)


def test_outbox_pr_references_groups_by_number(mocker):
    from jailbee.pr_outbox import Outbox, outbox_pr_references

    incus = mocker.Mock()
    read = mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(
            files={
                "b.json": _manifest_text(pr=5),
                "c.json": _manifest_text(pr=9),
                "a.json": _manifest_text(pr=5, actions=[{"type": "comment", "body_file": "a.md"}]),
                "a.md": "A comment",
                "a.progress.json": "{}",
            }
        ),
    )
    assert outbox_pr_references(incus, "jb-c1", uid=1000, repo="acme/widgets", exclude=[]) == {
        5: ("a.json", "b.json"),
        9: ("c.json",),
    }
    read.assert_called_once_with(incus, "jb-c1", uid=1000)


def test_outbox_pr_references_skips_other_repo_and_null_pr(mocker):
    from jailbee.pr_outbox import Outbox, outbox_pr_references

    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(
            files={
                "a.json": _manifest_text(pr=5, repo="ACME/Widgets"),
                "b.json": _manifest_text(pr=9, repo="other/widgets"),
                "c.json": _manifest_text(
                    pr=None, actions=[{"type": "description", "body": "New PR"}]
                ),
            }
        ),
    )
    assert outbox_pr_references(
        mocker.Mock(), "jb-c1", uid=None, repo="acme/widgets", exclude=[]
    ) == {5: ("a.json",)}


def test_outbox_pr_references_skips_unparsable_manifest(mocker):
    from jailbee.pr_outbox import Outbox, outbox_pr_references

    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(
            files={
                "a.json": "not JSON",
                "b.json": _manifest_text(
                    pr=5, actions=[{"type": "comment", "body_file": "missing.md"}]
                ),
                "c.json": _manifest_text(pr=9),
            }
        ),
    )
    assert outbox_pr_references(
        mocker.Mock(), "jb-c1", uid=None, repo="acme/widgets", exclude=[]
    ) == {9: ("c.json",)}


def test_outbox_pr_references_excludes_bound_numbers(mocker):
    from jailbee.pr_outbox import Outbox, outbox_pr_references

    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(
            files={
                "a.json": _manifest_text(pr=5),
                "b.json": _manifest_text(pr=9),
            }
        ),
    )
    assert outbox_pr_references(
        mocker.Mock(), "jb-c1", uid=None, repo="acme/widgets", exclude={5}
    ) == {9: ("b.json",)}


def test_outbox_pr_references_unreadable_outbox_is_empty(mocker):
    from jailbee.pr_outbox import OutboxReadError, outbox_pr_references

    mocker.patch("jailbee.pr_outbox.read_outbox", side_effect=OutboxReadError("unreadable"))
    assert (
        outbox_pr_references(mocker.Mock(), "jb-c1", uid=None, repo="acme/widgets", exclude=[])
        == {}
    )


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


def test_parse_manifest_source_fields_and_classification(mocker):
    from jailbee.pr_outbox import DescriptionAction, is_source_manifest, parse_manifest

    mocker.patch("jailbee.pr_outbox.git.check_ref_format", return_value=True)
    source = "refs/heads/imported/topic"
    source_sha = "a" * 40
    description = {"type": "description", "body": "Imported PR"}

    unnumbered = parse_manifest(
        "001-source.json",
        _manifest_text(
            pr=None,
            head_sha=None,
            source=source,
            source_sha=source_sha,
            base="main",
            actions=[description],
        ),
        {},
    )
    numbered = parse_manifest(
        "002-source.json",
        _manifest_text(pr=7, source=source, source_sha=source_sha, actions=[]),
        {},
    )

    assert (unnumbered.source, unnumbered.source_sha, unnumbered.base) == (
        source,
        source_sha,
        "main",
    )
    assert unnumbered.actions == (DescriptionAction(body="Imported PR"),)
    assert is_source_manifest(unnumbered)
    assert (numbered.source, numbered.source_sha, numbered.base) == (source, source_sha, None)
    assert numbered.actions == ()
    assert is_source_manifest(numbered)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"source": "refs/heads/topic"}, "source_sha is required"),
        ({"source": None}, "source must start with refs/"),
        ({"source_sha": None}, "source_sha requires source"),
        ({"base": None}, "base requires source"),
        ({"source_sha": "a" * 40}, "source_sha requires source"),
        (
            {"source": "refs/heads/topic", "source_sha": "abc"},
            "source_sha must be 40 lowercase hex",
        ),
        ({"source": "feature", "source_sha": "a" * 40}, "source must start with refs/"),
        ({"source": "refs/heads/a..b", "source_sha": "a" * 40}, "source is not a valid ref"),
        ({"base": "main"}, "base requires source"),
        (
            {"source": "refs/heads/topic", "source_sha": "a" * 40, "base": "a..b"},
            "base is not a valid branch name",
        ),
        (
            {
                "pr": 7,
                "source": "refs/heads/topic",
                "source_sha": "a" * 40,
                "actions": [{"type": "comment", "body": "not allowed"}],
            },
            "source manifests may only carry a description",
        ),
        ({"pr": 7, "actions": []}, "has no actions"),
    ],
)
def test_parse_manifest_source_rejects_invalid_fields(mocker, overrides, error):
    from jailbee.pr_outbox import ManifestError, parse_manifest

    mocker.patch(
        "jailbee.pr_outbox.git.check_ref_format",
        side_effect=lambda ref: ".." not in ref,
    )
    with pytest.raises(ManifestError, match=error):
        parse_manifest("003-source.json", _manifest_text(**overrides), {})


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


def test_parse_accepts_more_than_fifty_actions():
    actions = [{"type": "comment", "body": "x"} for _ in range(51)]
    from jailbee.pr_outbox import parse_manifest

    text = _manifest_text(actions=actions)
    assert len(parse_manifest("001-x.json", text, {}).actions) == 51


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


def _incus_with(mocker, *rows):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = list(rows)
    return incus


def test_read_outbox_returns_every_stored_file(mocker):
    from jailbee.pr_outbox import read_outbox
    from tests.pr_store_support import instance, seed_pr_store

    manifest = json.dumps(
        {
            "version": 1,
            "repo": "acme/widgets",
            "pr": 1,
            "head_sha": "abc1234",
            "actions": [{"type": "comment", "body_file": "001-x.md"}],
        }
    )
    seed_pr_store({"001-x.json": manifest, "001-x.md": "prose"})
    incus = _incus_with(mocker, instance("c"), instance("acme-feat-foo", uuid="other"))

    outbox = read_outbox(incus, "c", uid=1000)

    assert outbox.files == {"001-x.json": manifest, "001-x.md": "prose"}
    assert outbox.manifest_names == ["001-x.json"]
    assert outbox.uuid == "uuid-test"
    incus.exec.assert_not_called()


def test_read_outbox_is_empty_when_nothing_is_stored(mocker):
    from jailbee.pr_outbox import read_outbox
    from tests.pr_store_support import instance

    incus = _incus_with(mocker, instance("c"))

    assert read_outbox(incus, "c", uid=1000).files == {}


def test_read_outbox_reads_only_the_instances_own_proposals(mocker):
    from jailbee.pr_outbox import read_outbox
    from tests.pr_store_support import instance, seed_pr_store

    seed_pr_store({"001-a.json": "{}"}, uuid="uuid-a")
    seed_pr_store({"001-b.json": "{}"}, uuid="uuid-b")
    incus = _incus_with(mocker, instance("a", uuid="uuid-a"), instance("b", uuid="uuid-b"))

    assert read_outbox(incus, "b", uid=1000).manifest_names == ["001-b.json"]


def test_read_outbox_requires_the_instance_uuid(mocker):
    from jailbee.pr_outbox import OutboxReadError, read_outbox

    incus = _incus_with(mocker, {"name": "c", "created_at": "2026-01-01T00:00:00Z"})

    with pytest.raises(OutboxReadError, match="uuid is unavailable"):
        read_outbox(incus, "c", uid=1000)


def test_read_outbox_wraps_incus_failure(mocker):
    from jailbee.incus import IncusError
    from jailbee.pr_outbox import OutboxReadError, read_outbox

    incus = mocker.MagicMock()
    incus.list_containers.side_effect = IncusError("exit 1: Instance is not running")

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
        # Credentialed remotes (common on CI-provisioned clones) must still
        # resolve — see the "github_slug silently narrowed" review finding.
        ("https://user@github.com/acme/widgets.git", "acme/widgets"),
        (
            "https://x-access-token:ghp_abc123@github.com/acme/widgets.git",
            "acme/widgets",
        ),
        ("git://github.com/acme/widgets.git", "acme/widgets"),
        # Hostile forms must stay refused even with the credentialed prefix
        # allowed.
        ("https://evil.com/?x=github.com/acme/widgets", None),
        ("/home/x/github.com/acme/widgets", None),
        ("https://www.github.com/acme/widgets", None),
        ("https://github.com@evil.com/acme/widgets", None),
        # A `?` or `#` in the userinfo position ends the authority (or
        # starts a fragment) before the regex reaches `github.com`, so the
        # real host is `evil.com`, not `github.com`.
        ("https://evil.com?x=@github.com/acme/widgets", None),
        ("https://evil.com#@github.com/acme/widgets", None),
        # A literal backslash in userinfo is treated as a path separator by
        # WHATWG URL parsers, again leaving `evil.com` as the real host.
        ("https://evil.com\\@github.com/acme/widgets", None),
        # Empty userinfo (`+`, not `*`, required — matching the ssh:// arm).
        ("https://@github.com/acme/widgets", None),
        # Unicode homographs of "github.com" (dotless i / dotted capital I,
        # both fold to ASCII "i" under IGNORECASE without re.ASCII).
        ("https://g\u0131thub.com/acme/widgets", None),
        ("https://g\u0130thub.com/acme/widgets", None),
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
    mocker.patch("jailbee.submodules.host_submodule_paths", return_value=[])
    mocker.patch("jailbee.git.get_remote_url", return_value="git@github.com:acme/widgets.git")
    mocker.patch("jailbee.pr.resolve_pr", return_value=pr or _pr_info())
    incus = mocker.MagicMock()
    label_map = labels if labels is not None else {"user.jailbee.pr": "1234"}
    incus.config_get.side_effect = lambda name, key: label_map.get(key)
    incus.config_get_checked.side_effect = incus.config_get.side_effect
    return incus


def test_resolve_target_accepts_the_containers_own_pr_scope(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    mocker.patch("subprocess.run", side_effect=AssertionError("unexpected real subprocess"))
    incus = _target_setup(mocker, tmp_path)
    cfg = make_cfg(tmp_path)
    manifest = parse_manifest("001-x.json", _manifest_text(), {})

    target = resolve_target(cfg, incus, "acme-feat-foo", manifest, force=False)

    assert target.pr is not None and target.pr.number == 1234
    assert target.stale is False
    assert target.scope == PrScope.for_repo(cfg)


@pytest.mark.parametrize("subpath", [None, "deps/library"])
def test_outbox_reads_pin_validated_repo_despite_another_gh_default(
    mocker, make_cfg, tmp_path, subpath
):
    from subprocess import CompletedProcess

    from jailbee.pr_outbox import _current_pr_body, parse_manifest, resolve_target

    cfg = make_cfg(tmp_path)
    scope = PrScope(tmp_path / subpath if subpath else tmp_path, "origin", "", subpath)
    mocker.patch("jailbee.pr_flow.candidate_scopes", return_value=[scope])
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/library")
    labels = (
        {"user.jailbee.sub_pr": json.dumps({subpath: {"branch": "library-head"}})}
        if subpath
        else {"user.jailbee.branch": "library-head"}
    )
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda container, key: labels.get(key)
    commands = []

    def run(cmd, **kwargs):
        if cmd[0] == "git":
            return CompletedProcess(cmd, 0, "https://github.com/acme/library\n", "")
        commands.append(cmd)
        assert kwargs["cwd"] == scope.repo_root
        # Model a checkout whose gh default/other preferred remote is unrelated.
        pinned = "--repo" in cmd and cmd[cmd.index("--repo") + 1] == "acme/library"
        data = (
            {"body": "library description" if pinned else "unrelated description"}
            if "body" in cmd
            else {
                "number": 42 if pinned else 999,
                "headRefName": "library-head",
                "headRefOid": "abc1234",
                "state": "OPEN",
                "baseRefName": "main",
            }
        )
        return CompletedProcess(cmd, 0, json.dumps(data), "")

    mocker.patch("subprocess.run", side_effect=run)
    manifest = parse_manifest(
        "sub.json",
        _manifest_text(
            repo="acme/library", pr=42, actions=[{"type": "description", "body": "new"}]
        ),
        {},
    )

    target = resolve_target(cfg, incus, "c", manifest, force=False)

    assert target.pr.number == 42
    assert _current_pr_body(cfg, target, None) == "library description"
    assert len(commands) == 3  # ownership, PR metadata, current description
    assert all(cmd[cmd.index("--repo") + 1] == "acme/library" for cmd in commands)


@pytest.mark.parametrize(
    ("action", "command_prefix"),
    [
        (
            {"type": "review", "body": "review", "comments": []},
            ["gh", "api", "--method", "POST", "repos/acme/library/pulls/42/reviews"],
        ),
        (
            {"type": "reply", "comment_id": 7, "body": "reply"},
            ["gh", "api", "--method", "POST", "repos/acme/library/pulls/42/comments/7/replies"],
        ),
        (
            {"type": "comment", "body": "comment"},
            ["gh", "api", "--method", "POST", "repos/acme/library/issues/42/comments"],
        ),
        (
            {"type": "description", "title": "title", "body": "description"},
            ["gh", "pr", "edit", "42", "--repo", "acme/library"],
        ),
    ],
)
def test_outbox_mutations_pin_validated_repo_despite_another_gh_default(
    mocker, make_cfg, tmp_path, action, command_prefix
):
    from subprocess import CompletedProcess

    from jailbee.pr_outbox import Progress, Target, apply_manifest, parse_manifest

    scope = PrScope(tmp_path / "deps/library", "origin", "", "deps/library")
    manifest = parse_manifest(
        "sub.json", _manifest_text(repo="acme/library", pr=42, actions=[action]), {}
    )
    run = mocker.patch(
        "subprocess.run", return_value=CompletedProcess([], 0, '{"html_url": "https://x"}', "")
    )
    # GH_REPO can otherwise override cwd-based repository detection.
    mocker.patch.dict("os.environ", {"GH_REPO": "unrelated/default"})

    outcome = apply_manifest(
        make_cfg(tmp_path),
        mocker.MagicMock(),
        "c",
        Target(manifest=manifest, pr=_pr_info(42), stale=False, scope=scope),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
        publisher="user",
        uuid="uuid-feat",
        digest=_staged_digest(manifest),
    )

    assert outcome.failure is None
    assert outcome.applied == (0,)
    run.assert_called_once()
    assert run.call_args.args[0][: len(command_prefix)] == command_prefix
    assert run.call_args.kwargs["cwd"] == scope.repo_root


def _submodule_gate_setup(mocker, make_cfg, tmp_path, *, records=None, labels=None):
    """Keep scope construction and recorded ownership real; fake git and Incus I/O."""
    cfg = make_cfg(tmp_path)
    paths = ["deps/one", "deps/two"]
    mocker.patch("jailbee.submodules.host_submodule_paths", return_value=paths)
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="upstream")
    urls = {
        (tmp_path, cfg.upstream_remote): "git@github.com:acme/widgets.git",
        (tmp_path / "deps/one", "upstream"): "https://github.com/acme/library.git",
        (tmp_path / "deps/two", "upstream"): "https://github.com/acme/library.git",
    }
    mocker.patch("jailbee.git.get_remote_url", side_effect=lambda root, remote: urls[root, remote])
    label_map = {"user.jailbee.sub_pr": json.dumps(records or {}), **(labels or {})}
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda container, key: label_map.get(key)
    mocker.patch("jailbee.pr.resolve_pr", side_effect=lambda root, n, **kw: _pr_info(number=n))
    return cfg, incus, PrScope.for_submodule(cfg, "deps/one"), urls


@pytest.mark.parametrize("check_other_scope", [False, True])
def test_recorded_transported_submodule_description_then_comments_are_publishable(
    mocker, make_cfg, tmp_path, check_other_scope
):
    from jailbee.pr_outbox import (
        Outbox,
        Progress,
        apply_manifest,
        parse_manifest,
        pending_pr_text,
        resolve_target,
    )

    mocker.patch("subprocess.run", side_effect=AssertionError("unexpected real subprocess"))
    cfg = make_cfg(tmp_path)
    # Transport initialized this host checkout; host .gitmodules has no entry.
    mocker.patch("jailbee.git.run_capture", return_value=(False, ""))
    mocker.patch(
        "jailbee.submodules.host_subrepo_exists",
        side_effect=lambda root, path: root == tmp_path and path == "new/library",
    )
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    mocker.patch(
        "jailbee.git.get_remote_url",
        side_effect=lambda root, remote: (
            "https://github.com/acme/library"
            if root == tmp_path / "new/library"
            else "https://github.com/acme/widgets"
        ),
    )
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda container, key: (
        json.dumps({"new/library": {"pr": 42, "branch": "library-head"}})
        if key == "user.jailbee.sub_pr"
        else None
    )
    text = _manifest_text(
        repo="acme/library",
        pr=42,
        actions=[
            {"type": "description", "title": "Library title", "body": "Library body"},
            {"type": "comment", "body": "Library comment"},
        ],
    )
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"new.json": text}))
    warn = mocker.patch("jailbee.pr_outbox.warn")
    resolve = mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(42))
    comment = mocker.patch("jailbee.pr.add_issue_comment", return_value="https://x/comment")

    if check_other_scope:
        assert (
            pending_pr_text(
                cfg, incus, "c", scope=PrScope.for_repo(cfg), source_branch="super-head", uid=1000
            )
            is None
        )
        warn.assert_not_called()
    selected = pending_pr_text(
        cfg,
        incus,
        "c",
        scope=PrScope.for_submodule(cfg, "new/library"),
        source_branch="library-head",
        for_pr=42,
        uid=1000,
    )
    assert selected is not None
    assert (selected.text.title, selected.text.body) == ("Library title", "Library body")

    target = resolve_target(cfg, incus, "c", parse_manifest("new.json", text, {}), force=False)
    assert target.scope.subpath == "new/library"
    resolve.assert_called_once_with(
        tmp_path / "new/library", 42, remote="origin", repo="acme/library"
    )
    outcome = apply_manifest(
        cfg,
        incus,
        "c",
        target,
        Progress(applied=frozenset({0}), urls={}),
        uid=1000,
        publisher="user",
        uuid="uuid-feat",
        digest=_staged_digest(target.manifest),
    )
    assert outcome.applied == (1,)
    assert outcome.urls == ("https://x/comment",)
    assert outcome.failure is None
    comment.assert_called_once_with(
        tmp_path / "new/library", 42, "Library comment", repo="acme/library"
    )


@pytest.mark.parametrize("recorded_path", ["deps/one", "deps/two"])
def test_submodule_scope_accepts_owned_number_from_either_duplicate_path(
    mocker, make_cfg, tmp_path, recorded_path
):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    cfg, incus, sub_scope, _ = _submodule_gate_setup(
        mocker, make_cfg, tmp_path, records={recorded_path: {"pr": 42}}
    )
    manifest = parse_manifest("sub.json", _manifest_text(repo="acme/library", pr=42), {})

    target = resolve_target(cfg, incus, "c", manifest, force=False)

    assert target.scope == sub_scope
    assert target.pr is not None and target.pr.number == 42


def test_submodule_scope_uses_each_duplicate_heads_ownership_fallback(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    cfg, incus, sub_scope, _ = _submodule_gate_setup(
        mocker,
        make_cfg,
        tmp_path,
        records={"deps/one": {"pr": 9}, "deps/two": {"branch": "library-head"}},
    )
    find = mocker.patch("jailbee.pr.find_pr_for_branch", return_value=_pr_info(number=42))
    manifest = parse_manifest("sub.json", _manifest_text(repo="acme/library", pr=42), {})

    target = resolve_target(cfg, incus, "c", manifest, force=False)

    assert target.scope == sub_scope
    assert target.pr is not None and target.pr.number == 42
    find.assert_called_once_with(tmp_path / "deps/two", "library-head", repo="acme/library")


def test_scope_mismatch_names_requested_and_all_known_github_repos(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import GateError, parse_manifest, resolve_target

    cfg, incus, _, urls = _submodule_gate_setup(mocker, make_cfg, tmp_path)
    urls[tmp_path / "deps/two", "upstream"] = "https://gitlab.com/acme/private.git"
    manifest = parse_manifest("sub.json", _manifest_text(repo="unknown/repo"), {})

    with pytest.raises(GateError) as exc:
        resolve_target(cfg, incus, "c", manifest, force=False)

    assert all(slug in str(exc.value) for slug in ("unknown/repo", "acme/widgets", "acme/library"))
    assert "acme/private" not in str(exc.value)
    incus.config_get.assert_called_once_with("c", "user.jailbee.sub_pr")


def test_submodule_scope_cannot_borrow_superproject_ownership(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import GateError, parse_manifest, resolve_target

    cfg, incus, _, _ = _submodule_gate_setup(
        mocker, make_cfg, tmp_path, labels={"user.jailbee.pr": "42"}
    )
    manifest = parse_manifest("sub.json", _manifest_text(repo="acme/library", pr=42), {})

    with pytest.raises(GateError, match="does not own"):
        resolve_target(cfg, incus, "c", manifest, force=False)


@pytest.mark.parametrize(
    ("records", "expected_number"),
    [
        ({}, None),
        ({"deps/one": {"branch": "unbound-head"}}, None),
        ({"deps/two": {"pr": 42}}, 42),
        ({"deps/one": {"pr": 42}, "deps/two": {"pr": 42}}, 42),
        ({"deps/one": {"pr": 9}, "deps/two": {"pr": 42}}, None),
    ],
)
def test_submodule_scope_null_pr_only_binds_one_unique_recorded_number(
    mocker, make_cfg, tmp_path, records, expected_number
):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    cfg, incus, sub_scope, _ = _submodule_gate_setup(mocker, make_cfg, tmp_path, records=records)
    find = mocker.patch("jailbee.pr.find_pr_for_branch")
    manifest = parse_manifest(
        "sub.json",
        _manifest_text(
            repo="acme/library", pr=None, actions=[{"type": "description", "body": "B"}]
        ),
        {},
    )

    target = resolve_target(cfg, incus, "c", manifest, force=False)

    assert target.scope == sub_scope
    assert (target.pr.number if target.pr else None) == expected_number
    find.assert_not_called()


def test_submodule_scope_staleness_uses_its_root_and_remote(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import StaleError, parse_manifest, resolve_target

    cfg, incus, sub_scope, _ = _submodule_gate_setup(
        mocker, make_cfg, tmp_path, records={"deps/one": {"pr": 42}}
    )
    resolve = mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(42, head_sha="new-head"))
    manifest = parse_manifest(
        "sub.json",
        _manifest_text(
            repo="acme/library", pr=42, actions=[{"type": "review", "body": "B", "comments": []}]
        ),
        {},
    )

    with pytest.raises(StaleError, match="head moved"):
        resolve_target(cfg, incus, "c", manifest, force=False)

    resolve.assert_called_once_with(sub_scope.repo_root, 42, remote="upstream", repo="acme/library")
    target = resolve_target(cfg, incus, "c", manifest, force=True)
    assert target.scope == sub_scope
    assert target.stale is True


def test_resolve_target_refuses_a_foreign_repo(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import GateError, parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path)
    manifest = parse_manifest("001-x.json", _manifest_text(repo="evil/other"), {})

    with pytest.raises(GateError, match="evil/other"):
        resolve_target(make_cfg(tmp_path), incus, "c", manifest, force=False)


def test_resolve_target_refuses_a_pr_the_container_does_not_own(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import ForeignPrError, parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path, labels={"user.jailbee.pr": "1234"})
    manifest = parse_manifest("001-x.json", _manifest_text(pr=999), {})

    with pytest.raises(ForeignPrError, match="#999"):
        resolve_target(make_cfg(tmp_path), incus, "c", manifest, force=False)


def test_resolve_target_admits_a_foreign_pr_when_allowed_and_marks_it(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    incus = _target_setup(
        mocker, tmp_path, labels={"user.jailbee.pr": "1234"}, pr=_pr_info(number=999)
    )
    manifest = parse_manifest("001-x.json", _manifest_text(pr=999), {})

    target = resolve_target(
        make_cfg(tmp_path), incus, "c", manifest, force=False, allow_foreign=True
    )

    assert target.pr is not None and target.pr.number == 999
    assert target.foreign is True


def test_resolve_target_does_not_mark_the_containers_own_pr_foreign(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path)
    manifest = parse_manifest("001-x.json", _manifest_text(), {})

    target = resolve_target(
        make_cfg(tmp_path), incus, "c", manifest, force=False, allow_foreign=True
    )

    assert target.foreign is False


def test_allow_foreign_keeps_the_repo_lock(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import GateError, parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path)
    manifest = parse_manifest("001-x.json", _manifest_text(repo="evil/other", pr=999), {})

    with pytest.raises(GateError, match="evil/other"):
        resolve_target(make_cfg(tmp_path), incus, "c", manifest, force=False, allow_foreign=True)


def test_allow_foreign_repo_admits_another_repository_and_marks_it(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path, pr=_pr_info(number=7))
    cfg = make_cfg(tmp_path)
    manifest = parse_manifest("001-x.json", _manifest_text(repo="other/plugins", pr=7), {})

    target = resolve_target(cfg, incus, "c", manifest, force=False, allow_foreign_repo=True)

    assert target.pr is not None and target.pr.number == 7
    assert target.foreign is True and target.foreign_repo is True
    assert target.scope == PrScope.for_repo(cfg)
    from jailbee import pr

    pr.resolve_pr.assert_called_once_with(cfg.repo_root, 7, remote="origin", repo="other/plugins")


def test_repo_lock_matches_the_slug_case_insensitively(mocker, make_cfg, tmp_path):
    """GitHub slugs are case-insensitive: a differently cased own repository
    is the container's own, not a foreign one to warn about."""
    from jailbee.pr_outbox import parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path)  # remote acme/widgets, owns #1234
    manifest = parse_manifest("001-x.json", _manifest_text(repo="ACME/Widgets"), {})

    target = resolve_target(
        make_cfg(tmp_path), incus, "c", manifest, force=False, allow_foreign_repo=True
    )

    assert target.foreign_repo is False and target.foreign is False


@pytest.mark.parametrize("repo", ["--repo=x/y", "a/b/c", "noslash"])
def test_allow_foreign_repo_still_refuses_a_malformed_slug(mocker, make_cfg, tmp_path, repo):
    from jailbee.pr_outbox import ForeignRepoError, parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path)
    manifest = parse_manifest("001-x.json", _manifest_text(repo=repo, pr=7), {})

    with pytest.raises(ForeignRepoError):
        resolve_target(
            make_cfg(tmp_path), incus, "c", manifest, force=False, allow_foreign_repo=True
        )


def test_allow_foreign_repo_never_binds_a_null_pr_manifest(mocker, make_cfg, tmp_path):
    """`pr: null` means "this container's PR", which cannot be in a repository
    the container has nothing to do with."""
    from jailbee.pr_outbox import ForeignRepoError, parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path)
    manifest = parse_manifest(
        "002-d.json",
        _manifest_text(
            repo="other/plugins",
            pr=None,
            head_sha=None,
            actions=[{"type": "description", "body": "B"}],
        ),
        {},
    )

    with pytest.raises(ForeignRepoError):
        resolve_target(
            make_cfg(tmp_path), incus, "c", manifest, force=False, allow_foreign_repo=True
        )


def test_allow_foreign_keeps_the_stale_review_refusal(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import StaleError, parse_manifest, resolve_target

    incus = _target_setup(mocker, tmp_path, pr=_pr_info(number=999, head_sha="def5678"))
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(
            pr=999,
            actions=[{"type": "review", "body": "s", "comments": []}],
        ),
        {},
    )

    with pytest.raises(StaleError):
        resolve_target(make_cfg(tmp_path), incus, "c", manifest, force=False, allow_foreign=True)


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


def _null_pr_manifest():
    from jailbee.pr_outbox import parse_manifest

    return parse_manifest(
        "002-d.json",
        _manifest_text(pr=None, head_sha=None, actions=[{"type": "description", "body": "B"}]),
        {},
    )


def test_null_pr_manifest_resolves_without_a_pr_when_none_is_bound(mocker, make_cfg, tmp_path):
    """No PR label: the description really is about one that does not exist
    yet, and the caller defers it to `jailbee pr`."""
    from jailbee.pr_outbox import resolve_target

    incus = _target_setup(mocker, tmp_path, labels={"user.jailbee.branch": "feat/foo"})
    find = mocker.patch("jailbee.pr.find_pr_for_branch")

    assert (
        resolve_target(make_cfg(tmp_path), incus, "c", _null_pr_manifest(), force=False).pr is None
    )
    # The branch lookup is not a binding: adopting a PR is the user's decision.
    find.assert_not_called()


def test_null_pr_manifest_binds_to_the_containers_own_pr(mocker, make_cfg, tmp_path):
    """The container has a PR bound — jailbee opened it, or `jailbee pr --pr N`
    adopted it — so the description its agent wrote is about that PR. Without
    this, an adopted container's `pr: null` description is publishable by
    neither command."""
    from jailbee.pr_outbox import resolve_target

    incus = _target_setup(mocker, tmp_path)  # user.jailbee.pr = 1234

    target = resolve_target(make_cfg(tmp_path), incus, "c", _null_pr_manifest(), force=False)

    assert target.pr is not None and target.pr.number == 1234
    assert target.stale is False


def test_null_pr_manifest_stays_unresolved_when_two_prs_are_bound(mocker, make_cfg, tmp_path):
    """A stacked container owns two PRs and the manifest names neither; guessing
    which one the description is for is not this gate's call."""
    from jailbee.pr_outbox import resolve_target

    incus = _target_setup(
        mocker,
        tmp_path,
        labels={"user.jailbee.pr": "1234", "user.jailbee.stacked_pr": "1300"},
    )

    assert (
        resolve_target(make_cfg(tmp_path), incus, "c", _null_pr_manifest(), force=False).pr is None
    )


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
        Target(
            manifest=manifest,
            pr=_pr_info(),
            stale=False,
            scope=PrScope(Path("/repo"), "origin", "", None),
        ),
        current_body="Old body.\n",
    )
    joined = "\n".join(lines)

    assert "src/a.py:88" in joined
    assert "src/a.py:120-134" in joined
    assert "This rounds half-down" in joined
    assert "shifts every total" not in joined  # truncated to one line
    assert "reply to general comment #4455" in joined
    assert "-Old body." in joined and "+New body." in joined  # unified diff


def _foreign_target(*, foreign: bool, author: str | None = "octocat"):
    from jailbee.pr import PrInfo
    from jailbee.pr_outbox import Target, parse_manifest

    return Target(
        manifest=parse_manifest("001-x.json", _manifest_text(pr=999), {}),
        pr=PrInfo(
            number=999,
            head_ref="feat/x",
            head_sha="abc1234",
            state="OPEN",
            base_ref="main",
            author_login=author,
        ),
        stale=False,
        scope=PrScope(Path("/repo"), "origin", "", None),
        foreign=foreign,
    )


def test_foreign_warning_names_the_pr_the_container_and_the_author():
    from jailbee.pr_outbox import foreign_warning

    warning = foreign_warning(_foreign_target(foreign=True), "feat-a")

    assert warning is not None
    assert "#999" in warning and "feat-a" in warning and "@octocat" in warning


def test_foreign_warning_without_a_known_author_still_warns():
    from jailbee.pr_outbox import foreign_warning

    warning = foreign_warning(_foreign_target(foreign=True, author=None), "feat-a")

    assert warning is not None and "#999" in warning and "@" not in warning


def test_foreign_warning_names_a_foreign_repository():
    from dataclasses import replace

    from jailbee.pr_outbox import foreign_warning

    target = _foreign_target(foreign=True)
    target = replace(
        target,
        manifest=replace(target.manifest, repo="other/plugins"),
        foreign_repo=True,
    )

    warning = foreign_warning(target, "feat-a")

    assert warning is not None
    assert "#999" in warning and "other/plugins" in warning and "feat-a" in warning


def test_foreign_warning_is_none_for_the_containers_own_pr():
    from jailbee.pr_outbox import foreign_warning

    assert foreign_warning(_foreign_target(foreign=False), "feat-a") is None


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


def _draft_manifest():
    from jailbee.pr_outbox import parse_manifest

    text = _manifest_text(
        pr=7,
        actions=[
            {"type": "review", "body": "review", "comments": []},
            {"type": "reply", "comment_id": 9, "body": "reply"},
            {"type": "comment", "body": "held back"},
            {"type": "description", "body": "description"},
        ],
    )
    return parse_manifest("draft.json", text, {}), text


def test_draft_indices_drop_general_comments():
    from jailbee.pr_outbox import draft_indices

    manifest, _ = _draft_manifest()
    assert draft_indices(manifest, iter(range(4))) == frozenset({0, 1, 3})
    assert draft_indices(manifest, [1, 2]) == frozenset({1})


def test_draftable_needs_review_or_reply():
    from jailbee.pr_outbox import draftable

    manifest, _ = _draft_manifest()
    assert draftable(manifest, [0])
    assert draftable(manifest, [1])
    assert not draftable(manifest, [2, 3])
    assert not draftable(manifest, [])


def test_draft_preview_counts():
    from jailbee.pr_outbox import DraftPreview, draft_preview

    manifest, _ = _draft_manifest()
    assert draft_preview(manifest, 7, iter(range(4))) == DraftPreview(7, 2, 1, True)
    assert draft_preview(manifest, 8, [1, 2]) == DraftPreview(8, 1, 1, False)


def test_apply_manifest_draft_routes_review_and_reply_to_session(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Progress, Target, apply_manifest

    calls = _apply_mocks(mocker)
    factory = mocker.patch("jailbee.pr_draft.DraftSession")
    session = factory.return_value
    session.review.return_value = session.reply.return_value = "https://x/draft"
    manifest, _ = _draft_manifest()
    target = Target(manifest, _pr_info(number=7), False, PrScope(tmp_path, "origin", "", None))
    outcome = apply_manifest(
        make_cfg(tmp_path),
        mocker.MagicMock(),
        "c",
        target,
        Progress(frozenset(), {}),
        uid=1000,
        indices=frozenset({0, 1, 3}),
        draft=True,
        publisher="user",
        uuid="uuid-feat",
        digest=_staged_digest(manifest),
    )
    factory.assert_called_once_with(target)
    assert outcome.applied == (0, 1, 3)
    assert outcome.urls == ("https://x/draft", "https://x/draft", "")
    assert outcome.failure is None and outcome.draft
    session.review.assert_called_once_with(manifest.actions[0])
    session.reply.assert_called_once_with(manifest.actions[1])
    calls["review"].assert_not_called()
    calls["reply"].assert_not_called()
    calls["comment"].assert_not_called()
    calls["edit"].assert_called_once_with(
        tmp_path, 7, title=None, body="description", repo="acme/widgets"
    )


def test_draft_holds_back_general_comments_and_keeps_manifest(mocker, make_cfg, tmp_path):
    _exercise_draft_finalization(mocker, make_cfg, tmp_path, publish_remaining=False)


def test_normal_after_draft_posts_only_held_back(mocker, make_cfg, tmp_path):
    _exercise_draft_finalization(mocker, make_cfg, tmp_path, publish_remaining=True)


def _exercise_draft_finalization(mocker, make_cfg, tmp_path, *, publish_remaining):
    from jailbee.pr_outbox import (
        Target,
        _publication_progress,
        apply_manifest,
        draft_indices,
        finalize,
        proposal_files_digest,
        read_outbox,
    )
    from tests.pr_store_support import UUID, journal, sent_records, stored_names

    calls = _apply_mocks(mocker)
    session = mocker.patch("jailbee.pr_draft.DraftSession").return_value
    session.review.return_value = session.reply.return_value = "https://x/draft"
    manifest, text = _draft_manifest()
    incus, outbox = _stored_outbox(mocker, {manifest.name: text})
    digest = proposal_files_digest(outbox.files, manifest.name)
    target = Target(manifest, _pr_info(number=7), False, PrScope(tmp_path, "origin", "", None))
    outcome = apply_manifest(
        make_cfg(tmp_path),
        incus,
        "c",
        target,
        _publication_progress(outbox, manifest.name, 4),
        uid=1000,
        indices=draft_indices(manifest, range(4)),
        draft=True,
        publisher="user",
        uuid=UUID,
        digest=digest,
    )
    finalize(incus, "c", outbox, target, outcome, uid=1000)
    assert stored_names() == [manifest.name], "the held-back comment keeps the proposal pending"
    recorded = journal(manifest.name)
    assert recorded is not None
    assert [(a.index, a.state) for a in recorded.actions] == [
        (0, "applied"),
        (1, "applied"),
        (3, "applied"),
    ]
    assert sorted(r.index for r in sent_records()) == [0, 1, 3]
    calls["comment"].assert_not_called()

    if publish_remaining:
        calls["edit"].reset_mock()
        outbox = read_outbox(incus, "c", uid=1000)
        outcome = apply_manifest(
            make_cfg(tmp_path),
            incus,
            "c",
            target,
            _publication_progress(outbox, manifest.name, 4),
            uid=1000,
            publisher="user",
            uuid=UUID,
            digest=digest,
        )
        assert outcome.applied == (2,)
        assert not outcome.draft
        finalize(incus, "c", outbox, target, outcome, uid=1000)
        calls["comment"].assert_called_once_with(tmp_path, 7, "held back", repo="acme/widgets")
        calls["review"].assert_not_called()
        calls["reply"].assert_not_called()
        calls["edit"].assert_not_called()
        assert session.review.call_count == session.reply.call_count == 1
        assert stored_names() == [], "the last action completed the proposal"
        assert sorted(r.index for r in sent_records()) == [0, 1, 2, 3]
    incus.exec.assert_not_called()


def test_apply_manifest_draft_rejects_general_comment(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Progress, Target, apply_manifest

    calls = _apply_mocks(mocker)
    manifest, _ = _draft_manifest()
    target = Target(manifest, _pr_info(number=7), False, PrScope(tmp_path, "origin", "", None))
    with pytest.raises(AssertionError):
        apply_manifest(
            make_cfg(tmp_path),
            mocker.MagicMock(),
            "c",
            target,
            Progress(frozenset(), {}),
            uid=1000,
            indices=frozenset({2}),
            draft=True,
            publisher="user",
            uuid="uuid-feat",
            digest=_staged_digest(manifest),
        )
    calls["comment"].assert_not_called()


@pytest.mark.parametrize("number,zero", [(None, False), (1234, False), (1234, True)])
@pytest.mark.parametrize(
    "comments_only,raise_errors", [(False, False), (True, False), (False, True)]
)
def test_source_gate_precedes_resolution_and_progress(
    mocker, make_cfg, tmp_path, number, zero, comments_only, raise_errors
):
    from jailbee.pr_outbox import Outbox, OutboxError, _gate_manifests

    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    text = _manifest_text(
        pr=number,
        head_sha=None,
        source="refs/heads/topic",
        source_sha="a" * 40,
        actions=[] if zero else [{"type": "description", "body": "new body"}],
    )
    resolve = mocker.patch("jailbee.pr_outbox.resolve_target")
    progress = mocker.patch("jailbee.pr_outbox._publication_progress")
    hint = "Use jailbee pr push topic --source-manifest source.json"

    def gate():
        return _gate_manifests(
            make_cfg(tmp_path),
            mocker.MagicMock(),
            "c",
            Outbox({"source.json": text}),
            "topic",
            force=False,
            comments_only=comments_only,
            raise_errors=raise_errors,
        )

    if raise_errors:
        with pytest.raises(OutboxError, match=hint):
            gate()
    else:
        targets, refusals, notes = gate()
        assert targets == []
        assert notes == ([hint] if comments_only else [])
        assert [refusal.text for refusal in refusals] == ([] if comments_only else [hint])
    resolve.assert_not_called()
    progress.assert_not_called()


@pytest.mark.parametrize("number,zero", [(None, False), (1234, False), (1234, True)])
def test_direct_apply_manifest_refuses_source(mocker, make_cfg, tmp_path, number, zero):
    from jailbee.pr_outbox import OutboxError, Progress, Target, apply_manifest, parse_manifest

    calls = _apply_mocks(mocker)
    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    push = mocker.patch("jailbee.sync.push_ref_to_remote")
    incus = mocker.MagicMock()
    manifest = parse_manifest(
        "source.json",
        _manifest_text(
            pr=number,
            head_sha=None,
            source="refs/heads/topic",
            source_sha="a" * 40,
            actions=[] if zero else [{"type": "description", "body": "new body"}],
        ),
        {},
    )
    target = Target(
        manifest, _pr_info() if number else None, False, PrScope(tmp_path, "origin", "", None)
    )
    with pytest.raises(OutboxError, match=r"jailbee pr push c --source-manifest source\.json"):
        apply_manifest(
            make_cfg(tmp_path),
            incus,
            "c",
            target,
            Progress(frozenset(), {}),
            uid=1000,
            publisher="user",
            uuid="uuid-feat",
            digest="digest-1",
        )
    for effect in (*calls.values(), push, incus.exec):
        effect.assert_not_called()


def test_apply_manifest_dispatches_every_operation_in_target_submodule_scope(
    mocker, make_cfg, tmp_path
):
    from jailbee.pr_outbox import Progress, Target, apply_manifest, parse_manifest

    calls = _apply_mocks(mocker)
    scope = PrScope(tmp_path / "deps/one", "upstream", "submodule: ", "deps/one")
    manifest = parse_manifest(
        "sub.json",
        _manifest_text(
            repo="acme/library",
            actions=[
                {"type": "review", "body": "review body", "comments": []},
                {"type": "reply", "comment_id": 7, "body": "reply body"},
                {"type": "comment", "body": "comment body"},
                {"type": "description", "body": "description body", "title": "title"},
            ],
        ),
        {},
    )

    outcome = apply_manifest(
        make_cfg(tmp_path),
        mocker.MagicMock(),
        "c",
        Target(manifest=manifest, pr=_pr_info(), stale=False, scope=scope),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
        publisher="user",
        uuid="uuid-feat",
        digest=_staged_digest(manifest),
    )

    assert outcome.failure is None
    assert outcome.applied == (0, 1, 2, 3)
    calls["review"].assert_called_once_with(
        scope.repo_root,
        1234,
        commit_id="abc1234",
        body="review body",
        comments=[],
        repo="acme/library",
    )
    calls["reply"].assert_called_once_with(
        scope.repo_root, 1234, 7, "reply body", repo="acme/library"
    )
    calls["comment"].assert_called_once_with(
        scope.repo_root, 1234, "comment body", repo="acme/library"
    )
    calls["edit"].assert_called_once_with(
        scope.repo_root, 1234, title="title", body="description body", repo="acme/library"
    )


def test_current_pr_body_reads_target_submodule_scope(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Target, _current_pr_body

    scope = PrScope(tmp_path / "deps/one", "upstream", "submodule: ", "deps/one")
    body = mocker.patch("jailbee.pr.pr_body", return_value="current library description")
    target = Target(manifest=_null_pr_manifest(), pr=_pr_info(), stale=False, scope=scope)

    assert _current_pr_body(make_cfg(tmp_path), target, None) == "current library description"
    body.assert_called_once_with(scope.repo_root, 1234, repo="acme/widgets")


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
        Target(
            manifest=manifest,
            pr=_pr_info(),
            stale=False,
            scope=PrScope(Path("/repo"), "origin", "", None),
        ),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
        publisher="user",
        uuid="uuid-feat",
        digest=_staged_digest(manifest),
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
        Target(
            manifest=manifest,
            pr=_pr_info(),
            stale=False,
            scope=PrScope(Path("/repo"), "origin", "", None),
        ),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
        publisher="user",
        uuid="uuid-feat",
        digest=_staged_digest(manifest),
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
        Target(
            manifest=manifest,
            pr=_pr_info(),
            stale=False,
            scope=PrScope(Path("/repo"), "origin", "", None),
        ),
        Progress(applied=frozenset({0}), urls={}),
        uid=1000,
        publisher="user",
        uuid="uuid-feat",
        digest=_staged_digest(manifest),
    )

    assert outcome.applied == (1,)
    assert calls["comment"].call_count == 1
    assert calls["comment"].call_args.args[2] == "two"


def test_apply_manifest_calls_before_action_once_per_attempt(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Progress, Target, apply_manifest, parse_manifest

    calls = _apply_mocks(mocker)
    events = []
    calls["comment"].side_effect = lambda *args, **kwargs: events.append("post") or "url"
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(actions=[{"type": "comment", "body": str(i)} for i in range(3)]),
        {},
    )
    target = Target(
        manifest=manifest,
        pr=_pr_info(),
        stale=False,
        scope=PrScope(Path("/repo"), "origin", "", None),
    )
    outcome = apply_manifest(
        make_cfg(tmp_path),
        mocker.MagicMock(),
        "c",
        target,
        Progress(frozenset({0}), {}),
        uid=1000,
        before_action=lambda: events.append("hook"),
        publisher="user",
        uuid="uuid-feat",
        digest=_staged_digest(manifest),
    )
    assert outcome.applied == (1, 2)
    assert events == ["hook", "post", "hook", "post"]


def test_apply_manifest_hook_stops_with_the_failure(mocker, make_cfg, tmp_path):
    from jailbee.pr import PrReviewError
    from jailbee.pr_outbox import Progress, Target, apply_manifest, parse_manifest

    calls = _apply_mocks(mocker)
    calls["comment"].side_effect = ["url", PrReviewError("HTTP 500")]
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(actions=[{"type": "comment", "body": str(i)} for i in range(3)]),
        {},
    )
    target = Target(
        manifest=manifest,
        pr=_pr_info(),
        stale=False,
        scope=PrScope(Path("/repo"), "origin", "", None),
    )
    events = []
    outcome = apply_manifest(
        make_cfg(tmp_path),
        mocker.MagicMock(),
        "c",
        target,
        Progress(frozenset(), {}),
        uid=1000,
        before_action=lambda: events.append("hook"),
        publisher="user",
        uuid="uuid-feat",
        digest=_staged_digest(manifest),
    )
    assert outcome.failure is not None
    assert events == ["hook", "hook"]


def test_planned_action_count_skips_applied_and_unselected():
    from jailbee.pr_outbox import Progress, parse_manifest, planned_action_count

    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(actions=[{"type": "comment", "body": str(i)} for i in range(5)]),
        {},
    )
    assert (
        planned_action_count(manifest, Progress(frozenset({0, 1}), {}), frozenset({1, 2, 3})) == 2
    )


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
        Target(
            manifest=manifest,
            pr=_pr_info(),
            stale=False,
            scope=PrScope(Path("/repo"), "origin", "", None),
        ),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
        publisher="user",
        uuid="uuid-feat",
        digest=_staged_digest(manifest),
    )

    assert outcome.applied == (0,)
    assert outcome.failure is not None and "HTTP 500" in outcome.failure
    assert calls["comment"].call_count == 2  # the third was never attempted


def _stored_apply(mocker, make_cfg, tmp_path, text, *, files=None, **kwargs):
    """`apply_manifest` over a proposal staged in the host store; returns (outcome, incus)."""
    from jailbee.pr_outbox import (
        Progress,
        apply_manifest,
        parse_manifest,
        proposal_files_digest,
    )
    from tests.pr_store_support import UUID, stored_names

    stored = {"001-x.json": text, **(files or {})}
    incus, outbox = _stored_outbox(mocker, stored, seed="001-x.json" not in stored_names())
    manifest = parse_manifest("001-x.json", text, files or {})
    outcome = apply_manifest(
        make_cfg(tmp_path),
        incus,
        "c",
        _target(manifest),
        kwargs.pop("progress", Progress(applied=frozenset(), urls={})),
        uid=1000,
        publisher="user",
        uuid=UUID,
        digest=proposal_files_digest(outbox.files, "001-x.json"),
        **kwargs,
    )
    return outcome, incus


def _staged_then(mocker, make_cfg, tmp_path, change):
    """Read a two-proposal outbox, let `change` edit the store, then apply ``002-y.json``.

    The publication runs with the digest read before `change`, the way a
    multi-manifest publish holds the digests it planned with while it works
    through the earlier proposals.
    """
    from jailbee.pr_outbox import Progress, apply_manifest, parse_manifest, proposal_files_digest
    from tests.pr_store_support import UUID

    calls = _apply_mocks(mocker)
    files = {"001-x.json": _manifest_text(), "002-y.json": _manifest_text()}
    incus, outbox = _stored_outbox(mocker, files)
    planned = proposal_files_digest(outbox.files, "002-y.json")
    change(outbox)
    outcome = apply_manifest(
        make_cfg(tmp_path),
        incus,
        "c",
        _target(parse_manifest("002-y.json", files["002-y.json"], {})),
        Progress(applied=frozenset(), urls={}),
        uid=1000,
        publisher="user",
        uuid=UUID,
        digest=planned,
    )
    return outcome, calls


def test_apply_refuses_a_proposal_dropped_after_it_was_planned(mocker, make_cfg, tmp_path):
    """A proposal the container dropped mid-run must not be published from the plan."""
    from jailbee.outbox import publication_db, store_db
    from tests.pr_store_support import UUID

    def drop(outbox):
        version = dict(outbox.versions)["002-y.json"]
        store_db.host_store(publication_db.default_root()).drop_proposal(
            UUID, "pr", "002-y.json", version
        )

    outcome, calls = _staged_then(mocker, make_cfg, tmp_path, drop)

    assert outcome.applied == ()
    assert outcome.failure is not None and "no longer" in outcome.failure
    calls["comment"].assert_not_called()


def test_apply_refuses_a_proposal_replaced_after_it_was_planned(mocker, make_cfg, tmp_path):
    """A proposal re-staged under the same name with other content is not the one planned."""
    from jailbee.outbox import publication_db
    from tests.pr_store_support import UUID

    def replace_text(outbox):
        store = publication_db.open_publications()._store
        with store.transaction() as con:
            con.execute(
                "UPDATE outbox_proposal SET manifest=? WHERE uuid=? AND kind='pr' AND name=?",
                (
                    _manifest_text(actions=[{"type": "comment", "body": "other"}]),
                    UUID,
                    "002-y.json",
                ),
            )

    outcome, calls = _staged_then(mocker, make_cfg, tmp_path, replace_text)

    assert outcome.applied == ()
    assert outcome.failure is not None and "changed" in outcome.failure
    calls["comment"].assert_not_called()


def test_apply_restricted_to_indices_leaves_the_rest_pending(mocker, make_cfg, tmp_path):
    """`indices` must reach GitHub *and* the journal, or a held-back action is lost.

    `jailbee pr`'s offer publishes a manifest's comments without touching a
    `description` the run did not consume. Recording the description as applied
    would be worse than publishing it: it would disappear from every later
    `jailbee review apply` without ever having been posted.
    """
    from tests.pr_store_support import journal, stored_names

    calls = _apply_mocks(mocker)
    text = _manifest_text(
        actions=[
            {"type": "comment", "body": "one"},
            {"type": "description", "body": "a new body"},
        ]
    )

    outcome, incus = _stored_apply(mocker, make_cfg, tmp_path, text, indices=frozenset({0}))

    assert outcome.applied == (0,)
    calls["comment"].assert_called_once()
    calls["edit"].assert_not_called()  # the description was never published...
    recorded = journal("001-x.json")
    assert recorded is not None
    assert [(a.index, a.state) for a in recorded.actions] == [(0, "applied")]  # ...nor recorded
    assert stored_names() == ["001-x.json"]
    incus.exec.assert_not_called()


def test_apply_journals_each_action_before_and_after_it_is_posted(mocker, make_cfg, tmp_path):
    """A crash between actions must not lose what already landed.

    The journal is written around every action, not once at the end: when the
    second post is made the first is already `applied` and the second is
    `prepared` (read back as `uncertain`, the state a crash would leave).
    """
    from tests.pr_store_support import journal, sent_records, stored_names

    calls = _apply_mocks(mocker)
    seen = []

    def post(*args, **kwargs):
        recorded = journal("001-x.json")
        seen.append([(a.index, a.state) for a in recorded.actions])
        return "https://x/c"

    calls["comment"].side_effect = post
    text = _manifest_text(
        actions=[{"type": "comment", "body": "one"}, {"type": "comment", "body": "two"}]
    )

    outcome, _ = _stored_apply(mocker, make_cfg, tmp_path, text)

    assert outcome.applied == (0, 1)
    # A `prepared` record reads back as `uncertain`: it is what a crash would leave.
    assert seen == [[(0, "uncertain")], [(0, "applied"), (1, "uncertain")]]
    assert stored_names() == [], "the last action completed the proposal"
    assert sorted(r.index for r in sent_records()) == [0, 1]


def test_apply_reports_a_recording_failure_after_a_successful_post_and_stops(
    mocker, make_cfg, tmp_path
):
    """A store failure after a successful GitHub post must not silently repost.

    Action 0 lands on GitHub, but the settle that was supposed to record it
    raises `JournalError`. That must not escape as a bare exception: index 0
    stays in `applied`/`urls` (it really did land), `failure` says plainly that
    it landed and could not be recorded, action 1 is never attempted, and the
    leftover `prepared` record makes a re-run refuse instead of reposting.
    """
    from jailbee.outbox import publication_db
    from jailbee.outbox_io import JournalError
    from tests.pr_store_support import journal, stored_names

    calls = _apply_mocks(mocker)
    text = _manifest_text(
        actions=[{"type": "comment", "body": "one"}, {"type": "comment", "body": "two"}]
    )
    publications = mocker.Mock(wraps=publication_db.open_publications())
    publications.settle.side_effect = JournalError("database is locked")

    outcome, _ = _stored_apply(mocker, make_cfg, tmp_path, text, publications=publications)

    assert outcome.applied == (0,), "the post landed even though it couldn't be recorded"
    assert outcome.urls == ("https://x/c",)
    assert outcome.failure is not None
    assert "action 0" in outcome.failure
    assert "published" in outcome.failure
    assert "uncertain" in outcome.failure
    assert calls["comment"].call_count == 1, "action 1 must never be attempted"
    recorded = journal("001-x.json")
    assert recorded is not None
    assert [(a.index, a.state) for a in recorded.actions] == [(0, "uncertain")]
    assert stored_names() == ["001-x.json"]

    publications.settle.side_effect = None
    rerun, _ = _stored_apply(mocker, make_cfg, tmp_path, text, publications=publications)
    assert rerun.applied == () and "uncertain" in (rerun.failure or "")
    assert calls["comment"].call_count == 1, "a re-run never reposts an uncertain action"
    assert calls["comment"].call_count == 1, "action 1 must never be attempted"


def _target(manifest):
    from jailbee.pr_outbox import Target

    return Target(
        manifest=manifest,
        pr=_pr_info(),
        stale=False,
        scope=PrScope(Path("/repo"), "origin", "", None),
    )


def test_finalize_keeps_a_partly_applied_proposal_and_its_journal(mocker):
    from jailbee.pr_outbox import ApplyOutcome, finalize, parse_manifest
    from tests.pr_store_support import journal, stored_names

    text = _manifest_text(
        actions=[{"type": "comment", "body": "a"}, {"type": "comment", "body": "b"}]
    )
    incus, outbox = _stored_outbox(
        mocker,
        {
            "001-x.json": text,
            "001-x.json.progress.json": '{"applied": [0], "urls": {"0": "https://x/a"}}',
        },
    )

    finalize(
        incus,
        "c",
        outbox,
        _target(parse_manifest("001-x.json", text, {})),
        ApplyOutcome(applied=(), urls=(), failure="HTTP 500"),
        uid=1000,
    )

    assert stored_names() == ["001-x.json"]
    recorded = journal("001-x.json")
    assert recorded is not None
    assert [(a.index, a.state) for a in recorded.actions] == [(0, "applied")]
    incus.exec.assert_not_called()


def test_finalize_raises_when_the_store_cannot_be_claimed(mocker):
    """A store failure inside finalize is a named FinalizeError, not a bare exception."""
    from jailbee.outbox_io import JournalError
    from jailbee.pr_outbox import ApplyOutcome, FinalizeError, finalize, parse_manifest

    text = _manifest_text(actions=[{"type": "comment", "body": "a"}])
    incus, outbox = _stored_outbox(mocker, {"001-x.json": text})
    publications = mocker.Mock()
    publications.claim.side_effect = JournalError("disk full")

    with pytest.raises(FinalizeError, match=r"001-x\.json"):
        finalize(
            incus,
            "c",
            outbox,
            _target(parse_manifest("001-x.json", text, {})),
            ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
            uid=1000,
            publications=publications,
        )


def test_finalize_raises_when_removing_a_fully_applied_proposal_fails(mocker):
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import publication_db
    from jailbee.pr_outbox import ApplyOutcome, FinalizeError, finalize, parse_manifest
    from tests.pr_store_support import stored_names

    text = _manifest_text(actions=[{"type": "comment", "body": "a"}])
    files = {"001-x.json": text}
    _record_applied_before_store(files, "001-x.json", [0])
    incus, outbox = _stored_outbox(mocker, files)
    real = publication_db.open_publications()
    publications = mocker.Mock(wraps=real)
    publications.finalize.side_effect = StoreError("permission denied")

    with pytest.raises(FinalizeError, match="could not be removed"):
        finalize(
            incus,
            "c",
            outbox,
            _target(parse_manifest("001-x.json", text, {})),
            ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
            uid=1000,
            publications=publications,
        )

    assert stored_names() == ["001-x.json"]


def test_finalize_requires_the_instance_uuid():
    from jailbee.pr_outbox import ApplyOutcome, FinalizeError, Outbox, finalize, parse_manifest

    text = _manifest_text(actions=[{"type": "comment", "body": "a"}])

    with pytest.raises(FinalizeError, match="uuid is unavailable"):
        finalize(
            None,
            "c",
            Outbox(files={"001-x.json": text}),
            _target(parse_manifest("001-x.json", text, {})),
            ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
            uid=1000,
        )


def test_finalize_removes_a_fully_applied_proposal_and_its_own_bodies(mocker):
    from jailbee.outbox import store_db
    from jailbee.pr_outbox import ApplyOutcome, finalize, parse_manifest
    from tests.pr_store_support import UUID, journal, root, stored_names

    text = _manifest_text(actions=[{"type": "comment", "body_file": "001-x.md"}])
    files = {"001-x.json": text, "001-x.md": "text", "002-y.json": _manifest_text()}
    _record_applied_before_store(files, "001-x.json", [0])
    incus, outbox = _stored_outbox(mocker, files)

    finalize(
        incus,
        "c",
        outbox,
        _target(parse_manifest("001-x.json", text, {"001-x.md": "text"})),
        ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
        uid=1000,
    )

    assert stored_names() == ["002-y.json"]
    assert [p.bodies for p in store_db.read_proposals(root(), UUID, "pr")] == [{}]
    assert journal("001-x.json") is None, "the journal is archived with the proposal"
    incus.exec.assert_not_called()


def test_finalize_keeps_a_neighbours_copy_of_a_same_named_body(mocker):
    from jailbee.outbox import store_db
    from jailbee.pr_outbox import ApplyOutcome, finalize, parse_manifest
    from tests.pr_store_support import UUID, root, stored_names

    text = _manifest_text(actions=[{"type": "comment", "body_file": "shared.md"}])
    files = {"001-x.json": text, "shared.md": "text", "002-y.json": text}
    _record_applied_before_store(files, "001-x.json", [0])
    incus, outbox = _stored_outbox(mocker, files)

    finalize(
        incus,
        "c",
        outbox,
        _target(parse_manifest("001-x.json", text, {"shared.md": "text"})),
        ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
        uid=1000,
    )

    assert stored_names() == ["002-y.json"]
    (neighbour,) = store_db.read_proposals(root(), UUID, "pr")
    assert neighbour.bodies == {"shared.md": "text"}


def test_finalize_refuses_a_proposal_changed_after_publication(mocker):
    from dataclasses import replace

    from jailbee.pr_outbox import ApplyOutcome, FinalizeError, finalize, parse_manifest
    from tests.pr_store_support import journal, stored_names

    published = {"001-x.json": _manifest_text(actions=[{"type": "comment", "body": "a"}])}
    _record_applied_before_store(published, "001-x.json", [0])
    # The stored text differs from the text whose publication the journal records.
    incus, current = _stored_outbox(
        mocker, {"001-x.json": _manifest_text(actions=[{"type": "comment", "body": "z"}])}
    )
    preview = replace(current, files=published)

    with pytest.raises(FinalizeError, match="changed after publication"):
        finalize(
            incus,
            "c",
            preview,
            _target(parse_manifest("001-x.json", published["001-x.json"], {})),
            ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
            uid=1000,
        )

    assert stored_names() == ["001-x.json"]
    assert journal("001-x.json") is not None


def test_a_second_run_applies_only_what_the_journal_does_not_record(mocker, make_cfg, tmp_path):
    """A second run must not repost what a first run's journal records as applied."""
    from jailbee.pr_outbox import (
        _publication_progress,
        apply_manifest,
        parse_manifest,
        proposal_files_digest,
    )
    from tests.pr_store_support import UUID, sent_records, stored_names

    calls = _apply_mocks(mocker)
    text = _manifest_text(
        actions=[{"type": "comment", "body": "a"}, {"type": "comment", "body": "b"}]
    )
    incus, outbox = _stored_outbox(
        mocker,
        {
            "001-x.json": text,
            "001-x.json.progress.json": '{"applied": [0], "urls": {"0": "https://x/a"}}',
        },
    )
    manifest = parse_manifest("001-x.json", text, {})

    outcome = apply_manifest(
        make_cfg(tmp_path),
        incus,
        "c",
        _target(manifest),
        _publication_progress(outbox, "001-x.json", 2),
        uid=1000,
        publisher="user",
        uuid=UUID,
        digest=proposal_files_digest(outbox.files, "001-x.json"),
    )

    assert outcome.applied == (1,) and outcome.failure is None
    calls["comment"].assert_called_once()
    assert calls["comment"].call_args.args[2] == "b"
    assert stored_names() == [], "the last action completed the proposal"
    assert [r.index for r in sent_records()] == [1]


def test_an_empty_receipt_url_is_journaled_as_the_prs_own_url(mocker, make_cfg, tmp_path):
    """A 2xx response that lacks `html_url` makes pr.py return "".

    The journal needs a receipt URL for an applied action, so it falls back to
    the PR page rather than a blank link.
    """
    from jailbee.pr_outbox import Progress, apply_manifest, parse_manifest, proposal_files_digest
    from tests.pr_store_support import UUID, sent_records

    calls = _apply_mocks(mocker)
    calls["comment"].return_value = ""
    text = _manifest_text(
        actions=[{"type": "comment", "body": "a"}, {"type": "comment", "body": "b"}]
    )
    incus, outbox = _stored_outbox(mocker, {"001-x.json": text})

    outcome = apply_manifest(
        make_cfg(tmp_path),
        incus,
        "c",
        _target(parse_manifest("001-x.json", text, {})),
        Progress(frozenset(), {}),
        uid=1000,
        indices=frozenset({0}),
        publisher="user",
        uuid=UUID,
        digest=proposal_files_digest(outbox.files, "001-x.json"),
    )

    assert outcome.failure is None and outcome.urls == ("",)
    (record,) = sent_records()
    assert record.url == "https://github.com/acme/widgets/pull/1234"


def test_record_consumed_raises_when_the_proposal_is_not_in_the_store(mocker):
    """The same named failure protects the single-index `record_consumed` path `jb pr` uses."""
    from jailbee.pr_outbox import FinalizeError, record_consumed
    from tests.pr_store_support import instance

    incus = mocker.MagicMock()
    incus.list_containers.return_value = [instance("c")]

    with pytest.raises(FinalizeError, match=r"002-d\.json"):
        record_consumed(incus, "c", "002-d.json", 0, "https://x/pr", uid=1000)


def test_record_consumed_journals_the_action_and_removes_a_fully_applied_proposal(mocker):
    """The single-index form `jb pr` uses once it turned a null-PR description into a PR."""
    from jailbee.pr_outbox import record_consumed
    from tests.pr_store_support import journal, sent_records, stored_names

    incus, _ = _stored_outbox(
        mocker,
        {
            "002-d.json": _manifest_text(
                pr=None, head_sha=None, actions=[{"type": "description", "body": "B"}]
            )
        },
    )

    record_consumed(incus, "c", "002-d.json", 0, "https://x/pr", uid=1000)

    assert stored_names() == [], "the manifest's only action is now applied; it must go"
    assert journal("002-d.json") is None
    (record,) = sent_records()
    assert (record.manifest, record.index, record.kind, record.url) == (
        "002-d.json",
        0,
        "description",
        "https://x/pr",
    )
    incus.exec.assert_not_called()


def test_record_consumed_keeps_a_manifest_still_missing_other_actions(mocker):
    from jailbee.pr_outbox import record_consumed
    from tests.pr_store_support import journal, stored_names

    incus, _ = _stored_outbox(
        mocker,
        {
            "001-x.json": _manifest_text(
                actions=[{"type": "description", "body": "B"}, {"type": "comment", "body": "c"}]
            )
        },
    )

    record_consumed(incus, "c", "001-x.json", 0, "https://x/pr", uid=1000)

    assert stored_names() == ["001-x.json"], "one of two actions is applied; the manifest stays"
    recorded = journal("001-x.json")
    assert recorded is not None
    assert [(a.index, a.state, a.url) for a in recorded.actions] == [(0, "applied", "https://x/pr")]


def test_record_consumed_refuses_an_action_already_recorded(mocker):
    from jailbee.pr_outbox import FinalizeError, record_consumed

    incus, _ = _stored_outbox(
        mocker,
        {
            "001-x.json": _manifest_text(
                actions=[{"type": "description", "body": "B"}, {"type": "comment", "body": "c"}]
            )
        },
    )
    record_consumed(incus, "c", "001-x.json", 0, "https://x/pr", uid=1000)

    with pytest.raises(FinalizeError, match="already recorded"):
        record_consumed(incus, "c", "001-x.json", 0, "https://x/pr", uid=1000)


def _pr_transport(mocker, tmp_path, incus, reader):
    import subprocess

    from jailbee.outbox.models import StoreSnapshot

    run_shell = subprocess.run
    directory = tmp_path / "pr-transport"
    directory.mkdir(exist_ok=True)

    def sync(snapshot):
        for path in directory.iterdir():
            path.unlink()
        for name, text in snapshot.files:
            (directory / name).write_text(text)
        for name in snapshot.rejected:
            (directory / name).write_bytes(b"\xff")

    def save(snapshot):
        files = {
            p.name: p.read_text() for p in directory.iterdir() if p.name not in snapshot.rejected
        }
        reader.return_value = StoreSnapshot(
            "pr", tuple(sorted(files.items())), snapshot.rejected, ()
        )

    def execute(container, command, **kwargs):
        if command[0] != "bash":
            raise AssertionError(command)
        snapshot = reader.return_value
        sync(snapshot)
        command = list(command)
        command[-1] = str(directory / command[-1].rsplit("/", 1)[-1])
        result = run_shell(command, text=True, capture_output=True, check=True).stdout
        save(snapshot)
        return result

    def mutate(container, command, text, **kwargs):
        snapshot = reader.return_value
        sync(snapshot)
        command = list(command)
        command[4] = str(directory)
        result = run_shell(command, input=text, text=True, capture_output=True, check=True).stdout
        save(snapshot)
        return result

    incus.exec.side_effect = execute
    incus.exec_with_input.side_effect = mutate
    return directory


def test_drop_manifest_deletes_it_and_its_own_bodies(mocker):
    from jailbee.pr_outbox import drop_manifest
    from tests.pr_store_support import stored_names

    incus, outbox = _stored_outbox(
        mocker,
        {
            "001-x.json": _manifest_text(actions=[{"type": "comment", "body_file": "001-x.md"}]),
            "001-x.md": "text",
            "002-y.json": _manifest_text(),
        },
    )

    deleted = drop_manifest(incus, "c", outbox, "001-x.json", uid=1000)

    assert deleted == ["001-x.json", "001-x.md"]
    assert stored_names() == ["002-y.json"]
    incus.exec.assert_not_called()


def test_drop_manifest_leaves_a_neighbours_copy_of_a_same_named_body(mocker):
    from jailbee.outbox import store_db
    from jailbee.pr_outbox import drop_manifest
    from tests.pr_store_support import UUID, root, stored_names

    shared = _manifest_text(actions=[{"type": "comment", "body_file": "shared.md"}])
    incus, outbox = _stored_outbox(
        mocker, {"001-x.json": shared, "002-y.json": shared, "shared.md": "text"}
    )

    deleted = drop_manifest(incus, "c", outbox, "001-x.json", uid=1000)

    assert deleted == ["001-x.json", "shared.md"]
    assert stored_names() == ["002-y.json"]
    (neighbour,) = store_db.read_proposals(root(), UUID, "pr")
    assert neighbour.bodies == {"shared.md": "text"}


def test_drop_manifest_raises_when_the_store_refuses_the_deletion(mocker):
    from jailbee.pr_outbox import FinalizeError, drop_manifest
    from tests.pr_store_support import stored_names

    two = _manifest_text(
        actions=[{"type": "comment", "body": "a"}, {"type": "comment", "body": "b"}]
    )
    incus, outbox = _stored_outbox(
        mocker,
        {
            "001-x.json": two,
            "001-x.json.progress.json": '{"applied": [0], "urls": {"0": "https://x/a"}}',
        },
    )

    # Action 0 already landed on GitHub: the proposal cannot just be discarded.
    with pytest.raises(FinalizeError, match=r"001-x\.json"):
        drop_manifest(incus, "c", outbox, "001-x.json", uid=1000)

    assert stored_names() == ["001-x.json"]


def test_drop_manifest_refuses_a_proposal_that_changed_since_the_preview(mocker):
    from jailbee.outbox import store_db
    from jailbee.pr_outbox import FinalizeError, drop_manifest
    from tests.pr_store_support import UUID, root, stored_names

    incus, outbox = _stored_outbox(mocker, {"001-x.json": _manifest_text()})
    store_db.host_store(root()).edit_proposal(
        UUID, "pr", "001-x.json", None, _manifest_text(actions=[{"type": "comment", "body": "new"}])
    )

    with pytest.raises(FinalizeError, match="changed"):
        drop_manifest(incus, "c", outbox, "001-x.json", uid=1000)

    assert stored_names() == ["001-x.json"]


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


def _pending_pr_text(cfg, incus, *, scope=None, source_branch="feat/foo", **kwargs):
    """Call the pending-description lookup with the publishing scope inputs."""
    from jailbee.pr_outbox import pending_pr_text

    return pending_pr_text(
        cfg,
        incus,
        "c",
        scope=scope if scope is not None else PrScope.for_repo(cfg),
        source_branch=source_branch,
        uid=1000,
        **kwargs,
    )


def test_pending_pr_text_returns_the_description_as_a_prtext(mocker, make_cfg, tmp_path):
    from jailbee.pr_ai import PrText
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    text = _description_manifest(
        actions=[{"type": "description", "body": "Body.", "title": "feat: x", "branch": "feat/x"}]
    )
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"002-d.json": text}))

    found = _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock())

    assert found is not None
    assert found.text == PrText(title="feat: x", body="Body.", branch="feat/x")
    assert found.manifest == "002-d.json"
    assert found.index == 0


@pytest.mark.parametrize("pr", [None, 1234])
def test_pending_pr_text_excludes_source_manifests(mocker, make_cfg, tmp_path, pr):
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    text = _description_manifest(pr=pr, source="refs/heads/topic", source_sha="a" * 40)
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"source.json": text}))

    assert _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), for_pr=pr) is None


def test_pending_pr_text_is_none_without_a_description(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-x.json": _manifest_text()}),
    )

    assert _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock()) is None


def test_pending_pr_text_skips_an_already_consumed_description(mocker, make_cfg, tmp_path):
    from jailbee.outbox.publication_db import Journal
    from jailbee.outbox_io import JournalAction
    from jailbee.pr_outbox import Outbox, proposal_files_digest

    _host_repo(mocker)
    files = {"001-x.json": _description_manifest()}
    journal = Journal(
        "uuid-1",
        "pr",
        "001-x.json",
        proposal_files_digest(files, "001-x.json"),
        "c",
        1,
        (JournalAction(0, "applied", "acme/widgets", url="https://x/1"),),
    )
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files=files, uuid="uuid-1", journals={"001-x.json": journal}),
    )

    assert _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock()) is None


def test_pending_pr_text_asks_which_manifest_when_two_compete(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    a = _description_manifest(actions=[{"type": "description", "body": "A"}])
    b = _description_manifest(actions=[{"type": "description", "body": "B"}])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-a.json": a, "002-b.json": b}),
    )

    found = _pending_pr_text(
        make_cfg(tmp_path), mocker.MagicMock(), pick=lambda names: "002-b.json"
    )

    assert found is not None and found.manifest == "002-b.json"
    assert found.text.body == "B"


def test_pending_pr_text_declines_to_guess_without_a_picker(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    a = _description_manifest(actions=[{"type": "description", "body": "A"}])
    b = _description_manifest(actions=[{"type": "description", "body": "B"}])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-a.json": a, "002-b.json": b}),
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    # `pick=None` is the off-TTY case: warn, name both, and fall back.
    assert _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock()) is None
    assert "001-a.json" in warn.call_args.args[0]
    assert "002-b.json" in warn.call_args.args[0]


def test_pending_pr_text_returns_none_when_the_picker_cancels(mocker, make_cfg, tmp_path):
    """A cancelled picker means "run Claude after all", not "guess"."""
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    a = _description_manifest(actions=[{"type": "description", "body": "A"}])
    b = _description_manifest(actions=[{"type": "description", "body": "B"}])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-a.json": a, "002-b.json": b}),
    )

    found = _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), pick=lambda names: None)

    assert found is None


def test_pending_pr_text_survives_an_unreadable_outbox(mocker, make_cfg, tmp_path):
    """`jailbee pr` must never die because the outbox could not be read."""
    from jailbee.pr_outbox import OutboxReadError

    mocker.patch(
        "jailbee.pr_outbox.read_outbox", side_effect=OutboxReadError("instance is not running")
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    assert _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock()) is None
    assert "not running" in warn.call_args.args[0]


def test_pending_pr_text_skips_a_malformed_manifest_and_uses_the_good_one(
    mocker, make_cfg, tmp_path
):
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    good = _description_manifest(actions=[{"type": "description", "body": "Good."}])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-bad.json": "{not json", "002-good.json": good}),
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    found = _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock())

    assert found is not None and found.manifest == "002-good.json"
    assert "001-bad.json" in warn.call_args.args[0]


def test_pending_pr_text_falls_back_to_the_source_branch_and_first_body_line(
    mocker, make_cfg, tmp_path
):
    """`title: null` / `branch: null` are filled in; `PrText` needs all three."""
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    text = _description_manifest(
        actions=[{"type": "description", "body": "# Add a thing\n\nMore."}]
    )
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))
    incus = mocker.MagicMock()

    found = _pending_pr_text(make_cfg(tmp_path), incus)

    assert found is not None
    assert found.text.title == "Add a thing"
    assert found.text.branch == "feat/foo"
    assert found.text.body == "# Add a thing\n\nMore."


def test_pending_pr_text_returns_a_matching_submodule_description(mocker, make_cfg, tmp_path):
    from jailbee.pr_ai import PrText
    from jailbee.pr_outbox import Outbox

    cfg = make_cfg(tmp_path)
    sub_scope = PrScope(
        repo_root=tmp_path / "libs" / "foo",
        remote="upstream",
        prefix="submodule 'libs/foo': ",
        subpath="libs/foo",
    )
    mocker.patch(
        "jailbee.git.get_remote_url",
        side_effect=lambda root, remote: (
            "git@github.com:acme/foo.git"
            if (root, remote) == (sub_scope.repo_root, "upstream")
            else "git@github.com:acme/widgets.git"
        ),
    )
    text = _description_manifest(
        repo="acme/foo",
        actions=[{"type": "description", "title": "Sub title", "body": "Sub body"}],
    )
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-sub.json": text}))

    found = _pending_pr_text(cfg, mocker.MagicMock(), scope=sub_scope, source_branch="sub-head")

    assert found is not None
    assert found.text == PrText(title="Sub title", body="Sub body", branch="sub-head")
    assert found.manifest == "001-sub.json"


def test_pending_pr_text_silently_skips_a_superproject_manifest_in_submodule_scope(
    mocker, make_cfg, tmp_path
):
    from jailbee.pr_outbox import Outbox

    cfg = make_cfg(tmp_path)
    super_scope = PrScope.for_repo(cfg)
    sub_scope = PrScope(
        repo_root=tmp_path / "libs" / "foo",
        remote="upstream",
        prefix="submodule 'libs/foo': ",
        subpath="libs/foo",
    )
    urls = {
        (super_scope.repo_root, super_scope.remote): "git@github.com:acme/widgets.git",
        (sub_scope.repo_root, sub_scope.remote): "git@github.com:acme/foo.git",
    }
    mocker.patch("jailbee.git.get_remote_url", side_effect=lambda root, remote: urls[root, remote])
    mocker.patch("jailbee.pr_flow.candidate_scopes", return_value=[super_scope, sub_scope])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-super.json": _description_manifest()}),
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    assert _pending_pr_text(cfg, incus, scope=sub_scope) is None
    warn.assert_not_called()


def test_pending_pr_text_silently_skips_a_submodule_manifest_in_superproject_scope(
    mocker, make_cfg, tmp_path
):
    from jailbee.pr_outbox import Outbox

    cfg = make_cfg(tmp_path)
    super_scope = PrScope.for_repo(cfg)
    sub_scope = PrScope(
        repo_root=tmp_path / "libs" / "foo",
        remote="upstream",
        prefix="submodule 'libs/foo': ",
        subpath="libs/foo",
    )
    urls = {
        (super_scope.repo_root, super_scope.remote): "git@github.com:acme/widgets.git",
        (sub_scope.repo_root, sub_scope.remote): "git@github.com:acme/foo.git",
    }
    mocker.patch("jailbee.git.get_remote_url", side_effect=lambda root, remote: urls[root, remote])
    mocker.patch("jailbee.pr_flow.candidate_scopes", return_value=[super_scope, sub_scope])
    text = _description_manifest(repo="acme/foo")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-sub.json": text}))
    warn = mocker.patch("jailbee.pr_outbox.warn")

    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    assert _pending_pr_text(cfg, incus, scope=super_scope) is None
    warn.assert_not_called()


def test_pending_pr_text_warns_for_a_manifest_outside_every_candidate_scope(
    mocker, make_cfg, tmp_path
):
    from jailbee.pr_outbox import Outbox

    cfg = make_cfg(tmp_path)
    scope = PrScope.for_repo(cfg)
    _host_repo(mocker)
    candidates = mocker.patch("jailbee.pr_flow.candidate_scopes", return_value=[scope])
    text = _description_manifest(repo="evil/other")
    mocker.patch(
        "jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-foreign.json": text})
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    assert _pending_pr_text(cfg, incus, scope=scope) is None
    candidates.assert_called_once_with(cfg, extra_paths=[])
    assert "001-foreign.json" in warn.call_args.args[0]
    assert "evil/other" in warn.call_args.args[0]


def test_pending_pr_text_discovers_candidate_scopes_only_once_after_mismatches(
    mocker, make_cfg, tmp_path
):
    from jailbee.pr_outbox import Outbox

    cfg = make_cfg(tmp_path)
    scope = PrScope.for_repo(cfg)
    _host_repo(mocker)
    candidates = mocker.patch("jailbee.pr_flow.candidate_scopes", return_value=[scope])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(
            files={
                "001-foreign.json": _description_manifest(repo="evil/one"),
                "002-foreign.json": _description_manifest(repo="evil/two"),
            }
        ),
    )

    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    assert _pending_pr_text(cfg, incus, scope=scope) is None
    candidates.assert_called_once_with(cfg, extra_paths=[])
    incus.config_get.assert_called_once_with("c", "user.jailbee.sub_pr")


def test_pending_pr_text_matching_active_scope_does_not_discover_candidates(
    mocker, make_cfg, tmp_path
):
    from jailbee.pr_outbox import Outbox

    cfg = make_cfg(tmp_path)
    scope = PrScope.for_repo(cfg)
    _host_repo(mocker)
    candidates = mocker.patch("jailbee.pr_flow.candidate_scopes")
    gitmodules_walk = mocker.patch("jailbee.submodules.host_submodule_paths")
    recorded = mocker.patch("jailbee.submodule_pr.recorded_paths")
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-super.json": _description_manifest()}),
    )

    found = _pending_pr_text(cfg, mocker.MagicMock(), scope=scope)

    assert found is not None and found.manifest == "001-super.json"
    candidates.assert_not_called()
    gitmodules_walk.assert_not_called()
    recorded.assert_not_called()


def test_pending_pr_text_falls_back_to_source_branch_without_reading_container_config(
    mocker, make_cfg, tmp_path
):
    from jailbee.pr_ai import PrText
    from jailbee.pr_outbox import Outbox

    cfg = make_cfg(tmp_path)
    _host_repo(mocker)
    text = _description_manifest(actions=[{"type": "description", "body": ""}])
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))
    incus = mocker.MagicMock()

    found = _pending_pr_text(cfg, incus, source_branch="sub-head")

    assert found is not None
    assert found.text == PrText(title="sub-head", body="", branch="sub-head")
    incus.config_get.assert_not_called()


def test_pending_pr_text_ambiguity_names_the_scoped_publishing_command(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox

    cfg = make_cfg(tmp_path)
    sub_scope = PrScope(
        repo_root=tmp_path / "libs" / "foo",
        remote="upstream",
        prefix="submodule 'libs/foo': ",
        subpath="libs/foo",
    )
    mocker.patch("jailbee.git.get_remote_url", return_value="git@github.com:acme/foo.git")
    a = _description_manifest(repo="acme/foo", actions=[{"type": "description", "body": "A"}])
    b = _description_manifest(repo="acme/foo", actions=[{"type": "description", "body": "B"}])
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-a.json": a, "002-b.json": b}),
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    assert _pending_pr_text(cfg, mocker.MagicMock(), scope=sub_scope) is None
    assert "jailbee submodule pr" in warn.call_args.args[0]


# --- Gate 1: the repo lock ------------------------------------------------


def test_pending_pr_text_skips_a_manifest_for_another_repo(mocker, make_cfg, tmp_path):
    """The gate the design calls the costliest to skip: a container must not
    hand `jailbee pr` a description written for an unrelated repository."""
    from jailbee.pr_outbox import Outbox

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.git.run_capture", side_effect=AssertionError("unexpected real git call"))
    mocker.patch("jailbee.pr_flow.candidate_scopes", return_value=[PrScope.for_repo(cfg)])
    _host_repo(mocker, "https://github.com/acme/widgets.git")
    text = _description_manifest(repo="evil/other")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))
    warn = mocker.patch("jailbee.pr_outbox.warn")

    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    assert _pending_pr_text(cfg, incus) is None
    assert "evil/other" in warn.call_args.args[0]


def test_pending_pr_text_refuses_when_the_host_has_no_github_remote(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker, "/srv/mirrors/widgets.git")
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-d.json": _description_manifest()}),
    )
    warn = mocker.patch("jailbee.pr_outbox.warn")

    assert _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock()) is None
    assert "GitHub remote" in warn.call_args.args[0]


def test_pending_pr_text_does_not_ask_git_for_an_empty_outbox(mocker, make_cfg, tmp_path):
    """The empty outbox is the common case; it must not cost a git round-trip."""
    from jailbee.pr_outbox import Outbox

    remote = _host_repo(mocker)
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={}))

    assert _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock()) is None
    remote.assert_not_called()


# --- Gate 2: which PR the description belongs to --------------------------


def test_pending_pr_text_skips_a_numbered_manifest_on_the_create_path(mocker, make_cfg, tmp_path):
    """`pr: 1234` describes a PR that already exists.

    Publishing its body as a brand-new PR's — a `jailbee new --pr 1234`
    container run through `jailbee pr --stacked` — would put the text somewhere
    it was never meant to go and burn the action index, so `jailbee review
    apply` could never post it where it belongs.
    """
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    text = _description_manifest(pr=1234, head_sha="abc1234")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))

    assert _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock()) is None


def test_pending_pr_text_accepts_a_numbered_manifest_for_the_named_pr(mocker, make_cfg, tmp_path):
    """`for_pr` is the update path's gate: that PR's own description is fair game."""
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    text = _description_manifest(pr=1234, head_sha="abc1234")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))

    found = _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), for_pr=1234)

    assert found is not None and found.manifest == "001-d.json"


def test_pending_pr_text_skips_a_manifest_for_a_different_pr(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    text = _description_manifest(pr=999, head_sha="abc1234")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))

    assert _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), for_pr=1234) is None


def test_pending_pr_text_accepts_a_null_pr_manifest_on_the_update_path(mocker, make_cfg, tmp_path):
    """The container may have written the description before the PR existed."""
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-d.json": _description_manifest()}),
    )

    found = _pending_pr_text(make_cfg(tmp_path), mocker.MagicMock(), for_pr=1234)

    assert found is not None


def test_numbered_only_still_accepts_a_manifest_naming_the_pr(mocker, make_cfg, tmp_path):
    """A container that names the number has said which PR it meant; that the
    PR was adopted rather than opened by jailbee does not unsay it."""
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    text = _description_manifest(pr=1234, head_sha="abc1234")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))

    found = _pending_pr_text(
        make_cfg(tmp_path), mocker.MagicMock(), for_pr=1234, numbered_only=True
    )

    assert found is not None and found.manifest == "001-d.json"


def test_numbered_only_withholds_a_null_pr_manifest_and_says_so(mocker, make_cfg, tmp_path, capsys):
    """`pr: null` means "the PR this container would open", which on a foreign
    head is not this PR. It is withheld — and named, because the run would have
    used it had the manifest carried the number."""
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files={"001-d.json": _description_manifest()}),
    )

    found = _pending_pr_text(
        make_cfg(tmp_path), mocker.MagicMock(), for_pr=1234, numbered_only=True
    )

    assert found is None
    captured = capsys.readouterr()
    assert "001-d.json" in captured.out + captured.err


def test_numbered_only_says_nothing_about_another_prs_manifest(mocker, make_cfg, tmp_path, capsys):
    """A review container legitimately carries manifests for other PRs; those
    were never this run's to use, so naming them would be noise, not news."""
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    text = _description_manifest(pr=999, head_sha="abc1234")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))

    found = _pending_pr_text(
        make_cfg(tmp_path), mocker.MagicMock(), for_pr=1234, numbered_only=True
    )

    assert found is None
    captured = capsys.readouterr()
    assert "001-d.json" not in captured.out + captured.err


# --- The proposed branch name is untrusted input --------------------------


def test_pending_pr_text_keeps_a_valid_proposed_branch(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    check = mocker.patch("jailbee.git.check_ref_format", return_value=True)
    text = _description_manifest(actions=[{"type": "description", "body": "B", "branch": "feat/x"}])
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))
    incus = mocker.MagicMock()
    incus.config_get.return_value = "feat/foo"

    found = _pending_pr_text(make_cfg(tmp_path), incus)

    assert found is not None and found.text.branch == "feat/x"
    check.assert_called_once_with("feat/x")


def test_pending_pr_text_rejects_an_invalid_proposed_branch(mocker, make_cfg, tmp_path):
    """`--as` exits 2 and the AI proposal falls back; the untrusted source is
    checked too, or `-x` / `a b` reaches `git push` and `gh pr create --head`
    and fails *after* this run has already pushed."""
    from jailbee.pr_outbox import Outbox

    _host_repo(mocker)
    mocker.patch("jailbee.git.check_ref_format", return_value=False)
    text = _description_manifest(actions=[{"type": "description", "body": "B", "branch": "-x"}])
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={"001-d.json": text}))
    warn = mocker.patch("jailbee.pr_outbox.warn")
    incus = mocker.MagicMock()
    incus.config_get.return_value = "feat/foo"

    found = _pending_pr_text(make_cfg(tmp_path), incus)

    assert found is not None
    assert found.text.branch == "feat/foo"  # the source branch, not '-x'
    assert found.text.body == "B"  # the description itself is still used
    assert "-x" in warn.call_args.args[0]


# Selected publication uses strict evidence and the exact inspection token.
def _store_files():
    """The proposals now in the host store as one {name: text} dict (bodies included)."""
    from jailbee.outbox import store_db
    from tests.pr_store_support import UUID, root

    files = {}
    for row in store_db.read_proposals(root(), UUID, "pr"):
        files[row.name] = row.manifest
        files.update(row.bodies)
    return files


def _store_sql(sql, *params):
    """Change the stored rows behind the API's back (a late edit during publication)."""
    from jailbee.orchestration.store import BrokerStore, store_path
    from tests.pr_store_support import root

    with BrokerStore(store_path(root())).transaction() as con:
        con.execute(sql, params)


def _selected_setup(mocker, make_cfg, tmp_path, *, files=None):
    """Stage `files` in the host store; returns the offer's fixtures.

    (cfg, incus, identity, store snapshot, `_store_files`, the faked GitHub apply).
    """
    files = files if files is not None else {"one.json": _manifest_text()}
    cfg = make_cfg(tmp_path)
    incus, outbox = _stored_outbox(mocker, files)
    _target_setup(mocker, tmp_path, labels={"user.jailbee.pr": "1234"})
    incus.config_get.side_effect = lambda c, key: {"user.jailbee.pr": "1234"}.get(key)
    mocker.patch("jailbee.pr.gh_login", return_value="octocat")
    apply = mocker.patch("jailbee.pr_outbox._apply_one", return_value="https://x/receipt")
    return cfg, incus, outbox.identity, outbox.snapshot(), _store_files, apply


def _selected_offer(cfg, incus, **kwargs):
    from jailbee.pr_outbox import offer_pending_comments

    return offer_pending_comments(
        cfg,
        incus,
        "c",
        "feature",
        pr_number=kwargs.pop("pr_number", None),
        manifest_names=("one.json",),
        confirm=kwargs.pop("confirm", lambda count: True),
        **kwargs,
        publisher="user",
    )


def test_offer_reports_progress_across_manifests(mocker, make_cfg, tmp_path, monkeypatch, capsys):
    from jailbee.pr_outbox import offer_pending_comments

    actions = [{"type": "comment", "body": "First"}, {"type": "comment", "body": "Second"}]
    files = {"one.json": _manifest_text(actions=actions), "two.json": _manifest_text()}
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path, files=files)
    monkeypatch.setenv("JAILBEE_PROGRESS_LINES", "1")
    assert (
        offer_pending_comments(
            cfg,
            incus,
            "c",
            "feature",
            pr_number=None,
            manifest_names=("one.json", "two.json"),
            confirm=lambda _: True,
            publisher="user",
        )
        == 0
    )
    lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("jailbee-progress:")
    ]
    assert lines == ["jailbee-progress: 1/3", "jailbee-progress: 2/3", "jailbee-progress: 3/3"]


def _draft_offer_setup(mocker, make_cfg, tmp_path, *, extra=False):
    from jailbee.pr_outbox import ApplyOutcome

    _, text = _draft_manifest()
    files = {"one.json": text}
    if extra:
        files["two.json"] = _manifest_text(
            pr=7,
            actions=[
                {"type": "comment", "body": "public"},
                {"type": "description", "body": "not public"},
            ],
        )
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path, files=files)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7))
    incus.config_get.side_effect = lambda c, key: {"user.jailbee.pr": "7"}.get(key)
    mocker.patch("jailbee.pr_outbox._current_pr_body", return_value="old")
    apply = mocker.patch(
        "jailbee.pr_outbox.apply_manifest", return_value=ApplyOutcome((), (), None)
    )
    mocker.patch("jailbee.pr_outbox.finalize")
    return cfg, incus, apply


def test_choose_mode_offered_only_when_draftable(mocker, make_cfg, tmp_path):
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path)
    choose = mocker.Mock(return_value="draft")
    confirm = mocker.Mock(return_value=False)
    assert _selected_offer(cfg, incus, confirm=confirm, choose_mode=choose) == 0
    choose.assert_not_called()
    confirm.assert_called_once_with(1)


@pytest.mark.parametrize("mode,indices", [("draft", {0, 1, 3}), ("publish", None)])
def test_choose_mode_draft_publishes_into_pending(mocker, make_cfg, tmp_path, mode, indices):
    from jailbee.pr_outbox import DraftPreview

    cfg, incus, apply = _draft_offer_setup(mocker, make_cfg, tmp_path)
    choose = mocker.Mock(return_value=mode)
    confirm = mocker.Mock(side_effect=AssertionError("no extra question"))
    assert _selected_offer(cfg, incus, confirm=confirm, choose_mode=choose) == 0
    choose.assert_called_once_with(4, DraftPreview(7, 2, 1, True, draft_total=3))
    assert apply.call_args.kwargs["indices"] == indices
    assert apply.call_args.kwargs.get("draft", False) == (mode == "draft")


def test_choose_mode_none_publishes_nothing(mocker, make_cfg, tmp_path, capsys):
    cfg, incus, apply = _draft_offer_setup(mocker, make_cfg, tmp_path)
    assert _selected_offer(cfg, incus, choose_mode=lambda *_: None) == 0
    apply.assert_not_called()
    assert "Nothing published." in capsys.readouterr().out


def test_preselected_draft_asks_yes_no_with_draft_total(mocker, make_cfg, tmp_path, capsys):
    cfg, incus, apply = _draft_offer_setup(mocker, make_cfg, tmp_path)
    confirm = mocker.Mock(return_value=True)
    choose = mocker.Mock(side_effect=AssertionError("preselected"))
    assert _selected_offer(cfg, incus, confirm=confirm, draft=True, choose_mode=choose) == 0
    confirm.assert_called_once_with(3)
    assert apply.call_args.kwargs["indices"] == {0, 1, 3}
    assert apply.call_args.kwargs["draft"] is True
    output = capsys.readouterr().out
    assert "3 actions will be published to GitHub" in output
    assert "4 actions will be published to GitHub" not in output
    assert "held back in draft mode: action 2 (general comment)" in output
    assert "action 0: pending" in output
    assert "action 1: pending" in output
    assert "action 3: published" in output


def test_draft_summary_line_above_question(mocker, make_cfg, tmp_path, capsys):
    cfg, incus, _ = _draft_offer_setup(mocker, make_cfg, tmp_path)

    def choose(total, preview):
        output = capsys.readouterr().out
        assert output.rstrip().endswith(
            "As a draft: 2 go into your pending review on PR #7; "
            "1 general comment(s) held back; the description is published."
        )
        return None

    assert _selected_offer(cfg, incus, choose_mode=choose) == 0


@pytest.mark.parametrize("raise_errors", [False, True])
def test_draft_with_nothing_draftable_is_an_error(mocker, make_cfg, tmp_path, capsys, raise_errors):
    from jailbee.pr_outbox import OutboxError

    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path)
    message = "nothing here can be a draft review: no review comments or replies are pending"
    if raise_errors:
        with pytest.raises(OutboxError, match=message):
            _selected_offer(cfg, incus, draft=True, raise_errors=True)
    else:
        assert _selected_offer(cfg, incus, draft=True) == 1
        captured = capsys.readouterr()
        assert message in captured.out + captured.err


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("raise_errors", [False, True])
def test_draft_no_prompt_requires_draftable_actions(
    mocker, make_cfg, tmp_path, capsys, empty, raise_errors
):
    from jailbee.pr_outbox import OutboxError, offer_pending_comments

    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path, files={} if empty else None)
    apply = mocker.patch("jailbee.pr_outbox.apply_manifest")
    confirm = mocker.Mock(side_effect=AssertionError("no prompt allowed"))
    choose = mocker.Mock(side_effect=AssertionError("no choice allowed"))
    message = "nothing here can be a draft review: no review comments or replies are pending"

    def offer():
        return offer_pending_comments(
            cfg,
            incus,
            "c",
            "feature",
            pr_number=None,
            manifest_names=() if empty else ("one.json",),
            draft=True,
            can_prompt=False,
            raise_errors=raise_errors,
            confirm=confirm,
            choose_mode=choose,
            publisher="user",
        )

    if raise_errors:
        with pytest.raises(OutboxError, match=message):
            offer()
    else:
        assert offer() == 1
        captured = capsys.readouterr()
        assert message in captured.out + captured.err
    apply.assert_not_called()
    confirm.assert_not_called()
    choose.assert_not_called()


def test_draft_skips_non_draftable_manifest_in_same_run(mocker, make_cfg, tmp_path, capsys):
    from jailbee.pr_outbox import offer_pending_comments

    cfg, incus, apply = _draft_offer_setup(mocker, make_cfg, tmp_path, extra=True)
    confirm = mocker.Mock(return_value=True)
    assert (
        offer_pending_comments(
            cfg,
            incus,
            "c",
            "feature",
            pr_number=None,
            confirm=confirm,
            manifest_names=("one.json", "two.json"),
            draft=True,
            publisher="user",
        )
        == 0
    )
    confirm.assert_called_once_with(3)
    apply.assert_called_once()
    assert apply.call_args.args[3].manifest.name == "one.json"
    assert (
        "two.json: no review comments or replies; left for a normal publish"
        in capsys.readouterr().out
    )


def test_dry_run_draft_lists_held_back_and_publishes_nothing(mocker, make_cfg, tmp_path, capsys):
    cfg, incus, apply = _draft_offer_setup(mocker, make_cfg, tmp_path)
    confirm = mocker.Mock(side_effect=AssertionError("dry run"))
    assert _selected_offer(cfg, incus, confirm=confirm, draft=True, dry_run=True) == 0
    apply.assert_not_called()
    output = capsys.readouterr().out
    assert "As a draft: 2 go into your pending review on PR #7" in output
    assert "held back in draft mode: action 2 (general comment)" in output
    assert output.rstrip().endswith("Dry run: nothing was published.")


def test_pr_offer_subset_draft_excludes_description(mocker, make_cfg, tmp_path):
    cfg, incus, apply = _draft_offer_setup(mocker, make_cfg, tmp_path)
    previews = []
    assert (
        _selected_offer(
            cfg,
            incus,
            pr_number=7,
            choose_mode=lambda total, preview: previews.append((total, preview)) or "draft",
        )
        == 0
    )
    assert previews[0][0] == 3
    assert previews[0][1].description is False
    assert apply.call_args.kwargs["indices"] == {0, 1}


@pytest.mark.parametrize("indices", [frozenset(), frozenset({2, 3})])
def test_draft_does_not_offer_applied_or_excluded_review(mocker, make_cfg, tmp_path, indices):
    cfg, incus, apply = _draft_offer_setup(mocker, make_cfg, tmp_path)
    from jailbee.pr_outbox import Target

    manifest, _ = _draft_manifest()
    target = Target(
        manifest=manifest, scope=PrScope.for_repo(cfg), pr=_pr_info(number=7), stale=False
    )
    mocker.patch("jailbee.pr_outbox._gate_manifests", return_value=([target], [], []))
    mocker.patch("jailbee.pr_outbox._offerable_indices", return_value=indices)
    assert _selected_offer(cfg, incus, pr_number=7, draft=True) == 1
    apply.assert_not_called()


def test_draft_counts_only_pending_indices(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Progress

    cfg, incus, apply = _draft_offer_setup(mocker, make_cfg, tmp_path)
    mocker.patch(
        "jailbee.pr_outbox._publication_progress", return_value=Progress(frozenset({0}), {})
    )
    confirm = mocker.Mock(return_value=True)
    assert _selected_offer(cfg, incus, draft=True, confirm=confirm) == 0
    confirm.assert_called_once_with(2)
    assert apply.call_args.kwargs["indices"] == {1, 3}


def _revision(tmp_path, identity, store):
    from jailbee.outbox.inspect import build_views

    return next(
        v.revision
        for v in build_views(identity, (store,), pr_journals={})
        if v.id.name == "one.json"
    )


def test_selected_publication_preserves_full_cleanup_scope(mocker, make_cfg, tmp_path):
    from jailbee.outbox import store_db
    from tests.pr_store_support import UUID, root

    text = _manifest_text(actions=[{"type": "comment", "body_file": "shared.md"}])
    files = {"one.json": text, "two.json": text, "shared.md": "body"}
    cfg, incus, _, _, stored, apply = _selected_setup(mocker, make_cfg, tmp_path, files=files)
    assert _selected_offer(cfg, incus, pr_number=9999) == 0
    assert apply.call_count == 1
    # Only the published proposal and its own bodies go; the neighbour keeps its copy.
    assert stored() == {"two.json": text, "shared.md": "body"}
    (neighbour,) = store_db.read_proposals(root(), UUID, "pr")
    assert (neighbour.name, neighbour.bodies) == ("two.json", {"shared.md": "body"})


@pytest.mark.parametrize("change", ["body", "identity", "progress", "manifest"])
def test_selected_rechecks_confirmation_inputs(mocker, make_cfg, tmp_path, change):
    from jailbee.outbox import publication_db
    from jailbee.pr_outbox import proposal_files_digest
    from tests.pr_store_support import UUID

    text = _manifest_text(actions=[{"type": "comment", "body_file": "body.md"}])
    cfg, incus, identity, store, _, apply = _selected_setup(
        mocker, make_cfg, tmp_path, files={"one.json": text, "body.md": "original"}
    )

    def confirm(count):
        if change == "body":
            _store_sql("UPDATE outbox_body SET text=? WHERE body_name=?", "changed", "body.md")
        elif change == "identity":
            incus.list_containers.return_value[0]["created_at"] = "2026-10-01T00:00:00Z"
        elif change == "progress":
            digest = proposal_files_digest(_store_files(), "one.json")
            pubs = publication_db.open_publications()
            with pubs.claim(UUID, "pr", "one.json") as claim:
                pubs.begin(claim, digest=digest, action_count=1, container_full="c", now=1.0)
                pubs.mark_prepared(claim, digest, 0, repo="acme/widgets", now=1.0)
        else:
            _store_sql(
                "UPDATE outbox_proposal SET manifest=?, revision=revision+1 WHERE name=?",
                _manifest_text(),
                "one.json",
            )
        return True

    assert (
        _selected_offer(
            cfg, incus, confirm=confirm, expected_revision=_revision(tmp_path, identity, store)
        )
        == 1
    )
    apply.assert_not_called()
    incus.exec.assert_not_called()


@pytest.mark.parametrize("bad", ["other_text", "uncertain", "wrong_count"])
def test_selected_progress_never_authorizes_replay(mocker, make_cfg, tmp_path, bad):
    from jailbee.outbox import publication_db
    from jailbee.pr_outbox import proposal_files_digest
    from tests.pr_store_support import UUID

    cfg, incus, _, _, stored, apply = _selected_setup(mocker, make_cfg, tmp_path)
    digest = (
        "journal-of-another-text"
        if bad == "other_text"
        else proposal_files_digest(stored(), "one.json")
    )
    pubs = publication_db.open_publications()
    with pubs.claim(UUID, "pr", "one.json") as claim:
        pubs.begin(
            claim,
            digest=digest,
            action_count=2 if bad == "wrong_count" else 1,
            container_full="c",
            now=1.0,
        )
        pubs.mark_prepared(claim, digest, 0, repo="acme/widgets", now=1.0)
        if bad != "uncertain":
            pubs.settle(claim, digest, 0, outcome="applied", url="https://x/old", now=1.0)
    assert _selected_offer(cfg, incus) == 1
    apply.assert_not_called()
    incus.exec.assert_not_called()


def test_selected_uses_shared_revision_not_domain_digest(mocker, make_cfg, tmp_path):
    from jailbee.outbox_io import proposal_digest

    cfg, incus, identity, store, _, apply = _selected_setup(mocker, make_cfg, tmp_path)
    revision = _revision(tmp_path, identity, store)
    assert (
        _selected_offer(
            cfg,
            incus,
            expected_revision=proposal_digest("one.json", store.as_dict()["one.json"], {}),
        )
        == 1
    )
    apply.assert_not_called()
    assert _selected_offer(cfg, incus, expected_revision=revision) == 0
    apply.assert_called_once()


def test_selected_rejects_changed_supplied_snapshot(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox

    cfg, incus, _, _, _, apply = _selected_setup(mocker, make_cfg, tmp_path)
    preview = Outbox({"one.json": _manifest_text(actions=[{"type": "comment", "body": "old"}])})
    assert _selected_offer(cfg, incus, outbox=preview) == 1
    apply.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
def test_selected_rechecks_moved_head(mocker, make_cfg, tmp_path, force):
    text = _manifest_text(actions=[{"type": "review", "body": "review", "comments": []}])
    cfg, incus, _, _, _, apply = _selected_setup(
        mocker, make_cfg, tmp_path, files={"one.json": text}
    )

    def confirm(count):
        mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(head_sha="changed"))
        return True

    assert _selected_offer(cfg, incus, confirm=confirm, force=force) == (0 if force else 1)
    assert apply.call_count == int(force)


def test_selected_rechecks_ownership(mocker, make_cfg, tmp_path):
    cfg, incus, _, _, _, apply = _selected_setup(mocker, make_cfg, tmp_path)

    def confirm(count):
        incus.config_get.side_effect = lambda c, key: "9999" if key == "user.jailbee.pr" else None
        return True

    assert _selected_offer(cfg, incus, confirm=confirm) == 1
    apply.assert_not_called()


def test_selected_publication_leaves_an_invalid_neighbour_alone(mocker, make_cfg, tmp_path):
    text = _manifest_text(actions=[{"type": "comment", "body_file": "body.md"}])
    files = {"one.json": text, "body.md": "body", "two.json": "{invalid"}
    cfg, incus, _, _, stored, apply = _selected_setup(mocker, make_cfg, tmp_path, files=files)
    assert _selected_offer(cfg, incus) == 0
    apply.assert_called_once()
    assert stored() == {"two.json": "{invalid"}


def test_drop_rejects_changed_snapshot(mocker):
    from jailbee.outbox import store_db
    from jailbee.pr_outbox import FinalizeError, drop_manifest
    from tests.pr_store_support import UUID, root, stored_names

    incus, old = _stored_outbox(mocker, {"one.json": _manifest_text()})
    changed = _manifest_text(actions=[{"type": "comment", "body": "changed"}])
    store_db.host_store(root()).edit_proposal(UUID, "pr", "one.json", None, changed)
    with pytest.raises(FinalizeError, match="refresh"):
        drop_manifest(incus, "c", old, "one.json", uid=1000)
    assert stored_names() == ["one.json"]
    incus.exec.assert_not_called()


def test_selected_publishes_only_what_the_journal_does_not_record(mocker, make_cfg, tmp_path):
    from tests.pr_store_support import sent_records, stored_names

    files = {
        "one.json": _manifest_text(
            actions=[{"type": "comment", "body": "landed"}, {"type": "comment", "body": "pending"}]
        ),
        "one.json.progress.json": '{"applied": [0], "urls": {"0": "https://x/old"}}',
    }
    cfg, incus, _, _, _, apply = _selected_setup(mocker, make_cfg, tmp_path, files=files)
    assert _selected_offer(cfg, incus) == 0
    assert [c.args[1].body for c in apply.call_args_list] == ["pending"]
    assert stored_names() == [], "the last action completed the proposal"
    assert [(r.index, r.url) for r in sent_records()] == [(1, "https://x/receipt")]
    incus.exec.assert_not_called()


@pytest.mark.parametrize("offer", [None, 9999])
def test_selected_failed_read_is_failure_even_in_offer_mode(mocker, make_cfg, tmp_path, offer):
    from jailbee.outbox.models import OutboxExecutionError

    cfg, incus, _, _, _, apply = _selected_setup(mocker, make_cfg, tmp_path)
    mocker.patch(
        "jailbee.outbox.service.pr_snapshot", side_effect=OutboxExecutionError("unavailable")
    )
    assert _selected_offer(cfg, incus, pr_number=offer) == 1
    apply.assert_not_called()


@pytest.mark.parametrize("location", ["root", "action", "inline"])
@pytest.mark.parametrize(
    "metadata", [{"body_file": 17}, {"body_file": "extension.md"}, {"nested": [{"body_file": 17}]}]
)
def test_selected_ignored_extensions_never_abort_remote_finalization(
    mocker, make_cfg, tmp_path, location, metadata
):
    from tests.pr_store_support import sent_records

    payload = json.loads(_manifest_text(actions=[{"type": "comment", "body_file": "body.md"}]))
    if location == "inline":
        payload["actions"] = [
            {
                "type": "review",
                "body": "Review",
                "comments": [
                    {"path": "a.py", "line": 1, "body_file": "body.md", "agent_metadata": metadata}
                ],
            }
        ]
    else:
        (payload if location == "root" else payload["actions"][0])["agent_metadata"] = metadata
    files = {"one.json": json.dumps(payload), "body.md": "Body"}
    cfg, incus, _, _, stored, apply = _selected_setup(mocker, make_cfg, tmp_path, files=files)
    assert _selected_offer(cfg, incus) == 0
    apply.assert_called_once()
    assert stored() == {}, "the proposal and its body are gone"
    assert [r.url for r in sent_records()] == ["https://x/receipt"]


@pytest.mark.parametrize("failure_type", [ValueError, KeyError, RecursionError])
def test_finalization_extraction_failure_retains_receipts_cause_and_retry(
    mocker, make_cfg, tmp_path, failure_type
):
    from jailbee import pr_outbox
    from jailbee.outbox.io import PrManagement
    from jailbee.outbox.models import OutboxExecutionError
    from tests.pr_store_support import journal, sent_records

    cfg, incus, _, _, stored, apply = _selected_setup(mocker, make_cfg, tmp_path)
    original = pr_outbox.body_references
    failure = failure_type("extraction failed")
    posted = []
    apply.side_effect = lambda *args, **kwargs: posted.append(1) or "https://x/receipt"

    def extract(text):
        if posted:
            raise failure
        return original(text)

    mocker.patch.object(pr_outbox, "body_references", side_effect=extract)
    with pytest.raises(OutboxExecutionError) as caught:
        _selected_offer(cfg, incus, raise_errors=True, management=PrManagement(tmp_path / "locks"))
    assert isinstance(caught.value.__cause__, pr_outbox.FinalizeError)
    assert caught.value.__cause__.__cause__ is failure
    # The receipt was journaled with its sent record in the same transaction that
    # completed (and removed) the proposal; the failed cleanup lost none of it.
    assert [r.url for r in sent_records()] == ["https://x/receipt"]
    assert stored() == {}
    assert journal("one.json") is None
    incus.exec.assert_not_called()

    # A retry has nothing left to publish and never reposts.
    mocker.patch.object(pr_outbox, "body_references", side_effect=original)
    assert _selected_offer(cfg, incus, management=PrManagement(tmp_path / "fresh-locks")) == 1
    apply.assert_called_once()


@pytest.mark.parametrize("action_type", ["comment", "reply", "description", "review"])
def test_body_references_follow_domain_fields_and_null_compatibility(action_type):
    from jailbee.pr_outbox import body_references, parse_manifest

    action = {
        "type": action_type,
        "body": "Inline",
        "body_file": None,
        "agent_metadata": {"body_file": 17},
        "comments": [{"body_file": "ignored.md"}],
    }
    if action_type == "reply":
        action["comment_id"] = 1
    if action_type == "review":
        action["comments"] = [
            {
                "path": "a.py",
                "line": 1,
                "body_file": " space pr=7 .md",
                "metadata": {"body_file": "ignored.md"},
            }
        ]
    raw = _manifest_text(actions=[action])
    files = {" space pr=7 .md": "Comment"}
    parse_manifest("one.json", raw, files)
    assert body_references(raw) == (set(files) if action_type == "review" else set())


@pytest.mark.parametrize("late_change", [False, True])
def test_inline_body_reference_is_freshness_input_and_exclusive_cleanup(
    mocker, make_cfg, tmp_path, late_change
):
    from tests.pr_store_support import journal, sent_records

    name = " space pr=7 .md"
    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body": "Review",
                "comments": [
                    {"path": "a.py", "line": 1, "body_file": name, "metadata": {"body_file": 17}}
                ],
            }
        ]
    )
    cfg, incus, _, _, stored, apply = _selected_setup(
        mocker, make_cfg, tmp_path, files={"one.json": text, name: "Original"}
    )
    if late_change:

        def publish(*args, **kwargs):
            _store_sql("UPDATE outbox_body SET text=? WHERE body_name=?", "Changed", name)
            return "https://x/receipt"

        apply.side_effect = publish
    assert _selected_offer(cfg, incus) == (1 if late_change else 0)
    remaining = stored()
    assert (name in remaining) == late_change
    assert ("one.json" in remaining) == late_change
    assert [r.url for r in sent_records()] == ["https://x/receipt"]
    assert (journal("one.json") is not None) == late_change
    apply.assert_called_once()


@pytest.mark.parametrize("change", ["manifest", "body"])
def test_selected_cleanup_refuses_late_change_retains_receipts(mocker, make_cfg, tmp_path, change):
    from tests.pr_store_support import journal, sent_records

    text = _manifest_text(actions=[{"type": "comment", "body_file": "body.md"}])
    cfg, incus, _, _, stored, apply = _selected_setup(
        mocker, make_cfg, tmp_path, files={"one.json": text, "body.md": "body"}
    )

    def publish(*args, **kwargs):
        if change == "manifest":
            _store_sql(
                "UPDATE outbox_proposal SET manifest=manifest || ?, revision=revision+1 "
                "WHERE name=?",
                " ",
                "one.json",
            )
        else:
            _store_sql(
                "UPDATE outbox_body SET text=text || ? WHERE body_name=?", "changed", "body.md"
            )
        return "https://x/receipt"

    apply.side_effect = publish
    assert _selected_offer(cfg, incus) == 1
    remaining = stored()
    assert "one.json" in remaining and "body.md" in remaining
    recorded = journal("one.json")
    assert recorded is not None
    assert [(a.index, a.state, a.url) for a in recorded.actions] == [
        (0, "applied", "https://x/receipt")
    ]
    assert [r.url for r in sent_records()] == ["https://x/receipt"]
    assert apply.call_count == 1


def test_drop_uses_same_manager_under_outer_lock(mocker, tmp_path):
    from jailbee.outbox.io import PrManagement
    from jailbee.pr_outbox import drop_manifest

    incus, outbox = _stored_outbox(mocker, {"one.json": _manifest_text()})
    manager = PrManagement(tmp_path / "locks")
    with manager.lock(outbox.identity):
        assert drop_manifest(incus, "c", outbox, "one.json", uid=1000, management=manager) == [
            "one.json"
        ]


def test_drop_rejects_replaced_preview_identity(mocker):
    from dataclasses import replace

    from jailbee.outbox_io import ContainerIdentity
    from jailbee.pr_outbox import FinalizeError, drop_manifest
    from tests.pr_store_support import stored_names

    incus, outbox = _stored_outbox(mocker, {"one.json": _manifest_text()})
    outbox = replace(outbox, identity=ContainerIdentity("c", "older"))
    with pytest.raises(FinalizeError, match="refresh"):
        drop_manifest(incus, "c", outbox, "one.json", uid=1000)
    assert stored_names() == ["one.json"]
    incus.exec.assert_not_called()


def test_selected_duplicate_names_do_not_repost(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import offer_pending_comments

    cfg, incus, _, _, _, apply = _selected_setup(mocker, make_cfg, tmp_path)
    assert (
        offer_pending_comments(
            cfg,
            incus,
            "c",
            "feature",
            pr_number=None,
            manifest_names=("one.json", "one.json"),
            confirm=lambda count: True,
            publisher="user",
        )
        == 1
    )
    apply.assert_not_called()


def test_selected_checks_identity_after_target_revalidation(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Target, parse_manifest

    cfg, incus, _, store, _, apply = _selected_setup(mocker, make_cfg, tmp_path)
    target = Target(
        parse_manifest("one.json", store.as_dict()["one.json"], {}),
        _pr_info(),
        False,
        PrScope(tmp_path, "origin", "", None),
    )
    calls = 0

    def resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            incus.list_containers.return_value[0]["created_at"] = "replacement"
        return target

    mocker.patch("jailbee.pr_outbox.resolve_target", side_effect=resolve)
    assert _selected_offer(cfg, incus) == 1
    apply.assert_not_called()


def test_selected_offer_reenters_injected_manager(mocker, make_cfg, tmp_path):
    from jailbee.outbox.io import PrManagement

    cfg, incus, identity, _, _, apply = _selected_setup(mocker, make_cfg, tmp_path)
    manager = PrManagement(tmp_path / "locks")
    with manager.lock(identity):
        assert _selected_offer(cfg, incus, management=manager) == 0
    apply.assert_called_once()


def test_legacy_offer_does_not_replace_supplied_missing_proposal(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Outbox, offer_pending_comments

    cfg, incus, _, _, _, apply = _selected_setup(mocker, make_cfg, tmp_path, files={})
    preview = Outbox({"one.json": _manifest_text()})
    assert (
        offer_pending_comments(
            cfg,
            incus,
            "c",
            "feature",
            pr_number=None,
            outbox=preview,
            confirm=lambda count: True,
            publisher="user",
        )
        == 1
    )
    apply.assert_not_called()


def test_selected_deep_json_is_refusal_not_crash(mocker, make_cfg, tmp_path):
    cfg, incus, _, _, _, apply = _selected_setup(
        mocker, make_cfg, tmp_path, files={"one.json": "[" * 2000 + "0" + "]" * 2000}
    )
    assert _selected_offer(cfg, incus) == 1
    apply.assert_not_called()


def test_read_outbox_binds_preview_identity(mocker):
    from jailbee.outbox_io import ContainerIdentity
    from jailbee.pr_outbox import read_outbox
    from tests.pr_store_support import instance, seed_pr_store

    seed_pr_store({"one.json": _manifest_text()})
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [instance("c", created_at="original")]
    preview = read_outbox(incus, "c", uid=1000)
    assert preview.identity == ContainerIdentity("c", "original")
    assert preview.manifest_names == ["one.json"]


def test_drop_refuses_identityless_preview(mocker):
    from dataclasses import replace

    from jailbee.pr_outbox import FinalizeError, drop_manifest
    from tests.pr_store_support import stored_names

    incus, outbox = _stored_outbox(mocker, {"one.json": _manifest_text()})
    with pytest.raises(FinalizeError, match="refresh"):
        drop_manifest(incus, "c", replace(outbox, identity=None), "one.json", uid=1000)
    assert stored_names() == ["one.json"]
    incus.exec.assert_not_called()


def _description_source_setup(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    _host_repo(mocker)
    raw = _description_manifest(
        pr=123,
        head_sha="abc123",
        actions=[
            {"type": "comment", "body_file": "other.md"},
            {"type": "description", "body_file": "body.md", "title": "Title"},
        ],
    )
    incus, _ = _stored_outbox(mocker, {"one.json": raw, "body.md": "Body", "other.md": "Other"})
    incus.list_containers.return_value = [
        {
            "name": "c",
            "created_at": "original",
            "config": {"volatile.uuid": "uuid-test"},
        }
    ]
    incus.config_get.return_value = None
    source = _pending_pr_text(cfg, incus, for_pr=123)
    return cfg, incus, raw, source


def test_description_source_carries_exact_evidence(mocker, make_cfg, tmp_path):
    from jailbee.outbox_io import ContainerIdentity

    _, _, raw, source = _description_source_setup(mocker, make_cfg, tmp_path)
    assert source is not None
    assert source.identity == ContainerIdentity("c", "original")
    assert source.manifest_text == raw
    assert source.body_files == (("body.md", "Body"), ("other.md", "Other"))
    assert source.index == 1
    assert isinstance(source.digest, str) and len(source.digest) == 64


@pytest.mark.parametrize(
    "change",
    [
        "unchanged",
        "body",
        "other-body",
        "identity",
        "insert",
        "consumed",
        "progress",
        "uncertain",
    ],
)
def test_description_source_validation(mocker, make_cfg, tmp_path, change):
    from jailbee import pr_flow
    from jailbee.outbox import publication_db
    from jailbee.pr_outbox import OutboxChanged, proposal_files_digest
    from tests.pr_store_support import UUID

    cfg, incus, raw, source = _description_source_setup(mocker, make_cfg, tmp_path)
    assert source is not None
    if change in ("body", "other-body"):
        _store_sql(
            "UPDATE outbox_body SET text=? WHERE body_name=?",
            "Changed",
            "body.md" if change == "body" else "other.md",
        )
    elif change == "identity":
        incus.list_containers.return_value = [
            {"name": "c", "created_at": "replacement", "config": {"volatile.uuid": "uuid-test"}}
        ]
    elif change == "insert":
        payload = json.loads(raw)
        payload["actions"].insert(0, {"type": "comment", "body": "Inserted"})
        _store_sql(
            "UPDATE outbox_proposal SET manifest=?, revision=revision+1 WHERE name=?",
            json.dumps(payload),
            "one.json",
        )
    elif change in ("consumed", "progress", "uncertain"):
        digest = proposal_files_digest(_store_files(), "one.json")
        pubs = publication_db.open_publications()
        with pubs.claim(UUID, "pr", "one.json") as claim:
            pubs.begin(claim, digest=digest, action_count=2, container_full="c", now=1.0)
            index = 1 if change == "consumed" else 0
            pubs.mark_prepared(claim, digest, index, repo="acme/widgets", now=1.0)
            if change != "uncertain":
                pubs.settle(claim, digest, index, outcome="applied", url="https://x/u", now=1.0)
    picker = mocker.patch("jailbee.pr_flow._pick_outbox_manifest")
    if change == "unchanged":
        pr_flow.validate_outbox_source(cfg, incus, "c", source)
    else:
        with pytest.raises(OutboxChanged, match="refresh"):
            pr_flow.validate_outbox_source(cfg, incus, "c", source)
    picker.assert_not_called()


@pytest.mark.parametrize("evidence", ["other_text", "uncertain"])
def test_description_selection_refuses_unusable_progress(mocker, make_cfg, tmp_path, evidence):
    from jailbee.outbox import publication_db
    from jailbee.pr_outbox import proposal_files_digest
    from tests.pr_store_support import UUID

    cfg, incus, _, source = _description_source_setup(mocker, make_cfg, tmp_path)
    assert source is not None
    digest = (
        "journal-of-another-text"
        if evidence == "other_text"
        else proposal_files_digest(_store_files(), "one.json")
    )
    pubs = publication_db.open_publications()
    with pubs.claim(UUID, "pr", "one.json") as claim:
        pubs.begin(claim, digest=digest, action_count=2, container_full="c", now=1.0)
        pubs.mark_prepared(claim, digest, 0, repo="acme/widgets", now=1.0)
        if evidence == "other_text":
            pubs.settle(claim, digest, 0, outcome="applied", url="https://x/u", now=1.0)
    assert _pending_pr_text(cfg, incus, for_pr=123) is None


@pytest.mark.parametrize("change", ["receipt", "unreadable", "text", "index", "name"])
def test_description_validation_binds_existing_progress(mocker, make_cfg, tmp_path, change):
    from dataclasses import replace

    from jailbee.outbox import publication_db
    from jailbee.outbox.models import OutboxExecutionError
    from jailbee.pr_flow import validate_outbox_source
    from jailbee.pr_outbox import OutboxChanged, proposal_files_digest
    from tests.pr_store_support import UUID

    cfg, incus, _, _ = _description_source_setup(mocker, make_cfg, tmp_path)
    digest = proposal_files_digest(_store_files(), "one.json")
    pubs = publication_db.open_publications()
    with pubs.claim(UUID, "pr", "one.json") as claim:
        pubs.begin(claim, digest=digest, action_count=2, container_full="c", now=1.0)
        pubs.mark_prepared(claim, digest, 0, repo="acme/widgets", now=1.0)
        pubs.settle(claim, digest, 0, outcome="applied", url="old-url", now=1.0)
    source = _pending_pr_text(cfg, incus, for_pr=123)
    assert source is not None
    validate_outbox_source(cfg, incus, "c", source)
    if change == "receipt":
        _store_sql(
            "UPDATE outbox_journal_action SET url=? WHERE proposal_name=? AND idx=0",
            "new-url",
            "one.json",
        )
    elif change == "unreadable":
        mocker.patch(
            "jailbee.outbox.service.pr_snapshot", side_effect=OutboxExecutionError("unavailable")
        )
    elif change == "text":
        source = replace(source, text=replace(source.text, body="substitute"))
    elif change == "index":
        source = replace(source, index=0)
    elif change == "name":
        source = replace(source, manifest="other.json")
    with pytest.raises(OutboxChanged, match="refresh"):
        validate_outbox_source(cfg, incus, "c", source)


def _description_only_outbox(name="003-docs.json"):
    from jailbee.pr_outbox import Outbox

    text = _manifest_text(
        pr=None, head_sha=None, actions=[{"type": "description", "body": "Body."}]
    )
    return Outbox(files={name: text})


def test_offer_stays_quiet_about_a_description_for_a_pr_not_yet_opened(mocker, tmp_path):
    """`jailbee pr` offers comments after each submodule PR, before the next
    submodule's and the superproject's own PRs exist. A `pr: null`
    description for one of those is about to be used by this same run, and
    `jailbee review apply` would only defer it — so naming it as "did not use"
    with that command as the remedy is wrong on both counts."""
    from jailbee.pr_outbox import Target, _gate_manifests

    outbox = _description_only_outbox()
    mocker.patch(
        "jailbee.pr_outbox.resolve_target",
        side_effect=lambda cfg, incus, c, manifest, **kw: Target(
            manifest=manifest, pr=None, stale=False, scope=mocker.MagicMock()
        ),
    )

    targets, refusals, notes = _gate_manifests(
        mocker.MagicMock(),
        mocker.MagicMock(),
        "c",
        outbox,
        "feat",
        force=False,
        comments_only=True,
    )

    assert (targets, refusals, notes) == ([], [], [])


def test_offer_still_names_a_description_for_an_existing_pr(mocker, tmp_path):
    from jailbee.pr_outbox import Target, _gate_manifests

    outbox = _description_only_outbox()
    mocker.patch(
        "jailbee.pr_outbox.resolve_target",
        side_effect=lambda cfg, incus, c, manifest, **kw: Target(
            manifest=manifest, pr=mocker.MagicMock(), stale=False, scope=mocker.MagicMock()
        ),
    )

    _, _, notes = _gate_manifests(
        mocker.MagicMock(),
        mocker.MagicMock(),
        "c",
        outbox,
        "feat",
        force=False,
        comments_only=True,
    )

    assert len(notes) == 1
    assert "003-docs.json still holds a description" in notes[0]


@pytest.mark.parametrize("mode", [None, "draft"])
def test_draft_chooser_multiple_manifests_partial_progress(mocker, make_cfg, tmp_path, mode):
    from jailbee.pr_outbox import DraftPreview, Progress

    _, text = _draft_manifest()
    files = {
        "one.json": text,
        "two.json": _manifest_text(
            pr=7,
            actions=[
                {"type": "comment", "body": "held"},
                {"type": "description", "body": "skipped"},
            ],
        ),
        "three.json": text,
    }
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path, files=files)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_pr_info(number=7))
    incus.config_get.side_effect = lambda c, key: {"user.jailbee.pr": "7"}.get(key)
    mocker.patch("jailbee.pr_outbox._current_pr_body", return_value="old")
    mocker.patch(
        "jailbee.pr_outbox._publication_progress", return_value=Progress(frozenset({0}), {})
    )
    choose = mocker.Mock(return_value=mode)
    from jailbee.pr_outbox import ApplyOutcome

    applied = mocker.patch(
        "jailbee.pr_outbox.apply_manifest", return_value=ApplyOutcome((), (), None)
    )
    mocker.patch("jailbee.pr_outbox.finalize")
    from jailbee.pr_outbox import offer_pending_comments

    assert (
        offer_pending_comments(
            cfg,
            incus,
            "c",
            "feature",
            pr_number=None,
            manifest_names=tuple(files),
            confirm=lambda _: True,
            choose_mode=choose,
            publisher="user",
        )
        == 0
    )
    choose.assert_called_once_with(7, DraftPreview(7, 1, 1, True, draft_total=4))
    if mode is None:
        applied.assert_not_called()
    else:
        assert [call.args[3].manifest.name for call in applied.call_args_list] == [
            "one.json",
            "three.json",
        ]
        assert all(call.kwargs["indices"] == {1, 3} for call in applied.call_args_list)


_PINNED_PLAN = (
    "Container: c\nHead: abc1234\none.json  acme/widgets  PR #7\n\n"
    "action 0 \u00b7 REVIEW (COMMENT)\nreview\n\n"
    "action 1 \u00b7 REPLY to review comment #9\nreply\n\n"
    "action 2 \u00b7 COMMENT (general)\nheld back\n\n"
    "action 3 \u00b7 DESCRIPTION\ndescription\n\n"
)


def test_text_plan_unchanged_after_refactor(mocker, make_cfg, tmp_path, capsys):
    cfg, incus, _ = _draft_offer_setup(mocker, make_cfg, tmp_path)
    assert _selected_offer(cfg, incus, choose_mode=lambda *_: None) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        _PINNED_PLAN + "4 actions will be published to GitHub as octocat.\n"
        "As a draft: 2 go into your pending review on PR #7; "
        "1 general comment(s) held back; the description is published.\n"
        "Nothing published.\n"
    )


def test_text_plan_unchanged_for_preselected_draft(mocker, make_cfg, tmp_path, capsys):
    cfg, incus, _ = _draft_offer_setup(mocker, make_cfg, tmp_path)
    assert _selected_offer(cfg, incus, draft=True, confirm=lambda n: False) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        _PINNED_PLAN + "3 actions will be published to GitHub as octocat.\n"
        "  action 0: pending (in your pending review)\n"
        "  action 1: pending (in your pending review)\n"
        "  held back in draft mode: action 2 (general comment)\n"
        "  action 3: published (description)\n"
        "As a draft: 2 go into your pending review on PR #7; "
        "1 general comment(s) held back; the description is published.\n"
        "Nothing published.\n"
    )


def _plan_offer(cfg, incus, **kwargs):
    plans = []
    kwargs.setdefault("dry_run", True)
    result = _selected_offer(cfg, incus, plan_sink=plans.append, **kwargs)
    return result, plans


def test_plan_sink_receives_one_json_and_prints_nothing(mocker, make_cfg, tmp_path, capsys):
    cfg, incus, _ = _draft_offer_setup(mocker, make_cfg, tmp_path)
    confirm = mocker.Mock(side_effect=AssertionError("no question"))
    result, plans = _plan_offer(cfg, incus, confirm=confirm)
    captured = capsys.readouterr()
    assert (result, captured.out, captured.err) == (0, "", "")
    (plan,) = plans
    assert plan["schema"] == 1 and plan["kind"] == "pr" and plan["proposal"] == "pr/one.json"
    assert plan["login"] == "octocat"
    assert [(a["kind"], a["will"], a["will_draft"]) for a in plan["actions"]] == [
        ("review", "publish", "draft"),
        ("reply", "publish", "draft"),
        ("comment", "publish", "held_back"),
        ("description", "publish", "publish"),
    ]
    assert plan["actions"][0]["target"] == "acme/widgets#7"
    assert (plan["total"], plan["total_draft"], plan["draftable"]) == (4, 3, True)
    assert (plan["foreign"], plan["warnings"], plan["refusals"], plan["notes"]) == (
        False,
        [],
        [],
        [],
    )


def test_plan_json_carries_the_inspection_revision(mocker, make_cfg, tmp_path):
    cfg, incus, identity, store, *_ = _selected_setup(mocker, make_cfg, tmp_path)
    _, plans = _plan_offer(cfg, incus)
    assert plans[0]["revision"] == _revision(tmp_path, identity, store)


def test_plan_json_already_published_index(mocker, make_cfg, tmp_path):
    text = _manifest_text(
        actions=[
            {"type": "comment", "body": "a"},
            {"type": "comment", "body": "b"},
            {"type": "comment", "body": "c"},
            {"type": "comment", "body": "d"},
        ]
    )
    files = {"one.json": text, "one.json.progress.json": '{"applied": [0], "urls": {"0": "u"}}'}
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path, files=files)
    _, plans = _plan_offer(cfg, incus)
    actions = plans[0]["actions"]
    assert actions[0]["will"] == "already_published"
    assert [a["will"] for a in actions[1:]] == ["publish"] * 3
    assert plans[0]["total"] == 3


def test_plan_json_resolved_foreign_target(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import foreign_warning

    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path)
    incus.config_get.side_effect = lambda c, key: "9999" if key == "user.jailbee.pr" else None
    _, plans = _plan_offer(cfg, incus, allow_foreign=True)
    plan = plans[0]
    assert plan["foreign"] is True
    # The warning text is derived from the same gated target.
    assert plan["warnings"][0] == foreign_warning(
        _resolved_foreign_target(mocker, cfg, incus), "feature"
    )


def _resolved_foreign_target(mocker, cfg, incus):
    from jailbee.pr_outbox import parse_manifest, resolve_target

    manifest = parse_manifest("one.json", _manifest_text(), {})
    return resolve_target(cfg, incus, "c", manifest, force=False, allow_foreign=True)


def test_plan_json_not_draftable(mocker, make_cfg, tmp_path):
    text = _manifest_text(
        actions=[{"type": "comment", "body": "c"}, {"type": "description", "body": "d"}]
    )
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path, files={"one.json": text})
    _, plans = _plan_offer(cfg, incus)
    plan = plans[0]
    assert plan["draftable"] is False and plan["total_draft"] == 0
    assert [a["will_draft"] for a in plan["actions"]] == [None, None]


def test_plan_json_reports_a_refusal(mocker, make_cfg, tmp_path):
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path)
    incus.config_get.side_effect = lambda c, key: "9999" if key == "user.jailbee.pr" else None
    result, plans = _plan_offer(cfg, incus)
    assert result == 1
    assert plans[0]["actions"] == [] and len(plans[0]["refusals"]) == 1
    assert plans[0]["refusals"][0]["stale"] is False


def test_plan_sink_requires_dry_run(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import OutboxError

    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path)
    with pytest.raises(OutboxError, match="plan output requires a dry run"):
        _selected_offer(cfg, incus, plan_sink=lambda plan: None, raise_errors=True)


_REVIEW = {"type": "review", "body": "r", "comments": []}


def test_plan_json_stale_flag_is_structural(mocker, make_cfg, tmp_path):
    text = _manifest_text(head_sha="old0000", actions=[_REVIEW])
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path, files={"one.json": text})
    result, plans = _plan_offer(cfg, incus)
    assert result == 1
    assert plans[0]["refusals"][0]["stale"] is True


def test_plan_json_container_text_cannot_set_the_stale_flag(mocker, make_cfg, tmp_path):
    text = _manifest_text(actions=[{"type": "comment", "body_file": "../head moved"}])
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path, files={"one.json": text})
    result, plans = _plan_offer(cfg, incus)
    assert result == 1
    (refusal,) = plans[0]["refusals"]
    assert "head moved" in refusal["reason"] and refusal["stale"] is False


def test_plan_json_warns_about_a_moved_head_when_forced(mocker, make_cfg, tmp_path):
    text = _manifest_text(head_sha="old0000", actions=[_REVIEW])
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path, files={"one.json": text})
    _, plans = _plan_offer(cfg, incus, force=True)
    assert "the PR head has moved since this was written" in plans[0]["warnings"]


def test_plan_json_applied_review_is_already_published_in_a_draft(mocker, make_cfg, tmp_path):
    text = _manifest_text(actions=[_REVIEW, {"type": "reply", "comment_id": 9, "body": "x"}])
    files = {"one.json": text, "one.json.progress.json": '{"applied": [0], "urls": {"0": "u"}}'}
    cfg, incus, *_ = _selected_setup(mocker, make_cfg, tmp_path, files=files)
    _, plans = _plan_offer(cfg, incus)
    plan = plans[0]
    assert plan["draftable"] is True
    assert [(a["will"], a["will_draft"]) for a in plan["actions"]] == [
        ("already_published", "already_published"),
        ("publish", "draft"),
    ]


def test_branch_binding_owns_numbered_manifest(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Manifest, resolve_target

    binding = {"a": {"head": "a", "pr": 42, "base": "main", "source": "s", "source_sha": "x"}}
    incus = _target_setup(
        mocker, tmp_path, labels={"user.jailbee.branch_prs": json.dumps(binding)}, pr=_pr_info(42)
    )
    target = resolve_target(
        make_cfg(tmp_path), incus, "c", Manifest("m", "acme/widgets", 42, None), force=False
    )
    assert target.pr.number == 42 and not target.foreign


def test_branch_binding_does_not_change_legacy_null_resolution(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import resolve_target

    binding = {"a": {"head": "a", "pr": 42, "base": "main", "source": "s", "source_sha": "x"}}
    incus = _target_setup(
        mocker,
        tmp_path,
        labels={"user.jailbee.pr": "10", "user.jailbee.branch_prs": json.dumps(binding)},
        pr=_pr_info(10),
    )
    assert (
        resolve_target(make_cfg(tmp_path), incus, "c", _null_pr_manifest(), force=False).pr.number
        == 10
    )


def test_source_null_manifest_does_not_resolve_own_pr(mocker, make_cfg, tmp_path):
    from dataclasses import replace

    from jailbee.pr_outbox import resolve_target

    binding = {"a": {"head": "a", "pr": 42, "base": "main", "source": "s", "source_sha": "x"}}
    incus = _target_setup(
        mocker,
        tmp_path,
        labels={"user.jailbee.pr": "10", "user.jailbee.branch_prs": json.dumps(binding)},
    )
    manifest = replace(_null_pr_manifest(), source="topic", source_sha="a" * 40, base="main")
    assert resolve_target(make_cfg(tmp_path), incus, "c", manifest, force=False).pr is None
    from jailbee import pr

    pr.resolve_pr.assert_not_called()


def test_source_manifest_refuses_submodule_scope(mocker, make_cfg, tmp_path):
    from jailbee.pr_outbox import Manifest, ManifestError, resolve_target

    cfg, incus, _, _ = _submodule_gate_setup(mocker, make_cfg, tmp_path)
    manifest = Manifest("m", "acme/library", None, None, source="topic")
    with pytest.raises(ManifestError, match="source manifests are not supported for submodules"):
        resolve_target(cfg, incus, "c", manifest, force=False)


def _sent_apply(mocker, make_cfg, tmp_path, actions, *, progress=None, publisher="user", **kw):
    """Stage `actions` as proposal rv.json (unless it is already stored) and publish it."""
    from jailbee.pr_outbox import (
        Progress,
        Target,
        apply_manifest,
        parse_manifest,
        proposal_files_digest,
    )
    from tests.pr_store_support import UUID, stored_names

    text = _manifest_text(actions=actions)
    incus, _ = _stored_outbox(
        mocker, {"rv.json": text}, container="app-feat", seed="rv.json" not in stored_names()
    )
    manifest = parse_manifest("rv.json", text, {})
    target = Target(manifest, _pr_info(number=7), False, PrScope(tmp_path, "origin", "", None))
    return apply_manifest(
        make_cfg(tmp_path),
        incus,
        "app-feat",
        target,
        progress or Progress(frozenset(), {}),
        uid=1000,
        publisher=publisher,
        uuid=UUID,
        digest=proposal_files_digest({"rv.json": text}, "rv.json"),
        **kw,
    )


_FOUR_ACTIONS = [
    {
        "type": "review",
        "body": "overall",
        "comments": [{"path": "a.py", "line": 3, "body": "nit"}],
    },
    {"type": "reply", "comment_id": 9, "body": "reply body"},
    {"type": "comment", "body": "general"},
    {"type": "description", "title": "New title", "body": "New body"},
]


def test_apply_manifest_records_each_action_in_sent_log(mocker, make_cfg, tmp_path):
    from tests.pr_store_support import sent_records

    _apply_mocks(mocker)
    outcome = _sent_apply(mocker, make_cfg, tmp_path, _FOUR_ACTIONS)
    assert outcome.failure is None
    rows = {r.index: r for r in sent_records()}
    assert sorted(rows) == [0, 1, 2, 3]
    assert [rows[i].kind for i in range(4)] == ["review", "reply", "comment", "description"]
    assert all(r.target == 7 and r.repo == "acme/widgets" for r in rows.values())
    assert all(r.publisher == "user" and r.state == "applied" for r in rows.values())
    assert all(r.outbox == "review" and r.manifest == "rv.json" for r in rows.values())
    assert all(r.container == "app-feat" and r.uuid == "uuid-test" for r in rows.values())
    assert rows[0].url == "https://x/r"
    assert rows[0].content == "overall\n\na.py:3\nnit"
    assert rows[1].url == "https://x/p" and rows[1].content == "reply body"
    assert rows[2].url == "https://x/c" and rows[2].content == "general"
    assert rows[3].title == "New title" and rows[3].content == "New body"


def test_description_edit_row_has_pr_url(mocker, make_cfg, tmp_path):
    from tests.pr_store_support import sent_records

    _apply_mocks(mocker)  # edit_pr returns "" : no receipt of its own
    _sent_apply(mocker, make_cfg, tmp_path, [{"type": "description", "body": "text"}])
    (row,) = sent_records()
    assert row.kind == "description"
    assert row.url == "https://github.com/acme/widgets/pull/7"


def test_resumed_publish_records_each_action_once(mocker, make_cfg, tmp_path):
    from jailbee.outbox_io import JournalError
    from jailbee.pr import PrError
    from tests.pr_store_support import sent_records

    calls = _apply_mocks(mocker)
    calls["comment"].side_effect = PrError("rate limited")
    first = _sent_apply(mocker, make_cfg, tmp_path, _FOUR_ACTIONS)
    assert first.applied == (0, 1) and first.failure
    assert sorted(r.index for r in sent_records()) == [0, 1]

    calls["comment"].side_effect = None
    calls["comment"].return_value = "https://x/c"
    # The journal alone says what landed: no progress is passed in.
    second = _sent_apply(mocker, make_cfg, tmp_path, _FOUR_ACTIONS)
    assert second.applied == (2, 3)
    assert sorted(r.index for r in sent_records()) == [0, 1, 2, 3]

    # The finished proposal's journal is archived: the same text cannot go out again.
    with pytest.raises(JournalError, match="already published"):
        _sent_apply(mocker, make_cfg, tmp_path, _FOUR_ACTIONS)
    assert sorted(r.index for r in sent_records()) == [0, 1, 2, 3]
    assert calls["review"].call_count == 1


def test_failed_action_is_not_recorded(mocker, make_cfg, tmp_path):
    from jailbee.pr import PrError
    from tests.pr_store_support import journal, sent_records, stored_names

    calls = _apply_mocks(mocker)
    calls["review"].side_effect = PrError("nope")
    assert _sent_apply(mocker, make_cfg, tmp_path, _FOUR_ACTIONS[:1]).failure
    assert sent_records() == []
    recorded = journal("rv.json")
    assert recorded is not None and recorded.actions == (), "a refused action stays pending"
    assert stored_names() == ["rv.json"]


def test_review_publish_survives_sent_construction_failure(mocker, make_cfg, tmp_path):
    from tests.pr_store_support import journal, sent_records, stored_names

    _apply_mocks(mocker)
    mocker.patch("jailbee.pr_outbox._sent_action", side_effect=OSError("snapshot unavailable"))
    outcome = _sent_apply(mocker, make_cfg, tmp_path, _FOUR_ACTIONS)
    assert outcome.failure is None and outcome.applied == (0, 1, 2, 3)
    assert outcome.urls == ("https://x/r", "https://x/p", "https://x/c", "")
    # The outcomes were journaled (and the proposal finished) without the sent rows.
    assert sent_records() == []
    assert stored_names() == []
    assert journal("rv.json") is None


def test_reused_review_manifest_name_keeps_earlier_sent_rows(mocker, make_cfg, tmp_path):
    from tests.pr_store_support import sent_records

    _apply_mocks(mocker)
    _sent_apply(mocker, make_cfg, tmp_path, [{"type": "comment", "body": "first"}])
    _sent_apply(mocker, make_cfg, tmp_path, [{"type": "comment", "body": "second"}])
    rows = sorted(sent_records(), key=lambda r: r.id)
    assert [r.content for r in rows] == ["first", "second"]
