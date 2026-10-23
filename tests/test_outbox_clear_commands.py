"""Clear execution rechecks each listed proposal without expanding its scope."""

import pytest

from jailbee.outbox import commands, service
from jailbee.outbox.models import OutboxError, OutboxExecutionError
from jailbee.outbox_io import JournalStore
from tests.outbox_support import IDENTITY, issue_files, pr_files, store


@pytest.fixture
def env(mocker, make_cfg, tmp_path):
    root = tmp_path / "acme"
    root.mkdir()
    cfg = make_cfg(root, container_prefix="acme")
    incus = mocker.Mock()
    incus.list_containers.return_value = [
        {
            "name": name,
            "created_at": IDENTITY.created_at,
            "profiles": ["acme-base"],
            "status": "Running",
            "config": {"user.jailbee.issue_count": "0", "user.jailbee.review_count": "0"},
        }
        for name in ("acme-feature", "acme-other")
    ]
    incus.exists.side_effect = lambda name: any(
        r["name"] == name for r in incus.list_containers.return_value
    )
    mocker.patch("jailbee.config.load_repo_config", return_value=cfg)
    files = {
        (name, kind): factory()
        for name in ("acme-feature", "acme-other")
        for kind, factory in (("pr", pr_files), ("issue", issue_files))
    }
    reader = mocker.patch.object(
        service, "read_store", side_effect=lambda i, c, k, **kw: store(k, files[c, k])
    )
    mutation = mocker.patch.object(
        service, "mutate_store", side_effect=lambda *a, **kw: kw["delete_names"]
    )
    journals = JournalStore(tmp_path / "journals")
    return cfg, incus, files, reader, mutation, journals


def clear(env, confirm, **kwargs):
    cfg, incus, _, _, _, journals = env
    return commands.clear_selected(
        cfg,
        incus,
        ["acme-feature", "acme-other"],
        journal_store=journals,
        confirm=confirm,
        **kwargs,
    )


def test_clears_both_stores_after_one_confirmation(env, mocker, capsys):
    confirm = mocker.Mock(return_value=True)
    assert clear(env, confirm) == 0
    confirm.assert_called_once()
    assert len(confirm.call_args.args[0]) == 2
    mutation = env[4]
    assert mutation.call_count == 4
    assert {
        (c.args[1], c.args[2], c.kwargs["delete_names"]) for c in mutation.call_args_list
    } == {
        (name, kind, ("001.json", "body.md") if kind == "issue" else ("001.json",))
        for name in ("acme-feature", "acme-other")
        for kind in ("pr", "issue")
    }
    assert "acme-feature: deleted 2 (1 PR, 1 issue)" in capsys.readouterr().out


@pytest.mark.parametrize("error", [OutboxError("vanished"), OutboxExecutionError("I/O failed"), OSError("offline")])
def test_initial_discovery_failure_does_not_block_healthy_target(env, mocker, capsys, error):
    real = commands.discover

    def discover(cfg, incus, name, **kwargs):
        if name == "acme-feature":
            raise error
        return real(cfg, incus, name, **kwargs)

    mocker.patch.object(commands, "discover", side_effect=discover)
    assert clear(env, lambda plans: True) == 1
    assert env[4].call_count == 2
    assert {c.args[1] for c in env[4].call_args_list} == {"acme-other"}
    assert "acme-feature: unavailable" in capsys.readouterr().out


def test_approved_snapshot_survives_detached_handoff_without_expanding(env):
    from jailbee.outbox.clear_snapshot import encode_snapshot, decode_snapshot

    cfg, incus, files, _, mutation, journals = env
    plans = commands.prepare_clear(cfg, incus, ["acme-feature"], journal_store=journals)
    approved = decode_snapshot(encode_snapshot(plans))
    files["acme-feature", "pr"]["001.json"] += "\n"
    files["acme-feature", "issue"]["002.json"] = issue_files()["001.json"]
    assert commands.clear_selected(
        cfg, incus, ["acme-feature"], journal_store=journals,
        confirm=lambda plans: True, approved=approved,
    ) == 1
    assert mutation.call_count == 1
    assert mutation.call_args.args[1:3] == ("acme-feature", "issue")
    assert mutation.call_args.kwargs["delete_names"] == ("001.json",)


def test_declined_confirmation_deletes_nothing(env, mocker, capsys):
    assert clear(env, mocker.Mock(return_value=False)) == 0
    env[4].assert_not_called()
    assert "Nothing deleted." in capsys.readouterr().out


def test_dry_run_prints_plan_and_never_asks(env, mocker, capsys):
    confirm = mocker.Mock()
    assert clear(env, confirm, dry_run=True) == 0
    confirm.assert_not_called()
    env[4].assert_not_called()
    assert "delete pr/001.json" in capsys.readouterr().out


def test_kept_item_makes_exit_one(env, capsys):
    env[2]["acme-feature", "pr"]["001.json.progress.json"] = '{"applied":[],"urls":{}}'
    assert clear(env, lambda plans: True) == 1
    calls = env[4].call_args_list
    assert not any(c.args[1:3] == ("acme-feature", "pr") for c in calls)
    assert any(c.args[1:3] == ("acme-feature", "issue") for c in calls)
    captured = capsys.readouterr()
    assert "kept 1" in captured.out + captured.err


def test_changed_revision_is_kept_and_rest_continues(env, capsys):
    def confirm(plans):
        env[2]["acme-feature", "pr"]["001.json"] += "\n"
        return True

    assert clear(env, confirm) == 1
    assert not any(c.args[1:3] == ("acme-feature", "pr") for c in env[4].call_args_list)
    assert any(c.args[1:3] == ("acme-feature", "issue") for c in env[4].call_args_list)
    captured = capsys.readouterr()
    assert "changed since listing" in captured.out + captured.err


def test_new_manifest_after_listing_survives(env):
    def confirm(plans):
        env[2]["acme-feature", "issue"]["002.json"] = issue_files()["001.json"]
        return True

    assert clear(env, confirm) == 0
    assert all("002.json" not in c.kwargs["delete_names"] for c in env[4].call_args_list)


def test_shared_body_does_not_keep_the_second(env):
    files = env[2]["acme-feature", "issue"]
    files["002.json"] = files["001.json"]

    def mutate(i, container, kind, **kw):
        for name in kw["delete_names"]:
            env[2][container, kind].pop(name)
        return kw["delete_names"]

    env[4].side_effect = mutate
    assert clear(env, lambda plans: True) == 0
    assert files == {}
    calls = [c for c in env[4].call_args_list if c.args[1:3] == ("acme-feature", "issue")]
    assert calls[0].kwargs["delete_names"] == ("001.json",)
    assert set(calls[1].kwargs["delete_names"]) == {"002.json", "body.md"}


def test_stopped_container_is_reported_and_others_cleared(env, capsys):
    env[1].list_containers.return_value[1]["status"] = "Stopped"
    assert clear(env, lambda plans: True) == 1
    assert {c.args[1] for c in env[4].call_args_list} == {"acme-feature"}
    captured = capsys.readouterr()
    assert "acme-other: unavailable" in captured.out + captured.err


def test_stops_after_confirmation_and_other_container_continues(env, capsys):
    def confirm(plans):
        env[1].list_containers.return_value[0]["status"] = "Stopped"
        return True

    assert clear(env, confirm) == 1
    assert {c.args[1] for c in env[4].call_args_list} == {"acme-other"}
    assert "failed 2" in capsys.readouterr().err


def test_the_summary_is_the_last_stderr_line(env, capsys):
    env[2]["acme-feature", "pr"]["001.json.progress.json"] = '{"applied":[],"urls":{}}'
    assert clear(env, lambda plans: True) == 1
    assert capsys.readouterr().err.splitlines()[-1].endswith("Cleared 3, kept 1, failed 0.")


def test_execution_failure_does_not_stop_other_items(env, capsys):
    def mutate(i, container, kind, **kw):
        if container == "acme-feature" and kind == "pr":
            raise OutboxExecutionError("write failed")
        return kw["delete_names"]

    env[4].side_effect = mutate
    assert clear(env, lambda plans: True) == 1
    assert env[4].call_count == 4
    captured = capsys.readouterr()
    assert "write failed" in captured.err
    assert captured.err.splitlines()[-1].endswith("Cleared 3, kept 0, failed 1.")


def test_nothing_deletable_never_asks_and_unavailable_is_nonzero(env, mocker):
    for row in env[1].list_containers.return_value:
        row["status"] = "Stopped"
    confirm = mocker.Mock()
    assert clear(env, confirm) == 1
    confirm.assert_not_called()
    env[4].assert_not_called()
