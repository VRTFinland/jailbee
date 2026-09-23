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
    from jailbee import issue_github, issue_outbox
    from jailbee.issue_manifest import CreateAction, ExistingIssue, LabelsAction, parse_manifest
    from jailbee.outbox_io import ContainerIdentity, JournalStore, journal_key, proposal_digest

    identity = ContainerIdentity("test-box", "2026-09-18")
    store = JournalStore(tmp_path / "journals")
    files = {}
    events = []
    incus = mocker.Mock()
    incus.list_containers.return_value = [{"name": "test-box", "created_at": "2026-09-18"}]
    mocker.patch.object(
        issue_outbox, "read_issue_outbox", side_effect=lambda *a, **kw: issue_outbox.OutboxSnapshot(dict(files))
    )

    def append(incus, container, directory, lines, *, uid):
        events.append(("log", tuple(lines)))

    def delete(incus, container, directory, names, *, uid):
        events.append(("delete", tuple(names)))
        for name in names:
            files.pop(name, None)

    log = mocker.patch.object(issue_outbox, "append_applied_log", side_effect=append, create=True)
    remove = mocker.patch.object(issue_outbox, "delete_outbox_files", side_effect=delete, create=True)
    mutations = {
        name: mocker.patch.object(
            issue_github, name,
            return_value=issue_github.MutationReceipt(7, "https://github.com/acme/app/issues/7"),
        )
        for name in ("create_issue", "edit_issue", "replace_labels", "add_comment", "set_state")
    }

    def batch(manifests, extras=None):
        files.update(extras or {})
        files.update({name: json.dumps({"version": 1, "actions": actions}) for name, actions in manifests.items()})
        prepared = []
        for name in manifests:
            manifest = parse_manifest(name, files[name], files)
            digest = proposal_digest(name, files[name], {body: files[body] for body in manifest.body_files})
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
                labels = action.labels if isinstance(action, CreateAction) else ("Feature",) if isinstance(action, LabelsAction) else None
                receipt = receipts.get(index)
                status = "pending" if receipt is None else "applied" if receipt.state == "applied" else "uncertain"
                resolved.append(issue_outbox.ResolvedAction(index, action, repo, issue, status, labels))
            prepared.append(issue_outbox.PreparedManifest(manifest, digest, journal, tuple(resolved)))
        return issue_outbox.PreparedBatch("test-box", tmp_path, identity, "alice", issue_outbox.OutboxSnapshot(dict(files)), tuple(prepared), {})

    return {"batch": batch, "files": files, "store": store, "identity": identity, "incus": incus,
            "log": log, "remove": remove, "mutations": mutations, "events": events}


def _apply(execution, batch):
    from jailbee import issue_outbox

    return issue_outbox.apply_batch(batch, incus=execution["incus"], uid=1000, journal_store=execution["store"])


def _seed_execution(execution, batch, *, index=0, state="applied", issue=901, repo=None):
    from jailbee.outbox_io import journal_key

    prepared = batch.manifests[0]
    key = journal_key(batch.identity, prepared.manifest.name)
    store = execution["store"]
    store.create(key, prepared.digest, len(prepared.actions))
    repo = repo or prepared.actions[index].repo.slug
    store.mark_prepared(key, index, repo=repo)
    if state == "applied":
        store.mark_applied(key, index, repo=repo, url=f"https://github.com/{repo}/issues/{issue or 7}", issue=issue)
    elif state == "uncertain":
        store.mark_uncertain(key, index, repo=repo, detail="unknown")
    return key


def test_apply_executes_manifests_and_refs_in_displayed_order(execution):
    from jailbee.issue_github import MutationReceipt

    calls = []
    batch = execution["batch"]({
        "z.json": [_create(ref="cache-cleanup"), {"type": "comment", "repo": ".", "issue_ref": "cache-cleanup", "body": "Next"}],
        "a.json": [_labels(repo="lib", issue=27)],
    })
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

    assert calls == [("create", "acme/app", "cache-cleanup"), ("comment", "acme/app", 901), ("labels", "acme/parser", 27)]
    assert report.failure is None
    assert report.cleaned == ("z.json", "a.json")
    assert [(name, receipt.index) for name, receipt in report.applied] == [("z.json", 0), ("z.json", 1), ("a.json", 0)]


@pytest.mark.parametrize("action, payload", [
    (_edit(title="Next", expected={"title": "Old"}), {"title": "Next"}),
    (_edit(body="", expected={"body": "Old"}), {"body": ""}),
    (_edit(title="Next", body="", expected={"title": "Old", "body": "Old"}), {"title": "Next", "body": ""}),
    (_labels(), {"labels": ["Feature"]}),
    (_state(), {"state": "closed", "state_reason": "completed"}),
    ({**_state(), "reason": "not_planned"}, {"state": "closed", "state_reason": "not_planned"}),
    ({"type": "state", "repo": ".", "issue": 7, "state": "open", "expected": {"state": "closed"}}, {"state": "open", "state_reason": "reopened"}),
])
def test_apply_dispatches_exact_single_patch(execution, mocker, action, payload):
    from jailbee import issue_github

    for name in ("edit_issue", "replace_labels", "set_state"):
        mocker.stop(execution["mutations"][name])
    run = mocker.patch.object(issue_github.subprocess, "run", return_value=mocker.Mock(returncode=0, stdout=json.dumps({"number": 7, "html_url": "https://github.com/acme/app/issues/7"}), stderr=""))
    batch = execution["batch"]({"a.json": [action]})

    assert _apply(execution, batch).failure is None

    assert run.call_count == 1
    assert run.call_args.args[0] == ["gh", "api", "repos/acme/app/issues/7", "--method", "PATCH", "--input", "-"]
    assert json.loads(run.call_args.kwargs["input"]) == payload
    assert run.call_args.kwargs["cwd"] == batch.host_repo_root


@pytest.mark.parametrize("uncertain", [False, True])
def test_apply_stops_first_failure_and_preserves_only_known_progress(execution, uncertain):
    from jailbee.issue_github import IssueGithubMutationError
    from jailbee.outbox_io import journal_key

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
    batch = execution["batch"]({"a.json": [_create(), {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Next"}]})
    _seed_execution(execution, batch, repo="ACME/APP")
    report = _apply(execution, batch)
    assert report.failure is None
    execution["mutations"]["create_issue"].assert_not_called()
    execution["mutations"]["add_comment"].assert_called_once_with(batch.host_repo_root, "acme/app", 901, body="Next")
    assert report.skipped[0][1].issue == 901


@pytest.mark.parametrize("state", ["prepared", "uncertain"])
def test_apply_never_replays_unknown_outcomes(execution, state):
    batch = execution["batch"]({"a.json": [_create(), _comment()]})
    _seed_execution(execution, batch, state=state)
    report = _apply(execution, batch)
    assert report.failure.uncertain
    assert report.failure.index == 0
    assert all(not mutation.called for mutation in execution["mutations"].values())


def test_apply_durable_preparation_and_receipt_are_inside_one_exclusive_lock(execution):
    from tests.test_outbox_io import _other_process_can_lock

    batch = execution["batch"]({"a.json": [_comment(), _comment()]})
    seen = []
    def mutate(*args, **kwargs):
        paths = [p for p in execution["store"].root.rglob("*.json") if "archive" not in p.parts]
        records = json.loads(paths[0].read_text())["actions"]
        seen.append([a["state"] for a in records])
        (lock,) = execution["store"].root.rglob("*.lock")
        assert not _other_process_can_lock(lock)
        return execution["mutations"]["add_comment"].return_value
    execution["mutations"]["add_comment"].side_effect = mutate
    assert _apply(execution, batch).failure is None
    assert seen == [["prepared"], ["applied", "prepared"]]


@pytest.mark.parametrize("method", ["create", "mark_prepared"])
def test_apply_journal_failure_before_dispatch_makes_no_remote_call(execution, mocker, method):
    from jailbee.outbox_io import JournalError

    batch = execution["batch"]({"a.json": [_comment()]})
    mocker.patch.object(execution["store"], method, side_effect=JournalError("disk failed"))
    report = _apply(execution, batch)
    assert report.failure is not None
    assert all(not mutation.called for mutation in execution["mutations"].values())


def test_apply_receipt_failure_reports_uncertainty_and_blocks_next_process(execution, mocker):
    from jailbee.outbox_io import JournalError, JournalStore, journal_key

    batch = execution["batch"]({"a.json": [_comment(), _comment()]})
    mocker.patch.object(execution["store"], "mark_applied", side_effect=JournalError("disk failed"))
    report = _apply(execution, batch)
    assert report.failure.uncertain
    assert "succeeded" in report.failure.detail
    execution["store"] = JournalStore(execution["store"].root)
    loaded = execution["store"].load(journal_key(batch.identity, "a.json"))
    assert loaded.actions[0].state == "uncertain"
    assert _apply(execution, batch).failure.uncertain
    assert execution["mutations"]["add_comment"].call_count == 1


@pytest.mark.parametrize("uncertain,method", [(False, "clear_prepared"), (True, "mark_uncertain")])
def test_apply_failure_to_record_remote_failure_still_blocks_replay(execution, mocker, uncertain, method):
    from jailbee.issue_github import IssueGithubMutationError
    from jailbee.outbox_io import JournalError

    batch = execution["batch"]({"a.json": [_comment()]})
    execution["mutations"]["add_comment"].side_effect = IssueGithubMutationError("rejected", uncertain=uncertain)
    mocker.patch.object(execution["store"], method, side_effect=JournalError("disk failed"))
    assert _apply(execution, batch).failure.uncertain
    assert _apply(execution, batch).failure.uncertain
    assert execution["mutations"]["add_comment"].call_count == 1


def test_apply_replaces_an_empty_old_digest_only_inside_lock(execution, mocker):
    from jailbee.outbox_io import journal_key
    from tests.test_outbox_io import _other_process_can_lock

    batch = execution["batch"]({"a.json": [_comment()]})
    key = journal_key(batch.identity, "a.json")
    store = execution["store"]
    store.create(key, "0" * 64, 1)
    archive = store.archive
    def checked_archive(key):
        (lock,) = store.root.rglob("*.lock")
        assert not _other_process_can_lock(lock)
        return archive(key)
    mocker.patch.object(store, "archive", side_effect=checked_archive)
    assert _apply(execution, batch).failure is None
    assert len(list(store.root.glob("*/archive/*.json"))) == 2


@pytest.mark.parametrize("change", ["digest", "repo", "count", "identity", "missing", "body"])
def test_apply_rechecks_every_proposal_before_any_remote_mutation(execution, change):
    from jailbee.outbox_io import journal_key

    batch = execution["batch"]({"a.json": [_comment()], "b.json": [_comment()]})
    store = execution["store"]
    second = replace(batch, manifests=(batch.manifests[1],))
    if change in ("digest", "repo"):
        _seed_execution(execution, second, state="prepared", repo="wrong/repo" if change == "repo" else None)
        if change == "digest":
            batch = replace(batch, manifests=(batch.manifests[0], replace(batch.manifests[1], digest="0" * 64)))
    elif change == "count":
        store.create(journal_key(batch.identity, "b.json"), batch.manifests[1].digest, 2)
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
    from jailbee.outbox_io import journal_key

    prepared = batch.manifests[0]
    arguments = {"key": journal_key(batch.identity, prepared.manifest.name), "index": 0,
                 "resolution": resolution, "repo": prepared.actions[0].repo,
                 "manifest": prepared.manifest, "digest": prepared.digest,
                 "journal_store": execution["store"], **changes}
    issue_outbox.reconcile_action(**arguments)


def test_reconcile_create_accepts_matching_number_and_case_insensitive_repo(execution):
    from jailbee import issue_outbox

    batch = execution["batch"]({"a.json": [_create()]})
    key = _seed_execution(execution, batch, state="prepared", repo="ACME/APP")
    _reconcile(execution, batch, issue_outbox.AppliedResolution("https://github.com/Acme/App/issues/123", issue=123))
    receipt = execution["store"].load(key).actions[0]
    assert (receipt.state, receipt.issue, receipt.url) == ("applied", 123, "https://github.com/Acme/App/issues/123")


@pytest.mark.parametrize("action, issue, url", [
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
])
def test_reconcile_rejects_invalid_receipts_without_changing_journal(execution, action, issue, url):
    from jailbee import issue_outbox
    from jailbee.outbox_io import JournalError

    batch = execution["batch"]({"a.json": [action]})
    key = _seed_execution(execution, batch, state="uncertain")
    before = execution["store"].load(key)
    with pytest.raises(JournalError):
        _reconcile(execution, batch, issue_outbox.AppliedResolution(url, issue=issue))
    assert execution["store"].load(key) == before


@pytest.mark.parametrize("change", ["digest", "identity", "name", "count", "repo", "index", "pending", "applied"])
def test_reconcile_rejects_stale_or_non_uncertain_targets(execution, change):
    from jailbee import issue_outbox
    from jailbee.outbox_io import ContainerIdentity, JournalError, journal_key

    batch = execution["batch"]({"a.json": [_comment(), _comment()]})
    key = _seed_execution(execution, batch, state="applied" if change == "applied" else "uncertain")
    before = execution["store"].load(key)
    kwargs = {}
    if change == "digest":
        kwargs["digest"] = "0" * 64
    elif change == "identity":
        kwargs["key"] = journal_key(ContainerIdentity("other", "2026-09-18"), "a.json")
    elif change == "name":
        kwargs["manifest"] = replace(batch.manifests[0].manifest, name="other.json")
    elif change == "count":
        kwargs["manifest"] = replace(batch.manifests[0].manifest, actions=())
    elif change == "repo":
        kwargs["repo"] = replace(batch.manifests[0].actions[0].repo, slug="wrong/repo")
    elif change in ("index", "pending"):
        kwargs["index"] = 99 if change == "index" else 1
    with pytest.raises(JournalError):
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

    batch = execution["batch"]({"a.json": [_create(), {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Next"}]})
    key = _seed_execution(execution, batch)
    execution["store"].mark_prepared(key, 1, repo="acme/app")
    _reconcile(execution, batch, issue_outbox.AppliedResolution("https://github.com/acme/app/issues/901#issuecomment-45", issue=None), index=1)
    receipt = execution["store"].load(key).actions[1]
    assert receipt.state == "applied" and receipt.issue is None


def test_cleanup_logs_receipts_then_deletes_only_unshared_bodies_then_archives(execution, mocker):
    from jailbee.outbox_io import journal_key

    action = {"type": "comment", "repo": ".", "issue": 7, "body_file": "shared.md"}
    batch = execution["batch"]({"a.json": [action, {**action, "body_file": "private.md"}], "b.json": [action]}, extras={"shared.md": "Shared secret body", "private.md": "Private secret body", "unrelated.md": "Keep"})
    batch = replace(batch, manifests=(batch.manifests[0],))
    store = execution["store"]
    archive = store.archive
    def checked_archive(key):
        assert "a.json" not in execution["files"]
        execution["events"].append(("archive", key.manifest_name))
        return archive(key)
    mocker.patch.object(store, "archive", side_effect=checked_archive)

    report = _apply(execution, batch)

    assert report.failure is None
    assert [event[0] for event in execution["events"]] == ["log", "delete", "archive"]
    assert set(execution["remove"].call_args.args[3]) == {"a.json", "private.md"}
    assert set(execution["files"]) == {"b.json", "shared.md", "unrelated.md"}
    lines = execution["log"].call_args.args[3]
    assert len(lines) == 2
    for index, line in enumerate(lines):
        record = json.loads(line)
        assert record["manifest"] == "a.json"
        assert record["index"] == index
        assert record["repo"] == "acme/app"
        assert record["issue"] == 7
        assert record["url"] == "https://github.com/acme/app/issues/7"
        assert "T" in record["timestamp"]
        assert "secret body" not in line
    assert store.load(journal_key(batch.identity, "a.json")) is None


@pytest.mark.parametrize("boundary", ["log", "remove"])
def test_cleanup_failure_keeps_journal_and_rerun_skips_github(execution, boundary):
    from jailbee.incus import IncusError
    from jailbee.outbox_io import journal_key

    batch = execution["batch"]({"a.json": [_comment()]})
    operation = execution[boundary]
    original = operation.side_effect
    operation.side_effect = IncusError("offline")
    report = _apply(execution, batch)
    assert report.failure.index is None
    assert not report.failure.uncertain
    assert execution["store"].load(journal_key(batch.identity, "a.json")).actions[0].state == "applied"
    operation.side_effect = original
    assert _apply(execution, batch).failure is None
    assert execution["mutations"]["add_comment"].call_count == 1


def test_cleanup_preserves_shared_files_added_since_approval(execution):
    action = {"type": "comment", "repo": ".", "issue": 7, "body_file": "shared.md"}
    batch = execution["batch"]({"a.json": [action]}, extras={"shared.md": "Shared"})
    def mutate(*args, **kwargs):
        execution["files"]["later.json"] = json.dumps({"version": 1, "actions": [action]})
        return execution["mutations"]["add_comment"].return_value
    execution["mutations"]["add_comment"].side_effect = mutate
    assert _apply(execution, batch).failure is None
    assert "shared.md" in execution["files"]


def test_cleanup_invalid_pending_manifest_conservatively_keeps_body_files(execution):
    batch = execution["batch"]({"a.json": [{"type": "comment", "repo": ".", "issue": 7, "body_file": "body.md"}]}, extras={"body.md": "Body", "broken.json": "{"})
    assert _apply(execution, batch).failure is None
    assert "body.md" in execution["files"]


def test_completed_manifest_filename_can_be_reused_with_new_digest(execution):
    first = execution["batch"]({"a.json": [_comment(body="First")]})
    assert _apply(execution, first).failure is None
    second = execution["batch"]({"a.json": [_comment(body="Second")]})
    assert _apply(execution, second).failure is None
    assert execution["mutations"]["add_comment"].call_count == 2
    archives = list(execution["store"].root.glob("*/archive/*.json"))
    assert {json.loads(path.read_text())["digest"] for path in archives} == {first.manifests[0].digest, second.manifests[0].digest}


def _drop(execution, batch, **kwargs):
    from jailbee import issue_outbox

    return issue_outbox.drop_manifest(execution["incus"], batch.container, batch.outbox, "a.json", uid=1000, journal_store=execution["store"], identity=batch.identity, **kwargs)


@pytest.mark.parametrize("state, archive, allowed", [(None, False, True), ("applied", False, False), ("prepared", False, False), ("uncertain", True, False), ("prepared", True, False), ("applied", True, True)])
def test_drop_refuses_progress_by_default_and_never_discards_uncertainty(execution, state, archive, allowed):
    from jailbee.outbox_io import JournalError, journal_key

    action = {"type": "comment", "repo": ".", "issue": 7, "body_file": "body.md"}
    batch = execution["batch"]({"a.json": [action, _comment()]}, extras={"body.md": "Body"})
    if state:
        _seed_execution(execution, batch, state=state)
    if allowed:
        assert _drop(execution, batch, archive_journal=archive) == ("a.json", "body.md")
        assert execution["store"].load(journal_key(batch.identity, "a.json")) is None
        assert not execution["files"]
    else:
        with pytest.raises(JournalError):
            _drop(execution, batch, archive_journal=archive)
        assert "a.json" in execution["files"]
        execution["remove"].assert_not_called()
    assert all(not mutation.called for mutation in execution["mutations"].values())


def test_drop_delete_failure_keeps_settled_partial_journal(execution):
    from jailbee.incus import IncusError

    batch = execution["batch"]({"a.json": [_comment(), _comment()]})
    key = _seed_execution(execution, batch)
    execution["remove"].side_effect = IncusError("offline")
    with pytest.raises(IncusError):
        _drop(execution, batch, archive_journal=True)
    assert execution["store"].load(key).actions[0].state == "applied"


def test_drop_refuses_changed_proposal_and_preserves_shared_bodies(execution):
    from jailbee.outbox_io import JournalError

    action = {"type": "comment", "repo": ".", "issue": 7, "body_file": "shared.md"}
    batch = execution["batch"]({"a.json": [action], "b.json": [action]}, extras={"shared.md": "Shared"})
    execution["files"]["a.json"] += "\n"
    with pytest.raises(JournalError):
        _drop(execution, batch)
    execution["files"]["a.json"] = batch.outbox.files["a.json"]
    assert _drop(execution, batch) == ("a.json",)
    assert "shared.md" in execution["files"]
