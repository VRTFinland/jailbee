import json
from unittest.mock import MagicMock

import pytest


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("refs/jailbee/from/child/feature/nested", "feature/nested"),
        ("refs/heads/feature/nested", "feature/nested"),
        ("refs/tags/release", "release"),
    ],
)
def test_default_head_preserves_imported_branch_path(source, expected):
    from jailbee.branch_pr import default_head

    assert default_head(source) == expected


@pytest.fixture
def source_publisher(mocker, make_cfg, tmp_path):
    from contextlib import nullcontext

    from jailbee.outbox_io import ContainerIdentity
    from jailbee.pr import PrCreated
    from jailbee.pr_outbox import Outbox

    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    incus.config_get_checked.return_value = None
    data = {
        "version": 1,
        "repo": "acme/widgets",
        "pr": None,
        "head_sha": None,
        "source": "refs/jailbee/from/child/feature/a",
        "source_sha": "a" * 40,
        "base": "main",
        "actions": [{"type": "description", "title": "Title", "body": "Body"}],
    }
    snapshot = Outbox({"m.json": json.dumps(data)}, identity=ContainerIdentity("full", "created"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="full")
    mocker.patch("jailbee.pr_flow.outbox_publication_guard", return_value=nullcontext())
    read = mocker.patch("jailbee.pr_outbox.read_outbox", return_value=snapshot)
    mocker.patch("jailbee.pr_outbox.scope_slug", return_value="acme/widgets")
    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    base_fetch = mocker.patch("jailbee.pr.fetch_base_ref", return_value="c" * 40)
    count = mocker.patch("jailbee.git.count_commits_between", return_value="3")
    remote = mocker.patch("jailbee.git.remote_branch_sha_strict", return_value=None)
    fetch = mocker.patch(
        "jailbee.sync.fetch_ref_from_container", return_value="refs/jailbee/host/source"
    )
    ff = mocker.patch("jailbee.sync.is_fast_forward", return_value=True)
    push = mocker.patch("jailbee.sync.push_ref_to_remote")
    create = mocker.patch(
        "jailbee.pr.create_pr",
        return_value=PrCreated(42, "https://github.com/acme/widgets/pull/42", False),
    )
    edit = mocker.patch("jailbee.pr.edit_pr")
    resolve = mocker.patch("jailbee.pr.resolve_pr")
    discover = mocker.patch(
        "jailbee.pr.find_open_pr_for_branch_strict", create=True, return_value=None
    )
    from jailbee.pr_outbox import consume_manifest as real_consume

    consume = mocker.patch("jailbee.pr_outbox.consume_manifest", create=True)
    return locals() | {"real_consume": real_consume}


def test_pending_lists_source_manifests_in_order(source_publisher):
    from jailbee.branch_pr import PendingSource, pending_source_manifests

    p = source_publisher
    other = dict(p["data"], source="refs/heads/other")
    other.pop("base")
    other["actions"] = [{"type": "description", "title": "Other", "body": "Text"}]
    p["snapshot"].files["a.json"] = json.dumps(other)
    assert pending_source_manifests(p["cfg"], p["incus"], "full") == [
        PendingSource("a.json", "Other", "refs/heads/other", None),
        PendingSource("m.json", "Title", "refs/jailbee/from/child/feature/a", "main"),
    ]
    p["read"].assert_called_once_with(
        p["incus"], "full", uid=p["cfg"].container_user.uid, strict=True
    )


def test_pending_skips_own_branch_description(source_publisher):
    from jailbee.branch_pr import pending_source_manifests

    p = source_publisher
    data = dict(p["data"])
    for key in ("source", "source_sha", "base"):
        data.pop(key)
    p["snapshot"].files["m.json"] = json.dumps(data)
    assert pending_source_manifests(p["cfg"], p["incus"], "full") == []


def test_pending_skips_published_receipt(source_publisher, mocker):
    from jailbee.branch_pr import pending_source_manifests

    p = source_publisher
    mocker.patch("jailbee.pr_outbox.source_publication_receipt", return_value={"url": "url"})
    assert pending_source_manifests(p["cfg"], p["incus"], "full") == []


def test_pending_skips_rejected_manifest(source_publisher):
    from jailbee.branch_pr import pending_source_manifests

    p = source_publisher
    p["snapshot"].files["m.json"] = "invalid"
    assert pending_source_manifests(p["cfg"], p["incus"], "full") == []


@pytest.mark.parametrize("incomplete", ["identity", "rejected"])
def test_pending_incomplete_snapshot_is_empty(source_publisher, incomplete):
    from dataclasses import replace

    from jailbee.branch_pr import pending_source_manifests

    p = source_publisher
    changes = {"identity": None} if incomplete == "identity" else {"rejected": ("bad.json",)}
    p["read"].return_value = replace(p["snapshot"], **changes)
    assert pending_source_manifests(p["cfg"], p["incus"], "full") == []


def test_pending_real_reader_skips_snapshot_with_non_utf8_neighbor(mocker, make_cfg, tmp_path):
    from jailbee.branch_pr import pending_source_manifests
    from jailbee.outbox_io import ContainerIdentity
    from tests.test_outbox_io_strict import encoded_snapshot

    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    data = {
        "version": 1,
        "repo": "acme/widgets",
        "pr": None,
        "head_sha": None,
        "source": "refs/heads/topic",
        "source_sha": "a" * 40,
        "actions": [{"type": "description", "title": "Title", "body": "Body"}],
    }
    incus.exec.return_value = encoded_snapshot(
        {
            "m.json": json.dumps(data).encode(),
            "bad.json": b"\xff",
        }
    )
    mocker.patch(
        "jailbee.pr_outbox.container_identity", return_value=ContainerIdentity("full", "created")
    )
    mocker.patch("jailbee.pr_outbox._same_identity")

    assert pending_source_manifests(cfg, incus, "full") == []


def test_pending_unreadable_outbox_is_empty(source_publisher):
    from jailbee.branch_pr import pending_source_manifests
    from jailbee.outbox_io import OutboxReadError

    p = source_publisher
    p["read"].side_effect = OutboxReadError("unreadable")
    assert pending_source_manifests(p["cfg"], p["incus"], "full") == []


@pytest.mark.parametrize("selected", [0, 1, 2])
def test_choose_pr_source_lists_own_branch_first(mocker, selected):
    from jailbee.branch_pr import PendingSource, choose_pr_source

    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    pending = [
        PendingSource("a.json", "First", "topic", "main"),
        PendingSource("b.json", "Second", "other", None),
    ]

    def select(noun, options):
        assert noun == "PR source"
        assert options[0].title == "container branch"
        assert "First" in options[1].title and "a.json" in options[1].title
        assert "topic" in options[1].title and "main" in options[1].title
        assert "default base" in options[2].title
        return options[selected].value

    mocker.patch("jailbee.prompting._select", side_effect=select)
    assert (
        choose_pr_source(pending, own_label="container branch")
        == [None, "a.json", "b.json"][selected]
    )


@pytest.mark.parametrize(
    "case",
    ["default", "base", "remote", "unbound_number", "unbound_target", "bound_base", "new_base"],
)
def test_own_branches_only_refuses_before_fetch_or_push(source_publisher, case):
    from jailbee.branch_pr import publish_source_manifest
    from jailbee.pr import PrError, PrInfo

    p = source_publisher
    data = json.loads(p["snapshot"].files["m.json"])
    if case in {"default", "base"}:
        data["actions"][0]["branch"] = "main" if case == "default" else "develop"
        if case == "base":
            data["base"] = "develop"
    elif case == "remote":
        p["remote"].return_value = "b" * 40
    elif case == "unbound_number":
        data["pr"] = 77
        p["resolve"].return_value = PrInfo(77, "feature/a", "b" * 40, "OPEN", "main")
    elif case == "unbound_target":
        p["discover"].return_value = PrInfo(77, "feature/a", "b" * 40, "OPEN", "main")
    elif case == "bound_base":
        data["pr"] = 77
        p["incus"].config_get_checked.side_effect = lambda full, key: (
            "77" if key == "user.jailbee.pr" else None
        )
        p["resolve"].return_value = PrInfo(77, "feature/a", "b" * 40, "OPEN", "develop")
    else:
        data["base"] = "develop"
    p["snapshot"].files["m.json"] = json.dumps(data)
    with pytest.raises(PrError):
        publish_source_manifest(
            p["cfg"],
            p["incus"],
            "c",
            "m.json",
            force=False,
            yes=True,
            confirm=lambda _: True,
            own_branches_only=True,
        )
    p["fetch"].assert_not_called()
    p["push"].assert_not_called()


@pytest.mark.parametrize("label", ["user.jailbee.pr", "user.jailbee.stacked_pr"])
def test_own_source_refuses_review_label_only(source_publisher, label):
    from jailbee.branch_pr import publish_source_manifest
    from jailbee.pr import PrError, PrInfo
    p = source_publisher
    data = json.loads(p["snapshot"].files["m.json"])
    data["pr"] = 42
    p["snapshot"].files["m.json"] = json.dumps(data)
    p["incus"].config_get_checked.side_effect = lambda full, key: "42" if key == label else None
    p["resolve"].return_value = PrInfo(42, "colleague", "b" * 40, "OPEN", "main")
    with pytest.raises(PrError, match="bound"):
        publish_source_manifest(p["cfg"], p["incus"], "c", "m.json", force=False, yes=True, confirm=lambda _: True, own_branches_only=True)
    p["push"].assert_not_called()
    p["edit"].assert_not_called()


@pytest.mark.parametrize("authored", [False, True])
def test_own_source_requires_authored_binding(source_publisher, authored):
    from jailbee.branch_pr import BRANCH_PRS_LABEL, publish_source_manifest
    from jailbee.pr import PrError, PrInfo
    p = source_publisher
    entry = {"feature/a": {"head": "feature/a", "pr": 42, "base": "main", "source": "refs/heads/topic", "source_sha": "a" * 40, "author": authored}}
    p["incus"].config_get_checked.side_effect = lambda full, key: json.dumps(entry) if key == BRANCH_PRS_LABEL else None
    p["resolve"].return_value = PrInfo(42, "feature/a", "b" * 40, "OPEN", "main")
    if authored:
        assert publish_source_manifest(p["cfg"], p["incus"], "c", "m.json", force=False, yes=True, confirm=lambda _: False, own_branches_only=True) == 0
    else:
        with pytest.raises(PrError, match="bound"):
            publish_source_manifest(p["cfg"], p["incus"], "c", "m.json", force=False, yes=True, confirm=lambda _: True, own_branches_only=True)
        p["push"].assert_not_called()


@pytest.mark.parametrize("bound", [False, True])
def test_own_branches_only_opens_or_updates_and_binds(source_publisher, bound):
    from jailbee.branch_pr import publish_source_manifest
    from jailbee.pr import PrInfo

    p = source_publisher
    if bound:
        data = json.loads(p["snapshot"].files["m.json"])
        data["pr"] = 42
        p["snapshot"].files["m.json"] = json.dumps(data)
        p["incus"].config_get_checked.side_effect = lambda full, key: (
            "42" if key == "user.jailbee.pr" else None
        )
        p["resolve"].return_value = PrInfo(42, "feature/a", "b" * 40, "OPEN", "main")
    if bound:
        from jailbee.pr import PrError
        with pytest.raises(PrError, match="bound"):
            publish_source_manifest(p["cfg"], p["incus"], "c", "m.json", force=False, yes=True, confirm=lambda _: False, own_branches_only=True)
        p["push"].assert_not_called()
        return
    lines = []
    assert (
        publish_source_manifest(
            p["cfg"],
            p["incus"],
            "c",
            "m.json",
            force=False,
            yes=True,
            confirm=lambda _: False,
            own_branches_only=True,
            result_sink=lines.append,
        )
        == 0
    )
    assert lines == [
        f"{'updated' if bound else 'opened'} PR #42: https://github.com/acme/widgets/pull/42"
    ]
    assert json.loads(p["incus"].config_set.call_args.args[2])["feature/a"]["pr"] == 42


def test_broker_source_worker_runs_real_domain_gates(source_publisher, tmp_path, mocker):
    from jailbee.orchestration import publishing
    p = source_publisher
    mocker.patch.object(publishing, "Incus", return_value=p["incus"])
    entry = {"ref": "pub-real", "kind": "source", "manifest": "m.json", "actions": [{"kind": "source", "repo": "acme/widgets"}], "rights": {"pull_requests": True}}
    publishing._run(p["cfg"], tmp_path, "full", "c", entry)
    result = json.loads(publishing._result(tmp_path, "full", "pub-real").read_text())
    assert result["state"] == "done"
    assert result["actions"] == [{"kind": "source", "repo": "acme/widgets"}]
    assert result["urls"] == ["https://github.com/acme/widgets/pull/42"]
    assert p["push"].call_args.kwargs["force"] is False
    assert p["push"].call_args.kwargs["require_absent"] is True
    assert json.loads(p["incus"].config_set.call_args.args[2])["feature/a"]["author"] is True


def test_own_source_rechecks_new_remote_and_uses_absent_lease(source_publisher):
    from jailbee.branch_pr import publish_source_manifest
    from jailbee.pr import PrError

    p = source_publisher
    p["remote"].side_effect = [None, "b" * 40]
    with pytest.raises(PrError, match="already exists"):
        publish_source_manifest(
            p["cfg"],
            p["incus"],
            "c",
            "m.json",
            force=False,
            yes=True,
            confirm=lambda _: False,
            own_branches_only=True,
        )
    p["push"].assert_not_called()


def test_own_new_source_push_requires_remote_absence(source_publisher):
    from jailbee.branch_pr import publish_source_manifest

    p = source_publisher
    assert (
        publish_source_manifest(
            p["cfg"],
            p["incus"],
            "c",
            "m.json",
            force=False,
            yes=True,
            confirm=lambda _: False,
            own_branches_only=True,
        )
        == 0
    )
    assert p["push"].call_args.kwargs["require_absent"] is True


def _publish(p, *, yes=True, force=False, confirm=lambda _: True):
    from jailbee.branch_pr import publish_source_manifest

    return publish_source_manifest(
        p["cfg"], p["incus"], "c", "m.json", force=force, yes=yes, confirm=confirm
    )


@pytest.mark.parametrize("existing", [False, True])
def test_source_large_escaped_progress_refused_before_publication(source_publisher, existing):
    from jailbee.pr import PrInfo
    from jailbee.pr_outbox import parse_manifest

    p = source_publisher
    data = json.loads(p["snapshot"].files["m.json"])
    data["actions"][0]["body"] = "\\" * 65536
    p["snapshot"].files["m.json"] = json.dumps(data)
    assert parse_manifest("m.json", p["snapshot"].files["m.json"], p["snapshot"].files)
    if existing:
        p["discover"].return_value = PrInfo(42, "feature/a", "b" * 40, "OPEN", "main")
    assert _publish(p) == 1
    for key in ("push", "edit", "create", "consume"):
        p[key].assert_not_called()


@pytest.mark.parametrize("existing", [False, True])
def test_source_near_full_log_refused_before_remote_effects(source_publisher, existing):
    from jailbee.outbox.io import FILE_LIMIT
    from jailbee.pr import PrInfo

    p = source_publisher
    p["snapshot"].files["applied.log"] = "h" * (FILE_LIMIT - 1)
    if existing:
        p["discover"].return_value = PrInfo(42, "feature/a", "b" * 40, "OPEN", "main")
    assert _publish(p) == 1
    for key in ("fetch", "push", "edit", "create", "consume"):
        p[key].assert_not_called()


def test_source_create_pins_push_and_records_binding(source_publisher, capsys):
    p = source_publisher
    assert _publish(p) == 0
    assert "(base main, 3 commits)" in " ".join(capsys.readouterr().out.split())
    p["push"].assert_called_once()
    assert p["push"].call_args.kwargs["expected_sha"] == "a" * 40
    p["base_fetch"].assert_called_once_with(
        p["cfg"].repo_root, "main", remote=p["cfg"].upstream_remote
    )
    p["count"].assert_called_once_with(p["cfg"].repo_root, "c" * 40, "refs/jailbee/host/source")
    assert p["create"].call_args.kwargs["head"] == "feature/a"
    assert p["create"].call_args.kwargs["base"] == "main"
    saved = json.loads(p["incus"].config_set.call_args.args[2])
    assert saved["feature/a"]["pr"] == 42
    p["consume"].assert_called_once()


@pytest.mark.parametrize("consent", [False, True])
def test_source_publish_consent_shows_base_range_count(source_publisher, capsys, consent):
    p = source_publisher
    # The range includes orchestrator commits not reachable from the refreshed base.
    p["count"].return_value = "7"
    questions = []

    def confirm(question):
        questions.append(question)
        assert "(base main, 7 commits)" in " ".join(capsys.readouterr().out.split())
        p["count"].assert_called_once_with(p["cfg"].repo_root, "c" * 40, "refs/jailbee/host/source")
        p["push"].assert_not_called()
        return consent

    assert _publish(p, yes=False, confirm=confirm) == (0 if consent else 1)
    assert questions == ["Publish?"]
    for key in ("push", "create", "consume"):
        assert p[key].call_count == int(consent)


def test_null_source_creates_independent_binding_without_own_label_writes(source_publisher):
    from jailbee.branch_pr import BRANCH_PRS_LABEL

    p = source_publisher
    p["incus"].config_get_checked.side_effect = lambda full, key: (
        "10" if key == "user.jailbee.pr" else None
    )
    assert _publish(p) == 0
    p["resolve"].assert_not_called()
    p["create"].assert_called_once()
    writes = p["incus"].config_set.call_args_list
    assert len(writes) == 1
    assert writes[0].args[:2] == ("full", BRANCH_PRS_LABEL)
    saved = json.loads(writes[0].args[2])
    assert saved["feature/a"]["pr"] == 42
    assert saved["feature/a"]["source_sha"] == "a" * 40
    p["incus"].config_unset.assert_not_called()
    p["edit"].assert_not_called()


def test_source_explicit_branch_conflicting_with_pr_is_refused(source_publisher):
    from jailbee.pr import PrInfo

    p = source_publisher
    data = json.loads(p["snapshot"].files["m.json"])
    data["pr"] = 42
    data["actions"][0]["branch"] = "other"
    p["snapshot"].files["m.json"] = json.dumps(data)
    p["resolve"].return_value = PrInfo(42, "actual", "b" * 40, "OPEN", "main")
    assert _publish(p) == 1
    p["fetch"].assert_not_called()
    p["push"].assert_not_called()


def test_source_base_refresh_failure_aborts_before_push(source_publisher):
    p = source_publisher
    p["base_fetch"].return_value = None
    assert _publish(p) == 1
    p["push"].assert_not_called()


def test_source_update_uses_actual_pr_head_and_skips_equal_remote(source_publisher):
    from jailbee.pr import PrInfo

    p = source_publisher
    data = json.loads(p["snapshot"].files["m.json"])
    data["pr"] = 42
    p["snapshot"].files["m.json"] = json.dumps(data)
    p["incus"].config_get_checked.return_value = "42"
    p["resolve"].return_value = PrInfo(42, "actual", "b" * 40, "OPEN", "develop")
    p["remote"].return_value = "a" * 40
    assert _publish(p) == 0
    p["push"].assert_not_called()
    p["edit"].assert_called_once()
    saved = json.loads(p["incus"].config_set.call_args.args[2])
    assert saved["actual"]["base"] == "develop"


def test_source_foreign_consent_before_fetch_even_with_yes(source_publisher):
    from jailbee.pr import PrInfo

    p = source_publisher
    data = json.loads(p["snapshot"].files["m.json"])
    data["pr"] = 42
    p["snapshot"].files["m.json"] = json.dumps(data)
    p["resolve"].return_value = PrInfo(42, "head", "b" * 40, "OPEN", "main")
    assert _publish(p, confirm=lambda _: False) == 1
    p["fetch"].assert_not_called()
    p["push"].assert_not_called()


@pytest.mark.parametrize("label", ["user.jailbee.pr", "user.jailbee.stacked_pr"])
def test_null_source_rejects_container_own_pr_collision(source_publisher, label):
    from jailbee.pr import PrInfo

    p = source_publisher
    p["incus"].config_get_checked.side_effect = lambda full, key: "77" if key == label else None
    p["discover"].return_value = PrInfo(77, "feature/a", "b" * 40, "OPEN", "main")
    assert _publish(p) == 1
    p["fetch"].assert_not_called()
    p["push"].assert_not_called()


def test_unbound_existing_pr_requires_consent_before_fetch(source_publisher):
    from jailbee.pr import PrInfo

    p = source_publisher
    p["discover"].return_value = PrInfo(77, "feature/a", "b" * 40, "OPEN", "develop")
    assert _publish(p, confirm=lambda _: False) == 1
    p["fetch"].assert_not_called()
    p["push"].assert_not_called()


def test_closed_binding_discovers_new_open_pr_before_push(source_publisher):
    from jailbee.branch_pr import BRANCH_PRS_LABEL
    from jailbee.pr import PrInfo

    p = source_publisher
    binding = {
        "feature/a": {
            "head": "feature/a",
            "pr": 41,
            "base": "main",
            "source": "refs/heads/x",
            "source_sha": "a" * 40,
        }
    }
    p["incus"].config_get_checked.side_effect = lambda full, key: (
        json.dumps(binding) if key == BRANCH_PRS_LABEL else None
    )
    p["resolve"].return_value = PrInfo(41, "feature/a", "b" * 40, "MERGED", "main")
    p["discover"].return_value = PrInfo(77, "feature/a", "b" * 40, "OPEN", "main")
    assert _publish(p, confirm=lambda _: False) == 1
    p["fetch"].assert_not_called()


def test_unbound_discovered_pr_uses_authoritative_base(source_publisher, capsys):
    from jailbee.pr import PrInfo

    p = source_publisher
    p["discover"].return_value = PrInfo(77, "feature/a", "b" * 40, "OPEN", "develop")
    assert _publish(p) == 0
    assert "base develop" in capsys.readouterr().out
    p["create"].assert_not_called()
    p["edit"].assert_called_once()


def test_source_success_then_missing_manifest_does_not_repush(source_publisher):
    p = source_publisher
    p["consume"].side_effect = lambda *args, **kwargs: p["snapshot"].files.clear()
    assert _publish(p) == 0
    assert _publish(p) == 0
    assert p["push"].call_count == 1


def test_source_failed_push_can_retry(source_publisher):
    from jailbee.sync import SyncError

    p = source_publisher
    p["push"].side_effect = [SyncError("push failed"), None]
    assert _publish(p) == 1
    p["consume"].assert_not_called()
    assert _publish(p) == 0
    assert p["push"].call_count == 2


def test_source_failed_consumption_retry_does_not_repush(source_publisher):
    from jailbee.pr import PrInfo
    from jailbee.pr_outbox import FinalizeError

    p = source_publisher
    p["consume"].side_effect = [FinalizeError("failed"), None]
    assert _publish(p) == 1
    p["discover"].return_value = PrInfo(42, "feature/a", "a" * 40, "OPEN", "main")
    p["remote"].return_value = "a" * 40
    assert _publish(p) == 0
    assert p["push"].call_count == 1


@pytest.mark.parametrize("remove_manifest", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_source_published_retry_only_cleans_up(source_publisher, mocker, remove_manifest, existing):
    from jailbee.outbox.io import MutationExecutionError
    from jailbee.pr import PrInfo

    p = source_publisher
    p["consume"].side_effect = p["real_consume"]
    if existing:
        p["discover"].return_value = PrInfo(42, "feature/a", "b" * 40, "OPEN", "main")
    p["incus"].list_containers.return_value = [{"name": "full", "created_at": "created"}]
    files = p["snapshot"].files
    failed = False

    def mutate(*args, expected, new_manifest, delete_names, **kwargs):
        nonlocal failed
        assert expected == files
        if new_manifest:
            files[new_manifest[0]] = new_manifest[1]
        if delete_names and not failed:
            failed = True
            if remove_manifest:
                del files["m.json"]
            raise MutationExecutionError("delete failed")
        for name in delete_names:
            del files[name]
        return delete_names

    mocker.patch("jailbee.outbox.io.mutate_store", side_effect=mutate)
    assert _publish(p) == 1
    # A published retry must not even query/fetch the remote, let alone edit it.
    for key in ("push", "fetch", "remote", "base_fetch", "create", "edit", "discover"):
        p[key].reset_mock()
    assert _publish(p) == 0
    for key in ("push", "fetch", "remote", "base_fetch", "create", "edit", "discover"):
        p[key].assert_not_called()
    assert set(files) == {"applied.log"}


def test_source_missing_manifest_is_idempotent(source_publisher):
    p = source_publisher
    p["snapshot"].files.clear()
    assert _publish(p) == 0
    p["push"].assert_not_called()


def test_source_unexpected_existing_pr_create_does_not_bind_or_consume(source_publisher):
    from jailbee.pr import PrCreated

    p = source_publisher
    p["create"].return_value = PrCreated(99, "https://github.com/acme/widgets/pull/99", True)
    assert _publish(p) == 1
    p["incus"].config_set.assert_not_called()
    p["consume"].assert_not_called()


def test_source_sha_mismatch_stops_before_push(source_publisher, capsys):
    from jailbee.sync import SyncError

    p = source_publisher
    p["fetch"].side_effect = SyncError("at bbbbbbb, pins aaaaaaa")
    assert _publish(p) == 1
    assert "bbbbbbb" in capsys.readouterr().err
    p["push"].assert_not_called()


def test_source_cross_repository_refused(source_publisher):
    from jailbee.pr import PrInfo

    p = source_publisher
    data = json.loads(p["snapshot"].files["m.json"])
    data["pr"] = 42
    p["snapshot"].files["m.json"] = json.dumps(data)
    p["resolve"].return_value = PrInfo(
        42, "head", "b" * 40, "OPEN", "main", is_cross_repository=True
    )
    assert _publish(p) == 1
    p["fetch"].assert_not_called()


def test_source_nonfastforward_requires_force(source_publisher, capsys):
    from jailbee.pr import PrInfo

    p = source_publisher
    data = json.loads(p["snapshot"].files["m.json"])
    data["pr"] = 42
    p["snapshot"].files["m.json"] = json.dumps(data)
    p["incus"].config_get_checked.side_effect = lambda full, key: (
        "42" if key == "user.jailbee.pr" else None
    )
    p["resolve"].return_value = PrInfo(42, "head", "b" * 40, "OPEN", "main")
    p["remote"].return_value = "b" * 40
    p["ff"].return_value = False
    assert _publish(p) == 1
    assert "jailbee pr push c --source-manifest m.json --force" in " ".join(
        capsys.readouterr().err.split()
    )
    p["push"].assert_not_called()


def test_source_force_allows_nonfastforward_with_pin(source_publisher):
    from jailbee.pr import PrInfo

    p = source_publisher
    data = json.loads(p["snapshot"].files["m.json"])
    data["pr"] = 42
    p["snapshot"].files["m.json"] = json.dumps(data)
    p["incus"].config_get_checked.side_effect = lambda full, key: (
        "42" if key == "user.jailbee.pr" else None
    )
    p["resolve"].return_value = PrInfo(42, "head", "b" * 40, "OPEN", "main")
    p["remote"].return_value = "b" * 40
    p["ff"].return_value = False
    assert _publish(p, force=True) == 0
    assert p["push"].call_args.kwargs["force"] is True
    assert p["push"].call_args.kwargs["expected_sha"] == "a" * 40


def test_source_pre_push_callback_refuses_mutated_snapshot(source_publisher):
    from jailbee.pr_outbox import Outbox

    p = source_publisher
    changed = Outbox({"m.json": "changed"}, identity=p["snapshot"].identity)

    def before_push(*args, **kwargs):
        p["read"].return_value = changed
        kwargs["on_before_push"]()

    p["push"].side_effect = before_push
    assert _publish(p) == 1
    p["create"].assert_not_called()
    p["consume"].assert_not_called()


def test_source_changed_after_confirmation_never_pushes(source_publisher):
    from jailbee.pr_outbox import Outbox

    p = source_publisher
    changed = Outbox({"m.json": "changed"}, identity=p["snapshot"].identity)
    p["read"].side_effect = [p["snapshot"], changed]
    assert _publish(p) == 1
    p["push"].assert_not_called()
    p["consume"].assert_not_called()


def test_source_zero_actions_updates_without_edit(source_publisher):
    from jailbee.pr import PrInfo

    p = source_publisher
    data = json.loads(p["snapshot"].files["m.json"])
    data.update(pr=42, actions=[])
    p["snapshot"].files["m.json"] = json.dumps(data)
    p["incus"].config_get_checked.side_effect = lambda full, key: (
        "42" if key == "user.jailbee.pr" else None
    )
    p["resolve"].return_value = PrInfo(42, "head", "b" * 40, "OPEN", "main")
    assert _publish(p) == 0
    p["push"].assert_called_once()
    p["edit"].assert_not_called()
    p["consume"].assert_called_once()


def test_source_lookup_failure_never_pushes(source_publisher):
    from jailbee.git import GitError

    p = source_publisher
    p["remote"].side_effect = GitError("network unavailable")
    assert _publish(p) == 1
    p["push"].assert_not_called()
    p["consume"].assert_not_called()


def test_list_pr_bindings_removes_merged_and_shows_own(mocker, make_cfg, tmp_path, capsys):
    from jailbee.branch_pr import BRANCH_PRS_LABEL, list_pr_bindings
    from jailbee.pr import PrInfo

    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    entries = {
        "topic": {
            "head": "topic",
            "pr": 42,
            "base": "main",
            "source": "refs/heads/topic",
            "source_sha": "a" * 40,
        }
    }
    incus.config_get_checked.side_effect = lambda full, key: (
        json.dumps(entries)
        if key == BRANCH_PRS_LABEL
        else ("10" if key == "user.jailbee.pr" else None)
    )
    from types import SimpleNamespace

    mocker.patch("jailbee.lifecycle.list_containers", return_value=[SimpleNamespace(name="full")])
    mocker.patch("jailbee.lifecycle.short_name", return_value="c")
    mocker.patch("jailbee.pr_outbox.scope_slug", return_value="acme/widgets")
    mocker.patch(
        "jailbee.pr.resolve_pr", return_value=PrInfo(42, "topic", "a" * 40, "MERGED", "main")
    )
    assert list_pr_bindings(cfg, incus) == 0
    output = capsys.readouterr()
    assert "#10" in output.out
    assert "42" in output.out + output.err
    incus.config_unset_checked.assert_called_once_with("full", BRANCH_PRS_LABEL)


def test_list_pr_bindings_shows_open_source(mocker, make_cfg, tmp_path, capsys):
    from types import SimpleNamespace

    from jailbee.branch_pr import BRANCH_PRS_LABEL, list_pr_bindings
    from jailbee.pr import PrInfo

    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    entries = {
        "topic": {
            "head": "topic",
            "pr": 42,
            "base": "main",
            "source": "refs/heads/topic",
            "source_sha": "a" * 40,
        }
    }
    incus.config_get_checked.side_effect = lambda full, key: (
        json.dumps(entries) if key == BRANCH_PRS_LABEL else None
    )
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[SimpleNamespace(name="full")])
    mocker.patch("jailbee.lifecycle.short_name", return_value="c")
    mocker.patch("jailbee.pr_outbox.scope_slug", return_value="acme/widgets")
    mocker.patch(
        "jailbee.pr.resolve_pr", return_value=PrInfo(42, "topic", "a" * 40, "OPEN", "main")
    )
    assert list_pr_bindings(cfg, incus) == 0
    assert "c  #42  topic ← refs/heads/topic" in capsys.readouterr().out
    incus.config_unset_checked.assert_not_called()


def test_round_trip_and_drop():
    from jailbee.branch_pr import BRANCH_PRS_LABEL, BranchPr, BranchPrMap

    incus = MagicMock()
    labels = {}
    incus.config_get_checked.side_effect = lambda full, key: labels.get(key)
    incus.config_set.side_effect = lambda full, key, value: labels.update({key: value})
    incus.config_unset_checked.side_effect = lambda full, key: labels.pop(key, None)
    state = BranchPrMap(incus, "c")
    a = BranchPr("a", 42, "main", "src/a", "a" * 40)
    b = BranchPr("b", 43, "main", "src/b", "b" * 40, False, True)
    state.put(a)
    state.put(b)
    assert state.read() == {"a": a, "b": b}
    assert state.get("a") == a
    assert state.numbers() == frozenset({42, 43})
    assert state.drop("a") is True
    assert state.read() == {"b": b}
    assert state.drop("missing") is False
    assert state.drop("b") is True
    incus.config_unset_checked.assert_called_once_with("c", BRANCH_PRS_LABEL)
    assert incus.config_set.call_count == 3
    assert state.read() == {}


@pytest.mark.parametrize("value", ["not json", "[]", "null", json.dumps({"a": {}, "b": 1})])
def test_malformed_label_is_ignored(value):
    from jailbee.branch_pr import BranchPrMap

    incus = MagicMock()
    incus.config_get_checked.return_value = value
    assert BranchPrMap(incus, "c").read() == {}


def test_malformed_entries_are_skipped():
    from jailbee.branch_pr import BranchPrMap

    incus = MagicMock()
    incus.config_get_checked.return_value = json.dumps(
        {
            "ok": {"head": "ok", "pr": 42, "base": "main", "source": "s", "source_sha": "x"},
            "bad": {"head": "bad", "pr": True, "base": "main", "source": "s", "source_sha": "x"},
        }
    )
    assert list(BranchPrMap(incus, "c").read()) == ["ok"]


def test_checked_read_error_prevents_overwrite():
    from jailbee.branch_pr import BranchPr, BranchPrMap
    from jailbee.incus import IncusError

    incus = MagicMock()
    incus.config_get_checked.side_effect = IncusError("read failed")
    state = BranchPrMap(incus, "c")
    with pytest.raises(IncusError, match="read failed"):
        state.put(BranchPr("a", 42, "main", "source", "x"))
    incus.config_set.assert_not_called()


def test_deeply_nested_json_is_ignored():
    from jailbee.branch_pr import BranchPrMap

    incus = MagicMock()
    value = "[" * 100000 + "]" * 100000
    incus.config_get_checked.return_value = value
    assert BranchPrMap(incus, "c").read() == {}
