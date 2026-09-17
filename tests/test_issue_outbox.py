from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest


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


def _state():
    return {
        "type": "state",
        "repo": ".",
        "issue": 7,
        "state": "closed",
        "reason": "completed",
        "expected": {"state": "open"},
    }


@pytest.fixture
def preflight(tmp_path, mocker, make_cfg):
    from jailbee import issue_github, issue_outbox
    from jailbee.outbox_io import ContainerIdentity, JournalStore

    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.list_containers.return_value = [{"name": "test-box", "created_at": "2026-09-18"}]
    targets = {
        ".": issue_outbox.RepoTarget(".", tmp_path, "acme/app"),
        "lib": issue_outbox.RepoTarget("lib", tmp_path / "lib", "acme/lib"),
    }
    mocker.patch.object(issue_outbox, "resolve_repo_targets", return_value=targets)
    reader = mocker.patch.object(issue_outbox, "read_text_outbox", return_value={})
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
        for name in ("create_issue", "edit_issue", "replace_labels", "add_comment", "set_state")
    ]
    store = JournalStore(tmp_path / "journals")

    def prepare(manifests, names=None, extras=None):
        reader.return_value = {
            name: json.dumps({"version": 1, "actions": actions})
            for name, actions in manifests.items()
        }
        reader.return_value.update(extras or {})
        return issue_outbox.prepare_batch(
            cfg,
            incus,
            "test-box",
            list(manifests) if names is None else names,
            uid=1000,
            journal_store=store,
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


def test_read_issue_outbox_empty_ordering_and_sidecars(mocker):
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.issue_outbox import read_issue_outbox

    incus = mocker.Mock()
    reader = mocker.patch("jailbee.issue_outbox.read_text_outbox", return_value={})
    assert read_issue_outbox(incus, "box", uid=42).manifest_names == ()
    reader.return_value = {
        "z.json": "{}",
        "a.json": "{}",
        "a.progress.json": "{}",
        "body.md": "body",
    }
    snapshot = read_issue_outbox(incus, "box", uid=42)
    assert snapshot.manifest_names == ("a.json", "z.json")
    assert snapshot.files["body.md"] == "body"
    assert reader.call_args.args == (
        incus,
        "box",
        f"/home/{CONTAINER_USERNAME}/.jailbee/issue-outbox",
    )
    assert reader.call_args.kwargs["uid"] == 42


def test_outbox_read_wraps_incus_errors(mocker):
    from jailbee.incus import IncusError
    from jailbee.issue_outbox import read_issue_outbox
    from jailbee.outbox_io import OutboxReadError

    incus = mocker.Mock()
    incus.exec.side_effect = IncusError("offline")
    with pytest.raises(OutboxReadError, match=r"box.*offline"):
        read_issue_outbox(incus, "box", uid=None)


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
    assert not preflight["store"].root.exists()


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


def _record(preflight, actions, *, state="applied", digest=None, issue=42, repo="acme/app"):
    from jailbee.outbox_io import journal_key, proposal_digest

    text = json.dumps({"version": 1, "actions": actions})
    key = journal_key(preflight["identity"], "a.json")
    store = preflight["store"]
    store.create(key, digest or proposal_digest("a.json", text, {}), len(actions))
    store.mark_prepared(key, 0, repo=repo)
    if state == "applied":
        store.mark_applied(
            key, 0, repo=repo, url="https://github.com/acme/app/issues/42", issue=issue
        )
    elif state == "uncertain":
        store.mark_uncertain(key, 0, repo=repo, detail="unknown")
    return key


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
    before = {p: p.read_bytes() for p in preflight["store"].root.rglob("*") if p.is_file()}
    batch = preflight["prepare"]({"a.json": actions})
    prepared = batch.manifests[0]
    for lines in (plan_lines(batch), show_lines(prepared.manifest, prepared.journal)):
        assert any(display in line for line in lines)
        assert "    Full [body]" in lines
        if state == "applied":
            assert any("https://github.com/acme/app/issues/42" in line for line in lines)
    assert {p: p.read_bytes() for p in preflight["store"].root.rglob("*") if p.is_file()} == before


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
