"""Discovery and callback boundaries resolve the target's own effective config."""

import json
from datetime import UTC, datetime

import pytest
from sqlmodel import Session

from jailbee.db import get_engine
from jailbee.db.models import RegisteredRepo
from jailbee.outbox import service
from jailbee.outbox.models import OutboxError
from jailbee.outbox_io import JournalStore
from tests.outbox_support import IDENTITY, issue_files, store


@pytest.fixture
def env(mocker, make_cfg, tmp_path):
    root = tmp_path / "acme"
    root.mkdir()
    cfg = make_cfg(root, container_prefix="acme")
    incus = mocker.Mock()
    incus.list_containers.return_value = [
        {
            "name": IDENTITY.full_name,
            "created_at": IDENTITY.created_at,
            "profiles": ["acme-base"],
            "status": "Running",
            "config": {"user.jailbee.issue_count": "0", "user.jailbee.review_count": "0"},
        },
    ]
    incus.exists.side_effect = lambda name: any(
        r["name"] == name for r in incus.list_containers.return_value
    )
    loader = mocker.patch("jailbee.config.load_repo_config", return_value=cfg)
    reader = mocker.patch.object(
        service,
        "read_store",
        side_effect=lambda i, c, k, **kw: store(k, issue_files() if k == "issue" else {}),
    )
    mutation = mocker.patch.object(
        service, "mutate_store", side_effect=lambda *a, **kw: kw["delete_names"]
    )
    journals = JournalStore(tmp_path / "journals")
    return cfg, incus, loader, reader, mutation, journals


def register(prefix, root):
    with Session(get_engine()) as session:
        session.add(
            RegisteredRepo(
                container_prefix=prefix,
                repo_root=str(root),
                registered_at=datetime.now(UTC),
                synthetic_config=True,
            )
        )
        session.commit()


def test_existing_registry_is_read_only_and_preserves_registration(env, tmp_path, mocker):
    import sqlite3

    from jailbee.db import state_dir
    from jailbee.outbox.commands import discover

    cfg, incus, _, _, _, journals = env
    register("other", tmp_path / "missing-root")
    database = state_dir() / "state.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = database.read_bytes()
    mocker.patch("jailbee.db.get_engine", side_effect=AssertionError("inspection bootstrapped DB"))
    discover(cfg, incus, None, all_repos=True, journal_store=journals)
    assert database.read_bytes() == before
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT container_prefix, repo_root FROM registered_repo"
        ).fetchall() == [("other", str(tmp_path / "missing-root"))]


def test_zero_probe_count_does_not_filter(env):
    from jailbee.outbox.commands import discover

    cfg, incus, _, reader, _, journals = env
    result = discover(cfg, incus, None, all_repos=False, journal_store=journals)
    assert len(result) == 1 and result[0].available
    assert str(result[0].proposals[0].id) == "issue/001.json"
    assert reader.call_count == 2


def test_all_repos_each_own_uid_and_missing_root(env, make_cfg, tmp_path):
    from jailbee.outbox.commands import discover

    cfg, incus, loader, reader, _, journals = env
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    other = make_cfg(foreign, container_prefix="other", container_user={"uid": 2345})
    register("other", foreign)
    register("gone", tmp_path / "gone")
    loader.side_effect = lambda root: other if root == foreign else cfg
    incus.list_containers.return_value += [
        {
            "name": "other-feature",
            "profiles": ["other-base"],
            "status": "Running",
            "created_at": IDENTITY.created_at,
        },
        {
            "name": "gone-feature",
            "profiles": ["gone-base"],
            "status": "Running",
            "created_at": IDENTITY.created_at,
        },
    ]
    result = discover(cfg, incus, None, all_repos=True, journal_store=journals)
    assert [v.available for v in result] == [True, True, False]
    assert "root" in result[2].error
    assert {c.kwargs["uid"] for c in reader.call_args_list if c.args[1] == "other-feature"} == {
        2345
    }


def test_absent_config_file_accepts_registered_effective_config(env, make_cfg, tmp_path):
    from jailbee.outbox.commands import resolve_target

    cfg, incus, loader, _, _, _ = env
    foreign = tmp_path / "scratch"
    foreign.mkdir()
    other = make_cfg(foreign, container_prefix="other", container_user={"uid": 2345})
    register("other", foreign)
    loader.side_effect = lambda root: other if root == foreign else cfg
    incus.list_containers.return_value = [
        {"name": "other-feature", "profiles": ["other-base"], "status": "Running"}
    ]
    target_cfg, full = resolve_target(cfg, incus, "other-feature")
    assert target_cfg is other and full == "other-feature"
    assert not (foreign / ".jailbee/config.yaml").exists()


def test_config_load_error_never_falls_back(env, tmp_path):
    from jailbee.config import ConfigError
    from jailbee.outbox.commands import discover, resolve_target

    cfg, incus, loader, reader, _, journals = env
    root = tmp_path / "bad"
    root.mkdir()
    register("bad", root)
    incus.list_containers.return_value = [
        {"name": "bad-feature", "profiles": ["bad-base"], "status": "Running"}
    ]
    loader.side_effect = ConfigError("invalid config")
    result = discover(cfg, incus, None, all_repos=True, journal_store=journals)
    assert not result[0].available and "invalid config" in result[0].error
    with pytest.raises(OutboxError, match="invalid config"):
        resolve_target(cfg, incus, "bad-feature")
    reader.assert_not_called()


def test_scope_filters_before_any_read(env, mocker):
    from jailbee.outbox.commands import discover, resolve_target
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    cfg, incus, _, reader, _, journals = env
    mocker.patch(
        "jailbee.remote_ssh.repo_scope.scope_for_session",
        return_value=RemoteRepoScope(frozenset({"acme"})),
    )
    assert discover(cfg, incus, None, all_repos=True, journal_store=journals) == ()
    with pytest.raises(OutboxError):
        resolve_target(cfg, incus, "feature")
    reader.assert_not_called()


def test_mutation_callback_rechecks_scope_after_confirmation(env, mocker):
    from jailbee.outbox.commands import drop_selected
    from jailbee.outbox.delete import DeleteSelection
    from jailbee.outbox.models import ProposalId
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    cfg, incus, _, _, mutation, journals = env
    scope = mocker.patch(
        "jailbee.remote_ssh.repo_scope.scope_for_session", return_value=RemoteRepoScope(frozenset())
    )

    def confirm(plan):
        scope.return_value = RemoteRepoScope(frozenset({"acme"}))
        return True

    with pytest.raises(OutboxError):
        drop_selected(
            cfg,
            incus,
            "feature",
            ProposalId("issue", "001.json"),
            selection=DeleteSelection(),
            journal_store=journals,
            confirm=confirm,
        )
    mutation.assert_not_called()


def test_explicit_first_read_preserves_type_and_cause_but_overview_reports_unavailable(env):
    from jailbee.incus import IncusTimeoutError
    from jailbee.outbox.commands import _selected, discover
    from jailbee.outbox.models import OutboxExecutionError, ProposalId

    cfg, incus, _, reader, mutation, journals = env
    transport = IncusTimeoutError("read expired")
    failure = OutboxExecutionError("outbox read failed")
    failure.__cause__ = transport
    reader.side_effect = failure
    with pytest.raises(OutboxExecutionError) as caught:
        _selected(cfg, incus, "feature", ProposalId("issue", "001.json"), journal_store=journals)
    assert caught.value is failure
    assert caught.value.__cause__ is transport
    (unavailable,) = discover(cfg, incus, "feature", all_repos=False, journal_store=journals)
    assert not unavailable.available
    assert unavailable.error == "outbox read failed"
    mutation.assert_not_called()


def test_inventory_timeout_is_typed_before_config_or_store_read(env, mocker):
    import subprocess

    from jailbee.incus import Incus
    from jailbee.outbox.commands import resolve_target
    from jailbee.outbox.models import OutboxExecutionError

    cfg, _, loader, reader, _, _ = env

    def expire(argv, **kwargs):
        assert 0 < (kwargs.get("timeout") or 0) <= 30
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    mocker.patch("jailbee.incus.subprocess.run", side_effect=expire)
    with pytest.raises(OutboxExecutionError, match="timed out"):
        resolve_target(cfg, Incus(), "feature")
    loader.assert_not_called()
    reader.assert_not_called()


@pytest.mark.parametrize("name", ["feature", "acme-feature"])
def test_resolution_uses_inventory_without_exists(env, name):
    from jailbee.outbox.commands import resolve_target

    cfg, incus, _, _, _, _ = env
    incus.exists.side_effect = AssertionError("redundant unbounded existence query")
    target, full = resolve_target(cfg, incus, name)
    assert target is cfg and full == "acme-feature"
    assert incus.list_containers.call_count == 1


def test_excluded_exact_candidate_never_bypasses_scoped_inventory(env, mocker):
    from jailbee.outbox.commands import resolve_target
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    cfg, incus, _, reader, _, _ = env
    incus.list_containers.return_value.append(
        {"name": "feature", "profiles": ["excluded-base"], "status": "Running"}
    )
    mocker.patch(
        "jailbee.remote_ssh.repo_scope.scope_for_session",
        return_value=RemoteRepoScope(frozenset({"excluded"})),
    )
    incus.exists.side_effect = AssertionError("unscoped lookup")
    assert resolve_target(cfg, incus, "feature") == (cfg, "acme-feature")
    with pytest.raises(OutboxError):
        resolve_target(cfg, incus, "excluded-feature")
    reader.assert_not_called()


def test_exact_visible_name_wins_over_prefixed_candidate(env):
    from jailbee.outbox.commands import resolve_target

    cfg, incus, _, _, _, _ = env
    incus.list_containers.return_value.append(
        {"name": "feature", "profiles": ["acme-base"], "status": "Running"}
    )
    incus.exists.side_effect = AssertionError("redundant unbounded existence query")
    assert resolve_target(cfg, incus, "feature") == (cfg, "feature")


@pytest.mark.parametrize("drift", ["prefix", "root"])
def test_target_identity_drift_prevents_reads(env, tmp_path, drift):
    from jailbee.outbox.commands import discover, resolve_target

    cfg, incus, loader, reader, _, journals = env
    loader.return_value = cfg.model_copy(
        update={"container_prefix": "other"} if drift == "prefix" else {"repo_root": tmp_path}
    )
    with pytest.raises(OutboxError, match="identity changed"):
        resolve_target(cfg, incus, "feature")
    views = discover(cfg, incus, None, all_repos=False, journal_store=journals)
    assert not views[0].available and "identity changed" in views[0].error
    reader.assert_not_called()


def test_stale_inventory_cannot_authorize_deletion(env):
    from jailbee.outbox.commands import drop_selected
    from jailbee.outbox.delete import DeleteSelection
    from jailbee.outbox.models import ProposalId

    cfg, incus, _, _, mutation, journals = env
    original = incus.list_containers.return_value
    changed = [{**original[0], "created_at": "2026-10-01T12:00:00Z"}]
    incus.list_containers.side_effect = [original] * 4 + [changed] * 4
    with pytest.raises(OutboxError, match="changed"):
        drop_selected(
            cfg,
            incus,
            "feature",
            ProposalId("issue", "001.json"),
            selection=DeleteSelection(),
            journal_store=journals,
            confirm=lambda plan: True,
        )
    mutation.assert_not_called()


def test_mutation_callback_reloads_target_config(env):
    from jailbee.outbox.commands import drop_selected
    from jailbee.outbox.delete import DeleteSelection
    from jailbee.outbox.models import ProposalId

    cfg, incus, loader, _, mutation, journals = env
    changed = cfg.model_copy(
        update={"container_user": cfg.container_user.model_copy(update={"uid": 2345})}
    )

    def confirm(plan):
        loader.return_value = changed
        return True

    with pytest.raises(OutboxError, match=r"config.*changed"):
        drop_selected(
            cfg,
            incus,
            "feature",
            ProposalId("issue", "001.json"),
            selection=DeleteSelection(),
            journal_store=journals,
            confirm=confirm,
        )
    mutation.assert_not_called()


@pytest.mark.parametrize("scope_paths", [["libs/core"], [], ["libs/core", "libs/duplicate"]])
@pytest.mark.parametrize("mode", ["description", "comment", "bound"])
def test_pr_creation_scope_requires_unique_matching_repo_with_body_file(
    env, mocker, capsys, scope_paths, mode
):
    from jailbee.outbox.commands import show_overview
    from jailbee.pr_flow import PrScope

    cfg, incus, _, reader, _, journals = env
    files = {
        "002.json": json.dumps(
            {
                "version": 1,
                "repo": "AndBible/jsword",
                "pr": None,
                "head_sha": None,
                "actions": [
                    {
                        "type": "description",
                        "title": "Probe",
                        "body_file": "body.md",
                        "branch": "ios-log-crash-probe",
                    }
                ],
            }
        ),
        "body.md": "Simulator output.",
    }
    if mode == "comment":
        raw = json.loads(files["002.json"])
        raw["actions"] = [{"type": "comment", "body_file": "body.md"}]
        files["002.json"] = json.dumps(raw)
    reader.side_effect = lambda i, c, k, **kw: store(k, files if k == "pr" else {})
    scopes = [PrScope.for_repo(cfg)] + [
        PrScope(cfg.repo_root / path, "origin", "", path) for path in scope_paths
    ]
    mocker.patch("jailbee.pr_flow.candidate_scopes", return_value=scopes)
    mocker.patch("jailbee.submodule_pr.recorded_paths", return_value=[])
    mocker.patch(
        "jailbee.pr_outbox.scope_slug",
        side_effect=lambda s: "AndBible/jsword" if s.subpath else "acme/main",
    )
    mocker.patch(
        "jailbee.submodule_pr.SubmodulePrState.read",
        return_value=mocker.Mock(number=42 if mode == "bound" else None),
    )
    incus.config_get.return_value = None
    assert (
        show_overview(cfg, incus, None, all_repos=False, output="json", journal_store=journals) == 0
    )
    proposal = json.loads(capsys.readouterr().out)["containers"][0]["proposals"][0]
    if len(scope_paths) == 1 and mode == "description":
        assert proposal.get("create_scope") == {"kind": "submodule", "path": "libs/core"}
    else:
        assert proposal.get("create_scope") is None
    if len(scope_paths) == 1 and mode == "bound":
        assert proposal["state"] == "pending"
