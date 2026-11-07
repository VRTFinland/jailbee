"""Deletion reconstructs real previews and journals under publication locks."""

import json
from dataclasses import replace

import pytest

from jailbee.incus import IncusTimeoutError
from jailbee.outbox.delete import DeleteSelection, plan_delete
from jailbee.outbox.models import OutboxChanged, OutboxError, OutboxExecutionError, ProposalId
from jailbee.outbox_io import proposal_digest
from tests.issue_store_support import StoreJournals, instance
from tests.outbox_support import IDENTITY, issue_files, pr_files, route_issue_snapshot, store


def setup_service(mocker, make_cfg, tmp_path, kind="issue"):
    from jailbee.outbox import service

    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.list_containers.return_value = [instance(IDENTITY.full_name, IDENTITY.created_at)]
    files = issue_files() if kind == "issue" else pr_files()
    snapshots = {
        kind: store(kind, files),
        "pr" if kind == "issue" else "issue": store("pr" if kind == "issue" else "issue", {}),
    }
    reader = mocker.patch.object(
        service, "read_store", create=True, side_effect=lambda i, c, k, **kw: snapshots[k]
    )
    route_issue_snapshot(mocker, service)
    mutation = mocker.patch.object(
        service, "mutate_store", create=True, side_effect=lambda *args, **kw: kw["delete_names"]
    )
    return service, cfg, incus, files, snapshots, reader, mutation, None


def preview(env, selection=None, kind="issue"):
    if selection is None:
        selection = DeleteSelection(action=2)
    service, cfg, incus, _, _, _, _, _journals = env
    view = service.load_container(cfg, incus, IDENTITY.full_name)
    return plan_delete(view, ProposalId(kind, "001.json"), selection)


def execute(env, plan):
    service, cfg, incus, _, _, _, _, _journals = env
    return service.execute_delete(cfg, incus, IDENTITY.full_name, plan)


@pytest.mark.parametrize("kind", ["pr"])
def test_delete_busy_lock_has_bounded_wait_without_mutation(mocker, make_cfg, tmp_path, kind):
    import time

    from jailbee.outbox.io import PrManagement
    from tests.test_outbox_locks import held_by_process

    env = setup_service(mocker, make_cfg, tmp_path, kind)
    manager = PrManagement(tmp_path / "pr-locks")
    mocker.patch.object(env[0], "PrManagement", return_value=manager)
    root = manager.root
    plan = preview(env, DeleteSelection(), kind)
    env[5].reset_mock()
    with held_by_process(kind, root, IDENTITY):
        started = time.monotonic()
        with pytest.raises(OutboxExecutionError, match=r"(?i)timed out.*refresh.*retry"):
            env[0].execute_delete(
                env[1],
                env[2],
                IDENTITY.full_name,
                plan,
                lock_timeout=0.1,
            )
        assert 0.08 <= time.monotonic() - started < 0.8
        env[6].assert_not_called()
        env[5].assert_not_called()
    assert (
        env[0].execute_delete(
            env[1],
            env[2],
            IDENTITY.full_name,
            plan,
            lock_timeout=0.1,
        )
        == plan.delete_names
    )


def test_zero_based_action_and_bodies_retained(mocker, make_cfg, tmp_path):
    env = setup_service(mocker, make_cfg, tmp_path)
    plan = preview(env, DeleteSelection(action=0, with_dependents=True))
    execute(env, plan)
    kwargs = env[6].call_args.kwargs
    payload = json.loads(kwargs["new_manifest"][1])
    assert payload["actions"] == [
        {"type": "comment", "repo": ".", "issue": 42, "body": "Independent"}
    ]
    assert kwargs["delete_names"] == ()
    assert kwargs["expected"] == env[3]


@pytest.mark.parametrize("change", ["manifest", "body", "identity", "journal", "rejected"])
def test_changed_preview_refuses_before_mutation(mocker, make_cfg, tmp_path, change):
    env = setup_service(mocker, make_cfg, tmp_path)
    plan = preview(env)
    if change == "identity":
        env[2].list_containers.return_value[0]["created_at"] = "replacement"
    elif change == "journal":
        # Issue journals live in the host outbox store.
        StoreJournals(IDENTITY).create("001.json", "b" * 64, 9)
    elif change == "rejected":
        env[4]["issue"] = store("issue", env[3], rejected=("body.md",))
    else:
        files = env[3] | {"body.md" if change == "body" else "001.json": "changed"}
        env[4]["issue"] = store("issue", files)
    with pytest.raises(OutboxChanged):
        execute(env, plan)
    env[6].assert_not_called()


def test_neighbor_scope_recomputed(mocker, make_cfg, tmp_path):
    env = setup_service(mocker, make_cfg, tmp_path)
    plan = preview(env, DeleteSelection())
    env[4]["issue"] = store("issue", env[3] | {"002.json": env[3]["001.json"]})
    with pytest.raises(OutboxChanged):
        execute(env, plan)
    env[6].assert_not_called()


def test_forged_scope_refused(mocker, make_cfg, tmp_path):
    env = setup_service(mocker, make_cfg, tmp_path)
    plan = replace(preview(env), delete_names=("body.md",))
    with pytest.raises(OutboxChanged):
        execute(env, plan)
    env[6].assert_not_called()


@pytest.mark.parametrize("evidence", ["applied", "uncertain", "other-text"])
def test_pr_journal_blocks_whole_deletion(mocker, make_cfg, tmp_path, evidence):
    from jailbee.outbox.publication_db import Journal
    from jailbee.outbox_io import JournalAction

    env = setup_service(mocker, make_cfg, tmp_path, "pr")
    files = env[3]
    digest = proposal_digest("001.json", files["001.json"], {})
    if evidence == "other-text":
        digest = "0" * 64
    state = "uncertain" if evidence == "uncertain" else "applied"
    journal = Journal(
        "uuid",
        "pr",
        "001.json",
        digest,
        IDENTITY.full_name,
        1,
        (JournalAction(0, state, "acme/repo"),),
    )
    mocker.patch.object(env[0], "pr_journals", return_value={"001.json": journal})
    with pytest.raises(OutboxError):
        preview(env, DeleteSelection(), "pr")
    env[6].assert_not_called()


def test_prepared_recovery_cannot_be_archived(mocker, make_cfg, tmp_path):
    env = setup_service(mocker, make_cfg, tmp_path)
    files = env[3]
    digest = proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]})
    # A stored `prepared` action (a run died mid-publication) reads as uncertain.
    StoreJournals(IDENTITY).record("001.json", digest, 3, 0, state="prepared", repo="acme/repo")
    with pytest.raises(OutboxError):
        preview(env, DeleteSelection(archive_journal=True))


def test_unreadable_journal_blocks(mocker, make_cfg, tmp_path):
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import publication_db

    env = setup_service(mocker, make_cfg, tmp_path)
    mocker.patch.object(publication_db, "read_journals", side_effect=StoreError("corrupt"))
    with pytest.raises(OutboxError):
        preview(env)


def test_timeout_unavailable_not_empty(mocker, make_cfg, tmp_path):
    env = setup_service(mocker, make_cfg, tmp_path)
    env[5].side_effect = OutboxExecutionError("timeout")
    view = env[0].load_container(env[1], env[2], IDENTITY.full_name)
    assert not view.available and view.error
    assert view.proposals == ()


def test_identity_rechecked_after_read_before_action(mocker, make_cfg, tmp_path):
    env = setup_service(mocker, make_cfg, tmp_path)
    plan = preview(env)

    def read(i, c, k, **kw):
        if k == "issue":
            env[2].list_containers.return_value[0]["created_at"] = "replacement"
        return env[4][k]

    env[5].side_effect = read
    with pytest.raises(OutboxChanged):
        execute(env, plan)
    env[6].assert_not_called()


def test_pr_null_is_locally_awaiting_and_canonical(mocker, make_cfg, tmp_path):
    env = setup_service(mocker, make_cfg, tmp_path, "pr")
    payload = json.loads(env[3]["001.json"])
    payload["pr"] = None
    payload["actions"] = [{"type": "description", "body": "draft"}]
    env[4]["pr"] = store("pr", {"001.json": json.dumps(payload)})
    view = env[0].load_container(env[1], env[2], IDENTITY.full_name)
    assert view.proposals[0].state == "awaiting-pr"
    execute(env, plan_delete(view, ProposalId("pr", "001.json"), DeleteSelection()))


def test_rejected_neighbor_preserves_body(mocker, make_cfg, tmp_path):
    env = setup_service(mocker, make_cfg, tmp_path)
    env[4]["issue"] = store("issue", env[3], rejected=("neighbor.json",))
    plan = preview(env, DeleteSelection())
    assert plan.delete_names == ("001.json",)
    assert execute(env, plan) == ("001.json",)
    assert env[6].call_args.kwargs["rejected_names"] == ("neighbor.json",)


def test_pr_lock_held_across_mutation(mocker, make_cfg, tmp_path):
    from contextlib import contextmanager

    env = setup_service(mocker, make_cfg, tmp_path, "pr")
    held = []

    @contextmanager
    def lock(identity):
        assert identity == IDENTITY
        held.append(identity)
        try:
            yield
        finally:
            held.pop()

    mocker.patch.object(env[0].PrManagement, "lock", side_effect=lock)

    def mutation(*args, **kwargs):
        assert held == [IDENTITY]
        return kwargs["delete_names"]

    env[6].side_effect = mutation
    execute(env, preview(env, DeleteSelection(), "pr"))
    assert held == []


def test_identity_reads_have_finite_timeout(mocker, make_cfg, tmp_path):
    env = setup_service(mocker, make_cfg, tmp_path)
    preview(env)
    assert all(call.kwargs.get("timeout") == 30 for call in env[2].list_containers.call_args_list)


@pytest.mark.parametrize("kind", ["pr"])
def test_identity_rechecked_after_lock_wait(mocker, make_cfg, tmp_path, kind):
    from contextlib import contextmanager

    env = setup_service(mocker, make_cfg, tmp_path, kind)
    plan = preview(env, DeleteSelection(), kind)

    @contextmanager
    def replacing_lock(*args):
        env[2].list_containers.return_value[0]["created_at"] = "replacement"
        yield

    mocker.patch.object(env[0].PrManagement, "lock", side_effect=replacing_lock)
    with pytest.raises(OutboxChanged):
        execute(env, plan)
    env[6].assert_not_called()


def _stage_in_host_store(uuid: str) -> str:
    from jailbee.db import state_dir
    from jailbee.orchestration.store import BrokerStore, store_path
    from jailbee.outbox.store_db import OutboxStore

    files = issue_files()
    manifest = json.loads(files["001.json"])

    def build(stem: str) -> tuple[str, dict[str, str]]:
        manifest["actions"][0]["body_file"] = f"{stem}-1.md"
        return json.dumps(manifest), {f"{stem}-1.md": files["body.md"]}

    store_db = OutboxStore(BrokerStore(store_path(state_dir() / "orchestration")))
    return store_db.put_proposal(uuid, "issue", "example", build, now=1.0).name


def test_issue_proposals_are_listed_from_the_host_store_without_incus_exec(
    mocker, make_cfg, tmp_path
):
    from jailbee.incus import Incus
    from jailbee.outbox import service

    name = _stage_in_host_store("uuid-1")
    _stage_in_host_store("uuid-other")  # another instance's proposal stays out
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [
        {
            "name": IDENTITY.full_name,
            "created_at": IDENTITY.created_at,
            "config": {"volatile.uuid": "uuid-1"},
        }
    ]

    def read_pr_only(i, c, kind, **kw):
        assert kind == "pr", "issue proposals must not be read from the container"
        return store("pr", {})

    mocker.patch.object(service, "read_store", create=True, side_effect=read_pr_only)
    view = service.load_container(make_cfg(tmp_path), incus, IDENTITY.full_name)
    assert view.available, view.error
    (proposal,) = view.proposals
    assert proposal.id == ProposalId("issue", name)
    assert proposal.state == "pending"
    assert [a.kind for a in proposal.actions] == ["create", "comment", "comment"]
    assert proposal.actions[0].body == "Original body"
    assert not [c for c in incus.mock_calls if c[0].startswith("exec")]


def test_an_instance_without_a_uuid_is_unavailable_not_empty(mocker, make_cfg, tmp_path):
    from jailbee.outbox import service

    incus = mocker.Mock()
    incus.list_containers.return_value = [
        {"name": IDENTITY.full_name, "created_at": IDENTITY.created_at}
    ]
    mocker.patch.object(service, "read_store", create=True, return_value=store("pr", {}))
    view = service.load_container(make_cfg(tmp_path), incus, IDENTITY.full_name)
    assert not view.available
    assert "uuid" in (view.error or "")


def test_identity_timeout_unavailable(mocker, make_cfg, tmp_path):
    env = setup_service(mocker, make_cfg, tmp_path)
    env[2].list_containers.side_effect = IncusTimeoutError("timeout")
    assert not env[0].load_container(env[1], env[2], IDENTITY.full_name).available
