"""`jailbee outbox sent ls|show|clear` through the real root Typer app."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.incus import Incus
from jailbee.outbox import sent
from jailbee.outbox.sent import SentAction
from jailbee.remote_ssh.repo_scope import RemoteRepoScope

NEW = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
OLD = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def _rec(
    manifest: str,
    *,
    prefix: str = "acme",
    instance: str = "acme-feat",
    uuid: str = "uuid-new",
    at: datetime = datetime(2026, 11, 4, 10, 0, tzinfo=UTC),
    state: str = "applied",
    content: str = "hello body",
    url: str | None = "https://github.com/o/r/issues/5#c1",
    title: str | None = None,
) -> None:
    sent.record(
        instance=instance,
        container_prefix=prefix,
        uuid=uuid,
        publisher="user",
        action=SentAction("issue", manifest, 0, "comment", "o/r", 5, url, title, content, "d"),
        state=state,  # type: ignore[arg-type]
        now=at,
    )


def _run(*args: str, input: str | None = None):
    return CliRunner().invoke(app, ["outbox", "sent", *args], input=input)


def test_ls_without_args_lists_everything_newest_first_and_never_prompts(mocker) -> None:
    ask = mocker.patch("jailbee.prompting._select")
    _rec("a", at=datetime(2026, 11, 2, tzinfo=UTC))
    _rec("b", prefix="other", instance="other-x", at=datetime(2026, 11, 3, tzinfo=UTC))
    result = _run("ls", "-o", "json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["schema"] == 2
    assert [r["manifest"] for r in data["records"]] == ["b", "a"]
    ask.assert_not_called()


def test_ls_table_marks_uncertain_rows() -> None:
    _rec("a", state="uncertain", url=None)
    result = _run("ls")
    assert result.exit_code == 0, result.output
    assert "uncertain?" in result.stdout


def test_ls_repo_and_limit() -> None:
    _rec("a", at=datetime(2026, 11, 2, tzinfo=UTC))
    _rec("b", at=datetime(2026, 11, 3, tzinfo=UTC))
    _rec("c", prefix="other", instance="other-x")
    data = json.loads(_run("ls", "--repo", "acme", "--limit", "1", "-o", "json").stdout)
    assert [r["manifest"] for r in data["records"]] == ["b"]


def test_ls_container_limits_to_its_current_incarnation(mocker, make_cfg, tmp_path) -> None:
    cfg = make_cfg(tmp_path, container_prefix="acme")
    mocker.patch("jailbee.config.load_repo_config", return_value=cfg)
    mocker.patch("jailbee.config.load_config", return_value=cfg)
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [
        {
            "name": "acme-feat",
            "created_at": "2026-11-01T08:00:00Z",
            "profiles": ["acme-base"],
            "status": "Running",
            "config": {"volatile.uuid": "uuid-new"},
        }
    ]
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    _rec("now")
    _rec("past-life", uuid="uuid-old")
    _rec("elsewhere", instance="acme-other", uuid="uuid-other")
    result = _run("ls", "feat", "-o", "json")
    assert result.exit_code == 0, result.output
    assert [r["manifest"] for r in json.loads(result.stdout)["records"]] == ["now"]


def test_show_prints_content_and_url() -> None:
    _rec("a", content="line one\nline two", title="T")
    rid = sent.list_records()[0].id
    result = _run("show", str(rid))
    assert result.exit_code == 0, result.output
    assert "line two" in result.stdout
    assert "https://github.com/o/r/issues/5#c1" in result.stdout


def test_show_uncertain_is_flagged() -> None:
    _rec("a", state="uncertain", url=None)
    result = _run("show", str(sent.list_records()[0].id))
    assert "uncertain" in result.stdout


def test_show_unknown_id_exits_2() -> None:
    assert _run("show", "999").exit_code == 2


def _scope(mocker, *excluded: str) -> None:
    mocker.patch(
        "jailbee.remote_ssh.repo_scope.scope_for_session",
        return_value=RemoteRepoScope(frozenset(excluded)),
    )


def test_remote_scope_hides_other_repos(mocker) -> None:
    _rec("a")
    _rec("b", prefix="secret", instance="secret-x")
    _scope(mocker, "secret")
    data = json.loads(_run("ls", "-o", "json").stdout)
    assert [r["manifest"] for r in data["records"]] == ["a"]
    hidden = next(r for r in sent.list_records() if r.container_prefix == "secret")
    assert _run("show", str(hidden.id)).exit_code == 2


def test_clear_picker_always_shown_even_with_one_candidate(mocker) -> None:
    _rec("a")
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    select = mocker.patch("jailbee.prompting._select", return_value="acme")
    result = _run("clear", "-y")
    assert result.exit_code == 0, result.output
    select.assert_called_once()
    assert sent.list_records() == []


def test_clear_non_tty_without_repo_exits_2_naming_candidates(mocker) -> None:
    _rec("a")
    _rec("b", prefix="other", instance="other-x")
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = _run("clear", "-y")
    assert result.exit_code == 2
    assert "acme" in result.output and "other" in result.output
    assert len(sent.list_records()) == 2


def test_clear_confirmation_defaults_to_no(typed_confirmations) -> None:
    _rec("a")
    result = _run("clear", "--repo", "acme", input="\n")
    assert result.exit_code == 0, result.output
    assert len(sent.list_records()) == 1


def test_clear_confirmed_deletes_only_that_repo(typed_confirmations) -> None:
    _rec("a")
    _rec("b", prefix="other", instance="other-x")
    assert _run("clear", "--repo", "acme", input="y\n").exit_code == 0
    assert [r.container_prefix for r in sent.list_records()] == ["other"]


def test_clear_before_accepts_naive_date_as_utc() -> None:
    _rec("old", at=datetime(2026, 10, 1, tzinfo=UTC))
    _rec("new", at=datetime(2026, 11, 3, tzinfo=UTC))
    assert _run("clear", "--repo", "acme", "--before", "2026-11-01", "-y").exit_code == 0
    assert [r.manifest for r in sent.list_records()] == ["new"]


def test_clear_bad_before_exits_2() -> None:
    _rec("a")
    assert _run("clear", "--repo", "acme", "--before", "soon", "-y").exit_code == 2


def test_clear_remote_scope_excludes_repos(mocker) -> None:
    _rec("b", prefix="secret", instance="secret-x")
    _scope(mocker, "secret")
    result = _run("clear", "--repo", "secret", "-y")
    assert result.exit_code == 2
    assert len(sent.list_records()) == 1
