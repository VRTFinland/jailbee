"""`jailbee review resolve --applied` on an uncertain source publication.

A source manifest's sent record lives under the ``source`` outbox and its
success writes the branch -> PR binding; resolving it as landed must do both.
"""

from __future__ import annotations

import json

import pytest

from tests.test_branch_pr import _publish, journal, source_publisher  # noqa: F401

LANDED = "https://github.com/acme/widgets/pull/42"


def _uncertain(p):
    p["create"].side_effect = RuntimeError("gh crashed")
    with pytest.raises(RuntimeError):
        _publish(p)
    p["create"].side_effect = None
    assert journal().uncertain


def test_resolving_a_source_publication_as_landed_marks_its_sent_record_applied(
    source_publisher,  # noqa: F811
):
    from jailbee import pr_outbox
    from jailbee.outbox import sent

    p = source_publisher
    _uncertain(p)

    pr_outbox.resolve_uncertain(p["incus"], "full", "m.json", 0, landed_url=LANDED)

    (record,) = sent.list_records()
    assert (record.outbox, record.state) == ("source", "applied")
    assert record.url == LANDED


def test_resolving_a_source_publication_as_landed_records_the_binding(
    source_publisher,  # noqa: F811
):
    from jailbee import branch_pr
    from jailbee.pr import PrInfo

    p = source_publisher
    _uncertain(p)
    p["resolve"].return_value = PrInfo(42, "feature/a", "b" * 40, "OPEN", "main")

    branch_pr.resolve_uncertain_source(
        p["cfg"], p["incus"], "full", "m.json", 0, landed_url=LANDED
    )

    saved = json.loads(p["incus"].config_set.call_args.args[2])
    assert saved["feature/a"]["pr"] == 42
    assert saved["feature/a"]["source_sha"] == "a" * 40
    p["resolve"].assert_called_once()
    assert p["resolve"].call_args.args[1] == 42


def test_resolving_a_source_publication_for_retry_writes_no_binding(
    source_publisher,  # noqa: F811
):
    from jailbee import branch_pr

    p = source_publisher
    _uncertain(p)

    branch_pr.resolve_uncertain_source(p["cfg"], p["incus"], "full", "m.json", 0, landed_url=None)

    p["incus"].config_set.assert_not_called()
    assert journal() is not None and not journal().uncertain


def test_review_resolve_applied_settles_a_source_publication(
    source_publisher,  # noqa: F811
    mocker,
):
    """`jailbee review resolve --applied` on a source manifest: sent row and binding."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.outbox import sent
    from jailbee.pr import PrInfo

    p = source_publisher
    _uncertain(p)
    p["resolve"].return_value = PrInfo(42, "feature/a", "b" * 40, "OPEN", "main")
    mocker.patch("jailbee.cli._load_or_exit", return_value=p["cfg"])
    mocker.patch("jailbee.cli._resolve_existing", return_value=(p["incus"], "full"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="c")

    result = CliRunner().invoke(
        app, ["review", "resolve", "c", "m.json", "0", "--applied", LANDED]
    )

    assert result.exit_code == 0, result.output
    (record,) = sent.list_records()
    assert record.state == "applied"
    saved = json.loads(p["incus"].config_set.call_args.args[2])
    assert saved["feature/a"]["pr"] == 42
