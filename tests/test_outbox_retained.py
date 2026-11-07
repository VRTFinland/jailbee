"""D7: a destroyed container's uncertain publication stays resolvable in `jb outbox`.

Destroy keeps the proposals (and bodies) whose journal holds an uncertain action;
`jb outbox` lists them under ``full@uuid`` and `jb issue resolve` / `jb review
resolve` / `jb outbox drop` act on them through the real host store.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from jailbee import host_teardown
from jailbee.cli import app
from jailbee.incus import Incus
from jailbee.outbox import publication_db, store_db
from jailbee.outbox_io import proposal_digest

UUID = "u-dead"
FULL = "acme-gone"
REF = f"{FULL}@{UUID}"
REPO = "acme/widgets"
ISSUE_MANIFEST = {
    "version": 1,
    "actions": [{"type": "comment", "repo": ".", "issue": 42, "body": "Landed?"}],
}
PR_MANIFEST = {
    "version": 1,
    "repo": REPO,
    "pr": 12,
    "head_sha": "b" * 40,
    "actions": [{"type": "comment", "body": "General"}],
}

runner = CliRunner()


def _root():
    return publication_db.default_root()


def _stage(kind: str, slug: str, manifest: dict[str, object], uuid: str = UUID) -> str:
    def build(stem: str) -> tuple[str, dict[str, str]]:
        return json.dumps(manifest), {}

    return store_db.host_store(_root()).put_proposal(uuid, kind, slug, build, now=1.0).name  # type: ignore[arg-type]  # kind is issue or pr


def _uncertain(kind: str, name: str, uuid: str = UUID, full: str = FULL) -> None:
    """Journal the proposal's only action as prepared: a crash mid-publication."""
    (row,) = [p for p in store_db.read_proposals(_root(), uuid, kind) if p.name == name]  # type: ignore[arg-type]  # kind is issue or pr
    digest = proposal_digest(name, row.manifest, dict(row.bodies))
    pubs = publication_db.open_publications(_root())
    with pubs.claim(uuid, kind, name) as claim:  # type: ignore[arg-type]  # kind is issue or pr
        pubs.begin(claim, digest=digest, action_count=1, container_full=full, now=1.0)
        pubs.mark_prepared(claim, digest, 0, repo=REPO, now=1.0)


@pytest.fixture
def env(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path, container_prefix="acme")
    mocker.patch("jailbee.config.load_repo_config", return_value=cfg)
    mocker.patch("jailbee.config.load_config", return_value=cfg)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = []  # FULL is destroyed
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    return cfg, incus


def _destroy() -> None:
    host_teardown.cleanup(
        host_teardown.TeardownSnapshot(name=FULL, identity=None, log_paths=(), uuid=UUID)
    )


def _listed() -> dict[str, dict[str, object]]:
    result = runner.invoke(app, ["outbox", "ls", "-o", "json"])
    assert result.exit_code == 0, result.output
    return {c["name"]: c for c in json.loads(result.output)["containers"]}


def test_destroy_keeps_only_the_uncertain_proposal_with_its_bodies(env) -> None:
    def build(stem: str) -> tuple[str, dict[str, str]]:
        manifest = {
            "version": 1,
            "actions": [{"type": "comment", "repo": ".", "issue": 42, "body_file": f"{stem}-1.md"}],
        }
        return json.dumps(manifest), {f"{stem}-1.md": "long body"}

    kept = store_db.host_store(_root()).put_proposal(UUID, "issue", "kept", build, now=1.0).name
    _uncertain("issue", kept)
    _stage("issue", "plain", ISSUE_MANIFEST)
    _destroy()
    (row,) = store_db.read_proposals(_root(), UUID)
    assert row.name == kept
    assert row.bodies == {kept.removesuffix(".json") + "-1.md": "long body"}


def test_jb_outbox_lists_and_shows_a_destroyed_containers_uncertain_proposal(env) -> None:
    name = _stage("issue", "landed", ISSUE_MANIFEST)
    _uncertain("issue", name)
    _destroy()

    listed = _listed()[REF]
    assert listed["destroyed"] is True
    assert listed["available"] is True
    [proposal] = listed["proposals"]
    assert (proposal["id"], proposal["state"]) == (f"issue/{name}", "uncertain")

    shown = runner.invoke(app, ["outbox", "show", REF, f"issue/{name}", "-o", "json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["proposal"]["actions"][0]["state"] == "uncertain"

    table = runner.invoke(app, ["outbox", "ls"])
    assert "(destroyed)" in table.output


def test_resolving_applied_by_full_at_uuid_finishes_the_proposal(env, mocker, tmp_path) -> None:
    from jailbee.issue_outbox import RepoTarget

    name = _stage("issue", "landed", ISSUE_MANIFEST)
    _uncertain("issue", name)
    _destroy()
    mocker.patch(
        "jailbee.issue_outbox.resolve_repo_targets",
        return_value={".": RepoTarget(".", tmp_path, REPO)},
    )

    result = runner.invoke(
        app,
        [
            "issue",
            "resolve",
            REF,
            name,
            "0",
            "--applied",
            "--url",
            f"https://github.com/{REPO}/issues/42",
            "-y",
        ],
    )

    assert result.exit_code == 0, result.output
    assert REF not in _listed()
    assert store_db.read_proposals(_root(), UUID) == []


def test_retry_then_drop_clears_a_destroyed_containers_proposal(env, mocker, tmp_path) -> None:
    from jailbee.issue_outbox import RepoTarget

    name = _stage("issue", "landed", ISSUE_MANIFEST)
    _uncertain("issue", name)
    _destroy()
    # While the outcome is uncertain, the proposal cannot be dropped.
    refused = runner.invoke(app, ["outbox", "drop", REF, f"issue/{name}", "-y"])
    assert refused.exit_code != 0
    assert len(store_db.read_proposals(_root(), UUID)) == 1

    mocker.patch(
        "jailbee.issue_outbox.resolve_repo_targets",
        return_value={".": RepoTarget(".", tmp_path, REPO)},
    )
    retried = runner.invoke(app, ["issue", "resolve", REF, name, "0", "--retry", "-y"])
    assert retried.exit_code == 0, retried.output
    [proposal] = _listed()[REF]["proposals"]
    assert proposal["state"] == "pending"

    dropped = runner.invoke(app, ["outbox", "drop", REF, f"issue/{name}", "-y"])
    assert dropped.exit_code == 0, dropped.output
    assert REF not in _listed()
    assert store_db.read_proposals(_root(), UUID) == []


def test_a_destroyed_containers_proposal_cannot_be_published(env) -> None:
    name = _stage("issue", "landed", ISSUE_MANIFEST)
    _uncertain("issue", name)
    _destroy()
    result = runner.invoke(app, ["outbox", "apply", REF, f"issue/{name}", "-y"])
    assert result.exit_code == 2
    assert "destroyed" in result.output


def test_jb_review_resolve_settles_a_destroyed_containers_pr_action(env) -> None:
    name = _stage("pr", "comment", PR_MANIFEST)
    _uncertain("pr", name)
    _destroy()
    [proposal] = _listed()[REF]["proposals"]
    assert proposal["state"] == "uncertain"

    result = runner.invoke(
        app,
        ["review", "resolve", REF, name, "0", "--applied", f"https://github.com/{REPO}/pull/12"],
    )

    assert result.exit_code == 0, result.output
    assert REF not in _listed()
    assert store_db.read_proposals(_root(), UUID) == []


def test_a_destroyed_containers_uncertain_pr_source_is_retried_then_dropped(env) -> None:
    source = {
        "version": 1,
        "repo": REPO,
        "pr": None,
        "head_sha": None,
        "source": "refs/heads/topic",
        "source_sha": "a" * 40,
        "base": "main",
        "actions": [{"type": "description", "title": "Title", "body": "Body"}],
    }
    name = _stage("pr", "source", source)
    _uncertain("pr", name)
    _destroy()
    [proposal] = _listed()[REF]["proposals"]
    assert proposal["state"] == "uncertain"

    retried = runner.invoke(app, ["review", "resolve", REF, name, "0", "--retry"])
    assert retried.exit_code == 0, retried.output
    dropped = runner.invoke(app, ["outbox", "drop", REF, f"pr/{name}", "-y"])
    assert dropped.exit_code == 0, dropped.output
    assert REF not in _listed()


def test_a_live_instance_with_a_journal_is_not_listed_as_destroyed(env) -> None:
    _, incus = env
    name = _stage("issue", "landed", ISSUE_MANIFEST)
    _uncertain("issue", name)
    incus.list_containers.return_value = [
        {"name": "other-name", "status": "Running", "config": {"volatile.uuid": UUID}}
    ]
    assert REF not in _listed()
    shown = runner.invoke(app, ["outbox", "show", REF, f"issue/{name}"])
    assert shown.exit_code == 2


def test_an_unregistered_repositorys_destroyed_container_is_not_listed(env) -> None:
    name = _stage("issue", "landed", ISSUE_MANIFEST, uuid="u-other")
    _uncertain("issue", name, uuid="u-other", full="elsewhere-gone")
    assert "elsewhere-gone@u-other" not in _listed()
