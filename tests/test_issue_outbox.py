from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

# Captured before fixtures replace it, so one test can drive the real reader.


def _write_gitdir(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


def test_resolve_repo_targets_uses_only_the_host_declared_repository_tree(
    tmp_path, mocker, make_cfg
):
    from jailbee.issue_outbox import resolve_repo_targets
    from jailbee.submodules import DeclaredSubmodule

    cfg = make_cfg(tmp_path)
    parser_root = tmp_path / "libs/parser"
    lexer_root = parser_root / "vendor/lexer"
    _write_gitdir(parser_root)
    _write_gitdir(lexer_root)
    _write_gitdir(tmp_path / "container-only")
    mocker.patch(
        "jailbee.issue_outbox.submodules.declared_submodule_remotes",
        return_value=(
            DeclaredSubmodule("libs/parser", "../parser-declaration.git"),
            DeclaredSubmodule(
                "libs/parser/vendor/lexer", "https://github.com/acme/lexer-declaration.git"
            ),
        ),
    )
    mocker.patch(
        "jailbee.issue_outbox.git.detect_upstream_remote",
        side_effect=lambda root: {
            parser_root: "canonical",
            lexer_root: "origin",
        }[root],
    )
    mocker.patch(
        "jailbee.issue_outbox.git.get_remote_url",
        side_effect=lambda root, remote: {
            (tmp_path, "origin"): "https://github.com/acme/app.git",
            (parser_root, "canonical"): "git@github.com:acme/parser.git",
            (lexer_root, "origin"): "ssh://git@github.com/acme/lexer.git",
        }[(root, remote)],
    )

    targets = resolve_repo_targets(cfg)

    assert set(targets) == {".", "libs/parser", "libs/parser/vendor/lexer"}
    assert targets["."] == targets["."].__class__(".", tmp_path, "acme/app")
    assert targets["libs/parser"].slug == "acme/parser"
    assert targets["libs/parser/vendor/lexer"].slug == "acme/lexer"
    assert "container-only" not in targets


def test_resolve_repo_targets_uses_relative_declaration_for_uninitialized_leaf(
    tmp_path, mocker, make_cfg
):
    from jailbee.issue_outbox import RepoTarget, resolve_repo_targets
    from jailbee.submodules import DeclaredSubmodule

    cfg = make_cfg(tmp_path)
    mocker.patch(
        "jailbee.issue_outbox.submodules.declared_submodule_remotes",
        return_value=(DeclaredSubmodule("libs/parser", "../parser.git"),),
    )
    mocker.patch(
        "jailbee.issue_outbox.git.get_remote_url",
        return_value="https://github.com/acme/app.git",
    )
    detect = mocker.patch("jailbee.issue_outbox.git.detect_upstream_remote")

    targets = resolve_repo_targets(cfg)

    assert targets["libs/parser"] == RepoTarget(
        "libs/parser", tmp_path / "libs/parser", "acme/parser"
    )
    detect.assert_not_called()


def test_resolve_repo_targets_uses_the_exact_declaring_parent_for_overlapping_paths(
    tmp_path, mocker, make_cfg
):
    from jailbee.issue_outbox import resolve_repo_targets

    cfg = make_cfg(tmp_path)
    (tmp_path / ".gitmodules").write_text(
        '[submodule "vendor"]\n'
        "\tpath = vendor\n"
        "\turl = git@github.com:other/vendor.git\n"
        '[submodule "plugin"]\n'
        "\tpath = vendor/plugin\n"
        "\turl = ../plugin.git\n"
    )
    mocker.patch(
        "jailbee.submodules.git.run_capture",
        return_value=(
            True,
            "submodule.vendor.path vendor\n"
            "submodule.vendor.url git@github.com:other/vendor.git\n"
            "submodule.plugin.path vendor/plugin\n"
            "submodule.plugin.url ../plugin.git\n",
        ),
    )
    mocker.patch(
        "jailbee.issue_outbox.git.get_remote_url",
        return_value="https://github.com/acme/app.git",
    )

    targets = resolve_repo_targets(cfg)

    assert targets["vendor"].slug == "other/vendor"
    assert targets["vendor/plugin"].slug == "acme/plugin"


@pytest.mark.parametrize(
    ("root_url", "declared", "message"),
    [
        ("https://gitlab.com/acme/app.git", (), "superproject.*GitHub"),
        (
            "https://github.com/acme/app.git",
            (("libs/parser", "https://gitlab.com/acme/parser.git"),),
            "libs/parser.*GitHub",
        ),
        (
            "https://github.com/acme/app.git",
            (("libs/parser", "../../../../parser.git"),),
            "libs/parser.*GitHub",
        ),
    ],
)
def test_resolve_repo_targets_rejects_non_github_or_unresolvable_remotes(
    tmp_path, mocker, make_cfg, root_url, declared, message
):
    from jailbee.issue_outbox import resolve_repo_targets
    from jailbee.submodules import DeclaredSubmodule

    cfg = make_cfg(tmp_path)
    mocker.patch(
        "jailbee.issue_outbox.submodules.declared_submodule_remotes",
        return_value=tuple(DeclaredSubmodule(*entry) for entry in declared),
    )
    mocker.patch("jailbee.issue_outbox.git.get_remote_url", return_value=root_url)

    with pytest.raises(ValueError, match=message):
        resolve_repo_targets(cfg)


def test_resolve_repo_targets_does_not_fall_back_to_declaration_for_initialized_repo(
    tmp_path, mocker, make_cfg
):
    from jailbee.issue_outbox import resolve_repo_targets
    from jailbee.submodules import DeclaredSubmodule

    cfg = make_cfg(tmp_path)
    parser_root = tmp_path / "libs/parser"
    _write_gitdir(parser_root)
    mocker.patch(
        "jailbee.issue_outbox.submodules.declared_submodule_remotes",
        return_value=(DeclaredSubmodule("libs/parser", "https://github.com/acme/parser.git"),),
    )
    mocker.patch(
        "jailbee.issue_outbox.git.get_remote_url",
        side_effect=["https://github.com/acme/app.git", None],
    )
    mocker.patch("jailbee.issue_outbox.git.detect_upstream_remote", return_value="upstream")

    with pytest.raises(ValueError, match=r"libs/parser.*upstream remote"):
        resolve_repo_targets(cfg)


def _comment(repo=".", issue=7, body="A comment"):
    return {"type": "comment", "repo": repo, "issue": issue, "body": body}


def _edit(**changes):
    return {"type": "edit", "repo": ".", "issue": 7, **changes}


def _create(repo=".", ref="new", labels=()):
    return {
        "type": "create",
        "repo": repo,
        "ref": ref,
        "title": "New title",
        "body": "Full [body]\nSecond line",
        "labels": list(labels),
    }


def _labels(**changes):
    return {
        "type": "labels",
        "repo": ".",
        "issue": 7,
        "add": ["feature"],
        "remove": ["bug"],
        "expected": {"labels": ["BUG"]},
        **changes,
    }


def _assignees_receipt(*logins):
    from jailbee.issue_github import AssigneesReceipt

    return AssigneesReceipt(7, "https://github.com/acme/app/issues/7", tuple(logins))


def _assign(**changes):
    return {
        "type": "assignees",
        "repo": ".",
        "issue": 7,
        "add": ["@me", "bob"],
        "remove": ["carol"],
        "expected": {"assignees": ["carol"]},
        **changes,
    }


def _state():
    return {
        "type": "state",
        "repo": ".",
        "issue": 7,
        "state": "closed",
        "reason": "completed",
        "expected": {"state": "open"},
    }


def test_project_add_prepare_allows_pr_and_revalidates_membership(preflight, mocker):
    from dataclasses import replace

    from jailbee import issue_outbox, issue_projects

    preflight["fetch"].return_value = replace(preflight["snapshot"], is_pull_request=True)
    mocker.patch.object(
        issue_projects,
        "resolve_project",
        return_value=issue_projects.Project("acme/3", "P3", "Roadmap"),
    )
    read = mocker.patch.object(
        issue_projects,
        "get_target",
        return_value=issue_projects.ProjectTarget("I7", "https://github.com/acme/app/pull/7", None),
    )
    batch = preflight["prepare"](
        {
            "001-board.json": [
                {
                    "type": "project_add",
                    "repo": ".",
                    "issue": 7,
                    "project": "acme/3",
                    "expected": {"present": False},
                }
            ]
        }
    )
    assert any('project acme/3 "Roadmap"' in line for line in issue_outbox.plan_lines(batch))
    from jailbee.outbox.publish import issue_plan_json

    plan = issue_plan_json(batch, proposal="issue/001-board.json", revision="r1")
    assert plan["actions"][0]["project_title"] == "Roadmap"
    assert plan["actions"][0]["expected"] == {"present": False}
    read.return_value = issue_projects.ProjectTarget(
        "I7", "https://github.com/acme/app/pull/7", "ITEM"
    )
    with pytest.raises(issue_outbox.IssueStaleError, match="membership"):
        issue_outbox.revalidate_batch(batch)
    with pytest.raises(issue_outbox.IssueGateError, match="membership"):
        preflight["prepare"](
            {
                "001-board.json": [
                    {
                        "type": "project_add",
                        "repo": ".",
                        "issue": 7,
                        "project": "acme/3",
                        "expected": {"present": False},
                    }
                ]
            }
        )


@pytest.fixture
def preflight(tmp_path, mocker, make_cfg):
    from jailbee import issue_github, issue_outbox
    from jailbee.outbox_io import ContainerIdentity
    from tests.issue_store_support import instance, seed

    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.list_containers.return_value = [instance()]
    targets = {
        ".": issue_outbox.RepoTarget(".", tmp_path, "acme/app"),
        "lib": issue_outbox.RepoTarget("lib", tmp_path / "lib", "acme/lib"),
    }
    mocker.patch.object(issue_outbox, "resolve_repo_targets", return_value=targets)
    reader = mocker.Mock(return_value={})
    real_store_outbox = issue_outbox.store_outbox

    def store_outbox(uuid):
        reader()  # the seam a test breaks to fail the store read
        return real_store_outbox(uuid)

    mocker.patch.object(issue_outbox, "store_outbox", side_effect=store_outbox)
    login = mocker.patch.object(issue_github, "current_login", return_value="alice")
    labels = mocker.patch.object(
        issue_github, "list_labels", return_value={"bug": "Bug", "feature": "Feature"}
    )
    snapshot = issue_github.IssueSnapshot(
        7, "Old title", "Old body", ("Bug",), "open", "https://github.com/acme/app/issues/7", False
    )
    fetch = mocker.patch.object(issue_github, "get_issue", return_value=snapshot)
    mutations = [
        mocker.patch.object(issue_github, name)
        for name in (
            "create_issue",
            "edit_issue",
            "replace_labels",
            "add_comment",
            "set_state",
            "add_assignees",
            "remove_assignees",
        )
    ]
    store = _StoreJournals(ContainerIdentity("test-box", "2026-09-18"))

    def prepare(manifests, names=None, extras=None):
        reader.return_value = {
            name: json.dumps({"version": 1, "actions": actions})
            for name, actions in manifests.items()
        }
        reader.return_value.update(extras or {})
        seed(reader.return_value)
        return issue_outbox.prepare_batch(
            cfg, incus, "test-box", list(manifests) if names is None else names
        )

    yield {
        "prepare": prepare,
        "reader": reader,
        "fetch": fetch,
        "labels": labels,
        "snapshot": snapshot,
        "store": store,
        "incus": incus,
        "login": login,
        "identity": ContainerIdentity("test-box", "2026-09-18"),
        "targets": targets,
    }
    for mutation in mutations:
        mutation.assert_not_called()


@pytest.mark.parametrize("count", [20, 21])
def test_preflight_manifest_cap(preflight, count):
    from jailbee.issue_outbox import IssueGateError

    manifests = {f"{i}.json": [_comment()] for i in range(count)}
    if count == 21:
        with pytest.raises(IssueGateError, match="20 manifests"):
            preflight["prepare"](manifests)
        preflight["fetch"].assert_not_called()
    else:
        assert len(preflight["prepare"](manifests).manifests) == 20


@pytest.mark.parametrize("count", [100, 101])
def test_preflight_offer_action_cap(preflight, count):
    from jailbee.issue_outbox import IssueGateError

    manifests = {
        "a.json": [_comment()] * 50,
        "b.json": [_comment()] * 49,
        "c.json": [_comment()] * (count - 99),
    }
    if count == 101:
        with pytest.raises(IssueGateError, match="100 actions"):
            preflight["prepare"](manifests)
        preflight["fetch"].assert_not_called()
    else:
        assert sum(len(m.actions) for m in preflight["prepare"](manifests).manifests) == 100


def test_preflight_collects_bad_manifests_and_forbidden_paths_before_github(preflight):
    from jailbee.issue_outbox import IssueGateError

    with pytest.raises(IssueGateError) as caught:
        preflight["prepare"](
            {
                "bad.json": [],
                "forbidden.json": [_comment("untrusted")],
                "other.json": [_comment("secret")],
            },
            names=["bad.json", "missing.json", "forbidden.json", "other.json"],
        )
    message = str(caught.value)
    for text in ("bad.json", "missing.json", "untrusted", "secret", "known paths", "'.'", "'lib'"):
        assert text in message
    preflight["login"].assert_not_called()
    preflight["fetch"].assert_not_called()


def test_preflight_caches_issues_and_canonicalizes_labels_read_only(preflight):
    batch = preflight["prepare"](
        {
            "b.json": [_create(labels=["BUG"]), _labels(), _comment()],
            "a.json": [_comment(), _comment("lib")],
        }
    )
    assert [m.manifest.name for m in batch.manifests] == ["b.json", "a.json"]
    assert batch.login == "alice"
    assert batch.manifests[0].actions[0].labels == ("Bug",)
    assert batch.manifests[0].actions[1].labels == ("Feature",)
    assert batch.manifests[1].actions[1].repo.slug == "acme/lib"
    assert preflight["fetch"].call_count == 2
    assert preflight["labels"].call_count == 1
    assert preflight["store"].rows() == ([], [])


@pytest.mark.parametrize("boundary", ["login", "fetch", "labels", "reader"])
def test_domain_transport_has_execution_type_and_original_cause(preflight, boundary):
    from jailbee.issue_github import IssueGithubReadError
    from jailbee.issue_outbox import IssueExecutionError, IssueGateError
    from jailbee.orchestration.store import StoreError

    failure = (
        StoreError("read failed") if boundary == "reader" else IssueGithubReadError("read failed")
    )
    preflight[boundary].side_effect = failure
    with pytest.raises(IssueExecutionError) as caught:
        preflight["prepare"]({"a.json": [_comment(), _create()]})
    assert isinstance(caught.value, IssueGateError)
    assert caught.value.__cause__ is failure


def test_domain_revalidation_transport_is_not_stale(preflight):
    from jailbee.issue_github import IssueGithubReadError
    from jailbee.issue_outbox import IssueExecutionError, revalidate_batch

    batch = preflight["prepare"]({"a.json": [_edit(title="New", expected={"title": "Old title"})]})
    failure = IssueGithubReadError("late read failed")
    preflight["fetch"].side_effect = failure
    with pytest.raises(IssueExecutionError) as caught:
        revalidate_batch(batch)
    assert caught.value.__cause__ is failure


def test_preflight_rejects_pull_requests_and_caches_failed_reads(preflight):
    from jailbee.issue_github import IssueGithubReadError
    from jailbee.issue_outbox import IssueGateError

    preflight["fetch"].side_effect = [
        replace(preflight["snapshot"], is_pull_request=True),
        IssueGithubReadError("unavailable"),
    ]
    with pytest.raises(IssueGateError) as caught:
        preflight["prepare"]({"a.json": [_comment(), _comment("lib"), _comment("lib")]})
    assert "pull request" in str(caught.value)
    assert "unavailable" in str(caught.value)
    assert preflight["fetch"].call_count == 2


def test_preflight_collects_each_expected_field_difference(preflight):
    from jailbee.issue_outbox import IssueGateError

    preflight["fetch"].return_value = replace(preflight["snapshot"], state="closed", labels=())
    with pytest.raises(IssueGateError) as caught:
        preflight["prepare"](
            {
                "a.json": [
                    _edit(
                        title="New", body="New body", expected={"title": "Wrong", "body": "Wrong"}
                    ),
                    _labels(),
                    _state(),
                ]
            }
        )
    for field in ("title", "body", "labels", "state"):
        assert f"expected.{field}" in str(caught.value)


@pytest.mark.parametrize(
    "action, message",
    [
        (_create(labels=["missing"]), "unknown label"),
        (_labels(add=["missing"]), "unknown label"),
        (_labels(remove=["Feature"], add=[]), "absent from expected"),
        (_labels(add=["bUG"], remove=[]), "already present"),
    ],
)
def test_preflight_validates_label_membership(preflight, action, message):
    from jailbee.issue_outbox import IssueGateError

    with pytest.raises(IssueGateError, match=message):
        preflight["prepare"]({"a.json": [action]})


def _StoreJournals(identity):  # noqa: N802 - kept as the fixture's constructor name
    from tests.issue_store_support import StoreJournals

    return StoreJournals(identity)


def _record(preflight, actions, *, state="applied", digest=None, issue=42, repo="acme/app"):
    from jailbee.outbox_io import proposal_digest
    from tests.outbox_journal_fake import journal_key

    text = json.dumps({"version": 1, "actions": actions})
    preflight["store"].record(
        "a.json",
        digest or proposal_digest("a.json", text, {}),
        len(actions),
        0,
        state=state,
        repo=repo,
        issue=issue,
        url="https://github.com/acme/app/issues/42",
    )
    return journal_key(preflight["identity"], "a.json")


def test_applied_create_restores_refs_and_skips_old_expectations(preflight):
    actions = [_create(), {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Next"}]
    _record(preflight, actions)
    batch = preflight["prepare"]({"a.json": actions})
    first, second = batch.manifests[0].actions
    assert first.status == "applied"
    assert second.issue.number == 42
    assert second.issue.ref == "new"
    assert preflight["fetch"].call_args.args[2] == 42


@pytest.mark.parametrize("state", ["applied", "uncertain", "prepared"])
def test_changed_digest_with_progress_is_refused(preflight, state):
    from jailbee.issue_outbox import IssueGateError

    _record(preflight, [_create()], state=state, digest="0" * 64)
    with pytest.raises(IssueGateError, match=r"changed.*progress"):
        preflight["prepare"]({"a.json": [_create()]})


def test_preflight_rejects_changed_host_repo_for_receipt(preflight):
    from jailbee.issue_outbox import IssueGateError

    _record(preflight, [_create()], repo="wrong/repo")
    with pytest.raises(IssueGateError, match=r"journal.*repo"):
        preflight["prepare"]({"a.json": [_create()]})


@pytest.mark.parametrize(
    "action",
    [
        _edit(title="New", expected={"title": "Old title"}),
        _edit(body="New", expected={"body": "Old body"}),
        _labels(),
        _state(),
    ],
)
def test_offer_rejects_same_resolved_field_across_manifests(preflight, action):
    from jailbee.issue_outbox import IssueGateError

    with pytest.raises(IssueGateError, match="conflict") as caught:
        preflight["prepare"]({"a.json": [action], "b.json": [action]})
    assert "a.json" in str(caught.value) and "b.json" in str(caught.value)


def test_offer_conflicts_include_restored_refs(preflight):
    from jailbee.issue_outbox import IssueGateError

    edit = _edit(title="New", expected={"title": "Old title"})
    ref_edit = {**edit, "issue_ref": "new"}
    del ref_edit["issue"]
    actions = [_create(), ref_edit]
    _record(preflight, actions, issue=7)
    with pytest.raises(IssueGateError, match="conflict"):
        preflight["prepare"]({"a.json": actions, "b.json": [edit]})


def test_pending_refs_are_manifest_local_and_do_not_fetch(preflight):
    actions = [
        _create(),
        {
            "type": "edit",
            "repo": ".",
            "issue_ref": "new",
            "body": "",
            "expected": {"body": "Full [body]\nSecond line"},
        },
    ]
    batch = preflight["prepare"]({"a.json": actions, "b.json": actions})
    from jailbee.issue_outbox import revalidate_batch

    revalidate_batch(batch)
    assert batch.manifests[0].actions[1].issue.number is None
    preflight["fetch"].assert_not_called()


@pytest.mark.parametrize(
    "field,value", [("title", "Changed"), ("body", "Changed"), ("labels", ()), ("state", "closed")]
)
def test_second_stale_pass_checks_each_mutated_field(preflight, field, value):
    from jailbee.issue_outbox import IssueStaleError, revalidate_batch

    batch = preflight["prepare"](
        {
            "a.json": [
                _edit(title="New", body="", expected={"title": "Old title", "body": "Old body"}),
                _labels(),
                _state(),
            ]
        }
    )
    preflight["fetch"].return_value = replace(preflight["snapshot"], **{field: value})
    with pytest.raises(IssueStaleError, match=f"expected.{field}"):
        revalidate_batch(batch)
    assert preflight["fetch"].call_count == 2


def test_second_stale_pass_ignores_unrelated_fields_and_comment_only_targets(preflight):
    from jailbee.issue_outbox import revalidate_batch

    batch = preflight["prepare"](
        {"a.json": [_edit(title="New", expected={"title": "Old title"}), _comment("lib")]}
    )
    preflight["fetch"].return_value = replace(preflight["snapshot"], body="Changed", state="closed")
    preflight["fetch"].reset_mock()
    revalidate_batch(batch)
    assert preflight["fetch"].call_count == 1
    assert preflight["fetch"].call_args.args[1:] == ("acme/app", 7)


def test_second_stale_pass_collects_all_differences(preflight):
    from jailbee.issue_outbox import IssueStaleError, revalidate_batch

    batch = preflight["prepare"](
        {
            "a.json": [
                _edit(title="New", body="", expected={"title": "Old title", "body": "Old body"}),
                _labels(),
                _state(),
            ]
        }
    )
    preflight["fetch"].return_value = replace(
        preflight["snapshot"], title="X", body="Y", labels=(), state="closed"
    )
    with pytest.raises(IssueStaleError) as caught:
        revalidate_batch(batch)
    assert all(
        f"expected.{field}" in str(caught.value) for field in ("title", "body", "labels", "state")
    )


def test_plan_lines_show_complete_two_repo_plan(preflight):
    from jailbee.issue_outbox import plan_lines

    batch = preflight["prepare"](
        {
            "b.json": [
                _edit(
                    title="New title", body="", expected={"title": "Old title", "body": "Old body"}
                ),
                _labels(),
                _state(),
                _comment(body="Full [comment]\nLast line"),
            ],
            "a.json": [
                _create("lib"),
                {"type": "comment", "repo": "lib", "issue_ref": "new", "body": "Follow up"},
            ],
        }
    )
    assert plan_lines(batch) == [
        "Host GitHub login: alice",
        "Container: test-box",
        "Manifest: b.json",
        "Repository: . (acme/app)",
        "b.json action 0: edit #7 [pending]",
        "  title before:",
        "    Old title",
        "  title after:",
        "    New title",
        "  body before:",
        "    Old body",
        "  body after:",
        "    [empty]",
        "b.json action 1: labels #7 [pending]",
        "  remove: bug",
        "  add: Feature",
        "  labels after: Feature",
        "b.json action 2: state #7 [pending]",
        "  state: open -> closed",
        "  reason: completed",
        "b.json action 3: comment #7 [pending]",
        "  body:",
        "    Full [comment]",
        "    Last line",
        "Manifest: a.json",
        "Repository: lib (acme/lib)",
        "a.json action 0: create ref new [pending]",
        "  title:",
        "    New title",
        "  body:",
        "    Full [body]",
        "    Second line",
        "  labels: [empty]",
        "a.json action 1: comment ref new [pending]",
        "  depends on create ref new",
        "  body:",
        "    Follow up",
    ]


@pytest.mark.parametrize(
    "state,display",
    [("applied", "applied; skip"), ("prepared", "uncertain"), ("uncertain", "uncertain")],
)
def test_plan_and_show_render_receipts_and_uncertainty(preflight, state, display):
    from jailbee.issue_outbox import plan_lines, show_lines

    actions = [_create()]
    _record(preflight, actions, state=state)
    before = preflight["store"].rows()
    batch = preflight["prepare"]({"a.json": actions})
    prepared = batch.manifests[0]
    for lines in (
        plan_lines(batch),
        show_lines(prepared.manifest, prepared.journal, batch.container),
    ):
        assert any(display in line for line in lines)
        assert "    Full [body]" in lines
        if state == "applied":
            assert any("https://github.com/acme/app/issues/42" in line for line in lines)
        if display == "uncertain":
            assert any(
                "jailbee issue resolve test-box a.json 0 "
                "(--applied --url <url> [--issue <n>] | --retry)" in line
                for line in lines
            ), lines
    assert preflight["store"].rows() == before


def test_expected_null_body_matches_normalized_empty_body(preflight):
    preflight["fetch"].return_value = replace(preflight["snapshot"], body="")
    batch = preflight["prepare"]({"a.json": [_edit(body="New", expected={"body": None})]})
    assert batch.manifests[0].actions[0].status == "pending"


def test_uncertain_local_create_blocks_dependent_remote_assumptions(preflight):
    from jailbee.issue_outbox import revalidate_batch

    actions = [
        _create(),
        {
            "type": "edit",
            "repo": ".",
            "issue_ref": "new",
            "title": "Next",
            "expected": {"title": "Externally resolved title"},
        },
    ]
    _record(preflight, actions, state="uncertain")
    batch = preflight["prepare"]({"a.json": actions})
    assert batch.manifests[0].actions[0].status == "uncertain"
    revalidate_batch(batch)
    preflight["fetch"].assert_not_called()


def test_unknown_labels_do_not_hide_invalid_remove_or_add_sets(preflight):
    from jailbee.issue_github import IssueGithubReadError
    from jailbee.issue_outbox import IssueGateError

    preflight["labels"].side_effect = IssueGithubReadError("label listing failed")
    with pytest.raises(IssueGateError) as caught:
        preflight["prepare"]({"a.json": [_labels(add=["BUG"], remove=["Other"])]})
    assert "label listing failed" in str(caught.value)
    assert "already present" in str(caught.value)
    assert "absent from expected" in str(caught.value)


def test_uninitialized_submodule_uses_existing_host_root_for_all_github_reads(preflight):
    from jailbee.issue_outbox import revalidate_batch

    target = preflight["targets"]["lib"]
    assert not target.repo_root.exists()
    batch = preflight["prepare"](
        {
            "a.json": [
                {**_edit(title="Next", expected={"title": "Old title"}), "repo": "lib"},
                _create("lib", labels=["bug"]),
            ]
        }
    )
    revalidate_batch(batch)
    root = preflight["targets"]["."].repo_root
    assert all(call.args == (root, "acme/lib", 7) for call in preflight["fetch"].call_args_list)
    preflight["labels"].assert_called_once_with(root, "acme/lib")
    preflight["login"].assert_called_once_with(root)
    assert batch.host_repo_root == root


def _alias_repositories(preflight):
    targets = preflight["targets"]
    targets["."] = replace(targets["."], slug="Acme/App")
    targets["lib"] = replace(targets["lib"], slug="acme/app")


def test_case_variant_repo_aliases_conflict_on_the_same_issue_field(preflight):
    from jailbee.issue_outbox import IssueGateError

    _alias_repositories(preflight)
    edit = _edit(title="Next", expected={"title": "Old title"})
    with pytest.raises(IssueGateError, match="conflict on title") as caught:
        preflight["prepare"]({"a.json": [edit], "b.json": [{**edit, "repo": "lib"}]})
    assert "a.json" in str(caught.value) and "b.json" in str(caught.value)


def test_case_variant_aliases_share_issue_and_label_caches_preserving_display(preflight):
    from jailbee.issue_outbox import plan_lines

    _alias_repositories(preflight)
    batch = preflight["prepare"](
        {
            "a.json": [_comment(), _create(labels=["bug"])],
            "b.json": [_comment("lib"), _create("lib", labels=["BUG"])],
        }
    )
    root = preflight["targets"]["."].repo_root
    preflight["fetch"].assert_called_once_with(root, "Acme/App", 7)
    preflight["labels"].assert_called_once_with(root, "Acme/App")
    assert tuple(batch.initial_issues) == (("acme/app", 7),)
    assert batch.manifests[1].actions[1].labels == ("Bug",)
    assert "Repository: . (Acme/App)" in plan_lines(batch)
    assert "Repository: lib (acme/app)" in plan_lines(batch)


@pytest.mark.parametrize("boundary", ["fetch", "labels"])
def test_case_variant_aliases_share_failed_read_caches(preflight, boundary):
    from jailbee.issue_github import IssueGithubReadError
    from jailbee.issue_outbox import IssueGateError

    _alias_repositories(preflight)
    preflight[boundary].side_effect = IssueGithubReadError("unavailable")
    with pytest.raises(IssueGateError, match="unavailable"):
        preflight["prepare"](
            {
                "a.json": [_comment(), _create()],
                "b.json": [_comment("lib"), _create("lib")],
            }
        )
    assert preflight[boundary].call_count == 1


def test_case_variant_aliases_share_second_stale_pass_snapshot(preflight):
    from jailbee.issue_outbox import IssueStaleError, revalidate_batch

    _alias_repositories(preflight)
    batch = preflight["prepare"](
        {
            "a.json": [_edit(title="Next", expected={"title": "Old title"})],
            "b.json": [_edit(repo="lib", body="Next", expected={"body": "Old body"})],
        }
    )
    preflight["fetch"].reset_mock()
    preflight["fetch"].return_value = replace(
        preflight["snapshot"], title="Changed", body="Changed"
    )
    with pytest.raises(IssueStaleError) as caught:
        revalidate_batch(batch)
    assert "expected.title" in str(caught.value) and "expected.body" in str(caught.value)
    preflight["fetch"].assert_called_once_with(batch.host_repo_root, "Acme/App", 7)


def test_case_variant_journal_repo_restores_created_ref(preflight):
    _alias_repositories(preflight)
    actions = [_create(), {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Next"}]
    _record(preflight, actions, repo="aCME/aPP")
    batch = preflight["prepare"]({"a.json": actions})
    first, second = batch.manifests[0].actions
    assert first.status == "applied"
    assert second.issue.number == 42
    assert second.issue.ref == "new"
    preflight["fetch"].assert_called_once_with(batch.host_repo_root, "Acme/App", 42)


def test_restored_ref_conflicts_with_numbered_case_variant_alias(preflight):
    from jailbee.issue_outbox import IssueGateError

    _alias_repositories(preflight)
    actions = [
        _create(),
        {
            "type": "edit",
            "repo": ".",
            "issue_ref": "new",
            "title": "Next",
            "expected": {"title": "Old title"},
        },
    ]
    _record(preflight, actions, issue=7, repo="ACME/APP")
    with pytest.raises(IssueGateError, match="conflict on title"):
        preflight["prepare"](
            {
                "a.json": actions,
                "b.json": [_edit(repo="lib", title="Next", expected={"title": "Old title"})],
            }
        )


@pytest.fixture
def execution(tmp_path, mocker):
    """Store-backed apply rig: proposals and journals in the isolated broker.db."""
    from jailbee import issue_github, issue_outbox
    from jailbee.issue_manifest import CreateAction, ExistingIssue, LabelsAction, parse_manifest
    from jailbee.outbox_io import ContainerIdentity, proposal_digest
    from tests.issue_store_support import LiveFiles, instance, seed
    from tests.outbox_journal_fake import journal_key

    identity = ContainerIdentity("test-box", "2026-09-18")
    store = _StoreJournals(identity)
    files = LiveFiles()
    incus = mocker.Mock()
    incus.list_containers.return_value = [instance()]
    mutations = {
        name: mocker.patch.object(
            issue_github,
            name,
            return_value=issue_github.MutationReceipt(7, "https://github.com/acme/app/issues/7"),
        )
        for name in (
            "create_issue",
            "edit_issue",
            "replace_labels",
            "add_comment",
            "set_state",
        )
    }
    for name, logins in (
        ("add_assignees", ("alice", "bob", "carol")),
        ("remove_assignees", ("alice", "bob")),
    ):
        mutations[name] = mocker.patch.object(
            issue_github, name, return_value=_assignees_receipt(*logins)
        )

    def batch(manifests, extras=None):
        staged = dict(extras or {})
        staged.update(
            {
                name: json.dumps({"version": 1, "actions": actions})
                for name, actions in manifests.items()
            }
        )
        seed(staged)
        current = dict(files)
        prepared = []
        for name in manifests:
            manifest = parse_manifest(name, current[name], current)
            digest = proposal_digest(
                name, current[name], {body: current[body] for body in manifest.body_files}
            )
            journal = store.load(journal_key(identity, name))
            receipts = {r.index: r for r in journal.actions} if journal else {}
            resolved = []
            for index, action in enumerate(manifest.actions):
                if isinstance(action, CreateAction):
                    issue = issue_outbox.ResolvedIssue(None, action.ref)
                elif isinstance(action.target, ExistingIssue):
                    issue = issue_outbox.ResolvedIssue(action.target.number)
                else:
                    issue = issue_outbox.ResolvedIssue(None, action.target.ref)
                slug = "acme/app" if action.repo == "." else "acme/parser"
                repo = issue_outbox.RepoTarget(action.repo, tmp_path / action.repo, slug)
                labels = (
                    action.labels
                    if isinstance(action, CreateAction)
                    else ("Feature",)
                    if isinstance(action, LabelsAction)
                    else None
                )
                receipt = receipts.get(index)
                status = (
                    "pending"
                    if receipt is None
                    else "applied"
                    if receipt.state == "applied"
                    else "uncertain"
                )
                resolved.append(
                    issue_outbox.ResolvedAction(index, action, repo, issue, status, labels)
                )
            prepared.append(
                issue_outbox.PreparedManifest(manifest, digest, journal, tuple(resolved))
            )
        return issue_outbox.PreparedBatch(
            "test-box",
            tmp_path,
            identity,
            "alice",
            issue_outbox.OutboxSnapshot(current),
            tuple(prepared),
            {},
            store.uuid,
        )

    return {
        "batch": batch,
        "files": files,
        "store": store,
        "identity": identity,
        "incus": incus,
        "mutations": mutations,
        "pubs": store.pubs,
    }


def _apply(execution, batch, **kwargs):
    from jailbee import issue_outbox

    kwargs.setdefault("publications", execution["pubs"])
    return issue_outbox.apply_batch(
        batch,
        incus=execution["incus"],
        container_prefix="app",
        publisher="user",
        **kwargs,
    )


def _seed_execution(execution, batch, *, index=0, state="applied", issue=901, repo=None):
    from tests.outbox_journal_fake import journal_key

    prepared = batch.manifests[0]
    repo = repo or prepared.actions[index].repo.slug
    execution["store"].record(
        prepared.manifest.name,
        prepared.digest,
        len(prepared.actions),
        index,
        state=state,
        repo=repo,
        issue=issue,
    )
    return journal_key(batch.identity, prepared.manifest.name)


def test_apply_executes_manifests_and_refs_in_displayed_order(execution):
    from jailbee.issue_github import MutationReceipt

    calls = []
    batch = execution["batch"](
        {
            "z.json": [
                _create(ref="cache-cleanup"),
                {"type": "comment", "repo": ".", "issue_ref": "cache-cleanup", "body": "Next"},
            ],
            "a.json": [_labels(repo="lib", issue=27)],
        }
    )

    def create(root, repo, **kwargs):
        assert root == batch.host_repo_root
        calls.append(("create", repo, "cache-cleanup"))
        return MutationReceipt(901, "https://github.com/acme/app/issues/901")

    def comment(root, repo, number, **kwargs):
        assert root == batch.host_repo_root
        calls.append(("comment", repo, number))
        return MutationReceipt(number, f"https://github.com/{repo}/issues/{number}#issuecomment-5")

    def labels(root, repo, number, **kwargs):
        assert root == batch.host_repo_root
        assert kwargs == {"labels": ("Feature",)}
        calls.append(("labels", repo, number))
        return MutationReceipt(number, f"https://github.com/{repo}/issues/{number}")

    execution["mutations"]["create_issue"].side_effect = create
    execution["mutations"]["add_comment"].side_effect = comment
    execution["mutations"]["replace_labels"].side_effect = labels

    report = _apply(execution, batch)

    assert calls == [
        ("create", "acme/app", "cache-cleanup"),
        ("comment", "acme/app", 901),
        ("labels", "acme/parser", 27),
    ]
    assert report.failure is None
    assert report.cleaned == ("z.json", "a.json")
    assert [(name, receipt.index) for name, receipt in report.applied] == [
        ("z.json", 0),
        ("z.json", 1),
        ("a.json", 0),
    ]


def test_apply_batch_calls_before_action_per_executed_action(execution):
    from jailbee import issue_outbox
    from jailbee.issue_github import MutationReceipt

    actions = [_labels(issue=7), _labels(issue=8), _labels(issue=9)]
    batch = execution["batch"]({"a.json": actions})
    _seed_execution(execution, batch)
    batch = execution["batch"]({"a.json": actions})
    events = []

    def mutation(root, repo, number, **kwargs):
        events.append("post")
        return MutationReceipt(number, f"https://github.com/{repo}/issues/{number}")

    execution["mutations"]["replace_labels"].side_effect = mutation
    report = issue_outbox.apply_batch(
        batch,
        incus=execution["incus"],
        before_action=lambda: events.append("hook"),
        container_prefix="app",
        publisher="user",
    )
    assert report.failure is None
    assert events == ["hook", "post", "hook", "post"]


def test_before_action_failure_does_not_leave_uncertain_receipt(execution):
    from jailbee import issue_outbox

    batch = execution["batch"]({"a.json": [_labels()]})

    def fail():
        raise RuntimeError("progress failed")

    with pytest.raises(RuntimeError, match="progress failed"):
        issue_outbox.apply_batch(
            batch,
            incus=execution["incus"],
            before_action=fail,
            container_prefix="app",
            publisher="user",
        )
    assert execution["mutations"]["replace_labels"].call_count == 0
    report = _apply(execution, batch)
    assert report.failure is None
    assert len(report.applied) == 1
    assert execution["mutations"]["replace_labels"].call_count == 1


def test_pending_action_count_matches_apply_batch_attempts(execution):
    from jailbee import issue_outbox

    actions = [_labels(issue=7), _labels(issue=8)]
    batch = execution["batch"]({"a.json": actions})
    _seed_execution(execution, batch)
    batch = execution["batch"]({"a.json": actions})
    count = issue_outbox.pending_action_count(batch)
    events = []
    report = issue_outbox.apply_batch(
        batch,
        incus=execution["incus"],
        before_action=lambda: events.append("hook"),
        container_prefix="app",
        publisher="user",
    )
    assert report.failure is None
    assert count == 1 == len(events)


@pytest.mark.parametrize(
    "action, payload",
    [
        (_edit(title="Next", expected={"title": "Old"}), {"title": "Next"}),
        (_edit(body="", expected={"body": "Old"}), {"body": ""}),
        (
            _edit(title="Next", body="", expected={"title": "Old", "body": "Old"}),
            {"title": "Next", "body": ""},
        ),
        (_labels(), {"labels": ["Feature"]}),
        (_state(), {"state": "closed", "state_reason": "completed"}),
        ({**_state(), "reason": "not_planned"}, {"state": "closed", "state_reason": "not_planned"}),
        (
            {
                "type": "state",
                "repo": ".",
                "issue": 7,
                "state": "open",
                "expected": {"state": "closed"},
            },
            {"state": "open", "state_reason": "reopened"},
        ),
    ],
)
def test_apply_dispatches_exact_single_patch(execution, mocker, action, payload):
    from jailbee import issue_github

    for name in ("edit_issue", "replace_labels", "set_state"):
        mocker.stop(execution["mutations"][name])
    run = mocker.patch.object(
        issue_github.subprocess,
        "run",
        return_value=mocker.Mock(
            returncode=0,
            stdout=json.dumps({"number": 7, "html_url": "https://github.com/acme/app/issues/7"}),
            stderr="",
        ),
    )
    batch = execution["batch"]({"a.json": [action]})

    assert _apply(execution, batch).failure is None

    assert run.call_count == 1
    assert run.call_args.args[0] == [
        "gh",
        "api",
        "repos/acme/app/issues/7",
        "--method",
        "PATCH",
        "--input",
        "-",
    ]
    assert json.loads(run.call_args.kwargs["input"]) == payload
    assert run.call_args.kwargs["cwd"] == batch.host_repo_root


@pytest.mark.parametrize("uncertain", [False, True])
def test_apply_stops_first_failure_and_preserves_only_known_progress(execution, uncertain):
    from jailbee.issue_github import IssueGithubMutationError
    from tests.outbox_journal_fake import journal_key

    batch = execution["batch"]({"a.json": [_comment(), _comment()], "b.json": [_comment()]})
    mutation = execution["mutations"]["add_comment"]
    mutation.side_effect = IssueGithubMutationError("token=private", uncertain=uncertain)
    report = _apply(execution, batch)
    assert mutation.call_count == 1
    assert report.failure.index == 0
    assert report.failure.uncertain is uncertain
    assert "private" not in report.failure.detail
    journal = execution["store"].load(journal_key(batch.identity, "a.json"))
    assert [action.state for action in journal.actions] == (["uncertain"] if uncertain else [])
    assert "a.json" in execution["files"] and "b.json" in execution["files"]
    if uncertain:
        assert _apply(execution, batch).failure.uncertain
        assert mutation.call_count == 1


def test_apply_restores_applied_create_ref_even_from_old_prepared_batch(execution):
    batch = execution["batch"](
        {
            "a.json": [
                _create(),
                {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Next"},
            ]
        }
    )
    _seed_execution(execution, batch, repo="ACME/APP")
    report = _apply(execution, batch)
    assert report.failure is None
    execution["mutations"]["create_issue"].assert_not_called()
    execution["mutations"]["add_comment"].assert_called_once_with(
        batch.host_repo_root, "acme/app", 901, body="Next"
    )
    assert report.skipped[0][1].issue == 901


@pytest.mark.parametrize("state", ["prepared", "uncertain"])
def test_apply_never_replays_unknown_outcomes(execution, state):
    batch = execution["batch"]({"a.json": [_create(), _comment()]})
    _seed_execution(execution, batch, state=state)
    report = _apply(execution, batch)
    assert report.failure.uncertain
    assert report.failure.index == 0
    assert all(not mutation.called for mutation in execution["mutations"].values())


@pytest.mark.parametrize("method", ["begin", "mark_prepared"])
def test_apply_journal_failure_before_dispatch_makes_no_remote_call(execution, mocker, method):
    from jailbee.outbox_io import JournalError

    batch = execution["batch"]({"a.json": [_comment()]})
    mocker.patch.object(execution["pubs"], method, side_effect=JournalError("disk failed"))
    report = _apply(execution, batch)
    assert report.failure is not None
    assert all(not mutation.called for mutation in execution["mutations"].values())


def test_apply_receipt_failure_reports_uncertainty_and_blocks_next_process(execution, mocker):
    from jailbee.orchestration.store import StoreError
    from tests.outbox_journal_fake import journal_key

    batch = execution["batch"]({"a.json": [_comment(), _comment()]})
    mocker.patch.object(execution["pubs"], "settle", side_effect=StoreError("disk failed"))
    report = _apply(execution, batch)
    assert report.failure.uncertain
    assert "succeeded" in report.failure.detail
    mocker.stopall()
    loaded = execution["store"].load(journal_key(batch.identity, "a.json"))
    assert loaded.actions[0].state == "uncertain"
    assert _apply(execution, batch).failure.uncertain
    assert execution["mutations"]["add_comment"].call_count == 1


@pytest.mark.parametrize("uncertain,method", [(False, "clear_prepared"), (True, "settle")])
def test_apply_failure_to_record_remote_failure_still_blocks_replay(
    execution, mocker, uncertain, method
):
    from jailbee.issue_github import IssueGithubMutationError
    from jailbee.outbox_io import JournalError

    batch = execution["batch"]({"a.json": [_comment()]})
    execution["mutations"]["add_comment"].side_effect = IssueGithubMutationError(
        "rejected", uncertain=uncertain
    )
    mocker.patch.object(execution["pubs"], method, side_effect=JournalError("disk failed"))
    assert _apply(execution, batch).failure.uncertain
    assert _apply(execution, batch).failure.uncertain
    assert execution["mutations"]["add_comment"].call_count == 1


@pytest.mark.parametrize("change", ["digest", "repo", "count", "identity", "missing", "body"])
def test_apply_rechecks_every_proposal_before_any_remote_mutation(execution, change):

    batch = execution["batch"]({"a.json": [_comment()], "b.json": [_comment()]})
    store = execution["store"]
    second = replace(batch, manifests=(batch.manifests[1],))
    if change == "digest":
        batch = replace(
            batch, manifests=(batch.manifests[0], replace(batch.manifests[1], digest="0" * 64))
        )
    elif change == "repo":
        # A previous run recorded b.json's only action as *applied* under a
        # different repo than the one this batch now resolves it to (e.g. the
        # repo's remote URL moved between preparing and applying). apply_batch
        # must not silently trust a stale applied receipt for the wrong repo.
        _seed_execution(execution, second, repo="wrong/repo")
    elif change == "count":
        store.create("b.json", batch.manifests[1].digest, 2)
    elif change == "identity":
        execution["incus"].list_containers.return_value[0]["created_at"] = "replacement"
    elif change == "missing":
        del execution["files"]["b.json"]
    else:
        execution["files"]["b.json"] += "\n"
    assert _apply(execution, batch).failure is not None
    assert all(not mutation.called for mutation in execution["mutations"].values())


def test_completed_batch_cannot_replay_after_archive(execution):
    batch = execution["batch"]({"a.json": [_comment()]})
    assert _apply(execution, batch).failure is None
    assert _apply(execution, batch).failure is not None
    assert execution["mutations"]["add_comment"].call_count == 1


def _reconcile(execution, batch, resolution, **changes):
    from jailbee import issue_outbox

    prepared = batch.manifests[0]
    arguments = {
        "uuid": batch.uuid,
        "index": 0,
        "resolution": resolution,
        "repo": prepared.actions[0].repo,
        "manifest": prepared.manifest,
        "digest": prepared.digest,
        **changes,
    }
    return issue_outbox.reconcile_action(**arguments)


def test_reconcile_create_accepts_matching_number_and_case_insensitive_repo(execution):
    from jailbee import issue_outbox

    batch = execution["batch"]({"a.json": [_create()]})
    _seed_execution(execution, batch, state="prepared", repo="ACME/APP")
    removed = _reconcile(
        execution,
        batch,
        issue_outbox.AppliedResolution("https://github.com/Acme/App/issues/123", issue=123),
    )
    # The resolution completed the proposal: it is removed and its journal archived.
    assert removed is True
    assert "a.json" not in execution["files"]
    _journals, actions = execution["store"].rows()
    (row,) = actions
    assert (row[5], row[8], row[7]) == ("applied", 123, "https://github.com/Acme/App/issues/123")


@pytest.mark.parametrize(
    "action, issue, url",
    [
        (_create(), None, "https://github.com/acme/app/issues/123"),
        (_create(), 124, "https://github.com/acme/app/issues/123"),
        (_create(), True, "https://github.com/acme/app/issues/1"),
        (_comment(), 7, "https://github.com/acme/app/issues/7"),
        (_comment(), None, "https://github.com/acme/app/issues/8"),
        (_comment(), None, "https://github.com/acme/other/issues/7"),
        (_comment(), None, "https://github.com.evil/acme/app/issues/7"),
        (_comment(), None, "http://github.com/acme/app/issues/7"),
        (_comment(), None, "https://user@github.com/acme/app/issues/7"),
        (_comment(), None, "https://github.com/acme/app/pull/7"),
        (_comment(), None, "https://github.com/acme/app/issues/7?secret=x"),
        (_comment(), None, "https://github.com/acme/app/issues/0"),
        (_comment(), None, "https://github.com/acme/app/issues/7\n"),
    ],
)
def test_reconcile_rejects_invalid_receipts_without_changing_journal(execution, action, issue, url):
    from jailbee import issue_outbox
    from jailbee.outbox_io import JournalError

    batch = execution["batch"]({"a.json": [action]})
    key = _seed_execution(execution, batch, state="uncertain")
    before = execution["store"].load(key)
    with pytest.raises(JournalError):
        _reconcile(execution, batch, issue_outbox.AppliedResolution(url, issue=issue))
    assert execution["store"].load(key) == before


@pytest.mark.parametrize(
    "change", ["digest", "identity", "name", "count", "repo", "index", "pending", "applied"]
)
def test_reconcile_rejects_stale_or_non_uncertain_targets(execution, change):
    from jailbee import issue_outbox
    from jailbee.outbox_io import JournalError

    batch = execution["batch"]({"a.json": [_comment(), _comment()]})
    key = _seed_execution(execution, batch, state="applied" if change == "applied" else "uncertain")
    before = execution["store"].load(key)
    kwargs = {}
    if change == "digest":
        kwargs["digest"] = "0" * 64
    elif change == "identity":
        kwargs["uuid"] = "uuid-of-another-incarnation"
    elif change == "name":
        kwargs["manifest"] = replace(batch.manifests[0].manifest, name="other.json")
    elif change == "count":
        # A different non-zero count: index 0 must stay in bounds so this
        # exercises the action-count mismatch check, not the bounds guard.
        kwargs["manifest"] = replace(
            batch.manifests[0].manifest, actions=batch.manifests[0].manifest.actions[:1]
        )
    elif change == "repo":
        kwargs["repo"] = replace(batch.manifests[0].actions[0].repo, slug="wrong/repo")
    elif change in ("index", "pending"):
        kwargs["index"] = 99 if change == "index" else 1
    match = "action count" if change == "count" else None
    with pytest.raises(JournalError, match=match):
        _reconcile(execution, batch, issue_outbox.RetryResolution(), **kwargs)
    assert execution["store"].load(key) == before


def test_reconcile_retry_deletes_only_selected_unknown_action(execution):
    from jailbee import issue_outbox

    batch = execution["batch"]({"a.json": [_comment(), _comment()]})
    key = _seed_execution(execution, batch)
    execution["store"].mark_prepared(key, 1, repo="acme/app")
    _reconcile(execution, batch, issue_outbox.RetryResolution(), index=1)
    assert [(r.index, r.state) for r in execution["store"].load(key).actions] == [(0, "applied")]


def test_reconcile_comment_url_and_restored_ref(execution):
    from jailbee import issue_outbox

    batch = execution["batch"](
        {
            "a.json": [
                _create(),
                {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Next"},
            ]
        }
    )
    key = _seed_execution(execution, batch)
    execution["store"].mark_prepared(key, 1, repo="acme/app")
    assert _reconcile(
        execution,
        batch,
        issue_outbox.AppliedResolution(
            "https://github.com/acme/app/issues/901#issuecomment-45", issue=None
        ),
        index=1,
    )
    _journals, actions = execution["store"].rows()
    assert [(row[4], row[5], row[8]) for row in actions] == [
        (0, "applied", 901),
        (1, "applied", None),
    ]


def test_preflight_allows_empty_journal_with_old_digest_and_action_count(preflight):
    from tests.outbox_journal_fake import journal_key

    key = journal_key(preflight["identity"], "a.json")
    preflight["store"].create("a.json", "0" * 64, 2)
    batch = preflight["prepare"]({"a.json": [_create()]})
    assert batch.manifests[0].journal is None
    assert batch.manifests[0].actions[0].status == "pending"
    assert preflight["store"].load(key).action_count == 2


def test_github_repo_resolves_to_a_foreign_target_and_reads_through_its_slug(preflight):
    batch = preflight["prepare"]({"a.json": [_comment("github:BerriAI/litellm", issue=9)]})

    target = batch.manifests[0].actions[0].repo
    assert (target.path, target.slug, target.foreign) == (
        "github:BerriAI/litellm",
        "BerriAI/litellm",
        True,
    )
    assert preflight["fetch"].call_args.args[1:] == ("BerriAI/litellm", 9)
    assert batch.foreign_slugs == ("BerriAI/litellm",)
    assert batch.foreign_pending == 1


def test_github_repo_naming_the_superproject_is_not_foreign(preflight):
    batch = preflight["prepare"]({"a.json": [_comment("github:ACME/App")]})

    assert batch.manifests[0].actions[0].repo is preflight["targets"]["."]
    assert batch.foreign_slugs == ()


def test_unknown_plain_path_is_still_forbidden_and_names_the_github_form(preflight):
    from jailbee.issue_outbox import IssueGateError

    with pytest.raises(IssueGateError, match="forbidden repo path 'acme/other'") as caught:
        preflight["prepare"]({"a.json": [_comment("acme/other")]})
    assert "github:<owner>/<name>" in str(caught.value)


def test_plan_lines_warn_under_a_foreign_repository_header_only(preflight):
    from jailbee.issue_outbox import FOREIGN_WARNING, plan_lines

    batch = preflight["prepare"](
        {"a.json": [_comment(), _comment("github:BerriAI/litellm", issue=9)]}
    )
    lines = plan_lines(batch)

    header = lines.index("Repository: github:BerriAI/litellm (BerriAI/litellm)")
    assert lines[header + 1] == FOREIGN_WARNING
    assert lines.count(FOREIGN_WARNING) == 1


def test_foreign_label_read_failure_does_not_refuse_the_batch(preflight):
    from jailbee.issue_github import IssueGithubReadError

    preflight["labels"].side_effect = IssueGithubReadError("label listing failed")
    batch = preflight["prepare"](
        {
            "a.json": [_create("github:BerriAI/litellm", labels=["bug"])],
            "b.json": [_comment()],
        }
    )

    foreign, local = batch.manifests
    assert foreign.actions[0].status == "pending"
    assert foreign.actions[0].labels == ("bug",)
    assert local.actions[0].status == "pending"
    assert preflight["labels"].call_count == 1


def test_foreign_labels_action_keeps_its_own_labels_when_listing_fails(preflight):
    from jailbee.issue_github import IssueGithubReadError

    preflight["labels"].side_effect = IssueGithubReadError("label listing failed")
    batch = preflight["prepare"]({"a.json": [_labels(repo="github:BerriAI/litellm", issue=9)]})
    assert batch.manifests[0].actions[0].labels == ("feature",)


def test_non_foreign_label_read_failure_still_refuses_the_batch(preflight):
    from jailbee.issue_github import IssueGithubReadError
    from jailbee.issue_outbox import IssueExecutionError

    preflight["labels"].side_effect = IssueGithubReadError("label listing failed")
    with pytest.raises(IssueExecutionError, match="label listing failed"):
        preflight["prepare"]({"a.json": [_create(labels=["bug"])]})


def test_foreign_slugs_ignore_applied_foreign_actions(preflight):
    actions = [_comment("github:BerriAI/litellm", issue=9), _comment()]
    _record(preflight, actions, repo="BerriAI/litellm", issue=9)  # receipt for action 0

    batch = preflight["prepare"]({"a.json": actions})
    assert batch.manifests[0].actions[0].status == "applied"
    assert batch.foreign_slugs == ()


def test_foreign_slugs_ignore_uncertain_foreign_actions(preflight):
    from jailbee.issue_outbox import foreign_consent

    actions = [_create("github:BerriAI/litellm", labels=["bug"])]
    _record(preflight, actions, state="uncertain", repo="BerriAI/litellm")

    batch = preflight["prepare"]({"a.json": actions})
    assert batch.manifests[0].actions[0].status == "uncertain"
    assert batch.foreign_slugs == ()
    assert batch.foreign_pending == 0
    assert foreign_consent(batch, allowed=False, ask=None) is True


def test_target_for_covers_known_foreign_aliased_and_forbidden(tmp_path):
    from jailbee.issue_outbox import RepoTarget, target_for

    root = RepoTarget(".", tmp_path, "acme/app")
    targets = {".": root}
    assert target_for(targets, ".", tmp_path) is root
    assert target_for(targets, "github:acme/APP", tmp_path) is root
    assert target_for(targets, "lib", tmp_path) is None
    foreign = target_for(targets, "github:o/r", tmp_path)
    assert foreign == RepoTarget("github:o/r", tmp_path, "o/r", foreign=True)


@pytest.mark.parametrize(
    ("allowed", "ask", "expected"),
    [(True, None, True), (False, lambda q: True, True), (False, lambda q: False, False)],
)
def test_foreign_consent_allows_asks_or_declines(preflight, allowed, ask, expected):
    from jailbee.issue_outbox import foreign_consent

    batch = preflight["prepare"]({"a.json": [_comment("github:BerriAI/litellm", issue=9)]})
    assert foreign_consent(batch, allowed=allowed, ask=ask) is expected


def test_foreign_consent_question_and_refusal_text(preflight):
    from jailbee.issue_outbox import ForeignRepoRefused, foreign_consent

    batch = preflight["prepare"]({"a.json": [_comment("github:BerriAI/litellm", issue=9)]})
    asked = []
    foreign_consent(batch, allowed=False, ask=lambda q: asked.append(q) or True)
    assert asked == ["Publish 1 action to foreign repositories BerriAI/litellm?"]
    with pytest.raises(ForeignRepoRefused, match=r"\(BerriAI/litellm\) without --foreign"):
        foreign_consent(batch, allowed=False, ask=None)


def test_foreign_consent_is_silent_without_foreign_actions(preflight):
    from jailbee.issue_outbox import foreign_consent

    batch = preflight["prepare"]({"a.json": [_comment()]})
    assert foreign_consent(batch, allowed=False, ask=None) is True


def test_foreign_question_names_the_foreign_repositories_in_the_one_publish_question(
    preflight,
):
    from jailbee.issue_outbox import foreign_question

    only = preflight["prepare"]({"a.json": [_comment("github:BerriAI/litellm", issue=9)]})
    assert foreign_question(only) == (
        "Publish 1 pending action to foreign repositories BerriAI/litellm?"
    )
    mixed = preflight["prepare"](
        {"a.json": [_comment(), _comment("github:BerriAI/litellm", issue=9)]}
    )
    assert foreign_question(mixed) == (
        "Publish 2 pending actions, 1 of them to foreign repositories BerriAI/litellm?"
    )


def test_foreign_question_is_none_without_foreign_actions(preflight):
    from jailbee.issue_outbox import foreign_question

    assert foreign_question(preflight["prepare"]({"a.json": [_comment()]})) is None


def test_resolve_repo_targets_uses_located_gitdir(tmp_path, mocker, make_cfg):
    from jailbee.issue_outbox import resolve_repo_targets
    from jailbee.submodules import DeclaredSubmodule, HostSubRepo

    root = tmp_path / ".git/modules/custom"
    mocker.patch(
        "jailbee.submodules.declared_submodule_remotes",
        return_value=(DeclaredSubmodule("lib", "unused"),),
    )
    locate = mocker.patch(
        "jailbee.submodules.resolve_host_subrepo", return_value=HostSubRepo("lib", root, None)
    )
    mocker.patch("jailbee.git.detect_upstream_remote", return_value="upstream")
    mocker.patch(
        "jailbee.git.get_remote_url",
        side_effect=lambda cwd, remote: (
            "https://github.com/acme/lib.git" if cwd == root else "https://github.com/acme/app.git"
        ),
    )
    target = resolve_repo_targets(make_cfg(tmp_path))["lib"]
    assert target.repo_root == root
    assert target.slug == "acme/lib"
    locate.assert_called_once_with(tmp_path, "lib")


def _sent_rows():
    from jailbee.outbox import sent

    return sorted(sent.list_records(), key=lambda r: (r.manifest, r.index))


@pytest.mark.parametrize("present", [False, True])
def test_project_apply_and_noop_keep_board_title_and_durable_item(execution, mocker, present):
    from dataclasses import replace

    from jailbee import issue_projects

    board = issue_projects.Project("acme/3", "P3", "Roadmap")
    mocker.patch.object(issue_projects, "resolve_project", return_value=board)
    mocker.patch.object(
        issue_projects,
        "get_target",
        return_value=issue_projects.ProjectTarget(
            "I7", "https://github.com/acme/app/pull/7", "ITEM7" if present else None
        ),
    )
    run = mocker.patch(
        "jailbee.issue_projects.subprocess.run",
        return_value=subprocess.CompletedProcess(
            [], 0, '{"data":{"addProjectV2ItemById":{"item":{"id":"ITEM7"}}}}', ""
        ),
    )
    batch = execution["batch"](
        {
            "a.json": [
                {
                    "type": "project_add",
                    "repo": ".",
                    "issue": 7,
                    "project": "acme/3",
                    "expected": {"present": present},
                }
            ]
        }
    )
    prepared = batch.manifests[0]
    batch = replace(
        batch,
        manifests=(replace(prepared, actions=(replace(prepared.actions[0], project=board),)),),
    )
    report = _apply(execution, batch)
    assert report.failure is None
    assert report.applied[0][1].url == "https://github.com/acme/app/pull/7#project-item-ITEM7"
    assert run.call_count == (0 if present else 1)
    (row,) = _sent_rows()
    assert row.kind == "project_add"
    assert row.title == "Roadmap"
    assert "acme/3" in row.content and "add" in row.content
    assert row.url.endswith("#project-item-ITEM7")


def test_project_uncertain_resolves_pull_receipt_and_retry(execution, mocker):
    from jailbee import issue_outbox, issue_projects
    from jailbee.issue_github import IssueGithubMutationError

    mocker.patch.object(
        issue_projects,
        "resolve_project",
        return_value=issue_projects.Project("acme/3", "P3", "Roadmap"),
    )
    mocker.patch.object(
        issue_projects,
        "get_target",
        return_value=issue_projects.ProjectTarget("I7", "https://github.com/acme/app/pull/7", None),
    )
    mutation = mocker.patch.object(
        issue_projects,
        "add_item",
        side_effect=IssueGithubMutationError("GitHub Projects transport failed", uncertain=True),
    )
    actions = [
        {
            "type": "project_add",
            "repo": ".",
            "issue": 7,
            "project": "acme/3",
            "expected": {"present": False},
        }
    ]
    batch = execution["batch"]({"a.json": actions})
    assert _apply(execution, batch).failure.uncertain
    prepared = batch.manifests[0]
    args = dict(
        uuid=batch.uuid,
        index=0,
        repo=prepared.actions[0].repo,
        manifest=prepared.manifest,
        digest=prepared.digest,
    )
    assert not issue_outbox.reconcile_action(**args, resolution=issue_outbox.RetryResolution())
    assert _apply(execution, execution["batch"]({"a.json": actions})).failure.uncertain
    assert mutation.call_count == 2
    assert issue_outbox.reconcile_action(
        **args,
        resolution=issue_outbox.AppliedResolution(
            "https://github.com/acme/app/pull/7#project-item-ITEM7", None
        ),
    )
    (row,) = _sent_rows()
    assert row.state == "applied" and row.url.endswith("#project-item-ITEM7")


def test_apply_records_every_issue_action_in_sent_log(execution):
    from jailbee.issue_github import MutationReceipt
    from tests.issue_store_support import UUID

    batch = execution["batch"](
        {
            "a.json": [
                _create(labels=("bug",)),
                {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Next"},
                _labels(issue=27),
                _state(),
            ]
        }
    )
    execution["mutations"]["create_issue"].return_value = MutationReceipt(
        901, "https://github.com/acme/app/issues/901"
    )
    assert _apply(execution, batch).failure is None
    rows = _sent_rows()
    assert [r.kind for r in rows] == ["create", "comment", "labels", "state"]
    assert all(r.outbox == "issue" and r.manifest == "a.json" for r in rows)
    assert all(r.publisher == "user" and r.state == "applied" for r in rows)
    assert all(r.instance == "test-box" and r.container_prefix == "app" for r in rows)
    assert all(r.uuid == UUID and r.container == "test-box" for r in rows)
    assert rows[0].target == 901 and rows[0].url == "https://github.com/acme/app/issues/901"
    assert rows[0].title == "New title" and "Second line" in rows[0].content
    assert rows[1].content == "Next"
    assert rows[2].content == "labels +feature \u2212bug"
    assert rows[3].content == "state closed (completed)"
    assert all(r.repo == "acme/app" for r in rows)


def test_apply_records_uncertain_row_for_uncertain_outcome(execution):
    from jailbee.issue_github import IssueGithubMutationError

    batch = execution["batch"]({"a.json": [_comment(issue=7, body="maybe")]})
    execution["mutations"]["add_comment"].side_effect = IssueGithubMutationError(
        "timeout", uncertain=True
    )
    assert _apply(execution, batch).failure.uncertain
    (row,) = _sent_rows()
    assert (row.state, row.kind, row.target, row.url, row.content) == (
        "uncertain",
        "comment",
        7,
        None,
        "maybe",
    )


def test_apply_does_not_record_certain_failure(execution):
    from jailbee.issue_github import IssueGithubMutationError

    batch = execution["batch"]({"a.json": [_comment()]})
    execution["mutations"]["add_comment"].side_effect = IssueGithubMutationError(
        "rejected", uncertain=False
    )
    assert _apply(execution, batch).failure is not None
    assert _sent_rows() == []


def test_outcome_that_cannot_be_recorded_leaves_neither_sent_record_nor_receipt(execution, mocker):
    """Journal entry and sent record commit together: a failed commit records neither,
    and the prepared action reads back as uncertain."""
    from jailbee.orchestration.store import StoreError
    from tests.outbox_journal_fake import journal_key

    batch = execution["batch"]({"a.json": [_comment()]})
    mocker.patch.object(execution["pubs"], "settle", side_effect=StoreError("full"))
    assert _apply(execution, batch).failure.uncertain
    mocker.stopall()
    assert _sent_rows() == []
    journal = execution["store"].load(journal_key(batch.identity, "a.json"))
    assert [a.state for a in journal.actions] == ["uncertain"]
    assert "a.json" in execution["files"]


@pytest.mark.parametrize("uncertain", [False, True])
def test_issue_publish_survives_sent_construction_failure(execution, mocker, uncertain):
    from jailbee.issue_github import IssueGithubMutationError

    batch = execution["batch"]({"a.json": [_comment()]})
    mocker.patch("jailbee.issue_outbox._sent_action", side_effect=OSError("snapshot unavailable"))
    if uncertain:
        execution["mutations"]["add_comment"].side_effect = IssueGithubMutationError(
            "timeout", uncertain=True
        )
    report = _apply(execution, batch)
    if uncertain:
        assert report.failure.uncertain
        from tests.outbox_journal_fake import journal_key

        key = journal_key(batch.identity, "a.json")
        assert execution["store"].load(key).actions[0].state == "uncertain"
    else:
        assert report.failure is None and report.cleaned == ("a.json",)
        assert len(report.applied) == 1
    assert _sent_rows() == []


def test_resolve_applied_updates_uncertain_row_with_url(execution):
    from jailbee import issue_outbox
    from jailbee.issue_github import IssueGithubMutationError

    batch = execution["batch"]({"a.json": [_create()]})
    execution["mutations"]["create_issue"].side_effect = IssueGithubMutationError(
        "timeout", uncertain=True
    )
    assert _apply(execution, batch).failure.uncertain
    (before,) = _sent_rows()
    assert before.state == "uncertain" and before.target is None and before.url is None
    _reconcile(
        execution,
        batch,
        issue_outbox.AppliedResolution("https://github.com/acme/app/issues/123", issue=123),
    )
    (row,) = _sent_rows()
    assert (row.state, row.target, row.url) == (
        "applied",
        123,
        "https://github.com/acme/app/issues/123",
    )
    assert row.sent_at == before.sent_at and row.id == before.id


def test_retry_then_broker_success_updates_the_same_row_and_provenance(execution, mocker):
    from datetime import datetime, timedelta
    from unittest.mock import Mock

    from jailbee import issue_outbox
    from jailbee.issue_github import IssueGithubMutationError, MutationReceipt

    batch = execution["batch"]({"a.json": [_comment(issue=7)]})
    execution["mutations"]["add_comment"].side_effect = IssueGithubMutationError(
        "timeout", uncertain=True
    )
    assert _apply(execution, batch).failure.uncertain
    _reconcile(execution, batch, issue_outbox.RetryResolution())
    (before,) = _sent_rows()
    assert before.state == "uncertain" and before.publisher == "user"
    execution["mutations"]["add_comment"].side_effect = None
    execution["mutations"]["add_comment"].return_value = MutationReceipt(
        7, "https://github.com/acme/app/issues/7#issuecomment-1"
    )
    success_time = before.sent_at + timedelta(minutes=5)
    clock = Mock(wraps=datetime)
    clock.now.return_value = success_time
    batch = execution["batch"]({"a.json": [_comment(issue=7)]})
    report = issue_outbox.apply_batch(
        batch,
        incus=execution["incus"],
        container_prefix="app",
        publisher="orchestrator",
        clock=lambda: clock.now().timestamp(),
    )
    assert report.failure is None
    (row,) = _sent_rows()
    assert row.state == "applied" and row.url.endswith("#issuecomment-1")
    assert row.id == before.id
    assert (row.publisher, row.sent_at) == ("orchestrator", success_time)


def test_reused_manifest_name_keeps_the_earlier_sent_rows(execution):
    first = execution["batch"]({"a.json": [_comment(body="First")]})
    assert _apply(execution, first).failure is None
    second = execution["batch"]({"a.json": [_comment(body="Second")]})
    assert _apply(execution, second).failure is None
    rows = sorted(_sent_rows(), key=lambda r: r.id)
    assert [r.content for r in rows] == ["First", "Second"]
    assert all(r.state == "applied" for r in rows)


def test_uncertain_reuse_does_not_touch_the_earlier_applied_row(execution):
    from jailbee.issue_github import IssueGithubMutationError

    first = execution["batch"]({"a.json": [_comment(body="First")]})
    assert _apply(execution, first).failure is None
    second = execution["batch"]({"a.json": [_comment(body="Second")]})
    execution["mutations"]["add_comment"].side_effect = IssueGithubMutationError(
        "timeout", uncertain=True
    )
    assert _apply(execution, second).failure.uncertain
    rows = sorted(_sent_rows(), key=lambda r: r.id)
    assert [(r.content, r.state) for r in rows] == [("First", "applied"), ("Second", "uncertain")]


# --- assignees action ------------------------------------------------------


def _assigned(preflight, *logins):
    preflight["fetch"].return_value = replace(preflight["snapshot"], assignees=tuple(logins))


def test_assign_stale_expected_refuses(preflight):
    from jailbee import issue_github
    from jailbee.issue_outbox import IssueGateError, IssueStaleError, revalidate_batch

    _assigned(preflight, "carol")
    batch = preflight["prepare"]({"a.json": [_assign()]})

    _assigned(preflight, "dave")
    with pytest.raises(IssueStaleError, match=r"action 0: expected.assignees differs"):
        revalidate_batch(batch)

    with pytest.raises(IssueGateError, match=r"action 0: expected.assignees differs"):
        preflight["prepare"]({"a.json": [_assign(expected={"assignees": ["carol", "erin"]})]})

    for name in ("add_assignees", "remove_assignees"):
        getattr(issue_github, name).assert_not_called()


def test_assign_expected_compares_logins_case_insensitively_as_a_set(preflight):
    _assigned(preflight, "CAROL", "Dave")
    preflight["prepare"](
        {"a.json": [_assign(expected={"assignees": ["dave", "carol"]})]},
    )


def test_assign_retry_is_idempotent(preflight):
    """After an uncertain assign that landed, a retry is not refused as stale."""
    from jailbee import issue_outbox

    _assigned(preflight, "carol")
    action = _assign(add=["bob"], remove=["carol"])
    batch = preflight["prepare"]({"a.json": [action]})
    prepared = batch.manifests[0]
    preflight["store"].record("a.json", prepared.digest, 1, 0, state="uncertain", repo="acme/app")
    issue_outbox.reconcile_action(
        uuid=batch.uuid,
        index=0,
        resolution=issue_outbox.RetryResolution(),
        repo=prepared.actions[0].repo,
        manifest=prepared.manifest,
        digest=prepared.digest,
    )

    _assigned(preflight, "bob")  # the first attempt actually landed
    retried = preflight["prepare"]({"a.json": [action]})
    assert retried.manifests[0].actions[0].status == "pending"
    issue_outbox.revalidate_batch(retried)

    _assigned(preflight, "bob", "carol")  # add landed, removal did not
    issue_outbox.revalidate_batch(retried)


def test_assign_resolves_me_once_per_run_and_never_from_the_manifest(preflight):
    from jailbee.issue_outbox import plan_lines

    preflight["login"].return_value = "tuomas"
    _assigned(preflight, "carol")
    batch = preflight["prepare"](
        {
            "a.json": [_assign(issue=7), _assign(issue=8)],
            "b.json": [_assign(issue=9, add=["@me"], remove=[])],
        }
    )
    preflight["login"].assert_called_once()
    lines = plan_lines(batch)
    assert lines.count("  assignees +tuomas +bob \u2212carol") == 2
    assert "  assignees +tuomas" in lines


def test_assign_refuses_me_that_resolves_to_a_removed_login(preflight):
    from jailbee.issue_outbox import IssueGateError

    preflight["login"].return_value = "Tuomas"
    _assigned(preflight, "tuomas")
    with pytest.raises(IssueGateError, match=r"both add and remove"):
        preflight["prepare"](
            {
                "a.json": [
                    _assign(add=["@me"], remove=["tuomas"], expected={"assignees": ["tuomas"]})
                ]
            }
        )
    with pytest.raises(IssueGateError, match=r"both add and remove"):
        preflight["prepare"](
            {
                "a.json": [
                    _assign(add=["tuomas"], remove=["@me"], expected={"assignees": ["tuomas"]})
                ]
            }
        )


def test_assign_conflicts_with_another_assign_on_the_same_issue_across_manifests(preflight):
    from jailbee.issue_outbox import IssueGateError

    _assigned(preflight, "carol")
    with pytest.raises(IssueGateError, match=r"conflict on assignees"):
        preflight["prepare"]({"a.json": [_assign()], "b.json": [_assign()]})


def test_assign_target_created_in_the_same_manifest_expects_no_assignees(preflight):
    from jailbee.issue_outbox import IssueGateError

    new_ref = {"type": "assignees", "repo": ".", "issue_ref": "new", "add": ["bob"]}
    batch = preflight["prepare"](
        {"a.json": [_create(), {**new_ref, "expected": {"assignees": []}}]}
    )
    assert batch.manifests[0].actions[1].issue.ref == "new"
    preflight["fetch"].assert_not_called()
    with pytest.raises(IssueGateError, match=r"expected.assignees differs"):
        preflight["prepare"]({"a.json": [_create(), {**new_ref, "expected": {"assignees": ["x"]}}]})


def test_assign_show_lines_keep_me_unresolved(preflight):
    from jailbee import issue_outbox
    from jailbee.issue_manifest import parse_manifest

    text = json.dumps({"version": 1, "actions": [_assign()]})
    manifest = parse_manifest("a.json", text, {})
    lines = issue_outbox.show_lines(manifest, None, "test-box")
    assert "a.json action 0: assignees #7 [pending]" in lines
    assert "  assignees +@me +bob \u2212carol" in lines


def test_assign_apply_posts_resolved_adds_then_deletes_removals(execution):
    calls = []
    batch = execution["batch"]({"a.json": [_assign()]})
    execution["mutations"]["add_assignees"].side_effect = lambda *a, **k: (
        calls.append(("add", a[1:], k)) or _assignees_receipt("alice", "bob", "carol")
    )
    execution["mutations"]["remove_assignees"].side_effect = lambda *a, **k: (
        calls.append(("remove", a[1:], k)) or _assignees_receipt("alice", "bob")
    )
    report = _apply(execution, batch)
    assert report.failure is None
    assert calls == [
        ("add", ("acme/app", 7), {"logins": ("alice", "bob")}),
        ("remove", ("acme/app", 7), {"logins": ("carol",)}),
    ]
    assert [r.index for _, r in report.applied] == [0]


def test_assign_apply_skips_the_empty_side(execution):
    batch = execution["batch"]({"a.json": [_assign(add=["bob"], remove=[])]})
    assert _apply(execution, batch).failure is None
    execution["mutations"]["add_assignees"].assert_called_once()
    execution["mutations"]["remove_assignees"].assert_not_called()

    batch = execution["batch"]({"b.json": [_assign(add=[], remove=["carol"])]})
    assert _apply(execution, batch).failure is None
    execution["mutations"]["add_assignees"].assert_called_once()
    execution["mutations"]["remove_assignees"].assert_called_once()


def test_assign_failure_after_the_add_landed_is_uncertain(execution):
    from jailbee.issue_github import IssueGithubMutationError

    batch = execution["batch"]({"a.json": [_assign()]})
    execution["mutations"]["remove_assignees"].side_effect = IssueGithubMutationError(
        "GitHub mutation was rejected", uncertain=False
    )
    failure = _apply(execution, batch).failure
    assert failure is not None and failure.uncertain
    (row,) = _sent_rows()
    assert row.state == "uncertain" and row.kind == "assignees"


def test_assign_retry_after_uncertain_apply_succeeds_without_failure(execution):
    from jailbee import issue_outbox
    from jailbee.issue_github import IssueGithubMutationError

    batch = execution["batch"]({"a.json": [_assign(add=["bob"], remove=[])]})
    execution["mutations"]["add_assignees"].side_effect = IssueGithubMutationError(
        "timeout", uncertain=True
    )
    assert _apply(execution, batch).failure.uncertain
    _reconcile(execution, batch, issue_outbox.RetryResolution())

    execution["mutations"]["add_assignees"].side_effect = None  # GitHub: already assigned, 200
    retried = execution["batch"]({"a.json": [_assign(add=["bob"], remove=[])]})
    report = _apply(execution, retried)
    assert report.failure is None
    assert execution["mutations"]["add_assignees"].call_count == 2


def test_assign_sent_row_content_uses_the_resolved_login(execution):
    batch = execution["batch"]({"a.json": [_assign()]})
    assert _apply(execution, batch).failure is None
    (row,) = _sent_rows()
    assert row.kind == "assignees"
    assert row.content == "assignees +alice +bob \u2212carol"
    assert row.target == 7


from jailbee.outbox_io import (  # noqa: E402
    ASSIGN_NOT_APPLIED_DETAIL,
    UNASSIGN_NOT_APPLIED_DETAIL,
)


def test_assign_unassignable_add_is_a_definite_failure_and_skips_the_removal(execution):
    batch = execution["batch"]({"a.json": [_assign()]})
    # GitHub answered 2xx but silently ignored both logins (no push access).
    execution["mutations"]["add_assignees"].return_value = _assignees_receipt("carol")
    failure = _apply(execution, batch).failure
    assert failure is not None and failure.uncertain is False
    assert failure.detail == ASSIGN_NOT_APPLIED_DETAIL
    execution["mutations"]["remove_assignees"].assert_not_called()
    assert _sent_rows() == []


def test_assign_partially_landed_add_is_uncertain_and_skips_the_removal(execution):
    batch = execution["batch"]({"a.json": [_assign()]})
    execution["mutations"]["add_assignees"].return_value = _assignees_receipt("alice", "carol")
    failure = _apply(execution, batch).failure
    assert failure is not None and failure.uncertain is True
    assert failure.detail == ASSIGN_NOT_APPLIED_DETAIL
    execution["mutations"]["remove_assignees"].assert_not_called()
    (row,) = _sent_rows()
    assert row.state == "uncertain"


def test_assign_add_matching_is_case_insensitive(execution):
    batch = execution["batch"]({"a.json": [_assign(add=["Bob"], remove=[])]})
    execution["mutations"]["add_assignees"].return_value = _assignees_receipt("bob")
    assert _apply(execution, batch).failure is None


def test_assign_remove_that_did_not_take_effect_is_definite_when_remove_only(execution):
    batch = execution["batch"]({"a.json": [_assign(add=[], remove=["carol"])]})
    execution["mutations"]["remove_assignees"].return_value = _assignees_receipt("carol")
    failure = _apply(execution, batch).failure
    assert failure is not None and failure.uncertain is False
    assert failure.detail == UNASSIGN_NOT_APPLIED_DETAIL


def test_assign_remove_that_did_not_take_effect_is_uncertain_after_an_add(execution):
    batch = execution["batch"]({"a.json": [_assign()]})
    execution["mutations"]["remove_assignees"].return_value = _assignees_receipt("alice", "carol")
    failure = _apply(execution, batch).failure
    assert failure is not None and failure.uncertain is True


def test_assign_remove_only_rejection_is_definite(execution):
    from jailbee.issue_github import IssueGithubMutationError

    batch = execution["batch"]({"a.json": [_assign(add=[], remove=["carol"])]})
    execution["mutations"]["remove_assignees"].side_effect = IssueGithubMutationError(
        "GitHub mutation was rejected", uncertain=False
    )
    failure = _apply(execution, batch).failure
    assert failure is not None and failure.uncertain is False


def test_assign_stale_check_refuses_a_change_to_an_unrelated_login(preflight):
    from jailbee import issue_github
    from jailbee.issue_outbox import IssueGateError

    _assigned(preflight, "bob", "dave")
    with pytest.raises(IssueGateError, match=r"expected.assignees differs"):
        preflight["prepare"](
            {"a.json": [_assign(add=["bob"], remove=["carol"], expected={"assignees": ["carol"]})]}
        )
    for name in ("add_assignees", "remove_assignees"):
        getattr(issue_github, name).assert_not_called()


def test_assign_stale_check_refuses_an_unrelated_login_removed_by_someone_else(preflight):
    from jailbee.issue_outbox import IssueGateError

    _assigned(preflight, "bob")
    with pytest.raises(IssueGateError, match=r"expected.assignees differs"):
        preflight["prepare"](
            {"a.json": [_assign(add=["bob"], remove=[], expected={"assignees": ["dave"]})]}
        )


def test_assign_retry_after_a_partly_landed_add_is_not_stale(preflight):
    from jailbee import issue_outbox

    _assigned(preflight, "x")
    action = _assign(add=["a", "b"], remove=[], expected={"assignees": ["x"]})
    batch = preflight["prepare"]({"a.json": [action]})
    prepared = batch.manifests[0]
    preflight["store"].record("a.json", prepared.digest, 1, 0, state="uncertain", repo="acme/app")
    issue_outbox.reconcile_action(
        uuid=batch.uuid,
        index=0,
        resolution=issue_outbox.RetryResolution(),
        repo=prepared.actions[0].repo,
        manifest=prepared.manifest,
        digest=prepared.digest,
    )

    _assigned(preflight, "x", "a")  # GitHub assigned "a" of ["a", "b"] before failing
    retried = preflight["prepare"]({"a.json": [action]})
    assert retried.manifests[0].actions[0].status == "pending"
    issue_outbox.revalidate_batch(retried)


def test_assign_retry_after_a_partly_landed_add_applies_through_the_real_path(execution, mocker):
    from dataclasses import replace

    from jailbee import issue_github, issue_outbox
    from jailbee.issue_github import IssueGithubMutationError

    action = _assign(add=["a", "b"], remove=[], expected={"assignees": ["x"]})
    batch = execution["batch"]({"a.json": [action]})
    execution["mutations"]["add_assignees"].side_effect = IssueGithubMutationError(
        "timeout", uncertain=True
    )
    assert _apply(execution, batch).failure.uncertain
    _reconcile(execution, batch, issue_outbox.RetryResolution())

    # GitHub assigned "a" of ["a", "b"] before the connection dropped.
    live = issue_github.IssueSnapshot(
        7, "T", "B", (), "open", "https://github.com/acme/app/issues/7", False
    )
    mocker.patch.object(issue_github, "get_issue", return_value=replace(live, assignees=("x", "a")))
    mocker.patch.object(issue_github, "current_login", return_value="alice")
    retried = execution["batch"]({"a.json": [action]})
    issue_outbox.revalidate_batch(retried)

    execution["mutations"]["add_assignees"].side_effect = None
    execution["mutations"]["add_assignees"].return_value = _assignees_receipt("x", "a", "b")
    report = _apply(execution, retried)
    assert report.failure is None
    assert execution["mutations"]["add_assignees"].call_count == 2
    assert [r.index for _, r in report.applied] == [0]
    assert [r.state for _, r in report.applied] == ["applied"]
