from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)


def _staged(mocker, *, issue=None, pr=None):
    """The host store's rows: name -> manifest, per kind."""
    from types import SimpleNamespace

    rows = {"issue": dict(issue or {}), "pr": dict(pr or {})}

    def read(_root, _uuid, kind):
        return [SimpleNamespace(name=n, manifest=m) for n, m in sorted(rows[kind].items())]

    return mocker.patch("jailbee.outbox.store_db.read_proposals", side_effect=read)


def test_probe_counts_manifests_not_actions_and_summarizes(mocker):
    from jailbee.orchestration.child_outbox import probe

    read = _staged(
        mocker,
        issue={
            "001-a.json": json.dumps({"actions": [{"type": "create", "title": "Title"}]}),
            "broken.json": "not json",
        },
        pr={"002-y.json": json.dumps({"actions": [{"type": "review", "comments": [{}, {}, {}]}]})},
    )
    pending = probe("uuid-1")
    assert pending.count == 3
    assert pending.issue == ("001-a.json", "broken.json")
    assert pending.pr == ("002-y.json",)
    assert pending.summaries == (
        'issue/001-a.json: create "Title"',
        "issue/broken.json",
        "pr/002-y.json: review (3 comments)",
    )
    assert {call.args[1:] for call in read.call_args_list} == {
        ("uuid-1", "issue"),
        ("uuid-1", "pr"),
    }


def test_probe_deeply_nested_invalid_manifest_is_name_only(mocker):
    from jailbee.orchestration.child_outbox import probe

    _staged(mocker, issue={"deep.json": "[" * 30000 + "]" * 30000})
    assert probe("u").summaries == ("issue/deep.json",)


def test_probe_without_a_store_key_is_empty(mocker):
    from jailbee.orchestration.child_outbox import probe

    read = _staged(mocker, pr={"a.json": "{}"})
    assert probe(None).count == 0
    read.assert_not_called()


def test_unreadable_host_store_fails_closed(mocker):
    from jailbee.orchestration.child_outbox import ChildOutboxError, probe
    from jailbee.orchestration.store import StoreError

    mocker.patch("jailbee.outbox.store_db.read_proposals", side_effect=StoreError("corrupt"))
    with pytest.raises(ChildOutboxError):
        probe("uuid-1")


def test_describe_includes_recovery_instruction():
    from jailbee.orchestration.child_outbox import PendingGithub, describe

    assert describe("c1", PendingGithub(("a.json",), (), ())) == (
        "child `c1` has 1 staged GitHub action(s) in its outbox (issue: a.json); "
        "they are never published from a child — ask it to send the content, "
        "then destroy with force: true"
    )
