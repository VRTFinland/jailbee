import json
from collections.abc import MutableMapping
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


class StoreFiles(MutableMapping):
    """The PR proposals of one instance in the host outbox store, by name -> manifest."""

    def __init__(self, uuid):
        self.uuid = uuid

    def _store(self):
        from jailbee.orchestration.store import BrokerStore, store_path
        from jailbee.outbox import publication_db

        return BrokerStore(store_path(publication_db.default_root()))

    def _rows(self):
        from jailbee.outbox import publication_db, store_db

        return {
            p.name: p.manifest
            for p in store_db.read_proposals(publication_db.default_root(), self.uuid, "pr")
        }

    def __getitem__(self, name):
        return self._rows()[name]

    def __setitem__(self, name, text):
        with self._store().transaction() as con:
            key = (self.uuid, name)
            if con.execute(
                "SELECT 1 FROM outbox_proposal WHERE uuid=? AND kind='pr' AND name=?", key
            ).fetchone():
                con.execute(
                    "UPDATE outbox_proposal SET manifest=?, revision=revision+1 "
                    "WHERE uuid=? AND kind='pr' AND name=?",
                    (text, *key),
                )
                return
            (seq,) = con.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM outbox_proposal WHERE uuid=? AND kind='pr'",
                (self.uuid,),
            ).fetchone()
            con.execute(
                "INSERT INTO outbox_proposal(uuid, kind, name, seq, manifest, created_at) "
                "VALUES(?, 'pr', ?, ?, ?, 0)",
                (self.uuid, name, seq, text),
            )

    def __delitem__(self, name):
        with self._store().transaction() as con:
            con.execute(
                "DELETE FROM outbox_proposal WHERE uuid=? AND kind='pr' AND name=?",
                (self.uuid, name),
            )

    def __iter__(self):
        return iter(self._rows())

    def __len__(self):
        return len(self._rows())


def journal(uuid="uuid-full", name="m.json"):
    from jailbee.outbox import publication_db

    return publication_db.open_publications().load(uuid, "pr", name)


@pytest.fixture
def source_publisher(mocker, make_cfg, tmp_path):
    from types import SimpleNamespace

    from jailbee.pr import PrCreated

    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    incus.config_get_checked.return_value = None
    # The Sent log keys records by the instance uuid.
    incus.list_containers.return_value = [
        {"name": "full", "created_at": "created", "config": {"volatile.uuid": "uuid-full"}}
    ]
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
    # Source proposals live in the host outbox store, keyed by the instance uuid.
    files = StoreFiles("uuid-full")
    files["m.json"] = json.dumps(data)
    snapshot = SimpleNamespace(files=files)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="full")
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
    return locals()


def consumed(p):
    """Whether the publication removed the proposal from the store."""
    return "m.json" not in p["files"]


def test_pending_lists_source_manifests_in_order(source_publisher):
    from jailbee.branch_pr import PendingSource, pending_source_manifests

    p = source_publisher
    other = dict(p["data"], source="refs/heads/other")
    other.pop("base")
    other["actions"] = [{"type": "description", "title": "Other", "body": "Text"}]
    p["files"]["a.json"] = json.dumps(other)
    assert pending_source_manifests(p["cfg"], p["incus"], "full") == [
        PendingSource("m.json", "Title", "refs/jailbee/from/child/feature/a", "main"),
        PendingSource("a.json", "Other", "refs/heads/other", None),
    ]
    p["incus"].exec.assert_not_called()


def test_pending_skips_own_branch_description(source_publisher):
    from jailbee.branch_pr import pending_source_manifests

    p = source_publisher
    data = dict(p["data"])
    for key in ("source", "source_sha", "base"):
        data.pop(key)
    p["files"]["m.json"] = json.dumps(data)
    assert pending_source_manifests(p["cfg"], p["incus"], "full") == []


def test_pending_skips_invalid_manifest(source_publisher):
    from jailbee.branch_pr import pending_source_manifests

    p = source_publisher
    p["files"]["m.json"] = "invalid"
    assert pending_source_manifests(p["cfg"], p["incus"], "full") == []


def test_pending_ignores_another_instance(source_publisher):
    from jailbee.branch_pr import pending_source_manifests

    p = source_publisher
    p["incus"].list_containers.return_value = [
        {"name": "full", "created_at": "created", "config": {"volatile.uuid": "uuid-new"}}
    ]
    assert pending_source_manifests(p["cfg"], p["incus"], "full") == []


def test_pending_unreadable_store_is_empty(source_publisher, mocker):
    from jailbee.branch_pr import pending_source_manifests
    from jailbee.orchestration.store import StoreError

    p = source_publisher
    mocker.patch("jailbee.outbox.store_db.read_proposals", side_effect=StoreError("locked"))
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
            publisher="user",
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
        publish_source_manifest(
            p["cfg"],
            p["incus"],
            "c",
            "m.json",
            force=False,
            yes=True,
            confirm=lambda _: True,
            own_branches_only=True,
            publisher="orchestrator",
        )
    p["push"].assert_not_called()
    p["edit"].assert_not_called()


@pytest.mark.parametrize("authored,adopted", [(False, False), (True, False), (True, True)])
def test_own_source_requires_authored_binding(source_publisher, authored, adopted):
    from jailbee.branch_pr import BRANCH_PRS_LABEL, publish_source_manifest
    from jailbee.pr import PrError, PrInfo

    p = source_publisher
    entry = {
        "feature/a": {
            "head": "feature/a",
            "pr": 42,
            "base": "main",
            "source": "refs/heads/topic",
            "source_sha": "a" * 40,
            "author": authored,
            "adopted": adopted,
        }
    }
    p["incus"].config_get_checked.side_effect = lambda full, key: (
        json.dumps(entry) if key == BRANCH_PRS_LABEL else None
    )
    p["resolve"].return_value = PrInfo(42, "feature/a", "b" * 40, "OPEN", "main")
    if authored and not adopted:
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
                publisher="orchestrator",
            )
            == 0
        )
        saved = json.loads(p["incus"].config_set.call_args.args[2])["feature/a"]
        assert saved["author"] is True and saved["adopted"] is False
    else:
        with pytest.raises(PrError, match="bound"):
            publish_source_manifest(
                p["cfg"],
                p["incus"],
                "c",
                "m.json",
                force=False,
                yes=True,
                confirm=lambda _: True,
                own_branches_only=True,
                publisher="orchestrator",
            )
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
            publish_source_manifest(
                p["cfg"],
                p["incus"],
                "c",
                "m.json",
                force=False,
                yes=True,
                confirm=lambda _: False,
                own_branches_only=True,
                publisher="orchestrator",
            )
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
            publisher="user",
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
    entry = {
        "ref": "pub-real",
        "kind": "source",
        "manifest": "m.json",
        "actions": [{"kind": "source", "repo": "acme/widgets"}],
        "rights": {"pull_requests": True},
    }
    publishing._run(p["cfg"], tmp_path, "full", "c", entry)
    result = publishing._get_result(tmp_path, "full", "pub-real")
    assert result is not None
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
            publisher="user",
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
            publisher="user",
        )
        == 0
    )
    assert p["push"].call_args.kwargs["require_absent"] is True


def _publish(p, *, yes=True, force=False, confirm=lambda _: True):
    from jailbee.branch_pr import publish_source_manifest

    return publish_source_manifest(
        p["cfg"], p["incus"], "c", "m.json", force=force, yes=yes, confirm=confirm, publisher="user"
    )


def publish_own(p):
    from jailbee.branch_pr import publish_source_manifest

    return publish_source_manifest(
        p["cfg"],
        p["incus"],
        "c",
        "m.json",
        force=False,
        yes=True,
        confirm=lambda _: True,
        own_branches_only=True,
        publisher="orchestrator",
    )


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
    assert consumed(p)
    p["incus"].exec.assert_not_called()


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
    for key in ("push", "create"):
        assert p[key].call_count == int(consent)
    assert consumed(p) is consent


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
    assert _publish(p) == 0
    assert _publish(p) == 0
    assert p["push"].call_count == 1


def test_source_failed_push_can_retry(source_publisher):
    from jailbee.sync import SyncError

    p = source_publisher
    p["push"].side_effect = [SyncError("push failed"), None]
    assert _publish(p) == 1
    assert not consumed(p)
    assert journal().actions == ()  # the push is repeatable: nothing journaled
    assert _publish(p) == 0
    assert p["push"].call_count == 2


def test_source_failed_receipt_retry_asks_before_republishing(source_publisher, mocker, capsys):
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox.publication_db import PublicationStore
    from jailbee.pr import PrInfo

    p = source_publisher
    broken = mocker.patch.object(
        PublicationStore, "settle", autospec=True, side_effect=StoreError("disk full")
    )
    assert _publish(p) == 1
    assert "could not be recorded" in capsys.readouterr().err
    assert not consumed(p)
    (action,) = journal().actions
    assert action.state == "uncertain"
    mocker.stop(broken)  # the store works again
    p["discover"].return_value = PrInfo(42, "feature/a", "a" * 40, "OPEN", "main")
    p["remote"].return_value = "a" * 40
    questions = []
    assert _publish(p, confirm=lambda q: questions.append(q) or True) == 0
    assert "uncertain" in questions[0]
    assert p["push"].call_count == 1
    assert consumed(p)


@pytest.mark.parametrize("remove_manifest", [False, True])
def test_source_published_retry_only_cleans_up(source_publisher, mocker, remove_manifest):
    from jailbee.outbox.publication_db import PublicationStore

    p = source_publisher
    # The receipt committed, but the proposal's removal did not (a changed row,
    # then put back): only the cleanup is left for the next run.
    stuck = mocker.patch.object(
        PublicationStore, "_finalize", autospec=True, return_value=(False, False)
    )
    assert _publish(p) == 0
    assert not consumed(p)
    assert journal().complete
    mocker.stop(stuck)
    for key in ("push", "fetch", "remote", "base_fetch", "create", "edit", "discover"):
        p[key].reset_mock()
    assert _publish(p) == 0
    for key in ("push", "fetch", "remote", "base_fetch", "create", "edit", "discover"):
        p[key].assert_not_called()
    assert consumed(p)
    assert journal() is None  # archived with the removal


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
    assert not consumed(p)
    assert journal().actions == ()  # definitely not published by this run


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
    p = source_publisher

    def before_push(*args, **kwargs):
        p["files"]["m.json"] = json.dumps(dict(p["data"], base="develop"))
        kwargs["on_before_push"]()

    p["push"].side_effect = before_push
    assert _publish(p) == 1
    p["create"].assert_not_called()
    assert journal().actions == ()


def test_source_changed_after_confirmation_never_pushes(source_publisher):
    p = source_publisher

    def confirm(question):
        p["files"]["m.json"] = json.dumps(dict(p["data"], base="develop"))
        return True

    assert _publish(p, yes=False, confirm=confirm) == 1
    p["push"].assert_not_called()
    assert json.loads(p["files"]["m.json"])["base"] == "develop"


def test_source_replaced_instance_never_pushes(source_publisher):
    p = source_publisher

    def confirm(question):
        p["incus"].list_containers.return_value = [
            {"name": "full", "created_at": "later", "config": {"volatile.uuid": "uuid-new"}}
        ]
        return True

    assert _publish(p, yes=False, confirm=confirm) == 1
    p["push"].assert_not_called()


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
    assert consumed(p)


def test_source_lookup_failure_never_pushes(source_publisher):
    from jailbee.git import GitError

    p = source_publisher
    p["remote"].side_effect = GitError("network unavailable")
    assert _publish(p) == 1
    p["push"].assert_not_called()
    assert not consumed(p)


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


def test_source_create_records_sent_row(source_publisher):
    from jailbee.outbox import sent

    p = source_publisher
    assert _publish(p) == 0
    (row,) = sent.list_records()
    assert (row.outbox, row.manifest, row.index, row.kind) == ("source", "m.json", 0, "pr")
    assert (row.repo, row.target, row.state, row.publisher) == (
        "acme/widgets",
        42,
        "applied",
        "user",
    )
    assert row.url == "https://github.com/acme/widgets/pull/42"
    assert row.instance == "full" and row.container_prefix == p["cfg"].container_prefix
    assert row.title == "Title"
    assert row.content.startswith("create pull request")
    assert "Body" in row.content and "feature/a @ " + "a" * 40 in row.content


def test_source_update_records_sent_row_with_update_header(source_publisher):
    from jailbee.outbox import sent
    from jailbee.pr import PrInfo

    p = source_publisher
    p["discover"].return_value = PrInfo(42, "feature/a", "b" * 40, "OPEN", "main")
    assert _publish(p) == 0
    (row,) = sent.list_records()
    assert row.url == "https://github.com/acme/widgets/pull/42" and row.target == 42
    assert row.content.startswith("update pull request")


@pytest.mark.parametrize("failure", [RuntimeError("gh crashed"), KeyboardInterrupt()])
def test_a_crash_during_the_pr_mutation_leaves_it_uncertain(source_publisher, failure):
    from jailbee.outbox import sent

    p = source_publisher
    p["create"].side_effect = failure
    with pytest.raises(type(failure)):
        _publish(p)
    # A recorded failure and a hard crash (prepared, never settled) read alike.
    (action,) = journal().actions
    assert action.state == "uncertain"
    assert not consumed(p)
    records = sent.list_records()
    assert [r.state for r in records] == (["uncertain"] if failure.args else [])


def test_an_uncertain_source_is_not_republished_without_consent(source_publisher):
    from jailbee.pr import PrError

    p = source_publisher
    p["create"].side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        _publish(p)
    p["create"].side_effect = None
    assert _publish(p, confirm=lambda q: "uncertain" not in q) == 1
    with pytest.raises(PrError, match="uncertain"):
        publish_own(p)
    assert p["create"].call_count == 1
    assert journal().uncertain


def test_publish_survives_sent_construction_failure(source_publisher, mocker):
    p = source_publisher
    mocker.patch("jailbee.branch_pr._source_sent_content", side_effect=OSError("unavailable"))
    assert _publish(p) == 0
    p["create"].assert_called_once()
    assert consumed(p)
    saved = json.loads(p["incus"].config_set.call_args.args[2])
    assert saved["feature/a"]["pr"] == 42


def test_an_archived_publication_is_not_repeated(source_publisher, capsys):
    from jailbee.outbox import sent

    p = source_publisher
    assert _publish(p) == 0
    p["files"]["m.json"] = json.dumps(p["data"])  # the identical proposal again
    assert _publish(p) == 1
    assert "already published" in capsys.readouterr().err
    assert p["create"].call_count == 1
    assert len(sent.list_records()) == 1


def test_sent_source_shows_in_the_containers_outbox_sent(source_publisher):
    from jailbee.outbox import publication_db
    from jailbee.outbox.broker_ops import handle_sent

    p = source_publisher
    assert _publish(p) == 0
    result = handle_sent(publication_db.open_publications(), "uuid-full", {"kind": "pr"})
    (record,) = result["records"]
    assert (record["outbox"], record["kind"], record["manifest"]) == ("source", "pr", "m.json")
    assert record["url"] == "https://github.com/acme/widgets/pull/42"


def test_orchestrator_source_publish_records_orchestrator(source_publisher):
    from jailbee.branch_pr import publish_source_manifest
    from jailbee.outbox import sent

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
            publisher="orchestrator",
        )
        == 0
    )
    (row,) = sent.list_records()
    assert row.publisher == "orchestrator"


def test_reused_source_manifest_name_keeps_earlier_sent_row(source_publisher):
    from jailbee.outbox import sent

    p = source_publisher
    assert _publish(p) == 0
    changed = dict(p["data"], actions=[{"type": "description", "title": "Other", "body": "Text"}])
    p["snapshot"].files["m.json"] = json.dumps(changed)
    assert _publish(p) == 0
    rows = sorted(sent.list_records(), key=lambda r: r.id)
    assert [r.title for r in rows] == ["Title", "Other"]
